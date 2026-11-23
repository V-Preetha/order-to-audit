"""recipes, recipe lines, BOMs and BOM packaging lines

Revision ID: 0002
Revises: 0001

Implements design section 4.3 of docs/design/01-database-design.md.

  recipe  = the shared bulk formulation for a product (what goes into one standard batch)
  BOM     = binds ONE finished SKU to a recipe, a yield, and its own packaging
So a new pack size (500 g) is data entry only: a new SKU item, a new BOM that points at the
existing recipe with its own yield, and its own packaging lines.

Integrity enforced by the database:
  * recipe lines reference raw items only; BOM packaging lines reference packaging items only;
    a BOM's SKU must be a finished item (witness columns + composite foreign keys, design 4.0)
  * SKU, recipe and BOM belong to the same product (two composite foreign keys)
  * at most one active BOM per SKU (partial unique index)
  * versions: UNIQUE (sku_item_id, version); a BOM or recipe is edited only while `draft`.
    `active` rows can only be retired, `retired` rows never change, and their lines are frozen
    with them, so a retired version keeps the assumptions that production runs were based on.
  * a recipe cannot be retired while an active BOM uses it
  * line units are witnessed (unit = the item's base unit), so an item's base unit cannot be changed
    underneath a frozen quantity
  * lifecycle draft -> active -> retired is seeded into status_transitions and enforced by the
    function created in 0001; activation requires lines (and, for a BOM, an active recipe).

Quantities are NUMERIC(14,3) in the item's base unit, the same convention as everywhere else.
SQL must not contain a percent sign (see migration 0001).
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# recipes and boms share one lifecycle. A draft may be discarded by retiring it.
LIFECYCLE = [("(new)", "draft"), ("draft", "active"), ("draft", "retired"), ("active", "retired")]
ENTITIES = ("recipes", "boms")


def _sql(statement: str) -> None:
    op.get_bind().exec_driver_sql(statement)


def upgrade() -> None:
    _sql("""
    CREATE TABLE recipes (
      id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      product_id BIGINT NOT NULL REFERENCES products (id),
      code       TEXT NOT NULL,
      name       TEXT NOT NULL,
      version    INTEGER NOT NULL DEFAULT 1,
      status     TEXT NOT NULL DEFAULT 'draft',
      CONSTRAINT recipes_code_key UNIQUE (code),
      CONSTRAINT recipes_id_product_id_key UNIQUE (id, product_id),
      CONSTRAINT ck_recipes_code_not_blank CHECK (btrim(code) <> ''),
      CONSTRAINT ck_recipes_name_not_blank CHECK (btrim(name) <> ''),
      CONSTRAINT ck_recipes_version_positive CHECK (version >= 1),
      CONSTRAINT ck_recipes_status CHECK (status IN ('draft','active','retired'))
    )""")

    _sql("""
    CREATE TABLE recipe_lines (
      id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      recipe_id     BIGINT NOT NULL REFERENCES recipes (id),
      item_id       BIGINT NOT NULL,
      item_kind     TEXT NOT NULL DEFAULT 'raw',
      unit          unit_code NOT NULL,
      qty_per_batch NUMERIC(14,3) NOT NULL,
      CONSTRAINT recipe_lines_recipe_id_item_id_key UNIQUE (recipe_id, item_id),
      CONSTRAINT ck_recipe_lines_item_kind CHECK (item_kind = 'raw'),
      CONSTRAINT ck_recipe_lines_qty_positive CHECK (qty_per_batch > 0),
      CONSTRAINT fk_recipe_lines_item_kind FOREIGN KEY (item_id, item_kind) REFERENCES items (id, kind),
      CONSTRAINT fk_recipe_lines_item_unit FOREIGN KEY (item_id, unit) REFERENCES items (id, base_unit)
    )""")

    _sql("""
    CREATE TABLE boms (
      id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      sku_item_id             BIGINT NOT NULL,
      sku_kind                TEXT NOT NULL DEFAULT 'finished',
      product_id              BIGINT NOT NULL,
      recipe_id               BIGINT NOT NULL,
      yield_packets_per_batch NUMERIC(14,3) NOT NULL,
      version                 INTEGER NOT NULL DEFAULT 1,
      status                  TEXT NOT NULL DEFAULT 'draft',
      CONSTRAINT boms_id_sku_item_id_key UNIQUE (id, sku_item_id),
      CONSTRAINT boms_sku_item_id_version_key UNIQUE (sku_item_id, version),
      CONSTRAINT ck_boms_sku_kind CHECK (sku_kind = 'finished'),
      CONSTRAINT ck_boms_yield_positive CHECK (yield_packets_per_batch > 0),
      CONSTRAINT ck_boms_version_positive CHECK (version >= 1),
      CONSTRAINT ck_boms_status CHECK (status IN ('draft','active','retired')),
      CONSTRAINT fk_boms_sku_kind FOREIGN KEY (sku_item_id, sku_kind) REFERENCES items (id, kind),
      CONSTRAINT fk_boms_sku_product FOREIGN KEY (sku_item_id, product_id) REFERENCES items (id, product_id),
      CONSTRAINT fk_boms_recipe_product FOREIGN KEY (recipe_id, product_id) REFERENCES recipes (id, product_id)
    )""")
    _sql("CREATE UNIQUE INDEX uq_boms_one_active_per_sku ON boms (sku_item_id) WHERE status = 'active'")

    _sql("""
    CREATE TABLE bom_packaging_lines (
      id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      bom_id        BIGINT NOT NULL REFERENCES boms (id),
      item_id       BIGINT NOT NULL,
      item_kind     TEXT NOT NULL DEFAULT 'packaging',
      unit          unit_code NOT NULL,
      qty_per_batch NUMERIC(14,3) NOT NULL,
      CONSTRAINT bom_packaging_lines_bom_id_item_id_key UNIQUE (bom_id, item_id),
      CONSTRAINT ck_bom_packaging_lines_item_kind CHECK (item_kind = 'packaging'),
      CONSTRAINT ck_bom_packaging_lines_qty_positive CHECK (qty_per_batch > 0),
      CONSTRAINT fk_bom_packaging_lines_item_kind FOREIGN KEY (item_id, item_kind) REFERENCES items (id, kind),
      CONSTRAINT fk_bom_packaging_lines_item_unit FOREIGN KEY (item_id, unit) REFERENCES items (id, base_unit)
    )""")

    # ------------------------------------------------------------------ lifecycle seed
    transitions = sa.table("status_transitions", sa.column("entity"), sa.column("from_status"), sa.column("to_status"))
    op.bulk_insert(transitions, [
        {"entity": entity, "from_status": f, "to_status": t} for entity in ENTITIES for f, t in LIFECYCLE
    ])

    # ------------------------------------------------------------------ trigger functions
    # Draft rows may be edited and discarded. An active row may only be retired (nothing else
    # changes), a retired row never changes, and neither can be deleted.
    _sql("""
    CREATE FUNCTION guard_versioned_row() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
          RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
            MESSAGE = TG_TABLE_NAME::text || ' row is ' || OLD.status || ' and cannot be deleted; only drafts can be discarded';
        END IF;
        RETURN OLD;
      END IF;

      IF OLD.status = 'draft' THEN
        RETURN NEW;
      END IF;
      IF OLD.status = 'active' AND NEW.status = 'retired'
         AND (to_jsonb(NEW) - 'status') = (to_jsonb(OLD) - 'status') THEN
        IF TG_TABLE_NAME = 'recipes' AND EXISTS (SELECT 1 FROM boms WHERE recipe_id = OLD.id AND status = 'active') THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'recipe ' || OLD.code || ' is used by an active BOM and cannot be retired; retire the BOM first';
        END IF;
        RETURN NEW;
      END IF;
      RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
        MESSAGE = TG_TABLE_NAME::text || ' row is ' || OLD.status || ' and cannot be changed; create a new version instead';
    END
    $$""")

    # Lines belong to their parent's version, so they freeze with it. The parent row is read FOR SHARE:
    # a concurrent activation (an UPDATE of that row) then waits for this transaction to finish and
    # sees the line, so no line can appear after activation and none can vanish during it.
    _sql("""
    CREATE FUNCTION guard_version_lines() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    DECLARE
      fk      text;
      parent  bigint;
      st      text;
    BEGIN
      IF TG_TABLE_NAME = 'recipe_lines' THEN
        fk := 'recipe_id';
      ELSIF TG_TABLE_NAME = 'bom_packaging_lines' THEN
        fk := 'bom_id';
      ELSE
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = 'no guard rule for ' || TG_TABLE_NAME::text;
      END IF;

      FOR parent IN
        SELECT DISTINCT p FROM unnest(ARRAY[
          CASE WHEN TG_OP <> 'DELETE' THEN (to_jsonb(NEW) ->> fk)::bigint END,
          CASE WHEN TG_OP <> 'INSERT' THEN (to_jsonb(OLD) ->> fk)::bigint END
        ]) AS p WHERE p IS NOT NULL
      LOOP
        IF TG_TABLE_NAME = 'recipe_lines' THEN
          SELECT status INTO st FROM recipes WHERE id = parent FOR SHARE;
        ELSE
          SELECT status INTO st FROM boms WHERE id = parent FOR SHARE;
        END IF;
        -- a missing parent (st is null) is left for the foreign key to reject
        IF st IS NOT NULL AND st <> 'draft' THEN
          RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
            MESSAGE = TG_TABLE_NAME::text || ' cannot be changed: its parent is ' || st || ' (only draft versions are editable)';
        END IF;
      END LOOP;

      IF TG_OP = 'DELETE' THEN
        RETURN OLD;
      END IF;
      RETURN NEW;
    END
    $$""")

    # Runs only on the draft -> active edge (see the trigger's WHEN clause).
    _sql("""
    CREATE FUNCTION check_activation() RETURNS trigger LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
    BEGIN
      IF TG_TABLE_NAME = 'recipes' THEN
        IF NOT EXISTS (SELECT 1 FROM recipe_lines WHERE recipe_id = NEW.id) THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'recipe ' || NEW.code || ' needs at least one ingredient before it can be activated';
        END IF;
      ELSIF TG_TABLE_NAME = 'boms' THEN
        IF (SELECT status FROM recipes WHERE id = NEW.recipe_id) IS DISTINCT FROM 'active' THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'BOM cannot be activated: its recipe must be active';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM bom_packaging_lines WHERE bom_id = NEW.id) THEN
          RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'BOM cannot be activated: it needs at least one packaging line';
        END IF;
      ELSE
        RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = 'no activation rule for ' || TG_TABLE_NAME::text;
      END IF;
      RETURN NEW;
    END
    $$""")

    # Trigger names sort in the order they must fire: lifecycle edge, then freeze, then activation.
    for table in ENTITIES:
        _sql(f"CREATE TRIGGER trg_1_status BEFORE INSERT OR UPDATE ON {table} "
             f"FOR EACH ROW EXECUTE FUNCTION enforce_status_transition()")
        _sql(f"CREATE TRIGGER trg_2_freeze BEFORE UPDATE OR DELETE ON {table} "
             f"FOR EACH ROW EXECUTE FUNCTION guard_versioned_row()")
        _sql(f"CREATE TRIGGER trg_3_activation BEFORE UPDATE ON {table} "
             f"FOR EACH ROW WHEN (OLD.status = 'draft' AND NEW.status = 'active') EXECUTE FUNCTION check_activation()")
    for table in ("recipe_lines", "bom_packaging_lines"):
        _sql(f"CREATE TRIGGER trg_freeze BEFORE INSERT OR UPDATE OR DELETE ON {table} "
             f"FOR EACH ROW EXECUTE FUNCTION guard_version_lines()")


def downgrade() -> None:
    _sql("DELETE FROM status_transitions WHERE entity IN ('recipes', 'boms')")
    for table in ("bom_packaging_lines", "boms", "recipe_lines", "recipes"):
        _sql(f"DROP TABLE {table}")
    for function in ("check_activation()", "guard_version_lines()", "guard_versioned_row()"):
        _sql(f"DROP FUNCTION {function}")
