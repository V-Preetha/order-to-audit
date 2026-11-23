"""Constraint tests for migration 0001: identity, settings, sequences, request log, masters.

Each test violates exactly one rule and names the constraint that must reject it.
"""
import psycopg.errors as pge
import pytest

from tests.db.helpers import run, scalar, violates

GSTIN_TN = "33AABCR1234F1Z5"   # Retailer A, Tamil Nadu (state 33)
GSTIN_KA = "29AABCD5678G1Z2"   # Distributor B, Karnataka (state 29)


# ---------------------------------------------------------------- users
def _user(session, username="asha", role="admin"):
    run(session, "INSERT INTO users (username, full_name, password_hash, role) "
                 "VALUES (:u, 'Asha K', 'hash', :r)", u=username, r=role)


@pytest.mark.parametrize("role", ["salesman", "admin", "production", "auditor", "owner"])
def test_every_documented_role_is_accepted(db_session, role):
    _user(db_session, role=role)
    db_session.commit()
    assert scalar(db_session, "SELECT role FROM users") == role


def test_unknown_role_rejected(db_session):
    with violates(db_session, pge.CheckViolation, "ck_users_role"):
        _user(db_session, role="manager")


def test_username_must_be_lowercase(db_session):
    with violates(db_session, pge.CheckViolation, "ck_users_username_lowercase"):
        _user(db_session, username="Asha")


def test_username_must_not_be_blank(db_session):
    with violates(db_session, pge.CheckViolation, "ck_users_username_not_blank"):
        _user(db_session, username="  ")


def test_username_is_unique(db_session):
    _user(db_session)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "users_username_key"):
        _user(db_session)


def test_new_user_is_active_by_default(db_session):
    _user(db_session)
    assert scalar(db_session, "SELECT is_active FROM users") is True


# ---------------------------------------------------------------- settings
def _settings(session, **overrides):
    values = dict(id=1, name="Madurai Murukku Works", gstin=GSTIN_TN, state="33")
    values.update(overrides)
    run(session, "INSERT INTO settings (id, factory_name, factory_gstin, factory_state_code) "
                 "VALUES (:id, :name, :gstin, :state)", **values)


def test_settings_single_row(db_session):
    with violates(db_session, pge.CheckViolation, "ck_settings_single_row"):
        _settings(db_session, id=2)


def test_settings_second_row_with_id_one_rejected(db_session):
    _settings(db_session)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "settings_pkey"):
        _settings(db_session)


def test_settings_defaults_match_the_benchmark(db_session):
    _settings(db_session)
    db_session.commit()
    row = run(db_session, "SELECT variance_tolerance_pct, invoice_round_to_rupee FROM settings").one()
    assert str(row.variance_tolerance_pct) == "3.00"


def test_settings_invoice_rounding_defaults_off(db_session):
    _settings(db_session)
    assert scalar(db_session, "SELECT invoice_round_to_rupee FROM settings") is False


@pytest.mark.parametrize("pct", ["0", "3", "100"])
def test_settings_tolerance_inside_range_accepted(db_session, pct):
    _settings(db_session)
    run(db_session, "UPDATE settings SET variance_tolerance_pct = :p", p=pct)
    db_session.commit()


@pytest.mark.parametrize("pct", ["-0.01", "100.01"])
def test_settings_tolerance_outside_range_rejected(db_session, pct):
    _settings(db_session)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_settings_tolerance"):
        run(db_session, "UPDATE settings SET variance_tolerance_pct = :p", p=pct)


def test_settings_gstin_state_must_match_factory_state(db_session):
    with violates(db_session, pge.CheckViolation, "ck_settings_gstin_state"):
        _settings(db_session, gstin=GSTIN_KA, state="33")


def test_settings_gstin_format_checked(db_session):
    with violates(db_session, pge.CheckViolation, "ck_settings_gstin_format"):
        _settings(db_session, gstin="33AABCR1234F1X5")


