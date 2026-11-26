"""purchasing, stock adjustments, the append-only stock ledger, reservations

Revision ID: 0005
Revises: 0004

Implements design sections 4.4 and 4.5 of docs/design/01-database-design.md.

What this migration guarantees on its own (each rule has a named test in tests/db/test_inventory_constraints.py):
  * every ledger row has exactly ONE source document, of the kind its movement type requires; the item of the
    movement is the item of that source (typed composite foreign keys with witness columns)
  * raw and packaging movements carry a value and a cost per unit (value / qty to 4 places); finished goods
    carry neither and move in whole packets
  * signs follow the movement type; a reversal is the exact mirror of one earlier dispatch or purchase receipt
  * one-shot events (receipt, dispatch, production output, adjustment) can be posted once, whatever the posting key
  * the ledger, purchase lines and adjustments are append-only (UPDATE, DELETE and TRUNCATE are refused)
  * at COMMIT: no negative on-hand, no valuation residue, active reservations <= on-hand, and receipts, dispatches, outputs and adjustments agree
    with its source (dispatch qty = invoice line qty, output = the run's good output, receipt = the purchase line, ...)

What it does NOT guarantee. The balance rules read the SUM of rows, so they are decided at COMMIT by a deferred
constraint trigger. That trigger takes a row lock on the item (FOR NO KEY UPDATE) before it reads, and refuses to run
above READ COMMITTED, which makes two uncoordinated writers serialise their checks. It is a safety net, not the
protocol: weighted-average values and availability decisions are computed BEFORE commit from a read, and only a writer
that locks the item first (StockService.lock_items, Tasks 12-13) reads a stable balance. See design section 6.

Quantities NUMERIC(14,3), cost per unit NUMERIC(14,4), money NUMERIC(14,2); NaN is rejected explicitly because
PostgreSQL treats it as greater than every number. SQL must not contain a percent sign (see migration 0001).
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

FUNCTIONS = ("assert_stock_invariants", "assert_movement_source", "assert_source_posted", "assert_reservation_source",
             "guard_movement_insert", "guard_purchase_row", "guard_purchase_line_insert", "guard_reservation_row")


def _sql(statement: str) -> None:
    op.get_bind().exec_driver_sql(statement)


def upgrade() -> None:
    # ------------------------------------------------------------------ purchases (4.4)
    _sql("""
    CREATE TABLE purchases (
      id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      purchase_no         TEXT NOT NULL,
      supplier_id         BIGINT NOT NULL REFERENCES suppliers (id),
      supplier_invoice_no TEXT NOT NULL,
      purchase_date       DATE NOT NULL,
      status              TEXT NOT NULL DEFAULT 'posted',
      created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
      created_by          BIGINT NOT NULL REFERENCES users (id),
      voided_at           TIMESTAMPTZ,
      voided_by           BIGINT REFERENCES users (id),
      void_reason         TEXT,
      CONSTRAINT purchases_purchase_no_key UNIQUE (purchase_no),
      CONSTRAINT ck_purchases_purchase_no_not_blank CHECK (btrim(purchase_no) <> ''),
      CONSTRAINT ck_purchases_invoice_no_not_blank CHECK (btrim(supplier_invoice_no) <> ''),
      CONSTRAINT ck_purchases_status CHECK (status IN ('posted','voided')),
      CONSTRAINT ck_purchases_void_columns CHECK (
        (status = 'voided') = (voided_at IS NOT NULL AND voided_by IS NOT NULL AND btrim(coalesce(void_reason, '')) <> '')
        AND (status = 'voided' OR (voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL))),
      CONSTRAINT ck_purchases_voided_after_created CHECK (voided_at IS NULL OR voided_at >= created_at)
    )""")
    # The same supplier invoice entered twice, even with different case or spacing, is a duplicate.
    _sql("CREATE UNIQUE INDEX uq_purchases_supplier_invoice ON purchases (supplier_id, lower(btrim(supplier_invoice_no)))")
    _sql("CREATE INDEX ix_purchases_supplier ON purchases (supplier_id)")
    _sql("CREATE INDEX ix_purchases_date ON purchases (purchase_date)")

    _sql("""
    CREATE TABLE purchase_lines (
      id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      purchase_id  BIGINT NOT NULL REFERENCES purchases (id),
      item_id      BIGINT NOT NULL,
      item_kind    TEXT NOT NULL,
      base_unit    unit_code NOT NULL,
      qty          NUMERIC(14,3) NOT NULL,
      unit         unit_code NOT NULL,
      qty_base     NUMERIC(14,3) NOT NULL,
      rate         NUMERIC(14,4) NOT NULL,
      amount       NUMERIC(14,2) NOT NULL,
      gst_rate     NUMERIC(5,2) NOT NULL DEFAULT 0,
      tax_amount   NUMERIC(14,2) NOT NULL DEFAULT 0,
      CONSTRAINT purchase_lines_purchase_id_item_id_key UNIQUE (purchase_id, item_id),
      CONSTRAINT purchase_lines_id_item_id_key UNIQUE (id, item_id),
      CONSTRAINT fk_purchase_lines_item_kind FOREIGN KEY (item_id, item_kind) REFERENCES items (id, kind),
      CONSTRAINT fk_purchase_lines_item_unit FOREIGN KEY (item_id, base_unit) REFERENCES items (id, base_unit),
      CONSTRAINT ck_purchase_lines_item_kind CHECK (item_kind IN ('raw','packaging')),
      CONSTRAINT ck_purchase_lines_qty_positive CHECK (qty > 0 AND qty <> 'NaN' AND qty_base > 0 AND qty_base <> 'NaN'),
      CONSTRAINT ck_purchase_lines_rate CHECK (rate >= 0 AND rate <> 'NaN'),
      CONSTRAINT ck_purchase_lines_qty_base CHECK (unit_factor(unit, base_unit) IS NOT NULL AND qty_base = round(qty * unit_factor(unit, base_unit), 3)),
      CONSTRAINT ck_purchase_lines_amount CHECK (amount = round(qty * rate, 2)),
      CONSTRAINT ck_purchase_lines_gst_rate CHECK (gst_rate BETWEEN 0 AND 100),
      CONSTRAINT ck_purchase_lines_tax CHECK (tax_amount = round(amount * gst_rate / 100, 2))
    )""")
    _sql("CREATE INDEX ix_purchase_lines_item ON purchase_lines (item_id)")

    # ------------------------------------------------------------------ stock adjustments (4.5)
    _sql("""
    CREATE TABLE stock_adjustments (
      id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      adjustment_no   TEXT NOT NULL,
      kind            TEXT NOT NULL,
      item_id         BIGINT NOT NULL,
      item_kind       TEXT NOT NULL,
      qty_delta_base  NUMERIC(14,3) NOT NULL,
      unit_cost       NUMERIC(14,4),
      reason          TEXT NOT NULL,
      adjustment_date DATE NOT NULL,
      created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
      created_by      BIGINT NOT NULL REFERENCES users (id),
      CONSTRAINT stock_adjustments_adjustment_no_key UNIQUE (adjustment_no),
      CONSTRAINT stock_adjustments_id_item_id_key UNIQUE (id, item_id),
      CONSTRAINT fk_stock_adjustments_item_kind FOREIGN KEY (item_id, item_kind) REFERENCES items (id, kind),
      CONSTRAINT ck_stock_adjustments_no_not_blank CHECK (btrim(adjustment_no) <> ''),
      CONSTRAINT ck_stock_adjustments_kind CHECK (kind IN ('opening','count')),
      CONSTRAINT ck_stock_adjustments_delta_nonzero CHECK (qty_delta_base <> 0 AND qty_delta_base <> 'NaN'),
      CONSTRAINT ck_stock_adjustments_opening_positive CHECK (kind <> 'opening' OR qty_delta_base > 0),
      CONSTRAINT ck_stock_adjustments_unit_cost CHECK (unit_cost IS NULL OR (unit_cost >= 0 AND unit_cost <> 'NaN')),
      CONSTRAINT ck_stock_adjustments_finished_no_cost CHECK (item_kind <> 'finished' OR unit_cost IS NULL),
      CONSTRAINT ck_stock_adjustments_opening_cost CHECK (kind <> 'opening' OR item_kind = 'finished' OR unit_cost IS NOT NULL),
      CONSTRAINT ck_stock_adjustments_reason_not_blank CHECK (btrim(reason) <> '')
    )""")
    _sql("CREATE INDEX ix_stock_adjustments_item_date ON stock_adjustments (item_id, adjustment_date)")

    # ------------------------------------------------------------------ the ledger (4.5)
    _sql("""
    CREATE TABLE stock_movements (
      id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      posting_key          TEXT NOT NULL,
      item_id              BIGINT NOT NULL,
      item_kind            TEXT NOT NULL,
      movement_date        DATE NOT NULL,
      movement_type        TEXT NOT NULL,
      qty_base             NUMERIC(14,3) NOT NULL,
      unit_cost            NUMERIC(14,4),
      value                NUMERIC(14,2),
      batch_no             TEXT,
      purchase_line_id     BIGINT,
      production_run_id    BIGINT,
      invoice_line_id      BIGINT,
      adjustment_id        BIGINT,
      reverses_movement_id BIGINT,
      created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
      created_by           BIGINT NOT NULL REFERENCES users (id),
      CONSTRAINT stock_movements_posting_key_key UNIQUE (posting_key),
      CONSTRAINT stock_movements_id_item_id_key UNIQUE (id, item_id),
      CONSTRAINT stock_movements_id_item_id_movement_type_key UNIQUE (id, item_id, movement_type),
      CONSTRAINT stock_movements_reverses_movement_id_key UNIQUE (reverses_movement_id),
      CONSTRAINT fk_stock_movements_item_kind FOREIGN KEY (item_id, item_kind) REFERENCES items (id, kind),
      CONSTRAINT fk_stock_movements_purchase_line_item FOREIGN KEY (purchase_line_id, item_id) REFERENCES purchase_lines (id, item_id),
      CONSTRAINT fk_stock_movements_invoice_line_item FOREIGN KEY (invoice_line_id, item_id) REFERENCES invoice_lines (id, item_id),
      CONSTRAINT fk_stock_movements_adjustment_item FOREIGN KEY (adjustment_id, item_id) REFERENCES stock_adjustments (id, item_id),
      CONSTRAINT fk_stock_movements_production_run FOREIGN KEY (production_run_id) REFERENCES production_runs (id),
      CONSTRAINT fk_stock_movements_reverses FOREIGN KEY (reverses_movement_id, item_id, movement_type) REFERENCES stock_movements (id, item_id, movement_type),

      CONSTRAINT ck_stock_movements_posting_key_not_blank CHECK (btrim(posting_key) <> ''),
      CONSTRAINT ck_stock_movements_movement_type CHECK (movement_type IN
        ('opening','count_adjustment','purchase_receipt','production_issue','production_scrap','production_output','dispatch')),
      CONSTRAINT ck_stock_movements_qty CHECK (qty_base <> 0 AND qty_base <> 'NaN'),
      CONSTRAINT ck_stock_movements_finite CHECK ((value IS NULL OR value <> 'NaN') AND (unit_cost IS NULL OR unit_cost <> 'NaN')),

      -- exactly one source, and it is the kind of source the movement type needs
      CONSTRAINT ck_stock_movements_one_source CHECK (num_nonnulls(purchase_line_id, production_run_id, invoice_line_id, adjustment_id) = 1),
      CONSTRAINT ck_stock_movements_type_source CHECK (
        (movement_type = 'purchase_receipt' AND purchase_line_id IS NOT NULL)
        OR (movement_type IN ('production_issue','production_scrap','production_output') AND production_run_id IS NOT NULL)
        OR (movement_type = 'dispatch' AND invoice_line_id IS NOT NULL)
        OR (movement_type IN ('opening','count_adjustment') AND adjustment_id IS NOT NULL)),

      -- the item kind suits the movement type; finished goods are quantity-only and whole packets
      CONSTRAINT ck_stock_movements_type_kind CHECK (
        movement_type IN ('opening','count_adjustment')
        OR (movement_type IN ('purchase_receipt','production_issue','production_scrap') AND item_kind <> 'finished')
        OR (movement_type IN ('production_output','dispatch') AND item_kind = 'finished')),
      CONSTRAINT ck_stock_movements_valued_iff_stock_item CHECK ((item_kind = 'finished') = (value IS NULL)),
      CONSTRAINT ck_stock_movements_cost_pair CHECK ((value IS NULL) = (unit_cost IS NULL)),
      CONSTRAINT ck_stock_movements_whole_packets CHECK (item_kind <> 'finished' OR qty_base = trunc(qty_base)),

      -- signs: receipts and output add, issues, scrap and dispatch remove, a count adjustment may do either;
      -- a reversal takes the opposite sign of the movement it reverses (checked by guard_movement_insert)
      CONSTRAINT ck_stock_movements_sign CHECK (
        reverses_movement_id IS NOT NULL OR movement_type = 'count_adjustment'
        OR (movement_type IN ('opening','purchase_receipt','production_output') AND qty_base > 0)
        OR (movement_type IN ('production_issue','production_scrap','dispatch') AND qty_base < 0)),
      CONSTRAINT ck_stock_movements_value_sign CHECK (value IS NULL OR value = 0 OR (value > 0) = (qty_base > 0)),
      -- cost per unit is not an independent input: it is the value spread over the quantity
      CONSTRAINT ck_stock_movements_value_unit_cost CHECK (value IS NULL OR unit_cost = round(value / qty_base, 4)),
      -- only an earlier dispatch or purchase receipt is ever reversed (design 9.6); everything else is corrected forward
      CONSTRAINT ck_stock_movements_reversal_types CHECK (reverses_movement_id IS NULL OR movement_type IN ('dispatch','purchase_receipt'))
    )""")
    # one-shot events are structurally once-only, whatever the posting key says; reversals are excluded
    _sql("CREATE UNIQUE INDEX uq_stock_movements_receipt_once ON stock_movements (purchase_line_id) "
         "WHERE movement_type = 'purchase_receipt' AND reverses_movement_id IS NULL")
    _sql("CREATE UNIQUE INDEX uq_stock_movements_dispatch_once ON stock_movements (invoice_line_id) "
         "WHERE movement_type = 'dispatch' AND reverses_movement_id IS NULL")
    _sql("CREATE UNIQUE INDEX uq_stock_movements_output_once ON stock_movements (production_run_id) "
         "WHERE movement_type = 'production_output' AND reverses_movement_id IS NULL")
    _sql("CREATE UNIQUE INDEX uq_stock_movements_adjustment_once ON stock_movements (adjustment_id) WHERE reverses_movement_id IS NULL")
    # balances, WAC sums and the stock ledger report
    _sql("CREATE INDEX ix_stock_movements_item_cover ON stock_movements (item_id) INCLUDE (qty_base, value)")
    _sql("CREATE INDEX ix_stock_movements_item_date ON stock_movements (item_id, movement_date, id)")
    _sql("CREATE INDEX ix_stock_movements_date ON stock_movements (movement_date, id)")
    # run consumption, invoice dispatch and purchase receipt lookups
    _sql("CREATE INDEX ix_stock_movements_run ON stock_movements (production_run_id) WHERE production_run_id IS NOT NULL")
    _sql("CREATE INDEX ix_stock_movements_invoice_line ON stock_movements (invoice_line_id) WHERE invoice_line_id IS NOT NULL")
    _sql("CREATE INDEX ix_stock_movements_purchase_line ON stock_movements (purchase_line_id) WHERE purchase_line_id IS NOT NULL")

    # ------------------------------------------------------------------ reservations (4.5)
    _sql("""
    CREATE TABLE stock_reservations (
      id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      order_line_id BIGINT NOT NULL,
      item_id       BIGINT NOT NULL,
      qty_base      NUMERIC(14,3) NOT NULL,
      status        TEXT NOT NULL DEFAULT 'active',
      created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
      created_by    BIGINT NOT NULL REFERENCES users (id),
      closed_at     TIMESTAMPTZ,
      CONSTRAINT stock_reservations_order_line_id_key UNIQUE (order_line_id),
      CONSTRAINT fk_stock_reservations_order_line_item FOREIGN KEY (order_line_id, item_id) REFERENCES order_lines (id, item_id),
      CONSTRAINT ck_stock_reservations_qty_positive CHECK (qty_base > 0 AND qty_base <> 'NaN'),
      CONSTRAINT ck_stock_reservations_status CHECK (status IN ('active','released','fulfilled')),
      CONSTRAINT ck_stock_reservations_closed CHECK ((status = 'active') = (closed_at IS NULL)),
      CONSTRAINT ck_stock_reservations_closed_after_created CHECK (closed_at IS NULL OR closed_at >= created_at)
    )""")
    _sql("CREATE INDEX ix_stock_reservations_active_item ON stock_reservations (item_id) WHERE status = 'active'")

    # ------------------------------------------------------------------ functions
    # Posted purchases: header frozen except the void; lines only in the transaction that created the purchase.
    _sql("""
    CREATE FUNCTION guard_purchase_row() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      col text;
    BEGIN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'purchases rows cannot be deleted';
      END IF;
      FOR col IN SELECT n.key FROM jsonb_each(to_jsonb(NEW)) AS n JOIN jsonb_each(to_jsonb(OLD)) AS o ON o.key = n.key
                 WHERE n.value IS DISTINCT FROM o.value ORDER BY n.key LOOP
        IF OLD.status = 'voided' THEN
          RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'a voided purchase cannot be changed (column ' || col || ')';
        END IF;
        IF col NOT IN ('status', 'voided_at', 'voided_by', 'void_reason') THEN
          RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'purchases column ' || col || ' cannot be changed once posted';
        END IF;
      END LOOP;
      RETURN NEW;
    END
    $$""")
    _sql("""
    CREATE FUNCTION guard_purchase_line_insert() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM purchases WHERE id = NEW.purchase_id AND created_at <> now()) THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
          MESSAGE = 'a line cannot be added to a posted purchase (purchase ' || NEW.purchase_id || ')';
      END IF;
      RETURN NEW;
    END
    $$""")
    _sql("""
    CREATE FUNCTION guard_reservation_row() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      col text;
    BEGIN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'stock_reservations rows cannot be deleted';
      END IF;
      FOR col IN SELECT n.key FROM jsonb_each(to_jsonb(NEW)) AS n JOIN jsonb_each(to_jsonb(OLD)) AS o ON o.key = n.key
                 WHERE n.value IS DISTINCT FROM o.value ORDER BY n.key LOOP
        IF OLD.status <> 'active' THEN
          RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'a closed reservation cannot be changed (column ' || col || ')';
        END IF;
        IF col NOT IN ('status', 'closed_at') THEN
          RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'stock_reservations column ' || col || ' cannot be changed';
        END IF;
      END LOOP;
      RETURN NEW;
    END
    $$""")

    # A reversal is the exact mirror of one earlier movement and is itself never reversed. Item agreement is left to the
    # composite foreign key, so a wrong item is reported as the foreign-key violation it is.
    _sql("""
    CREATE FUNCTION guard_movement_insert() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      o stock_movements;
    BEGIN
      NEW.created_at := now();  -- stamped by the database; a client-supplied value is ignored
      IF NEW.reverses_movement_id IS NULL THEN
        RETURN NEW;
      END IF;
      SELECT * INTO o FROM stock_movements WHERE id = NEW.reverses_movement_id;
      IF NOT FOUND OR o.id >= NEW.id THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'a reversal must reference an earlier, already stored movement (' || NEW.reverses_movement_id || ')';
      END IF;
      IF o.reverses_movement_id IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = 'movement ' || o.id || ' is itself a reversal and cannot be reversed';
      END IF;
      IF NEW.movement_date < o.movement_date THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = 'a reversal cannot be dated before the movement it reverses';
      END IF;
      IF NEW.qty_base <> -o.qty_base OR NEW.value IS DISTINCT FROM -o.value
         OR NEW.purchase_line_id IS DISTINCT FROM o.purchase_line_id OR NEW.production_run_id IS DISTINCT FROM o.production_run_id
         OR NEW.invoice_line_id IS DISTINCT FROM o.invoice_line_id OR NEW.adjustment_id IS DISTINCT FROM o.adjustment_id THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'a reversal must be the exact mirror of movement ' || o.id || ' (same source, opposite quantity and value)';
      END IF;
      RETURN NEW;
    END
    $$""")

    # Deferred: the balance rules. The item row lock serialises the check across uncoordinated writers; the isolation
    # guard keeps the read after the lock fresh. Neither replaces the service locking protocol (see the module doc).
    _sql("""
    CREATE FUNCTION assert_stock_invariants() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      v_kind text;
      q      numeric;
      v      numeric;
      r      numeric;
    BEGIN
      IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'stock invariants can only be checked in a READ COMMITTED transaction (this one is '
                    || current_setting('transaction_isolation') || ')';
      END IF;
      PERFORM 1 FROM items WHERE id = NEW.item_id FOR NO KEY UPDATE;
      SELECT kind INTO v_kind FROM items WHERE id = NEW.item_id;
      SELECT coalesce(sum(qty_base), 0), coalesce(sum(value), 0) INTO q, v FROM stock_movements WHERE item_id = NEW.item_id;
      IF q < 0 THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'negative stock for item ' || NEW.item_id || ': on hand would be ' || q;
      END IF;
      IF v_kind <> 'finished' THEN
        IF v < 0 THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'negative valuation for item ' || NEW.item_id || ': stock value would be ' || v;
        END IF;
        IF q = 0 AND v <> 0 THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'valuation residue for item ' || NEW.item_id || ': no stock is left but ' || v || ' of value remains';
        END IF;
      ELSE
        SELECT coalesce(sum(qty_base), 0) INTO r FROM stock_reservations WHERE item_id = NEW.item_id AND status = 'active';
        IF r > q THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'active reservations exceed on-hand stock for item ' || NEW.item_id || ': reserved ' || r || ', on hand ' || q;
        END IF;
      END IF;
      RETURN NULL;
    END
    $$""")

    # Deferred: each movement agrees with the document it cites.
    _sql("""
    CREATE FUNCTION assert_movement_source() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      pl  RECORD;
      rn  RECORD;
      il  RECORD;
      adj RECORD;
      expected text;
    BEGIN
      IF NEW.movement_type = 'purchase_receipt' THEN
        SELECT l.qty_base, l.amount, p.status INTO pl FROM purchase_lines l JOIN purchases p ON p.id = l.purchase_id WHERE l.id = NEW.purchase_line_id;
        IF NEW.reverses_movement_id IS NULL THEN
          IF NEW.qty_base <> pl.qty_base OR NEW.value IS DISTINCT FROM pl.amount THEN
            RAISE EXCEPTION USING ERRCODE = 'check_violation',
              MESSAGE = 'receipt of ' || NEW.qty_base || ' (value ' || NEW.value || ') does not match purchase line ' || NEW.purchase_line_id
                        || ' (' || pl.qty_base || ', ' || pl.amount || ')';
          END IF;
        ELSIF pl.status <> 'voided' THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'receipt reversal for purchase line ' || NEW.purchase_line_id || ' needs a voided purchase: the purchase is not voided';
        END IF;

      ELSIF NEW.movement_type IN ('production_issue', 'production_scrap') THEN
        SELECT status, completed_at INTO rn FROM production_runs WHERE id = NEW.production_run_id;
        IF NOT EXISTS (SELECT 1 FROM production_run_lines WHERE run_id = NEW.production_run_id AND item_id = NEW.item_id) THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'item ' || NEW.item_id || ' is not on the run ' || NEW.production_run_id || ' (add a run line first)';
        END IF;
        IF rn.status = 'completed' AND rn.completed_at < NEW.created_at THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'production run ' || NEW.production_run_id || ' was already completed; nothing more can be issued to it';
        END IF;

      ELSIF NEW.movement_type = 'production_output' THEN
        SELECT status, sku_item_id, good_output_packets INTO rn FROM production_runs WHERE id = NEW.production_run_id;
        IF rn.status <> 'completed' THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = 'production run ' || NEW.production_run_id || ' is not completed';
        END IF;
        IF NEW.item_id <> rn.sku_item_id THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'item ' || NEW.item_id || ' is not the SKU of the run ' || NEW.production_run_id;
        END IF;
        IF NEW.qty_base <> rn.good_output_packets THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'output of ' || NEW.qty_base || ' does not equal the good output of run ' || NEW.production_run_id || ' (' || rn.good_output_packets || ')';
        END IF;

      ELSIF NEW.movement_type = 'dispatch' THEN
        SELECT l.qty, i.status INTO il FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id WHERE l.id = NEW.invoice_line_id;
        IF NEW.reverses_movement_id IS NULL THEN
          IF NEW.qty_base <> -il.qty THEN
            RAISE EXCEPTION USING ERRCODE = 'check_violation',
              MESSAGE = 'dispatch of ' || NEW.qty_base || ' does not match invoice line ' || NEW.invoice_line_id || ' quantity ' || il.qty;
          END IF;
          IF il.status <> 'issued' THEN
            RAISE EXCEPTION USING ERRCODE = 'check_violation',
              MESSAGE = 'dispatch needs an issued invoice: the invoice is ' || il.status;
          END IF;
        ELSIF il.status <> 'cancelled' THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'a dispatch reversal needs a cancelled invoice: the invoice is not cancelled (status ' || il.status || ')';
        END IF;

      ELSE
        SELECT kind, qty_delta_base, unit_cost INTO adj FROM stock_adjustments WHERE id = NEW.adjustment_id;
        expected := CASE adj.kind WHEN 'opening' THEN 'opening' ELSE 'count_adjustment' END;
        IF NEW.movement_type <> expected THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'movement type ' || NEW.movement_type || ' does not suit adjustment kind ' || adj.kind;
        END IF;
        IF NEW.qty_base <> adj.qty_delta_base THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'movement quantity ' || NEW.qty_base || ' does not match adjustment ' || NEW.adjustment_id || ' (' || adj.qty_delta_base || ')';
        END IF;
        IF adj.unit_cost IS NOT NULL AND NEW.qty_base > 0 AND NEW.value IS DISTINCT FROM round(NEW.qty_base * adj.unit_cost, 2) THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'movement value ' || NEW.value || ' does not match adjustment ' || NEW.adjustment_id || ' (quantity x unit cost)';
        END IF;
      END IF;
      RETURN NULL;
    END
    $$""")

    # Deferred: a source document that moves stock is not complete without its movements, and a voided purchase has
    # reversed every receipt.
    _sql("""
    CREATE FUNCTION assert_source_posted() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    BEGIN
      IF TG_TABLE_NAME = 'purchase_lines' THEN
        IF NOT EXISTS (SELECT 1 FROM stock_movements WHERE purchase_line_id = NEW.id AND movement_type = 'purchase_receipt' AND reverses_movement_id IS NULL) THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = 'purchase line ' || NEW.id || ' has no receipt movement';
        END IF;
      ELSIF TG_TABLE_NAME = 'stock_adjustments' THEN
        IF NOT EXISTS (SELECT 1 FROM stock_movements WHERE adjustment_id = NEW.id AND reverses_movement_id IS NULL) THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = 'adjustment ' || NEW.id || ' has no movement';
        END IF;
      ELSIF NEW.status = 'voided' THEN
        IF EXISTS (SELECT 1
                   FROM purchase_lines l
                   JOIN stock_movements r ON r.purchase_line_id = l.id AND r.movement_type = 'purchase_receipt' AND r.reverses_movement_id IS NULL
                   WHERE l.purchase_id = NEW.id
                     AND NOT EXISTS (SELECT 1 FROM stock_movements x WHERE x.reverses_movement_id = r.id)) THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'purchase ' || NEW.id || ' is voided but its receipts are not reversed';
        END IF;
      END IF;
      RETURN NULL;
    END
    $$""")

    # Deferred: a reservation holds the quantity of an approved or invoiced order line.
    _sql("""
    CREATE FUNCTION assert_reservation_source() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      ol RECORD;
    BEGIN
      SELECT l.qty, o.status INTO ol FROM order_lines l JOIN orders o ON o.id = l.order_id WHERE l.id = NEW.order_line_id;
      IF NEW.qty_base <> ol.qty THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'reservation of ' || NEW.qty_base || ' does not equal the order line quantity ' || ol.qty;
      END IF;
      IF ol.status NOT IN ('approved', 'invoiced') THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'the order is ' || ol.status || '; reservations are held only for approved or invoiced orders';
      END IF;
      RETURN NULL;
    END
    $$""")

    # ------------------------------------------------------------------ triggers (names sort in firing order)
    _sql("CREATE TRIGGER trg_1_status BEFORE INSERT OR UPDATE ON purchases FOR EACH ROW EXECUTE FUNCTION enforce_status_transition()")
    _sql("CREATE TRIGGER trg_2_guard BEFORE UPDATE OR DELETE ON purchases FOR EACH ROW EXECUTE FUNCTION guard_purchase_row()")
    _sql("CREATE TRIGGER trg_late_line BEFORE INSERT ON purchase_lines FOR EACH ROW EXECUTE FUNCTION guard_purchase_line_insert()")
    _sql("CREATE TRIGGER trg_1_status BEFORE INSERT OR UPDATE ON stock_reservations FOR EACH ROW EXECUTE FUNCTION enforce_status_transition()")
    _sql("CREATE TRIGGER trg_2_guard BEFORE UPDATE OR DELETE ON stock_reservations FOR EACH ROW EXECUTE FUNCTION guard_reservation_row()")
    _sql("CREATE TRIGGER trg_mirror BEFORE INSERT ON stock_movements FOR EACH ROW EXECUTE FUNCTION guard_movement_insert()")

    for table in ("stock_movements", "stock_adjustments", "purchase_lines"):
        _sql(f"CREATE TRIGGER trg_append_only BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION forbid_mutation()")
        _sql(f"CREATE TRIGGER trg_no_truncate BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()")

    deferred = "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION"
    _sql(f"CREATE CONSTRAINT TRIGGER trg_movement_source AFTER INSERT ON stock_movements {deferred} assert_movement_source()")
    _sql(f"CREATE CONSTRAINT TRIGGER trg_stock_invariants AFTER INSERT ON stock_movements {deferred} assert_stock_invariants()")
    _sql(f"CREATE CONSTRAINT TRIGGER trg_stock_invariants AFTER INSERT OR UPDATE OF status ON stock_reservations {deferred} assert_stock_invariants()")
    _sql(f"CREATE CONSTRAINT TRIGGER trg_reservation_source AFTER INSERT ON stock_reservations {deferred} assert_reservation_source()")
    _sql(f"CREATE CONSTRAINT TRIGGER trg_source_posted AFTER INSERT ON purchase_lines {deferred} assert_source_posted()")
    _sql(f"CREATE CONSTRAINT TRIGGER trg_source_posted AFTER INSERT ON stock_adjustments {deferred} assert_source_posted()")
    _sql(f"CREATE CONSTRAINT TRIGGER trg_source_posted AFTER UPDATE OF status ON purchases {deferred} assert_source_posted()")


def downgrade() -> None:
    for table in ("stock_reservations", "stock_movements", "stock_adjustments", "purchase_lines", "purchases"):
        _sql(f"DROP TABLE {table}")
    for function in FUNCTIONS:
        _sql(f"DROP FUNCTION {function}()")
