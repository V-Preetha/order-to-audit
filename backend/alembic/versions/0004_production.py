"""production runs, run lines, overheads

Revision ID: 0004
Revises: 0003

Implements design section 4.8 of docs/design/01-database-design.md.

A run records what was PLANNED and what was PRODUCED, and nothing else:
  * the run's SKU is its BOM's SKU (composite foreign key); the BOM must be active when the run starts
  * expected output, tolerance and every expected material quantity are snapshots taken at creation:
    expected output = BOM yield x planned batches; run lines with expected > 0 are exactly the BOM's recipe
    and packaging lines scaled by the batches (deferred check, at COMMIT); tolerance equals the settings value (a run cannot start before the settings row exists). Run lines are append-only, and cannot be added once the run is completed.
  * in_progress -> completed is the only lifecycle (status_transitions, seeded in 0001). A completed run
    accepts no UPDATE at all (guard_posted_row, 0001). Runs and overheads are never deleted.
  * ACTUAL consumption is not a column anywhere here: it is derived from stock-ledger movements
    (production_issue + production_scrap per run and item) once the ledger exists (migration 0005).
  * overheads are direct (one run) or shared (a month), never both; they may be recorded after the run is
    completed; values are frozen, an overhead can only be voided, once.

Quantities NUMERIC(14,3), money NUMERIC(14,2); NaN is rejected explicitly because PostgreSQL treats it as
greater than every number. SQL must not contain a percent sign (see migration 0001).
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _sql(statement: str) -> None:
    op.get_bind().exec_driver_sql(statement)


def upgrade() -> None:
    _sql("""
    CREATE TABLE production_runs (
      id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      run_no                  TEXT NOT NULL,
      sku_item_id             BIGINT NOT NULL,
      bom_id                  BIGINT NOT NULL,
      planned_batches         NUMERIC(8,3) NOT NULL,
      expected_output_packets NUMERIC(14,3) NOT NULL,
      tolerance_pct           NUMERIC(5,2) NOT NULL,
      good_output_packets     NUMERIC(14,3),
      completion_note         TEXT,
      status                  TEXT NOT NULL DEFAULT 'in_progress',
      started_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
      started_by              BIGINT NOT NULL REFERENCES users (id),
      completed_at            TIMESTAMPTZ,
      completed_by            BIGINT REFERENCES users (id),
      notes                   TEXT,
      CONSTRAINT production_runs_run_no_key UNIQUE (run_no),
      CONSTRAINT fk_production_runs_bom_sku FOREIGN KEY (bom_id, sku_item_id) REFERENCES boms (id, sku_item_id),
      CONSTRAINT ck_production_runs_run_no_not_blank CHECK (btrim(run_no) <> ''),
      CONSTRAINT ck_production_runs_planned_batches_positive CHECK (planned_batches > 0 AND planned_batches <> 'NaN'),
      CONSTRAINT ck_production_runs_expected_output_positive CHECK (expected_output_packets > 0 AND expected_output_packets <> 'NaN'),
      CONSTRAINT ck_production_runs_tolerance CHECK (tolerance_pct BETWEEN 0 AND 100),
      CONSTRAINT ck_production_runs_good_output_nonnegative CHECK (good_output_packets IS NULL OR (good_output_packets >= 0 AND good_output_packets <> 'NaN')),
      CONSTRAINT ck_production_runs_status CHECK (status IN ('in_progress','completed')),
      CONSTRAINT ck_production_runs_completion CHECK ((status = 'completed') = (good_output_packets IS NOT NULL AND completed_at IS NOT NULL)),
      CONSTRAINT ck_production_runs_completed_by_pair CHECK ((completed_at IS NULL) = (completed_by IS NULL)),
      CONSTRAINT ck_production_runs_completed_after_started CHECK (completed_at IS NULL OR completed_at >= started_at),
      CONSTRAINT ck_production_runs_in_progress_blank CHECK (status = 'completed' OR (good_output_packets IS NULL AND completed_at IS NULL AND completed_by IS NULL AND completion_note IS NULL)),
      CONSTRAINT ck_production_runs_zero_output_note CHECK (
        status <> 'completed' OR good_output_packets > 0 OR btrim(coalesce(completion_note, '')) <> '')
    )""")
    _sql("CREATE INDEX ix_production_runs_status_started ON production_runs (status, started_at)")
    _sql("CREATE INDEX ix_production_runs_bom ON production_runs (bom_id)")

    # expected_qty_base is the snapshot of what the BOM called for; the unit witness keeps the item's base unit fixed under it.
    _sql("""
    CREATE TABLE production_run_lines (
      id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      run_id           BIGINT NOT NULL REFERENCES production_runs (id),
      item_id          BIGINT NOT NULL,
      item_kind        TEXT NOT NULL,
      unit             unit_code NOT NULL,
      expected_qty_base NUMERIC(14,3) NOT NULL,
      CONSTRAINT production_run_lines_run_id_item_id_key UNIQUE (run_id, item_id),
      CONSTRAINT ck_production_run_lines_item_kind CHECK (item_kind IN ('raw','packaging')),
      CONSTRAINT ck_production_run_lines_expected_nonnegative CHECK (expected_qty_base >= 0 AND expected_qty_base <> 'NaN'),
      CONSTRAINT fk_production_run_lines_item_kind FOREIGN KEY (item_id, item_kind) REFERENCES items (id, kind),
      CONSTRAINT fk_production_run_lines_item_unit FOREIGN KEY (item_id, unit) REFERENCES items (id, base_unit)
    )""")
    _sql("CREATE INDEX ix_production_run_lines_item ON production_run_lines (item_id)")

    _sql("""
    CREATE TABLE production_overheads (
      id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      run_id       BIGINT REFERENCES production_runs (id),
      period_month DATE,
      kind         TEXT NOT NULL,
      amount       NUMERIC(14,2) NOT NULL,
      description  TEXT,
      created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
      created_by   BIGINT NOT NULL REFERENCES users (id),
      voided_at    TIMESTAMPTZ,
      voided_by    BIGINT REFERENCES users (id),
      CONSTRAINT ck_production_overheads_kind CHECK (kind IN ('labour','gas_power','other')),
      CONSTRAINT ck_production_overheads_amount_positive CHECK (amount > 0 AND amount <> 'NaN'),
      CONSTRAINT ck_production_overheads_target CHECK ((run_id IS NOT NULL) <> (period_month IS NOT NULL)),
      CONSTRAINT ck_production_overheads_period_first_of_month CHECK (period_month IS NULL OR extract(day FROM period_month) = 1),
      CONSTRAINT ck_production_overheads_description_not_blank CHECK (description IS NULL OR btrim(description) <> ''),
      CONSTRAINT ck_production_overheads_void_pair CHECK ((voided_at IS NULL) = (voided_by IS NULL)),
      CONSTRAINT ck_production_overheads_voided_after_created CHECK (voided_at IS NULL OR voided_at >= created_at)
    )""")
    _sql("CREATE INDEX ix_production_overheads_run ON production_overheads (run_id)")
    _sql("CREATE INDEX ix_production_overheads_period ON production_overheads (period_month)")

    # ------------------------------------------------------------------ trigger functions
    # A run's identity is fixed at creation; only notes (and the completion fields) can change while it runs.
    _sql("""
    CREATE FUNCTION guard_run_header() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.run_no IS DISTINCT FROM OLD.run_no OR NEW.sku_item_id IS DISTINCT FROM OLD.sku_item_id
         OR NEW.bom_id IS DISTINCT FROM OLD.bom_id OR NEW.planned_batches IS DISTINCT FROM OLD.planned_batches
         OR NEW.expected_output_packets IS DISTINCT FROM OLD.expected_output_packets OR NEW.tolerance_pct IS DISTINCT FROM OLD.tolerance_pct
         OR NEW.started_at IS DISTINCT FROM OLD.started_at OR NEW.started_by IS DISTINCT FROM OLD.started_by THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
          MESSAGE = 'production_runs: run number, SKU, BOM, batches, expected output, tolerance and starter are fixed at creation';
      END IF;
      RETURN NEW;
    END
    $$""")

    # Immediate, after the CHECKs: the BOM is active, expected output is its yield x batches, tolerance is the settings value.
    _sql("""
    CREATE FUNCTION check_run_insert() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      b_status text;
      b_yield  numeric;
      s_tol    numeric;
    BEGIN
      SELECT status, yield_packets_per_batch INTO b_status, b_yield FROM boms WHERE id = NEW.bom_id FOR SHARE;
      IF b_status <> 'active' THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'production run ' || NEW.run_no || ': its BOM is ' || b_status || ' and is not active';
      END IF;
      IF NEW.expected_output_packets <> round(b_yield * NEW.planned_batches, 3) THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'production run ' || NEW.run_no || ': expected output ' || NEW.expected_output_packets::text
                    || ' is not the BOM yield ' || b_yield::text || ' x ' || NEW.planned_batches::text || ' batches';
      END IF;
      SELECT variance_tolerance_pct INTO s_tol FROM settings WHERE id = 1;
      IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'production run ' || NEW.run_no || ': no settings row exists to take the tolerance from';
      END IF;
      IF NEW.tolerance_pct <> s_tol THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'production run ' || NEW.run_no || ': tolerance ' || NEW.tolerance_pct::text || ' is not the settings tolerance ' || s_tol::text;
      END IF;
      RETURN NULL;
    END
    $$""")

    # Deferred: the lines with expected > 0 are exactly the BOM's recipe and packaging lines scaled by the batches.
    _sql("""
    CREATE FUNCTION assert_run_snapshot() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      rid     bigint;
      r_no    text;
      r_bom   bigint;
      r_batch numeric;
      diff    bigint;
    BEGIN
      IF TG_TABLE_NAME = 'production_runs' THEN
        rid := NEW.id;
      ELSE
        rid := NEW.run_id;
      END IF;
      SELECT run_no, bom_id, planned_batches INTO r_no, r_bom, r_batch FROM production_runs WHERE id = rid;
      IF NOT FOUND THEN
        RETURN NULL;
      END IF;
      SELECT count(*) INTO diff FROM (
        (SELECT item_id, q FROM (
           SELECT rl.item_id, round(rl.qty_per_batch * r_batch, 3) AS q
             FROM recipe_lines rl JOIN boms b ON b.recipe_id = rl.recipe_id WHERE b.id = r_bom
           UNION ALL
           SELECT pl.item_id, round(pl.qty_per_batch * r_batch, 3) FROM bom_packaging_lines pl WHERE pl.bom_id = r_bom
         ) planned WHERE q > 0
         EXCEPT
         SELECT item_id, expected_qty_base FROM production_run_lines WHERE run_id = rid AND expected_qty_base > 0)
        UNION ALL
        (SELECT item_id, expected_qty_base FROM production_run_lines WHERE run_id = rid AND expected_qty_base > 0
         EXCEPT
         SELECT item_id, q FROM (
           SELECT rl.item_id, round(rl.qty_per_batch * r_batch, 3) AS q
             FROM recipe_lines rl JOIN boms b ON b.recipe_id = rl.recipe_id WHERE b.id = r_bom
           UNION ALL
           SELECT pl.item_id, round(pl.qty_per_batch * r_batch, 3) FROM bom_packaging_lines pl WHERE pl.bom_id = r_bom
         ) planned WHERE q > 0)
      ) d;
      IF diff > 0 THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
          MESSAGE = 'production run ' || r_no || ': planned lines do not match the BOM scaled by ' || r_batch::text || ' batches';
      END IF;
      RETURN NULL;
    END
    $$""")

    # Lines are added while the run is in progress (unplanned material shows up as expected 0), never after completion.
    _sql("""
    CREATE FUNCTION guard_run_line_insert() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      st text;
    BEGIN
      SELECT status INTO st FROM production_runs WHERE id = NEW.run_id FOR SHARE;
      IF st = 'completed' THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
          MESSAGE = 'production_run_lines: the run is completed; lines cannot be added';
      END IF;
      RETURN NEW;
    END
    $$""")

    # Overheads: never deleted; only the void fields may change, and only once.
    _sql("""
    CREATE FUNCTION guard_overhead() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'production_overheads rows cannot be deleted';
      END IF;
      IF (to_jsonb(NEW) - 'voided_at' - 'voided_by') <> (to_jsonb(OLD) - 'voided_at' - 'voided_by') THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'production_overheads: only the void fields can change';
      END IF;
      IF OLD.voided_at IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'production_overheads: the overhead is already voided';
      END IF;
      RETURN NEW;
    END
    $$""")

    # ------------------------------------------------------------------ triggers (names sort in firing order)
    _sql("CREATE TRIGGER trg_1_status BEFORE INSERT OR UPDATE ON production_runs FOR EACH ROW EXECUTE FUNCTION enforce_status_transition()")
    _sql("CREATE TRIGGER trg_2_freeze BEFORE UPDATE OR DELETE ON production_runs FOR EACH ROW EXECUTE FUNCTION guard_posted_row()")
    _sql("CREATE TRIGGER trg_3_identity BEFORE UPDATE ON production_runs FOR EACH ROW EXECUTE FUNCTION guard_run_header()")
    _sql("CREATE TRIGGER trg_start_check AFTER INSERT ON production_runs FOR EACH ROW EXECUTE FUNCTION check_run_insert()")
    _sql("CREATE CONSTRAINT TRIGGER trg_run_snapshot AFTER INSERT ON production_runs DEFERRABLE INITIALLY DEFERRED "
         "FOR EACH ROW EXECUTE FUNCTION assert_run_snapshot()")
    _sql("CREATE TRIGGER trg_late_line BEFORE INSERT ON production_run_lines FOR EACH ROW EXECUTE FUNCTION guard_run_line_insert()")
    _sql("CREATE TRIGGER trg_append_only BEFORE UPDATE OR DELETE ON production_run_lines FOR EACH ROW EXECUTE FUNCTION forbid_mutation()")
    _sql("CREATE CONSTRAINT TRIGGER trg_run_snapshot AFTER INSERT ON production_run_lines DEFERRABLE INITIALLY DEFERRED "
         "FOR EACH ROW EXECUTE FUNCTION assert_run_snapshot()")
    _sql("CREATE TRIGGER trg_guard BEFORE UPDATE OR DELETE ON production_overheads FOR EACH ROW EXECUTE FUNCTION guard_overhead()")


def downgrade() -> None:
    for table in ("production_overheads", "production_run_lines", "production_runs"):
        _sql(f"DROP TABLE {table}")
    for function in ("guard_overhead()", "guard_run_line_insert()", "assert_run_snapshot()", "check_run_insert()", "guard_run_header()"):
        _sql(f"DROP FUNCTION {function}")