def test_settings_state_code_must_be_a_gst_state(db_session):
    with violates(db_session, pge.CheckViolation, "ck_settings_state_code"):
        _settings(db_session, gstin="99AABCR1234F1Z5", state="99")


# ---------------------------------------------------------------- document_sequences
def test_sequence_starts_at_zero(db_session):
    run(db_session, "INSERT INTO document_sequences (scope) VALUES ('INV/2026-27')")
    assert scalar(db_session, "SELECT last_value FROM document_sequences") == 0


def test_sequence_cannot_go_negative(db_session):
    with violates(db_session, pge.CheckViolation, "ck_document_sequences_last_value"):
        run(db_session, "INSERT INTO document_sequences (scope, last_value) VALUES ('INV/2026-27', -1)")


def test_sequence_scope_is_unique(db_session):
    run(db_session, "INSERT INTO document_sequences (scope) VALUES ('INV/2026-27')")
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "document_sequences_pkey"):
        run(db_session, "INSERT INTO document_sequences (scope) VALUES ('INV/2026-27')")


# ---------------------------------------------------------------- request_log
GOOD_FINGERPRINT = "a" * 64
REQ = "11111111-1111-1111-1111-111111111111"


def _request(session, rid=REQ, fingerprint=GOOD_FINGERPRINT, etype=None, eid=None):
    run(session, "INSERT INTO request_log (request_id, action, fingerprint, result_entity_type, result_entity_id) "
                 "VALUES (CAST(:r AS uuid), 'approve_order', :f, :t, :i)", r=rid, f=fingerprint, t=etype, i=eid)


def test_request_log_request_id_is_unique(db_session):
    _request(db_session)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "request_log_pkey"):
        _request(db_session)


def test_request_log_fingerprint_must_be_sha256_hex(db_session):
    with violates(db_session, pge.CheckViolation, "ck_request_log_fingerprint"):
        _request(db_session, fingerprint="not-a-hash")


def test_request_log_result_entity_is_both_or_neither(db_session):
    with violates(db_session, pge.CheckViolation, "ck_request_log_result_pair"):
        _request(db_session, etype="order", eid=None)


def test_request_log_records_creation_time(db_session):
    _request(db_session, etype="order", eid=7)
    assert scalar(db_session, "SELECT created_at IS NOT NULL FROM request_log") is True


# ---------------------------------------------------------------- products
def _product(session, name="Murukku", hsn="1905", rate="5"):
    run(session, "INSERT INTO products (name, hsn_code, gst_rate) VALUES (:n, :h, :r)", n=name, h=hsn, r=rate)


@pytest.mark.parametrize("rate", ["0", "5", "12.5", "40", "40.00"])
def test_gst_rate_inside_zero_to_forty_accepted(db_session, rate):
    _product(db_session, rate=rate)


@pytest.mark.parametrize("rate", ["40.01", "41", "100", "-0.01"])
def test_gst_rate_outside_zero_to_forty_rejected(db_session, rate):
    with violates(db_session, pge.CheckViolation, "ck_products_gst_rate"):
        _product(db_session, rate=rate)


def test_gst_rate_is_required(db_session):
    with violates(db_session, pge.NotNullViolation):
        run(db_session, "INSERT INTO products (name, hsn_code) VALUES ('Murukku', '1905')")


@pytest.mark.parametrize("hsn", ["1905", "190590", "19059090"])
def test_hsn_of_four_six_or_eight_digits_accepted(db_session, hsn):
    _product(db_session, hsn=hsn)


@pytest.mark.parametrize("hsn", ["190", "19059", "1905909", "190590901", "ABCD", "19 5"])
def test_malformed_hsn_rejected(db_session, hsn):
    with violates(db_session, pge.CheckViolation, "ck_products_hsn_code"):
        _product(db_session, hsn=hsn)


def test_product_name_is_unique(db_session):
    _product(db_session)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "products_name_key"):
        _product(db_session)


