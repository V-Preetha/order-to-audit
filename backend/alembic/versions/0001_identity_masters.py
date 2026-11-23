"""identity, settings, shared infrastructure, masters, shared database functions

Revision ID: 0001
Revises: 0000

Implements design sections 4.0 - 4.2, 5.1 (functions 1-3) and 8 (the lifecycle seed) of
docs/design/01-database-design.md. Later migrations attach the trigger functions to the
workflow tables and reuse the typed keys on `items`.

Written as plain SQL, in the order a reader would want it:
  1. the unit type and its conversion function
  2. identity, settings, document sequences, request log
  3. the status-transition table, its seed, and the trigger functions that use it
  4. masters: products, items, customers, suppliers

Note for editors: SQL goes to the driver unparsed, so it must not contain a percent sign
(PL/pgSQL messages are built with || instead of RAISE's format placeholders).
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = "0000"
branch_labels = None
depends_on = None

# GST state / UT codes 01-38 and 97, as in app.domain.gst (checked against it by a test).
STATE_CODE = "^(0[1-9]|[12][0-9]|3[0-8]|97)$"
# 2-digit state, 5-letter PAN prefix, 4 digits, a letter, entity number 1-9/A-Z, 'Z', check character.
GSTIN = "^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"

# Design section 8. '(new)' is the legal initial status. Completed production runs are terminal.
LIFECYCLES = {
    "orders": [
        ("(new)", "submitted"),
        ("submitted", "approved"), ("submitted", "on_hold"), ("submitted", "rejected"),
        ("on_hold", "approved"), ("on_hold", "rejected"),
        ("approved", "invoiced"), ("approved", "cancelled"),
        ("invoiced", "dispatched"), ("invoiced", "cancelled"),
        ("dispatched", "cancelled"),
    ],
    "stock_reservations": [("(new)", "active"), ("active", "released"), ("active", "fulfilled")],
    "invoices": [("(new)", "issued"), ("issued", "cancelled")],
    "production_runs": [("(new)", "in_progress"), ("in_progress", "completed")],
    "purchases": [("(new)", "posted"), ("posted", "voided")],
    "payments": [("(new)", "posted"), ("posted", "reversed")],
}


def _sql(statement: str) -> None:
    op.get_bind().exec_driver_sql(statement)


def upgrade() -> None:
    # ------------------------------------------------------------------ 1. units
    _sql("CREATE TYPE unit_code AS ENUM ('kg','g','l','ml','pc','pkt')")

    # Multiplier that turns `f` into `t`; NULL when the dimensions differ (mass vs volume vs count).
    # Mirrors app.domain.units.UNIT_FACTORS pair for pair (tests/db/test_unit_factor.py).
    _sql("""
    CREATE FUNCTION unit_factor(f unit_code, t unit_code) RETURNS numeric
    LANGUAGE sql IMMUTABLE AS $$
      SELECT CASE
        WHEN f = t THEN 1
        WHEN f = 'kg' AND t = 'g'  THEN 1000   WHEN f = 'g'  AND t = 'kg' THEN 0.001
        WHEN f = 'l'  AND t = 'ml' THEN 1000   WHEN f = 'ml' AND t = 'l'  THEN 0.001
      END
    $$""")

    # ------------------------------------------------------------------ 2. identity and shared tables (4.1)
    _sql(f"""
    CREATE TABLE users (
      id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      username      TEXT NOT NULL,
      full_name     TEXT NOT NULL,
      password_hash TEXT NOT NULL,
      role          TEXT NOT NULL,
      is_active     BOOLEAN NOT NULL DEFAULT true,
      CONSTRAINT users_username_key UNIQUE (username),
      CONSTRAINT ck_users_username_lowercase CHECK (username = lower(username)),
      CONSTRAINT ck_users_username_not_blank CHECK (btrim(username) <> ''),
      CONSTRAINT ck_users_role CHECK (role IN ('salesman','admin','production','auditor','owner'))
    )""")

    _sql(f"""
    CREATE TABLE settings (
      id                     SMALLINT PRIMARY KEY,
      factory_name           TEXT NOT NULL,
      factory_gstin          CHAR(15) NOT NULL,
      factory_state_code     CHAR(2) NOT NULL,
      variance_tolerance_pct NUMERIC(5,2) NOT NULL DEFAULT 3.00,
      invoice_round_to_rupee BOOLEAN NOT NULL DEFAULT false,
      CONSTRAINT ck_settings_single_row CHECK (id = 1),
      CONSTRAINT ck_settings_factory_name_not_blank CHECK (btrim(factory_name) <> ''),
      CONSTRAINT ck_settings_state_code CHECK (factory_state_code ~ '{STATE_CODE}'),
      CONSTRAINT ck_settings_gstin_format CHECK (factory_gstin ~ '{GSTIN}'),
      CONSTRAINT ck_settings_gstin_state CHECK (substr(factory_gstin, 1, 2) = factory_state_code),
      CONSTRAINT ck_settings_tolerance CHECK (variance_tolerance_pct BETWEEN 0 AND 100)
    )""")

    _sql("""
    CREATE TABLE document_sequences (
      scope      TEXT PRIMARY KEY,
      last_value BIGINT NOT NULL DEFAULT 0,
      CONSTRAINT ck_document_sequences_scope_not_blank CHECK (btrim(scope) <> ''),
      CONSTRAINT ck_document_sequences_last_value CHECK (last_value >= 0)
    )""")

    # Written inside the same transaction as the action it records, so a rolled-back action leaves no row.
    _sql("""
    CREATE TABLE request_log (
      request_id         UUID PRIMARY KEY,
      action             TEXT NOT NULL,
      fingerprint        TEXT NOT NULL,
      result_entity_type TEXT,
      result_entity_id   BIGINT,
      created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT ck_request_log_fingerprint CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
      CONSTRAINT ck_request_log_result_pair CHECK ((result_entity_type IS NULL) = (result_entity_id IS NULL))
    )""")

    # ------------------------------------------------------------------ 3. lifecycles and trigger functions (5.1, 8)
    _sql("""
    CREATE TABLE status_transitions (
      entity      TEXT NOT NULL,
      from_status TEXT NOT NULL,
      to_status   TEXT NOT NULL,
      PRIMARY KEY (entity, from_status, to_status),
      CONSTRAINT ck_status_transitions_distinct CHECK (from_status <> to_status),
      CONSTRAINT ck_status_transitions_new_is_initial_only CHECK (to_status <> '(new)')
    )""")
    transitions = sa.table("status_transitions", sa.column("entity"), sa.column("from_status"), sa.column("to_status"))
    op.bulk_insert(transitions, [
        {"entity": entity, "from_status": f, "to_status": t}
        for entity, edges in LIFECYCLES.items() for f, t in edges
    ])

    # Trigger function 1. Attached (later) to stock_movements, audit_log, credit_notes, invoice_lines, purchase_lines.
    _sql("""
    CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
        MESSAGE = TG_TABLE_NAME::text || ' is append-only: ' || TG_OP || ' is not allowed';
    END
    $$""")

    # Trigger function 2. Posted documents are frozen except for the fields their lifecycle needs.
    # Any table without a rule here fails closed, so attaching it by mistake cannot silently allow edits.
    _sql("""
    CREATE FUNCTION guard_posted_row() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      changed text[];
      col     text;
    BEGIN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
          MESSAGE = TG_TABLE_NAME::text || ' rows cannot be deleted';
      END IF;

      IF TG_TABLE_NAME = 'production_runs' THEN
        IF OLD.status = 'completed' THEN
          RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
            MESSAGE = 'a completed production run cannot be changed';
        END IF;
        RETURN NEW;
      END IF;

      IF TG_TABLE_NAME NOT IN ('invoices', 'payments') THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
          MESSAGE = 'no guard rule for ' || TG_TABLE_NAME::text;
      END IF;

      SELECT coalesce(array_agg(n.key ORDER BY n.key), '{}') INTO changed
      FROM jsonb_each(to_jsonb(NEW)) AS n
      JOIN jsonb_each(to_jsonb(OLD)) AS o ON o.key = n.key
      WHERE n.value IS DISTINCT FROM o.value;

      FOREACH col IN ARRAY changed LOOP
        IF TG_TABLE_NAME = 'invoices' AND col IN ('status', 'dispatched_at', 'dispatched_by') THEN
          CONTINUE;
        END IF;
        IF TG_TABLE_NAME = 'payments' AND (col = 'status' OR left(col, 9) = 'reversed_') THEN
          CONTINUE;
        END IF;
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
          MESSAGE = TG_TABLE_NAME::text || ' column ' || col || ' cannot be changed once posted';
      END LOOP;
      RETURN NEW;
    END
    $$""")

    # Trigger function 3. search_path is pinned so a session cannot shadow status_transitions with a temp table.
    # For BEFORE INSERT OR UPDATE on a table with a `status` column.
    # An edit that leaves the status alone is not a transition; terminal states are enforced by guard_posted_row.
    _sql("""
    CREATE FUNCTION enforce_status_transition() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      from_s text;
    BEGIN
      IF TG_OP = 'INSERT' THEN
        from_s := '(new)';
      ELSE
        IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
          RETURN NEW;
        END IF;
        from_s := OLD.status;
      END IF;

      IF EXISTS (SELECT 1 FROM status_transitions s
                 WHERE s.entity = TG_TABLE_NAME::text AND s.from_status = from_s AND s.to_status = NEW.status) THEN
        RETURN NEW;
      END IF;

      RAISE EXCEPTION USING ERRCODE = 'check_violation',
        MESSAGE = 'illegal status transition for ' || TG_TABLE_NAME::text || ': '
                  || from_s || ' -> ' || coalesce(NEW.status, 'NULL');
    END
    $$""")

    # ------------------------------------------------------------------ 4. masters (4.2)
    _sql("""
    CREATE TABLE products (
      id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      name     TEXT NOT NULL,
      hsn_code TEXT NOT NULL,
      gst_rate NUMERIC(5,2) NOT NULL,
      CONSTRAINT products_name_key UNIQUE (name),
      CONSTRAINT ck_products_name_not_blank CHECK (btrim(name) <> ''),
      CONSTRAINT ck_products_hsn_code CHECK (hsn_code ~ '^[0-9]{4}([0-9]{2}([0-9]{2})?)?$'),
      CONSTRAINT ck_products_gst_rate CHECK (gst_rate BETWEEN 0 AND 40)
    )""")

    # The three extra UNIQUE constraints are the targets of the typed composite foreign keys that later
    # migrations use (design 4.0): a child row carries a witness column (kind, base_unit or product_id)
    # that must equal the item's real value, so the database rejects a wrong item type.
    _sql("""
    CREATE TABLE items (
      id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      code               TEXT NOT NULL,
      name               TEXT NOT NULL,
      kind               TEXT NOT NULL,
      base_unit          unit_code NOT NULL,
      is_active          BOOLEAN NOT NULL DEFAULT true,
      product_id         BIGINT REFERENCES products (id),
      pack_size_g        INTEGER,
      packets_per_carton INTEGER,
      list_price         NUMERIC(12,2),
      CONSTRAINT items_code_key UNIQUE (code),
      CONSTRAINT items_id_kind_key UNIQUE (id, kind),
      CONSTRAINT items_id_base_unit_key UNIQUE (id, base_unit),
      CONSTRAINT items_id_product_id_key UNIQUE (id, product_id),
      CONSTRAINT ck_items_code_not_blank CHECK (btrim(code) <> ''),
      CONSTRAINT ck_items_name_not_blank CHECK (btrim(name) <> ''),
      CONSTRAINT ck_items_kind CHECK (kind IN ('raw','packaging','finished')),
      CONSTRAINT ck_items_finished_has_product CHECK ((kind = 'finished') = (product_id IS NOT NULL)),
      CONSTRAINT ck_items_nonfinished_no_sales_fields CHECK (
        kind = 'finished' OR (pack_size_g IS NULL AND packets_per_carton IS NULL AND list_price IS NULL)),
      CONSTRAINT ck_items_finished_pkt_and_price CHECK (
        kind <> 'finished' OR (base_unit = 'pkt' AND list_price IS NOT NULL)),
      CONSTRAINT ck_items_pack_size_positive CHECK (pack_size_g IS NULL OR pack_size_g > 0),
      CONSTRAINT ck_items_packets_per_carton_positive CHECK (packets_per_carton IS NULL OR packets_per_carton > 0),
      CONSTRAINT ck_items_list_price_nonnegative CHECK (list_price IS NULL OR (list_price >= 0 AND list_price <> 'NaN'))
    )""")

    _sql(f"""
    CREATE TABLE customers (
      id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      name          TEXT NOT NULL,
      customer_type TEXT NOT NULL,
      gstin         CHAR(15),
      state_code    CHAR(2) NOT NULL,
      address       TEXT,
      phone         TEXT,
      CONSTRAINT ck_customers_name_not_blank CHECK (btrim(name) <> ''),
      CONSTRAINT ck_customers_type CHECK (customer_type IN ('retailer','distributor')),
      CONSTRAINT ck_customers_state_code CHECK (state_code ~ '{STATE_CODE}'),
      CONSTRAINT ck_customers_gstin_format CHECK (gstin IS NULL OR gstin ~ '{GSTIN}'),
      CONSTRAINT ck_customers_gstin_state CHECK (gstin IS NULL OR substr(gstin, 1, 2) = state_code)
    )""")
    _sql("CREATE UNIQUE INDEX uq_customers_gstin ON customers (gstin) WHERE gstin IS NOT NULL")

    _sql(f"""
    CREATE TABLE suppliers (
      id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      name       TEXT NOT NULL,
      gstin      CHAR(15),
      state_code CHAR(2) NOT NULL,
      address    TEXT,
      phone      TEXT,
      CONSTRAINT ck_suppliers_name_not_blank CHECK (btrim(name) <> ''),
      CONSTRAINT ck_suppliers_state_code CHECK (state_code ~ '{STATE_CODE}'),
      CONSTRAINT ck_suppliers_gstin_format CHECK (gstin IS NULL OR gstin ~ '{GSTIN}'),
      CONSTRAINT ck_suppliers_gstin_state CHECK (gstin IS NULL OR substr(gstin, 1, 2) = state_code)
    )""")
    _sql("CREATE UNIQUE INDEX uq_suppliers_gstin ON suppliers (gstin) WHERE gstin IS NOT NULL")


def downgrade() -> None:
    for table in ("suppliers", "customers", "items", "products", "status_transitions",
                  "request_log", "document_sequences", "settings", "users"):
        _sql(f"DROP TABLE {table}")
    for function in ("enforce_status_transition()", "guard_posted_row()", "forbid_mutation()"):
        _sql(f"DROP FUNCTION {function}")
    _sql("DROP FUNCTION unit_factor(unit_code, unit_code)")
    _sql("DROP TYPE unit_code")
