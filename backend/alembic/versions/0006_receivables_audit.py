"""receivables (payments, allocations) and the append-only audit log

Revision ID: 0006
Revises: 0005

Implements design sections 4.7 and 4.9 of docs/design/01-database-design.md (integrity rule 7 in section 5).

What this migration guarantees on its own (each rule has a named test in tests/db/test_receivables_constraints.py):
  * money is NUMERIC(14,2) and strictly positive (NaN included); paise are kept exactly
  * an allocation belongs to ONE customer: composite foreign keys tie it to a payment and to an invoice of that same customer
  * at COMMIT (deferred constraint trigger, per allocation): the payment is still posted, the invoice is still issued,
    the allocations of the payment add up to at most the payment, and the allocations from POSTED payments to the invoice
    add up to at most the invoice total
  * an invoice cannot be cancelled while allocations from posted payments exist (the payment is reversed first)
  * a payment is never deleted: it is posted, then (once) reversed; a reversal keeps the row and its allocations as history,
    and the allocations simply stop counting. Reversal time is stamped by the database; a reversed payment is frozen
  * allocations and audit entries are append-only (UPDATE, DELETE and TRUNCATE are refused); creation and occurrence times
    are stamped by the database, so history cannot be backdated by a client (times are the database's transaction time, and the audit log's wall-clock time). Business dates (payment_date) stay as the user entered them

What it does NOT do on its own:
  * the commit-time check serialises uncoordinated writers by locking the payment and invoice rows (FOR NO KEY UPDATE) and
    refuses to run above READ COMMITTED, but this is defence in depth: the payment service must still lock the payment and
    invoice rows first, in ascending id order (design section 6). Two uncoordinated multi-row writers that lock in opposite
    order can deadlock; PostgreSQL aborts one of them, so integrity is kept
  * payment numbers, receipts and ageing are service/reporting concerns (later tasks)

Raw driver SQL must not contain a percent sign (see migration 0001). NaN is greater than every number in PostgreSQL,
so every positivity CHECK also rejects it explicitly.
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

FUNCTIONS = ("guard_payment_row", "assert_allocation_limits", "assert_invoice_not_settled", "stamp_payment_lifecycle", "stamp_allocation_time", "stamp_audit_time")


def _sql(statement: str) -> None:
    op.get_bind().exec_driver_sql(statement)


def upgrade() -> None:
    _sql("""
    CREATE TABLE payments (
      id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      payment_no     TEXT NOT NULL,
      client_uuid    UUID NOT NULL,
      customer_id    BIGINT NOT NULL REFERENCES customers (id),
      amount         NUMERIC(14,2) NOT NULL,
      payment_date   DATE NOT NULL,
      method         TEXT NOT NULL,
      reference      TEXT,
      status         TEXT NOT NULL DEFAULT 'posted',
      created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
      created_by     BIGINT NOT NULL REFERENCES users (id),
      reversed_at    TIMESTAMPTZ,
      reversed_by    BIGINT REFERENCES users (id),
      reverse_reason TEXT,
      CONSTRAINT payments_payment_no_key UNIQUE (payment_no),
      CONSTRAINT payments_client_uuid_key UNIQUE (client_uuid),
      CONSTRAINT payments_id_customer_id_key UNIQUE (id, customer_id),
      CONSTRAINT ck_payments_payment_no_not_blank CHECK (btrim(payment_no) <> ''),
      CONSTRAINT ck_payments_amount CHECK (amount > 0 AND amount <> 'NaN'),
      CONSTRAINT ck_payments_method CHECK (method IN ('cash','upi','bank','cheque')),
      CONSTRAINT ck_payments_reference_not_blank CHECK (reference IS NULL OR btrim(reference) <> ''),
      CONSTRAINT ck_payments_status CHECK (status IN ('posted','reversed')),
      CONSTRAINT ck_payments_reverse_columns CHECK (
        (status = 'reversed') = (reversed_at IS NOT NULL AND reversed_by IS NOT NULL AND btrim(coalesce(reverse_reason, '')) <> '')
        AND (status = 'reversed' OR (reversed_at IS NULL AND reversed_by IS NULL AND reverse_reason IS NULL))),
      CONSTRAINT ck_payments_reversed_after_created CHECK (reversed_at IS NULL OR reversed_at >= created_at)
    )""")
    _sql("CREATE INDEX ix_payments_customer ON payments (customer_id)")
    _sql("CREATE INDEX ix_payments_date ON payments (payment_date)")

    _sql("""
    CREATE TABLE payment_allocations (
      id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      payment_id  BIGINT NOT NULL,
      invoice_id  BIGINT NOT NULL,
      customer_id BIGINT NOT NULL,
      amount      NUMERIC(14,2) NOT NULL,
      created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
      created_by  BIGINT NOT NULL REFERENCES users (id),
      CONSTRAINT payment_allocations_payment_id_invoice_id_key UNIQUE (payment_id, invoice_id),
      CONSTRAINT fk_payment_allocations_payment_customer FOREIGN KEY (payment_id, customer_id) REFERENCES payments (id, customer_id),
      CONSTRAINT fk_payment_allocations_invoice_customer FOREIGN KEY (invoice_id, customer_id) REFERENCES invoices (id, customer_id),
      CONSTRAINT ck_payment_allocations_amount CHECK (amount > 0 AND amount <> 'NaN')
    )""")
    _sql("CREATE INDEX ix_payment_allocations_invoice ON payment_allocations (invoice_id)")

    _sql("""
    CREATE TABLE audit_log (
      id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      user_id     BIGINT REFERENCES users (id),
      action      TEXT NOT NULL,
      entity_type TEXT NOT NULL,
      entity_id   BIGINT NOT NULL,
      before      JSONB,
      after       JSONB,
      request_id  UUID,
      CONSTRAINT ck_audit_log_action_not_blank CHECK (btrim(action) <> ''),
      CONSTRAINT ck_audit_log_entity_type_not_blank CHECK (btrim(entity_type) <> ''),
      CONSTRAINT ck_audit_log_before_object CHECK (before IS NULL OR jsonb_typeof(before) = 'object'),
      CONSTRAINT ck_audit_log_after_object CHECK (after IS NULL OR jsonb_typeof(after) = 'object')
    )""")
    _sql("CREATE INDEX ix_audit_log_entity ON audit_log (entity_type, entity_id, occurred_at)")
    _sql("CREATE INDEX ix_audit_log_user ON audit_log (user_id, occurred_at)")
    _sql("CREATE INDEX ix_audit_log_request ON audit_log (request_id) WHERE request_id IS NOT NULL")

    # A payment is frozen except for its reversal, and a reversed payment is frozen completely. (guard_posted_row from migration 0001 allows only columns that start with
    # 'reversed_', which does not cover the design's `reverse_reason`, so payments get their own guard instead of changing 0001.)
    _sql("""
    CREATE FUNCTION guard_payment_row() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      col text;
    BEGIN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'payments rows cannot be deleted';
      END IF;
      IF OLD.status = 'reversed' AND to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'a reversed payment cannot be changed (payment ' || OLD.id || ')';
      END IF;
      FOR col IN SELECT n.key FROM jsonb_each(to_jsonb(NEW)) AS n JOIN jsonb_each(to_jsonb(OLD)) AS o ON o.key = n.key
                 WHERE n.value IS DISTINCT FROM o.value ORDER BY n.key LOOP
        IF col NOT IN ('status', 'reversed_at', 'reversed_by', 'reverse_reason') THEN
          RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'payments column ' || col || ' cannot be changed once posted';
        END IF;
      END LOOP;
      RETURN NEW;
    END
    $$""")

    # Creation, reversal and occurrence times are stamped by the database; a client-supplied value is overwritten.
    # Runs after trg_2_posted (guard_payment_row), which freezes a reversed payment completely.
    _sql("""
    CREATE FUNCTION stamp_payment_lifecycle() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        NEW.created_at := now();
      ELSIF OLD.status = 'posted' AND NEW.status = 'reversed' THEN
        NEW.reversed_at := now();
      END IF;
      RETURN NEW;
    END
    $$""")
    _sql("""
    CREATE FUNCTION stamp_allocation_time() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    BEGIN
      NEW.created_at := now();
      RETURN NEW;
    END
    $$""")
    _sql("""
    CREATE FUNCTION stamp_audit_time() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    BEGIN
      NEW.occurred_at := clock_timestamp();   -- wall clock, not the transaction start: late entries of a long transaction are not backdated
      RETURN NEW;
    END
    $$""")

    # Deferred, per allocation. Locks the payment row and then the invoice row (FOR NO KEY UPDATE: it does not conflict with the
    # KEY SHARE locks foreign keys take, so inserts never upgrade-deadlock, but it does conflict with a concurrent reversal,
    # cancellation or another allocation check). The sums are read in LATER statements, so under READ COMMITTED they see
    # whatever the lock holder committed. Above READ COMMITTED the snapshot could be stale, so the check fails closed.
    _sql("""
    CREATE FUNCTION assert_allocation_limits() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      pay       RECORD;
      inv       RECORD;
      allocated numeric;
      settled   numeric;
    BEGIN
      IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'payment allocations can only be checked in a READ COMMITTED transaction (this one is '
                    || current_setting('transaction_isolation') || ')';
      END IF;
      SELECT status, amount INTO pay FROM payments WHERE id = NEW.payment_id FOR NO KEY UPDATE;
      SELECT status, grand_total INTO inv FROM invoices WHERE id = NEW.invoice_id FOR NO KEY UPDATE;
      IF pay.status <> 'posted' THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'payment ' || NEW.payment_id || ' is not posted (' || pay.status || '): it cannot receive allocations';
      END IF;
      IF inv.status <> 'issued' THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'invoice ' || NEW.invoice_id || ' is not issued (' || inv.status || '): it cannot receive allocations';
      END IF;
      SELECT coalesce(sum(amount), 0) INTO allocated FROM payment_allocations WHERE payment_id = NEW.payment_id;
      IF allocated > pay.amount THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'allocations of payment ' || NEW.payment_id || ' exceed the payment amount (allocated ' || allocated || ' of ' || pay.amount || ')';
      END IF;
      SELECT coalesce(sum(a.amount), 0) INTO settled
      FROM payment_allocations a JOIN payments p ON p.id = a.payment_id
      WHERE a.invoice_id = NEW.invoice_id AND p.status = 'posted';
      IF settled > inv.grand_total THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'allocations to invoice ' || NEW.invoice_id || ' exceed the invoice total (allocated ' || settled || ' of ' || inv.grand_total || ')';
      END IF;
      RETURN NULL;
    END
    $$""")

    # Deferred, on invoices: cancelling an invoice that still has allocations from posted payments is refused at COMMIT.
    _sql("""
    CREATE FUNCTION assert_invoice_not_settled() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    BEGIN
      IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'invoice cancellation can only be checked in a READ COMMITTED transaction (this one is '
                    || current_setting('transaction_isolation') || ')';
      END IF;
      IF NEW.status = 'cancelled' AND EXISTS (SELECT 1 FROM payment_allocations a JOIN payments p ON p.id = a.payment_id
                                              WHERE a.invoice_id = NEW.id AND p.status = 'posted') THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'invoice ' || NEW.id || ' is cancelled but still has allocations from posted payments: reverse the payments first';
      END IF;
      RETURN NULL;
    END
    $$""")

    deferred = "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION"
    _sql("CREATE TRIGGER trg_1_status BEFORE INSERT OR UPDATE ON payments FOR EACH ROW EXECUTE FUNCTION enforce_status_transition()")
    _sql("CREATE TRIGGER trg_2_posted BEFORE UPDATE OR DELETE ON payments FOR EACH ROW EXECUTE FUNCTION guard_payment_row()")
    _sql("CREATE TRIGGER trg_3_lifecycle BEFORE INSERT OR UPDATE ON payments FOR EACH ROW EXECUTE FUNCTION stamp_payment_lifecycle()")
    _sql("CREATE TRIGGER trg_1_stamp BEFORE INSERT ON payment_allocations FOR EACH ROW EXECUTE FUNCTION stamp_allocation_time()")
    _sql("CREATE TRIGGER trg_1_stamp BEFORE INSERT ON audit_log FOR EACH ROW EXECUTE FUNCTION stamp_audit_time()")
    for table in ("payment_allocations", "audit_log"):
        _sql(f"CREATE TRIGGER trg_append_only BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION forbid_mutation()")
    for table in ("payments", "payment_allocations", "audit_log"):
        _sql(f"CREATE TRIGGER trg_no_truncate BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()")
    _sql(f"CREATE CONSTRAINT TRIGGER trg_allocation_limits AFTER INSERT ON payment_allocations {deferred} assert_allocation_limits()")
    _sql(f"CREATE CONSTRAINT TRIGGER trg_invoice_not_settled AFTER UPDATE OF status ON invoices {deferred} assert_invoice_not_settled()")


def downgrade() -> None:
    _sql("DROP TRIGGER trg_invoice_not_settled ON invoices")
    for table in ("payment_allocations", "payments", "audit_log"):
        _sql(f"DROP TABLE {table}")
    for function in FUNCTIONS:
        _sql(f"DROP FUNCTION {function}()")