# ---------------------------------------------------------------- items
def _item(session, code="FLOUR", kind="raw", unit="kg", product_id=None, pack_size=None, per_carton=None,
          price=None, name="Item"):
    run(session,
        "INSERT INTO items (code, name, kind, base_unit, product_id, pack_size_g, packets_per_carton, list_price) "
        "VALUES (:c, :n, :k, CAST(:u AS unit_code), :p, :g, :pc, :lp)",
        c=code, n=name, k=kind, u=unit, p=product_id, g=pack_size, pc=per_carton, lp=price)


def _finished(session, product_id, **overrides):
    values = dict(code="MUR-200", kind="finished", unit="pkt", product_id=product_id,
                  pack_size=200, per_carton=20, price="50.00")
    values.update(overrides)
    _item(session, **values)


@pytest.fixture
def product_id(db_session):
    _product(db_session)
    return scalar(db_session, "SELECT id FROM products")


def test_benchmark_masters_insert_cleanly(db_session, product_id):
    _item(db_session, "FLOUR", "raw", "kg")
    _item(db_session, "OIL", "raw", "l")
    _item(db_session, "POUCH", "packaging", "pc")
    _finished(db_session, product_id)
    db_session.commit()
    assert scalar(db_session, "SELECT count(*) FROM items") == 4


def test_item_kind_must_be_documented(db_session):
    with violates(db_session, pge.CheckViolation, "ck_items_kind"):
        _item(db_session, kind="service")


def test_item_finished_requires_product_and_price(db_session, product_id):
    with violates(db_session, pge.CheckViolation, "ck_items_finished_has_product"):
        _finished(db_session, product_id=None)
    with violates(db_session, pge.CheckViolation, "ck_items_finished_pkt_and_price"):
        _finished(db_session, product_id, price=None)


def test_item_raw_rejects_product(db_session, product_id):
    with violates(db_session, pge.CheckViolation, "ck_items_finished_has_product"):
        _item(db_session, kind="raw", product_id=product_id)


def test_finished_item_must_be_packets(db_session, product_id):
    with violates(db_session, pge.CheckViolation, "ck_items_finished_pkt_and_price"):
        _finished(db_session, product_id, unit="kg")


@pytest.mark.parametrize("field", [dict(pack_size=200), dict(per_carton=20), dict(price="1.00")])
def test_raw_and_packaging_items_carry_no_sales_fields(db_session, field):
    with violates(db_session, pge.CheckViolation, "ck_items_nonfinished_no_sales_fields"):
        _item(db_session, kind="packaging", unit="pc", **field)


def test_packets_per_carton_must_be_positive(db_session, product_id):
    with violates(db_session, pge.CheckViolation, "ck_items_packets_per_carton_positive"):
        _finished(db_session, product_id, per_carton=0)


def test_pack_size_must_be_positive(db_session, product_id):
    with violates(db_session, pge.CheckViolation, "ck_items_pack_size_positive"):
        _finished(db_session, product_id, pack_size=0)


def test_list_price_cannot_be_negative(db_session, product_id):
    with violates(db_session, pge.CheckViolation, "ck_items_list_price_nonnegative"):
        _finished(db_session, product_id, price="-1")


def test_list_price_cannot_be_nan(db_session, product_id):
    with violates(db_session, pge.CheckViolation, "ck_items_list_price_nonnegative"):
        _finished(db_session, product_id, price="NaN")


def test_item_code_is_unique(db_session):
    _item(db_session, "FLOUR")
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "items_code_key"):
        _item(db_session, "FLOUR")


def test_item_code_and_name_must_not_be_blank(db_session):
    with violates(db_session, pge.CheckViolation, "ck_items_code_not_blank"):
        _item(db_session, code=" ")
    with violates(db_session, pge.CheckViolation, "ck_items_name_not_blank"):
        _item(db_session, name="")


def test_item_product_must_exist(db_session):
    with violates(db_session, pge.ForeignKeyViolation):
        _finished(db_session, product_id=999)


