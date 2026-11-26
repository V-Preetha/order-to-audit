"""orders, order lines, invoices, invoice lines, credit notes

Revision ID: 0003
Revises: 0002

Implements design section 4.6 of docs/design/01-database-design.md, with the integrity rules of
section 5. Nothing here touches stock: orders and invoices are documents. Stock leaves only at
dispatch (a later migration adds the ledger and the dispatch movement).

What the database enforces
  * order lines reference finished items only (witness column + composite foreign key)
  * an invoice's customer is its order's customer; one invoice per order (v1)
  * invoice number 'INV/26-27/0001' is derived from (financial_year, invoice_seq), at most 16
    characters, unique per financial year; the invoice date lies inside its financial year
  * supply type follows the states; CGST+SGST only when intra-state, IGST only when inter-state,
    per line and per header; header total = components + round-off
  * an invoice line belongs to the invoice's order and repeats its supply type (two composite
    keys; the design sketch left both unlinked), and header totals equal the sum of the lines
    (checked at commit by a deferred constraint trigger)
  * issued invoices are snapshots: only status and dispatch fields can ever change, lines and
    credit notes are append-only, nothing is deleted
  * cancellation is a credit note: a cancelled invoice and its credit note exist together
    (deferred check), and the credit note repeats every amount of the invoice (full reversal)
  * order and invoice lifecycles use the status_transitions seed from migration 0001

Money is NUMERIC(14,2) (paise are kept), quantities NUMERIC(14,3) but whole packets, rates
NUMERIC(5,2) between 0 and 40, as everywhere else.
SQL must not contain a percent sign (see migration 0001).
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

STATE_CODE = "^(0[1-9]|[12][0-9]|3[0-8]|97)$"
GSTIN = "^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
FY = "^[0-9]{4}-[0-9]{2}$"
HSN = "^[0-9]{4}([0-9]{2}([0-9]{2})?)?$"


def _sql(statement: str) -> None:
    op.get_bind().exec_driver_sql(statement)


def upgrade() -> None:
    # ------------------------------------------------------------------ orders
    _sql("""
    CREATE TABLE orders (
      id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      order_no    TEXT NOT NULL,
      client_uuid UUID NOT NULL,
      customer_id BIGINT NOT NULL REFERENCES customers (id),
      salesman_id BIGINT NOT NULL REFERENCES users (id),
      order_date  DATE NOT NULL,
      status      TEXT NOT NULL DEFAULT 'submitted',
      hold_reason TEXT,
      decided_by  BIGINT REFERENCES users (id),
      decided_at  TIMESTAMPTZ,
      notes       TEXT,
      created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT orders_order_no_key UNIQUE (order_no),
      CONSTRAINT orders_client_uuid_key UNIQUE (client_uuid),
      CONSTRAINT orders_id_customer_id_key UNIQUE (id, customer_id),
      CONSTRAINT ck_orders_order_no_not_blank CHECK (btrim(order_no) <> ''),
      CONSTRAINT ck_orders_status CHECK (status IN ('submitted','on_hold','approved','rejected','invoiced','dispatched','cancelled')),
      CONSTRAINT ck_orders_hold_reason CHECK (status <> 'on_hold' OR btrim(coalesce(hold_reason, '')) <> ''),
      CONSTRAINT ck_orders_decided_pair CHECK ((decided_by IS NULL) = (decided_at IS NULL)),
      CONSTRAINT ck_orders_decided_after_hold CHECK (status IN ('submitted','on_hold') OR decided_by IS NOT NULL)
    )""")
    _sql("CREATE INDEX ix_orders_salesman_created ON orders (salesman_id, created_at DESC)")
    _sql("CREATE INDEX ix_orders_status_created ON orders (status, created_at)")
    _sql("CREATE INDEX ix_orders_customer ON orders (customer_id)")

    _sql("""
    CREATE TABLE order_lines (
      id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      order_id   BIGINT NOT NULL REFERENCES orders (id),
      item_id    BIGINT NOT NULL,
      item_kind  TEXT NOT NULL DEFAULT 'finished',
      qty        NUMERIC(14,3) NOT NULL,
      unit_price NUMERIC(12,2) NOT NULL,
      CONSTRAINT order_lines_order_id_item_id_key UNIQUE (order_id, item_id),
      CONSTRAINT order_lines_id_item_id_key UNIQUE (id, item_id),
      CONSTRAINT order_lines_id_order_id_item_id_key UNIQUE (id, order_id, item_id, qty, unit_price),
      CONSTRAINT ck_order_lines_item_kind CHECK (item_kind = 'finished'),
      CONSTRAINT ck_order_lines_qty_positive CHECK (qty > 0 AND qty <> 'NaN'),
      CONSTRAINT ck_order_lines_whole_packets CHECK (qty = trunc(qty)),
      CONSTRAINT ck_order_lines_unit_price_nonnegative CHECK (unit_price >= 0 AND unit_price <> 'NaN'),
      CONSTRAINT fk_order_lines_item_kind FOREIGN KEY (item_id, item_kind) REFERENCES items (id, kind)
    )""")

    # ------------------------------------------------------------------ invoices
    _sql(f"""
    CREATE TABLE invoices (
      id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      invoice_no            TEXT NOT NULL,
      financial_year        TEXT NOT NULL,
      invoice_seq           INTEGER NOT NULL,
      order_id              BIGINT NOT NULL,
      customer_id           BIGINT NOT NULL,
      invoice_date          DATE NOT NULL,
      seller_gstin          CHAR(15) NOT NULL,
      seller_state_code     CHAR(2) NOT NULL,
      customer_name         TEXT NOT NULL,
      customer_gstin        CHAR(15),
      customer_address      TEXT,
      place_of_supply_state CHAR(2) NOT NULL,
      supply_type           TEXT NOT NULL,
      taxable_total         NUMERIC(14,2) NOT NULL,
      cgst_total            NUMERIC(14,2) NOT NULL,
      sgst_total            NUMERIC(14,2) NOT NULL,
      igst_total            NUMERIC(14,2) NOT NULL,
      round_off             NUMERIC(6,2) NOT NULL DEFAULT 0,
      grand_total           NUMERIC(14,2) NOT NULL,
      status                TEXT NOT NULL DEFAULT 'issued',
      dispatched_at         TIMESTAMPTZ,
      dispatched_by         BIGINT REFERENCES users (id),
      created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
      created_by            BIGINT NOT NULL REFERENCES users (id),
      CONSTRAINT invoices_invoice_no_key UNIQUE (invoice_no),
      CONSTRAINT invoices_financial_year_invoice_seq_key UNIQUE (financial_year, invoice_seq),
      CONSTRAINT invoices_order_id_key UNIQUE (order_id),
      CONSTRAINT invoices_id_customer_id_key UNIQUE (id, customer_id),
      CONSTRAINT invoices_id_order_id_supply_type_key UNIQUE (id, order_id, supply_type),
      CONSTRAINT invoices_id_amounts_key UNIQUE (id, taxable_total, cgst_total, sgst_total, igst_total, round_off, grand_total),
      CONSTRAINT fk_invoices_order_customer FOREIGN KEY (order_id, customer_id) REFERENCES orders (id, customer_id),

      -- numbering: 'INV/26-27/0001' is derived from (financial_year, invoice_seq); the pattern and the 16-character
      -- limit match domain.gst.format_invoice_no (parity tests)
      CONSTRAINT ck_invoices_financial_year_format CHECK (financial_year ~ '{FY}'),
      CONSTRAINT ck_invoices_financial_year_consecutive CHECK (
        CASE WHEN financial_year ~ '{FY}'
             THEN mod(substr(financial_year, 3, 2)::int + 1, 100) = substr(financial_year, 6, 2)::int
             ELSE true END),
      CONSTRAINT ck_invoices_invoice_seq_positive CHECK (invoice_seq >= 1),
      CONSTRAINT ck_invoices_invoice_no_format CHECK (
        invoice_no = 'INV/' || substr(financial_year, 3, 2) || '-' || substr(financial_year, 6, 2) || '/'
                     || repeat('0', greatest(0, 4 - length(invoice_seq::text))) || invoice_seq::text),
      CONSTRAINT ck_invoices_invoice_no_length CHECK (length(invoice_no) <= 16),
      CONSTRAINT ck_invoices_date_in_financial_year CHECK (
        CASE WHEN financial_year ~ '{FY}' THEN
          CASE WHEN substr(financial_year, 1, 4)::int BETWEEN 1 AND 9998
               THEN invoice_date >= make_date(substr(financial_year, 1, 4)::int, 4, 1)
                AND invoice_date <  make_date(substr(financial_year, 1, 4)::int + 1, 4, 1)
               ELSE false END
        ELSE true END),

      -- snapshots of the seller (settings) and the customer at the moment of issue
      CONSTRAINT ck_invoices_customer_name_not_blank CHECK (btrim(customer_name) <> ''),
      CONSTRAINT ck_invoices_seller_state_code CHECK (seller_state_code ~ '{STATE_CODE}'),
      CONSTRAINT ck_invoices_place_of_supply_state_code CHECK (place_of_supply_state ~ '{STATE_CODE}'),
      CONSTRAINT ck_invoices_seller_gstin_format CHECK (seller_gstin ~ '{GSTIN}'),
      CONSTRAINT ck_invoices_seller_gstin_state CHECK (substr(seller_gstin, 1, 2) = seller_state_code),
      CONSTRAINT ck_invoices_customer_gstin_format CHECK (customer_gstin IS NULL OR customer_gstin ~ '{GSTIN}'),
      CONSTRAINT ck_invoices_customer_gstin_state CHECK (customer_gstin IS NULL OR substr(customer_gstin, 1, 2) = place_of_supply_state),

      -- tax: supply type follows the states; the split follows the supply type; totals add up
      CONSTRAINT ck_invoices_supply_type CHECK (supply_type IN ('intra','inter')),
      CONSTRAINT ck_invoices_supply_type_matches_states CHECK ((supply_type = 'intra') = (place_of_supply_state = seller_state_code)),
      CONSTRAINT ck_invoices_tax_split CHECK (
        (supply_type = 'intra' AND igst_total = 0 AND cgst_total = sgst_total)
        OR (supply_type = 'inter' AND cgst_total = 0 AND sgst_total = 0)),
      CONSTRAINT ck_invoices_amounts_nonnegative CHECK (taxable_total >= 0 AND cgst_total >= 0 AND sgst_total >= 0 AND igst_total >= 0 AND grand_total >= 0
        AND taxable_total <> 'NaN' AND cgst_total <> 'NaN' AND sgst_total <> 'NaN' AND igst_total <> 'NaN' AND grand_total <> 'NaN'),
      CONSTRAINT ck_invoices_grand_total CHECK (grand_total = taxable_total + cgst_total + sgst_total + igst_total + round_off),
      CONSTRAINT ck_invoices_round_off CHECK (abs(round_off) <= 0.50 AND (round_off = 0 OR grand_total = round(grand_total, 0))),

      CONSTRAINT ck_invoices_status CHECK (status IN ('issued','cancelled')),
      CONSTRAINT ck_invoices_dispatch_pair CHECK ((dispatched_at IS NULL) = (dispatched_by IS NULL))
    )""")
    _sql("CREATE INDEX ix_invoices_customer_date ON invoices (customer_id, invoice_date)")
    _sql("CREATE INDEX ix_invoices_date ON invoices (invoice_date)")

    _sql(f"""
    CREATE TABLE invoice_lines (
      id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      invoice_id    BIGINT NOT NULL,
      order_id      BIGINT NOT NULL,
      order_line_id BIGINT NOT NULL,
      item_id       BIGINT NOT NULL,
      supply_type   TEXT NOT NULL,
      description   TEXT NOT NULL,
      hsn_code      TEXT NOT NULL,
      qty           NUMERIC(14,3) NOT NULL,
      unit_price    NUMERIC(12,2) NOT NULL,
      taxable_value NUMERIC(14,2) NOT NULL,
      gst_rate      NUMERIC(5,2) NOT NULL,
      cgst_amount   NUMERIC(14,2) NOT NULL,
      sgst_amount   NUMERIC(14,2) NOT NULL,
      igst_amount   NUMERIC(14,2) NOT NULL,
      CONSTRAINT invoice_lines_order_line_id_key UNIQUE (order_line_id),
      CONSTRAINT invoice_lines_id_item_id_key UNIQUE (id, item_id),
      CONSTRAINT fk_invoice_lines_invoice FOREIGN KEY (invoice_id, order_id, supply_type) REFERENCES invoices (id, order_id, supply_type),
      CONSTRAINT fk_invoice_lines_order_line FOREIGN KEY (order_line_id, order_id, item_id, qty, unit_price) REFERENCES order_lines (id, order_id, item_id, qty, unit_price),
      CONSTRAINT ck_invoice_lines_supply_type CHECK (supply_type IN ('intra','inter')),
      CONSTRAINT ck_invoice_lines_description_not_blank CHECK (btrim(description) <> ''),
      CONSTRAINT ck_invoice_lines_hsn_code CHECK (hsn_code ~ '{HSN}'),
      CONSTRAINT ck_invoice_lines_qty_positive CHECK (qty > 0 AND qty <> 'NaN'),
      CONSTRAINT ck_invoice_lines_whole_packets CHECK (qty = trunc(qty)),
      CONSTRAINT ck_invoice_lines_unit_price_nonnegative CHECK (unit_price >= 0 AND unit_price <> 'NaN'),
      CONSTRAINT ck_invoice_lines_gst_rate CHECK (gst_rate BETWEEN 0 AND 40),
      CONSTRAINT ck_invoice_lines_taxable_value CHECK (taxable_value = round(qty * unit_price, 2)),
      CONSTRAINT ck_invoice_lines_tax_math CHECK (
        (supply_type = 'intra' AND igst_amount = 0 AND cgst_amount = sgst_amount
           AND cgst_amount = round(taxable_value * gst_rate / 200, 2))
        OR (supply_type = 'inter' AND cgst_amount = 0 AND sgst_amount = 0
           AND igst_amount = round(taxable_value * gst_rate / 100, 2)))
    )""")

    # ------------------------------------------------------------------ credit notes
    # Full reversal only (design assumption 12): the composite foreign key makes every amount equal the invoice's.
    _sql("""
    CREATE TABLE credit_notes (
      id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      credit_note_no TEXT NOT NULL,
      invoice_id     BIGINT NOT NULL,
      issue_date     DATE NOT NULL,
      reason         TEXT NOT NULL,
      taxable_total  NUMERIC(14,2) NOT NULL,
      cgst_total     NUMERIC(14,2) NOT NULL,
      sgst_total     NUMERIC(14,2) NOT NULL,
      igst_total     NUMERIC(14,2) NOT NULL,
      round_off      NUMERIC(6,2) NOT NULL DEFAULT 0,
      grand_total    NUMERIC(14,2) NOT NULL,
      restock        BOOLEAN NOT NULL,
      created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
      created_by     BIGINT NOT NULL REFERENCES users (id),
      CONSTRAINT credit_notes_credit_note_no_key UNIQUE (credit_note_no),
      CONSTRAINT credit_notes_invoice_id_key UNIQUE (invoice_id),
      CONSTRAINT ck_credit_notes_number_not_blank CHECK (btrim(credit_note_no) <> ''),
      CONSTRAINT ck_credit_notes_number_length CHECK (length(credit_note_no) <= 16),
      CONSTRAINT ck_credit_notes_reason_not_blank CHECK (btrim(reason) <> ''),
      CONSTRAINT fk_credit_notes_invoice_amounts FOREIGN KEY (invoice_id, taxable_total, cgst_total, sgst_total, igst_total, round_off, grand_total)
        REFERENCES invoices (id, taxable_total, cgst_total, sgst_total, igst_total, round_off, grand_total)
    )""")

    # ------------------------------------------------------------------ trigger functions
    # Order lines change only while the order is submitted or on hold (design section 8). The order row is
    # read FOR SHARE so a concurrent approval waits for an in-flight line change.
    _sql("""
    CREATE FUNCTION guard_order_lines() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      parent bigint;
      st     text;
    BEGIN
      FOR parent IN
        SELECT DISTINCT p FROM unnest(ARRAY[
          CASE WHEN TG_OP <> 'DELETE' THEN NEW.order_id END,
          CASE WHEN TG_OP <> 'INSERT' THEN OLD.order_id END
        ]) AS p WHERE p IS NOT NULL
      LOOP
        SELECT status INTO st FROM orders WHERE id = parent FOR SHARE;
        IF st IS NOT NULL AND st NOT IN ('submitted', 'on_hold') THEN
          RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
            MESSAGE = 'order_lines cannot be changed: the order is ' || st || ' (lines are editable only while submitted or on hold)';
        END IF;
      END LOOP;
      IF TG_OP = 'DELETE' THEN
        RETURN OLD;
      END IF;
      RETURN NEW;
    END
    $$""")

    # Deferred (commit-time): header totals equal the sum of the lines, and an invoice has at least one line.
    # Header amounts never change after insert (guard_posted_row) and lines are append-only, so checking on
    # INSERT of either table is enough.
    _sql("""
    CREATE FUNCTION assert_invoice_totals() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      inv_id bigint;
      h_no   text;
      h_ord  bigint;
      n_ord  bigint;
      h_t    numeric;
      h_c    numeric;
      h_s    numeric;
      h_i    numeric;
      n      bigint;
      t      numeric;
      c      numeric;
      s      numeric;
      i      numeric;
    BEGIN
      IF TG_TABLE_NAME = 'invoices' THEN
        inv_id := NEW.id;
      ELSE
        inv_id := NEW.invoice_id;
      END IF;
      SELECT invoice_no, order_id, taxable_total, cgst_total, sgst_total, igst_total INTO h_no, h_ord, h_t, h_c, h_s, h_i FROM invoices WHERE id = inv_id;
      IF NOT FOUND THEN
        RETURN NULL;
      END IF;
      SELECT count(*), coalesce(sum(taxable_value), 0), coalesce(sum(cgst_amount), 0),
             coalesce(sum(sgst_amount), 0), coalesce(sum(igst_amount), 0)
        INTO n, t, c, s, i FROM invoice_lines WHERE invoice_id = inv_id;
      IF n = 0 THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = 'invoice ' || h_no || ' has no lines';
      END IF;
      SELECT count(*) INTO n_ord FROM order_lines WHERE order_id = h_ord;
      IF n <> n_ord THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'invoice ' || h_no || ' must cover every order line (' || n::text || ' of ' || n_ord::text || ')';
      END IF;
      IF t <> h_t OR c <> h_c OR s <> h_s OR i <> h_i THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'invoice ' || h_no || ': header totals do not equal the sum of its lines';
      END IF;
      RETURN NULL;
    END
    $$""")

    # Deferred (commit-time): an invoice is cancelled exactly when its credit note exists.
    _sql("""
    CREATE FUNCTION assert_cancellation_pair() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      inv_id bigint;
      st     text;
      has_cn boolean;
    BEGIN
      IF TG_TABLE_NAME = 'invoices' THEN
        inv_id := NEW.id;
      ELSE
        inv_id := NEW.invoice_id;
      END IF;
      SELECT status INTO st FROM invoices WHERE id = inv_id;
      has_cn := EXISTS (SELECT 1 FROM credit_notes WHERE invoice_id = inv_id);
      IF st = 'cancelled' AND NOT has_cn THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'invoice ' || inv_id::text || ' is cancelled without a credit note';
      END IF;
      IF st IS DISTINCT FROM 'cancelled' AND has_cn THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'a credit note exists for invoice ' || inv_id::text || ', which is not cancelled';
      END IF;
      RETURN NULL;
    END
    $$""")


    # An issued invoice never gains lines: a line may be added only in the transaction that created the invoice
    # (created_at defaults to now(), which is the transaction start time).
    _sql("""
    CREATE FUNCTION guard_invoice_line_insert() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      created timestamptz;
    BEGIN
      SELECT created_at INTO created FROM invoices WHERE id = NEW.invoice_id FOR SHARE;
      IF FOUND AND created <> now() THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
          MESSAGE = 'invoice_lines: an issued invoice cannot gain lines after the transaction that issued it';
      END IF;
      RETURN NEW;
    END
    $$""")

    # Dispatch is recorded once, on an issued invoice, and never rewritten or cleared.
    _sql("""
    CREATE FUNCTION guard_invoice_dispatch() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    BEGIN
      IF NEW.dispatched_at IS DISTINCT FROM OLD.dispatched_at OR NEW.dispatched_by IS DISTINCT FROM OLD.dispatched_by THEN
        IF OLD.dispatched_at IS NOT NULL THEN
          RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'invoices: dispatch is already recorded and cannot be changed';
        END IF;
        IF OLD.status <> 'issued' THEN
          RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'invoices: dispatch cannot be recorded on a ' || OLD.status || ' invoice';
        END IF;
      END IF;
      RETURN NEW;
    END
    $$""")

    # Orders: identity never changes; customer and date only while still editable; the decision is recorded once.
    _sql("""
    CREATE FUNCTION guard_order_row() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    BEGIN
      IF NEW.order_no IS DISTINCT FROM OLD.order_no OR NEW.client_uuid IS DISTINCT FROM OLD.client_uuid
         OR NEW.salesman_id IS DISTINCT FROM OLD.salesman_id OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'orders: order number, client id, salesman and creation time never change';
      END IF;
      IF (NEW.customer_id IS DISTINCT FROM OLD.customer_id OR NEW.order_date IS DISTINCT FROM OLD.order_date)
         AND (OLD.status NOT IN ('submitted', 'on_hold') OR NEW.status NOT IN ('submitted', 'on_hold')) THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'orders: customer and date can change only while the order is submitted or on hold';
      END IF;
      IF OLD.decided_at IS NOT NULL
         AND (NEW.decided_by IS DISTINCT FROM OLD.decided_by OR NEW.decided_at IS DISTINCT FROM OLD.decided_at) THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'orders: the decision is recorded once and cannot be changed';
      END IF;
      RETURN NEW;
    END
    $$""")

    # Immediate (after the CHECKs): the invoice's order is approved or later, and its snapshot follows the customer master.
    _sql("""
    CREATE FUNCTION check_invoice_insert() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      o_status text;
      c_name   text;
      c_gstin  text;
      c_state  text;
    BEGIN
      SELECT status INTO o_status FROM orders WHERE id = NEW.order_id FOR SHARE;
      IF o_status NOT IN ('approved', 'invoiced', 'dispatched') THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'invoice ' || NEW.invoice_no || ': its order is ' || o_status || ', not approved';
      END IF;
      SELECT name, gstin, state_code INTO c_name, c_gstin, c_state FROM customers WHERE id = NEW.customer_id;
      IF NEW.customer_name <> c_name OR NEW.customer_gstin IS DISTINCT FROM c_gstin OR NEW.place_of_supply_state <> c_state THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'invoice ' || NEW.invoice_no || ': name, GSTIN and place of supply must match the customer master';
      END IF;
      RETURN NULL;
    END
    $$""")

    # Deferred: an order is invoiced or dispatched exactly when it has an issued invoice (a cancelled invoice keeps its order as it was).
    _sql("""
    CREATE FUNCTION assert_order_invoice_link() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      ord_id     bigint;
      st         text;
      has_any    boolean;
      has_issued boolean;
    BEGIN
      IF TG_TABLE_NAME = 'orders' THEN
        ord_id := NEW.id;
      ELSE
        ord_id := NEW.order_id;
      END IF;
      SELECT status INTO st FROM orders WHERE id = ord_id;
      SELECT count(*) > 0, coalesce(bool_or(status = 'issued'), false) INTO has_any, has_issued FROM invoices WHERE order_id = ord_id;
      IF st IN ('invoiced', 'dispatched') AND NOT has_any THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = 'order ' || ord_id::text || ' is ' || st || ' without an invoice';
      END IF;
      IF has_issued AND st NOT IN ('invoiced', 'dispatched') THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'order ' || ord_id::text || ' has an issued invoice but is ' || st;
      END IF;
      RETURN NULL;
    END
    $$""")


    # A credit note is dated on or after the invoice it reverses.
    _sql("""
    CREATE FUNCTION guard_credit_note_date() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      inv_date date;
    BEGIN
      SELECT invoice_date INTO inv_date FROM invoices WHERE id = NEW.invoice_id;
      IF FOUND AND NEW.issue_date < inv_date THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'credit note ' || NEW.credit_note_no || ' is dated ' || NEW.issue_date::text || ', which precedes the invoice date ' || inv_date::text;
      END IF;
      RETURN NEW;
    END
    $$""")

    # ------------------------------------------------------------------ triggers
    # Functions from migration 0001 do the generic work; trigger names sort in firing order.
    _sql("CREATE TRIGGER trg_1_status BEFORE INSERT OR UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION enforce_status_transition()")
    _sql("CREATE TRIGGER trg_2_no_delete BEFORE DELETE ON orders FOR EACH ROW EXECUTE FUNCTION guard_posted_row()")
    _sql("CREATE TRIGGER trg_freeze BEFORE INSERT OR UPDATE OR DELETE ON order_lines FOR EACH ROW EXECUTE FUNCTION guard_order_lines()")

    _sql("CREATE TRIGGER trg_1_status BEFORE INSERT OR UPDATE ON invoices FOR EACH ROW EXECUTE FUNCTION enforce_status_transition()")
    _sql("CREATE TRIGGER trg_2_posted BEFORE UPDATE OR DELETE ON invoices FOR EACH ROW EXECUTE FUNCTION guard_posted_row()")
    _sql("CREATE TRIGGER trg_3_dispatch BEFORE UPDATE ON invoices FOR EACH ROW EXECUTE FUNCTION guard_invoice_dispatch()")
    _sql("CREATE TRIGGER trg_credit_note_date BEFORE INSERT ON credit_notes FOR EACH ROW EXECUTE FUNCTION guard_credit_note_date()")
    _sql("CREATE TRIGGER trg_snapshot AFTER INSERT ON invoices FOR EACH ROW EXECUTE FUNCTION check_invoice_insert()")
    _sql("CREATE TRIGGER trg_late_line BEFORE INSERT ON invoice_lines FOR EACH ROW EXECUTE FUNCTION guard_invoice_line_insert()")
    _sql("CREATE TRIGGER trg_3_guard BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION guard_order_row()")
    _sql("CREATE CONSTRAINT TRIGGER trg_order_invoice_link AFTER INSERT OR UPDATE OF status ON orders DEFERRABLE INITIALLY DEFERRED "
         "FOR EACH ROW EXECUTE FUNCTION assert_order_invoice_link()")
    _sql("CREATE CONSTRAINT TRIGGER trg_order_invoice_link AFTER INSERT OR UPDATE OF status ON invoices DEFERRABLE INITIALLY DEFERRED "
         "FOR EACH ROW EXECUTE FUNCTION assert_order_invoice_link()")
    _sql("CREATE TRIGGER trg_append_only BEFORE UPDATE OR DELETE ON invoice_lines FOR EACH ROW EXECUTE FUNCTION forbid_mutation()")
    _sql("CREATE TRIGGER trg_append_only BEFORE UPDATE OR DELETE ON credit_notes FOR EACH ROW EXECUTE FUNCTION forbid_mutation()")

    _sql("CREATE CONSTRAINT TRIGGER trg_invoice_totals AFTER INSERT ON invoices DEFERRABLE INITIALLY DEFERRED "
         "FOR EACH ROW EXECUTE FUNCTION assert_invoice_totals()")
    _sql("CREATE CONSTRAINT TRIGGER trg_invoice_totals AFTER INSERT ON invoice_lines DEFERRABLE INITIALLY DEFERRED "
         "FOR EACH ROW EXECUTE FUNCTION assert_invoice_totals()")
    _sql("CREATE CONSTRAINT TRIGGER trg_cancellation_pair AFTER INSERT OR UPDATE OF status ON invoices DEFERRABLE INITIALLY DEFERRED "
         "FOR EACH ROW EXECUTE FUNCTION assert_cancellation_pair()")
    _sql("CREATE CONSTRAINT TRIGGER trg_cancellation_pair AFTER INSERT ON credit_notes DEFERRABLE INITIALLY DEFERRED "
         "FOR EACH ROW EXECUTE FUNCTION assert_cancellation_pair()")


def downgrade() -> None:
    for table in ("credit_notes", "invoice_lines", "invoices", "order_lines", "orders"):
        _sql(f"DROP TABLE {table}")
    for function in ("guard_credit_note_date()", "assert_order_invoice_link()", "check_invoice_insert()", "guard_order_row()", "guard_invoice_dispatch()",
                     "guard_invoice_line_insert()", "assert_cancellation_pair()", "assert_invoice_totals()", "guard_order_lines()"):
        _sql(f"DROP FUNCTION {function}")