def test_unit_must_be_a_known_code(db_session):
    with violates(db_session, pge.InvalidTextRepresentation):
        _item(db_session, unit="lb")


def test_new_item_is_active_by_default(db_session):
    _item(db_session)
    assert scalar(db_session, "SELECT is_active FROM items") is True


# ---------------------------------------------------------------- typed composite keys for later migrations
def _unique_column_sets(session, table):
    rows = run(session, """
        SELECT array_agg(a.attname ORDER BY k.ord) AS cols
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
        WHERE c.relname = :t AND i.indisunique AND i.indpred IS NULL
        GROUP BY i.indexrelid""", t=table).all()
    return {tuple(r.cols) for r in rows}


def test_items_expose_the_typed_key_targets(db_session):
    keys = _unique_column_sets(db_session, "items")
    assert {("id", "kind"), ("id", "base_unit"), ("id", "product_id")} <= keys


@pytest.mark.parametrize("table", ["customers", "suppliers"])
def test_party_masters_expose_id_as_a_foreign_key_target(db_session, table):
    assert ("id",) in _unique_column_sets(db_session, table)


def test_a_witness_column_makes_the_database_reject_the_wrong_item_kind(db_session):
    """The pattern later migrations use (design 4.0): a recipe line may only point at a raw item.

    The probe tables are ordinary tables created inside the test transaction and rolled back with it
    (a TEMP table may not reference a permanent one).
    """
    _item(db_session, "FLOUR", "raw", "kg")
    _item(db_session, "POUCH", "packaging", "pc")
    run(db_session, """CREATE TABLE recipe_lines_probe (
        id serial PRIMARY KEY, item_id bigint NOT NULL,
        item_kind text NOT NULL DEFAULT 'raw' CHECK (item_kind = 'raw'),
        FOREIGN KEY (item_id, item_kind) REFERENCES items (id, kind))""")
    flour = scalar(db_session, "SELECT id FROM items WHERE code='FLOUR'")
    pouch = scalar(db_session, "SELECT id FROM items WHERE code='POUCH'")
    run(db_session, "INSERT INTO recipe_lines_probe (item_id) VALUES (:i)", i=flour)
    with violates(db_session, pge.ForeignKeyViolation):
        run(db_session, "INSERT INTO recipe_lines_probe (item_id) VALUES (:i)", i=pouch)


def test_a_base_unit_witness_ties_a_line_to_the_items_real_unit(db_session):
    _item(db_session, "FLOUR", "raw", "kg")
    run(db_session, """CREATE TABLE unit_probe (
        id serial PRIMARY KEY, item_id bigint NOT NULL, base_unit unit_code NOT NULL,
        FOREIGN KEY (item_id, base_unit) REFERENCES items (id, base_unit))""")
    flour = scalar(db_session, "SELECT id FROM items")
    run(db_session, "INSERT INTO unit_probe (item_id, base_unit) VALUES (:i, 'kg')", i=flour)
    with violates(db_session, pge.ForeignKeyViolation):
        run(db_session, "INSERT INTO unit_probe (item_id, base_unit) VALUES (:i, 'l')", i=flour)


def test_a_product_witness_ties_a_sku_to_its_product(db_session, product_id):
    run(db_session, "INSERT INTO products (name, hsn_code, gst_rate) VALUES ('Mixture', '1905', 5)")
    other = scalar(db_session, "SELECT id FROM products WHERE name='Mixture'")
    _finished(db_session, product_id)
    sku = scalar(db_session, "SELECT id FROM items")
    run(db_session, """CREATE TABLE bom_probe (
        id serial PRIMARY KEY, sku_item_id bigint NOT NULL, product_id bigint NOT NULL,
        FOREIGN KEY (sku_item_id, product_id) REFERENCES items (id, product_id))""")
    run(db_session, "INSERT INTO bom_probe (sku_item_id, product_id) VALUES (:s, :p)", s=sku, p=product_id)
    with violates(db_session, pge.ForeignKeyViolation):
        run(db_session, "INSERT INTO bom_probe (sku_item_id, product_id) VALUES (:s, :p)", s=sku, p=other)


# ---------------------------------------------------------------- customers and suppliers
def _party(session, table="customers", name="Retailer A", gstin=GSTIN_TN, state="33", ctype="retailer"):
    if table == "customers":
        run(session, "INSERT INTO customers (name, customer_type, gstin, state_code) "
                     "VALUES (:n, :t, :g, :s)", n=name, t=ctype, g=gstin, s=state)
    else:
        run(session, f"INSERT INTO {table} (name, gstin, state_code) VALUES (:n, :g, :s)", n=name, g=gstin, s=state)


@pytest.mark.parametrize("table", ["customers", "suppliers"])
class TestPartyRules:
    def test_valid_party_with_matching_gstin_accepted(self, db_session, table):
        _party(db_session, table)
        _party(db_session, table, "Other", GSTIN_KA, "29")

    def test_gstin_state_mismatch_rejected(self, db_session, table):
        # GSTIN prefix 29 (Karnataka) but the party is recorded in Tamil Nadu (33)
        with violates(db_session, pge.CheckViolation, f"ck_{table}_gstin_state"):
            _party(db_session, table, gstin=GSTIN_KA, state="33")

    @pytest.mark.parametrize("gstin", [
        "33aabcr1234f1z5",     # lowercase
        "33AABCR1234F1Z",      # 14 characters
        "33AABC11234F1Z5",     # digit where a PAN letter belongs
        "33AABCRA234F1Z5",     # letter where a PAN digit belongs
        "33AABCR12345FZ5",     # five digits where four digits and a letter belong
        "33AABCR1234F0Z5",     # entity number 0 is not allowed
        "33AABCR1234F1X5",     # 14th character must be Z
        "33AABCR1234F1Z-",     # symbol as check character
    ])
    def test_malformed_gstin_rejected(self, db_session, table, gstin):
        with violates(db_session, pge.CheckViolation, f"ck_{table}_gstin_format"):
            _party(db_session, table, gstin=gstin)

    def test_gstin_longer_than_fifteen_characters_rejected(self, db_session, table):
        with violates(db_session, pge.StringDataRightTruncation):
            _party(db_session, table, gstin="33AABCR1234F1Z55")

    def test_duplicate_gstin_rejected(self, db_session, table):
        _party(db_session, table, "First")
        db_session.commit()
        with violates(db_session, pge.UniqueViolation, f"uq_{table}_gstin"):
            _party(db_session, table, "Second")

    def test_unregistered_parties_may_share_a_missing_gstin(self, db_session, table):
        _party(db_session, table, "Walk-in 1", gstin=None)
        _party(db_session, table, "Walk-in 2", gstin=None)
        db_session.commit()
        assert scalar(db_session, f"SELECT count(*) FROM {table}") == 2

    def test_unregistered_party_still_needs_a_valid_state(self, db_session, table):
        with violates(db_session, pge.CheckViolation, f"ck_{table}_state_code"):
            _party(db_session, table, gstin=None, state="99")

    def test_state_code_is_required(self, db_session, table):
        with violates(db_session, pge.NotNullViolation):
            _party(db_session, table, gstin=None, state=None)

    def test_name_must_not_be_blank(self, db_session, table):
        with violates(db_session, pge.CheckViolation, f"ck_{table}_name_not_blank"):
            _party(db_session, table, name="  ")


def test_customer_type_must_be_retailer_or_distributor(db_session):
    with violates(db_session, pge.CheckViolation, "ck_customers_type"):
        _party(db_session, ctype="wholesaler")


@pytest.mark.parametrize("ctype", ["retailer", "distributor"])
def test_both_customer_types_accepted(db_session, ctype):
    _party(db_session, ctype=ctype)
