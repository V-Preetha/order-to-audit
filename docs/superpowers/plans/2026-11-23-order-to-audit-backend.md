# Order-to-Audit System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a ledger-based order-to-audit system for a small snack manufacturer that reproduces the nine-step benchmark from the NIT Trichy problem statement, using real business logic and database transactions.

**Architecture:** A modular monolith. Pure domain functions (units, GST, costing, variance) are wrapped by transactional services. `StockService` is the single writer of the append-only stock ledger, serialised by item-row locks. FastAPI routers sit on top. PostgreSQL enforces what it can declaratively; services enforce cross-table rules; every rule has a named test.

**Tech Stack:** Python 3.12 in Docker (code supports 3.10 and later), FastAPI, SQLAlchemy 2.0 (sync), psycopg 3, Alembic, PostgreSQL 16, pytest, Pydantic v2, WeasyPrint (invoice PDF), openpyxl (Excel), React + TypeScript + Tailwind (frontend, gated by the design discussion), Docker Compose.

**Spec:** `docs/design/01-database-design.md` (the plan argues from it; read both). Section numbers below (§) refer to that document.

## Global Constraints

- Money is `Decimal` in Python and `NUMERIC` in PostgreSQL. Floats are never used. Decimals cross the API as strings.
- Invoice totals keep paise precision. Rounding to whole rupees happens only when `settings.invoice_round_to_rupee` is explicitly enabled (§15, assumption 5).
- Precision: money `NUMERIC(14,2)`, quantity `NUMERIC(14,3)`, unit cost `NUMERIC(14,4)`, percent `NUMERIC(5,2)`.
- GST rate range: 0 to 40 percent inclusive, at most two decimal places (`products.gst_rate CHECK (0..40)` and `domain.gst`; §15, assumption 17). Range validation only: no product-specific slab classification and no government GST integration.
- PostgreSQL 16 in Docker (14 or later supported); isolation level stays `READ COMMITTED`; lock order is document row first, then item rows in ascending id (§6).
- Only `StockService.post()` / `reverse()` may insert into `stock_movements`; only `StockService` may touch `stock_reservations` (§6).
- The ledger and `audit_log` are append-only. Corrections are forward-only transactions (§9). Completed production runs are never voided or edited.
- Benchmark outputs are never hard-coded. They must emerge from services replaying the section 5 data (§13).
- Tests that exercise constraint triggers must use real commits. Deferred triggers fire only at `COMMIT`, so the harness truncates tables between tests and never wraps a test in a rolled-back transaction.
- **Test database isolation.** Tests read only `TEST_DATABASE_ADMIN_URL` (never `DATABASE_URL`). The harness creates its own `o2a_test_<random>` database on that server, writes a marker table, runs `alembic upgrade head` there and drops it afterwards. It aborts the whole run if the URL is missing, the host is not local, the server is the development database server, or the database to be truncated lacks the marker. No code path truncates anything before `assert_disposable()` passes.
- **Real PostgreSQL only for critical flows.** Ledger, reservation, production, invoicing and payment tests run against a real PostgreSQL with real commits, real constraints and triggers, and separate connections for concurrency. Mocks, SQLite and in-memory fakes are not allowed for them; only external edges (clock, PDF fonts) may be faked.
- **Production idempotency.** `start_run`, `issue_more` and `complete_run` take a `request_id`: same id + same payload replays the original result; a different request on a completed run fails; a reused id with a different payload is rejected (§7).
- **Zero-output completion** is a restricted, admin-only, documented exception requiring a written reason (§9). It is never the standard correction.
- Git: the user stages, commits and pushes. Each task ends with a **commit checkpoint** (files + message) and execution stops there until the user has committed.
- No frontend code until the design-direction discussion is complete and the user approves it. After approval, frontend slices are integrated incrementally with their backend slices (see *Execution sequence*), not left to the end.

## Review Focus

Failure modes the spec implies but a straight reading of the tasks would not test. Each has a pinning test in the named task.

1. **Retried requests.** A double-tapped or retried submit (same `client_uuid` / `request_id`) must not duplicate an order, run posting or payment. Tests: Tasks 17, 18, 20.
2. **Concurrent approvals.** Two admins approving orders for the same last packets must not oversell; the second goes on hold. Tests: Tasks 13, 18.
3. **Unit mistakes.** 250 g entered for a kg item converts exactly to 0.250 kg; litres for a kg item is rejected with a clear message. Tests: Tasks 2, 16.
4. **Financial-year boundary in IST.** An invoice at 00:30 IST on 1 April belongs to the new FY even though UTC is still 31 March; numbering restarts at 0001. Tests: Tasks 3, 19.
5. **Data scoping.** A salesman must never see another salesman's orders, and an auditor must be unable to write. Tests: Tasks 14, 24.

## File Structure

```
docker-compose.yml                      # db (postgres:16) + api; one command to run
backend/
  pyproject.toml  alembic.ini
  alembic/versions/                     # 0000 baseline (Task 1), then 0001 … 0008, one per task below
  app/
    main.py config.py db.py             # FastAPI app, settings, engine/session, tx helper
    models/                             # SQLAlchemy models, one file per area:
      identity.py masters.py bom.py purchasing.py inventory.py sales.py receivables.py production.py audit.py
    domain/                             # pure functions, no DB, no FastAPI
      units.py gst.py costing.py variance.py
    services/                           # transactional use cases
      stock.py audit.py auth.py masters.py purchasing.py production.py sales.py invoicing.py
      receivables.py costing.py reports.py exports.py
    api/                                # routers: auth, masters, purchasing, production, sales, receivables, reports
    seed.py                             # benchmark master data + opening balances via StockService
  tests/
    conftest.py safety.py               # disposable-DB fixtures and the unsafe-configuration guards
    factories.py                        # test data builders (added from Task 12)
    domain/ db/ services/ api/ acceptance/
frontend/                               # created only after design approval (Task 25, incremental)
docs/design/ docs/superpowers/plans/
```

---

## Execution sequence with incremental frontend integration

Backend foundations come first. Once the user approves the UI design direction, the frontend is built slice by slice alongside the backend and is **never left to the end**.

| Phase | Tasks | Notes |
|---|---|---|
| A. Foundations | 1–15 (harness, domain, migrations, stock ledger, auth, masters, seed) | Backend only. The design-direction discussion runs in parallel and writes no code |
| **Gate G** | User approves the visual direction | Then write `docs/design/02-frontend-design-direction.md` and a frontend plan `docs/superpowers/plans/<date>-frontend.md`. If Gate G is late, backend slices continue and UI slices queue behind them |
| B1. Access and masters | 24a (auth + masters routers) → F0 (frontend scaffold, design tokens, login and role shell) → F1 (masters screens) | First end-to-end vertical slice |
| B2. Purchasing and production | 16, 17, 24b → F2 (production-floor screens: receipt, start run, complete run with variance) | Highest evaluation weight |
| B3. Sales | 18, 19, 24c → F3 (salesman mobile order entry and tracking) and F4 (admin approval queue, invoice, dispatch, PDF) | Usability criterion: order in under a minute |
| B4. Money | 20, 21, 22 (benchmark acceptance), 24d → F5 (payments, outstanding, run cost) | Acceptance test runs here, at the service level |
| B5. Reporting | 23, 24e → F6 (auditor reports and exports, owner dashboard) | |
| C. Delivery | 24 cross-cutting access and API benchmark tests, 26 documentation | Demo video follows the nine steps through the real UI |

**Rule:** after Gate G, a slice's UI is built before the next slice's backend begins, unless the user waives it. A backend task is complete when its tests pass, regardless of UI.

### Task 1: Scaffold and safe test harness

**Files:**
- Create: `.gitignore`, `.env.example`, `docker-compose.yml`, `backend/Dockerfile`, `backend/pyproject.toml`, `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/script.py.mako`, `backend/alembic/versions/0000_baseline.py`, `backend/app/{__init__,main,config,db}.py`, `backend/tests/{__init__,conftest,safety}.py`, `backend/tests/{test_health,test_safety_guard,test_pg_harness}.py`

**Interfaces:**
- Produces: `app.config.Settings` (`database_url`, `jwt_secret`, `app_env`); `app.db.make_engine(url: str) -> Engine`; `app.db.get_session() -> Iterator[Session]`; `app.db.transaction(session: Session) -> ContextManager` (commit on success, rollback on error).
- Produces (tests): `tests.safety.UnsafeTestDatabase(Exception)`; `tests.safety.assert_safe_admin_url(admin_url: str | None, *, dev_url: str | None) -> None` (pure); `tests.safety.assert_disposable(conn: Connection) -> None` (name starts with `o2a_test_` and marker table `_disposable_test_db` present); fixtures `pg_admin_url`, `engine` (session scope: creates `o2a_test_<hex>`, marker, `alembic upgrade head`, drops with `FORCE` at the end), `db_session`, `connect()` (factory for independent connections, used by concurrency tests), autouse `clean_db` (calls `assert_disposable`, then `TRUNCATE` every public table except `alembic_version`, `status_transitions` and the marker, `RESTART IDENTITY CASCADE`).

- [ ] **Step 1: Check prerequisites** on the user's machine (Python, Docker and Compose, PostgreSQL, Node, git) and list anything missing with install commands. Install nothing without approval.
- [ ] **Step 2: Write failing tests** — `test_health`: `test_health_returns_ok` (`GET /health` → 200, `{"status": "ok"}`). `test_safety_guard` (pure): `test_missing_admin_url_aborts`; `test_remote_host_rejected` (`db.example.com`); `test_admin_server_equal_to_dev_server_rejected`; `test_non_maintenance_database_rejected` (`/order_to_audit`); `test_localhost_postgres_admin_url_accepted`; `test_disposable_check_requires_marker_and_name_prefix` (needs the server). `test_pg_harness` (real PostgreSQL): `test_server_is_real_postgres` (`server_version_num >= 140000`); `test_committed_row_visible_to_second_connection`; `test_uncommitted_row_invisible_to_second_connection`; `test_row_lock_blocks_second_connection` (first connection holds `FOR UPDATE`, second with `lock_timeout='200ms'` raises `LockNotAvailable`); `test_clean_db_truncates_between_tests` (two ordered tests: write, then find the table empty); `test_clean_db_refuses_unmarked_database` (a second database created without the marker: `assert_disposable` raises and a seeded row survives).
- [ ] **Step 3: Run** `cd backend && pytest -v` → FAIL (modules missing; with no `TEST_DATABASE_ADMIN_URL` the DB tests abort rather than run).
- [ ] **Step 4: Implement** the files above. Compose has `db` (development, volume, port 5432), `db-test` (disposable, `tmpfs`, port 5433) and `api`. `0000_baseline.py` is a no-op revision that only establishes `alembic_version`.
- [ ] **Step 5: Run** the same command against a real PostgreSQL via `TEST_DATABASE_ADMIN_URL` → PASS. Also confirm the guard: run once with the variable unset and once pointing at a non-local host; both must abort without connecting.
- [ ] **Step 6: Commit checkpoint** — `chore: scaffold backend, compose and isolated PostgreSQL test harness`.

### Task 2: Domain — unit conversion

**Files:** Create `backend/app/domain/units.py`, `backend/tests/domain/test_units.py`

**Interfaces:**
- Produces: `class IncompatibleUnits(ValueError)`; `convert(qty: Decimal, from_unit: str, to_unit: str) -> Decimal` (rounds half-up to 3 dp); `split_cartons(packets: Decimal, packets_per_carton: int) -> tuple[int, int]` (full cartons, loose packets); `UNIT_FACTORS: dict[tuple[str, str], Decimal]` (used by the SQL parity test in Task 6).

- [ ] **Step 1: Write failing tests** — `test_g_to_kg_is_exact`: `convert(Decimal("250"), "g", "kg") == Decimal("0.250")`; `test_kg_to_g`: `convert(Decimal("1.2"), "kg", "g") == Decimal("1200.000")`; `test_litre_to_ml`; `test_same_unit_identity`; `test_litre_for_kg_rejected`: `pytest.raises(IncompatibleUnits, match="litre")`; `test_split_cartons`: `split_cartons(Decimal("188"), 20) == (9, 8)`; `test_split_cartons_exact`: `(200, 20) → (10, 0)`.
- [ ] **Step 2: Run** `pytest tests/domain/test_units.py -v` → FAIL.
- [ ] **Step 3: Implement** with a factor table matching §4.0 (`kg↔g`, `l↔ml`, identity for `pc`, `pkt`); quantise with `ROUND_HALF_UP`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(domain): unit conversion`.

### Task 3: Domain — GST, financial year, invoice numbers

**Files:** Create `backend/app/domain/gst.py`, `backend/tests/domain/test_gst.py`

**Interfaces:**
- Produces: `SupplyType = Literal["intra","inter"]`; `@dataclass(frozen=True) TaxSplit(cgst, sgst, igst: Decimal)`; `supply_type(seller_state: str, place_of_supply: str) -> SupplyType`; `split_tax(taxable: Decimal, rate_pct: Decimal, supply: SupplyType) -> TaxSplit`; `financial_year(d: date) -> str`; `to_ist_date(ts: datetime) -> date`; `format_invoice_no(fy: str, seq: int) -> str`; `round_grand_total(amount: Decimal, *, round_to_rupee: bool = False) -> tuple[Decimal, Decimal]` (grand total, round-off; the default keeps paise and returns round-off `0.00`).

- [ ] **Step 1: Write failing tests** — `test_intra_state_split`: `split_tax(Decimal("7500"), Decimal("5"), "intra") == TaxSplit(Decimal("187.50"), Decimal("187.50"), Decimal("0"))`; `test_inter_state_igst`: `split_tax(Decimal("6000"), Decimal("5"), "inter").igst == Decimal("300.00")`; `test_supply_type_tn_vs_ka`: `("33","33") → intra`, `("33","29") → inter`; `test_financial_year_boundaries`: `2027-03-31 → "2026-27"`, `2027-04-01 → "2027-28"`; `test_ist_conversion_crosses_midnight`: `to_ist_date(datetime(2027,3,31,19,0,tzinfo=UTC)) == date(2027,4,1)` (Review Focus 4); `test_invoice_number_format`: `format_invoice_no("2026-27", 1) == "INV/26-27/0001"` and `len(...) <= 16`; `test_default_keeps_paise`: `round_grand_total(Decimal("7875.00")) == (Decimal("7875.00"), Decimal("0.00"))` and `round_grand_total(Decimal("100.49")) == (Decimal("100.49"), Decimal("0.00"))`; `test_explicit_rupee_rounding`: `round_grand_total(Decimal("100.49"), round_to_rupee=True) == (Decimal("100.00"), Decimal("-0.49"))`; `test_per_component_rounding`: odd-paisa case rounds each component half-up; `test_rates_from_zero_through_forty_accepted` (0, 28.01, 40 accepted) and `test_rate_outside_zero_to_40_rejected` (40.01 rejected); the SQL parity test covers every rate from 0.00 to 40.00.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** per §4.6/§15 (per-component rounding; paise kept unless `round_to_rupee`). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(domain): GST split, financial year, invoice numbering`.

### Task 4: Domain — costing

**Files:** Create `backend/app/domain/costing.py`, `backend/tests/domain/test_costing.py`

**Interfaces:**
- Produces: `wac_issue_value(qty: Decimal, on_hand_qty: Decimal, on_hand_value: Decimal) -> Decimal` (positive qty issued; ROUND_HALF_UP to 2 dp; if `qty == on_hand_qty` returns `on_hand_value`); `@dataclass(frozen=True) RunCost(materials, overheads, total, per_packet: Decimal)`; `run_cost(materials: Decimal, overheads: Decimal, good_packets: Decimal) -> RunCost` (`per_packet` quantised to 4 dp; zero packets raises `ZeroOutput`); `allocate_shared(pool: Decimal, weights: Mapping[int, Decimal]) -> dict[int, Decimal]` (largest remainder, sums exactly to the pool).

- [ ] **Step 1: Write failing tests** — `test_wac_flour_issue`: on-hand 160 kg / ₹6,900, issue 40 → `Decimal("1725.00")`; `test_issue_that_empties_stock_takes_remaining_value`; `test_wac_rounding_no_residue` (three issues of an awkward average sum back to the pool); `test_benchmark_run_cost`: `run_cost(Decimal("4055.00"), Decimal("1800.00"), Decimal("188")).per_packet == Decimal("31.1436")`; `test_zero_output_raises`; `test_allocate_shared_sums_to_pool`: `allocate_shared(Decimal("100.00"), {1: Decimal("1"), 2: Decimal("1"), 3: Decimal("1")})` sums to `100.00`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(domain): weighted-average issue value, run cost, shared overhead allocation`.

### Task 5: Domain — variance numeric layer and wording

**Files:** Create `backend/app/domain/variance.py`, `backend/tests/domain/test_variance.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) Variance(variance_qty, variance_pct: Decimal, deviation: Literal["over","under","on_standard"], exceeds_tolerance: bool)`; `compute_variance(expected: Decimal, actual: Decimal, tolerance_pct: Decimal) -> Variance`; `describe_variance(kind: Literal["yield","raw","packaging"], v: Variance, *, scrap_qty: Decimal = Decimal(0), uncartoned_packets: int = 0) -> str`.

- [ ] **Step 1: Write failing tests** — yield `compute_variance(200, 188, 3)` → pct `-6.00`, `under`, exceeds; oil `(10, 10.5, 3)` → `+5.00`, `over`, exceeds; pouches `(200, 194, 3)` → `-3.00`, **not** exceeding (strict); cartons `(10, 9, 3)` → `-10.00`, exceeds; flour `(40, 40, 3)` → `on_standard`; `test_expected_zero_is_unplanned`: expected 0 with actual > 0 → exceeds, pct is `None`-safe (documented sentinel); `test_carton_wording_not_favourable`: `describe_variance("packaging", v, uncartoned_packets=8)` contains `"8 packets uncartoned"` and does not contain `"favourable"` or `"unfavourable"`; `test_yield_wording`: contains `"below expected"`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** per §10. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(domain): variance numeric layer and neutral wording`.

### Task 6: Migration 0001 — identity, settings, masters, shared DB functions

**Files:** Create `backend/alembic/versions/0001_identity_masters.py`, `backend/app/models/{identity,masters}.py`, `backend/tests/db/test_masters_constraints.py`, `backend/tests/db/test_unit_factor.py`

**Interfaces:**
- Produces: tables `users`, `settings`, `document_sequences`, `request_log`, `status_transitions` (seeded with every edge in §8), `products`, `items`, `customers`, `suppliers`; type `unit_code`; functions `unit_factor(unit_code, unit_code)`, `forbid_mutation()`, `enforce_status_transition()` (reads `status_transitions`; attached to workflow tables in later migrations), `guard_posted_row()`. Models `User, Settings, RequestLog, Product, Item, Customer, Supplier`.

- [ ] **Step 1: Write failing tests** (raw SQL via `db_session`) — `test_item_finished_requires_product_and_price`; `test_item_raw_rejects_product`; `test_finished_item_must_be_packets`; `test_customer_gstin_state_mismatch_rejected` (GSTIN prefix `29`, `state_code='33'`); `test_customer_bad_gstin_format_rejected`; `test_settings_single_row`; `test_username_must_be_lowercase`; `test_sql_and_python_unit_factors_agree`: for every pair in `UNIT_FACTORS`, `SELECT unit_factor(f,t)` equals the Python factor, and `unit_factor('kg','l') IS NULL`; `test_status_transitions_seeded`: spot-check `('orders','on_hold','approved')`, `('production_runs','in_progress','completed')`, and that no `('production_runs','completed', *)` row exists; `test_settings_invoice_rounding_defaults_off`; `test_request_log_request_id_is_unique`.
- [ ] **Step 2: Run** `pytest tests/db -v` → FAIL.
- [ ] **Step 3: Implement** migration per §4.0–4.2 including the `UNIQUE (id, kind)`, `(id, base_unit)`, `(id, product_id)` helper constraints, then the models.
- [ ] **Step 4: Run** → PASS. **Step 5: Commit checkpoint** — `feat(db): identity, settings, masters, status transitions`.

### Task 7: Migration 0002 — recipes and BOMs

**Files:** Create `backend/alembic/versions/0002_bom.py`, `backend/app/models/bom.py`, `backend/tests/db/test_bom_constraints.py`

**Interfaces:**
- Consumes: `items`, `products` from Task 6.
- Produces: tables `recipes`, `recipe_lines`, `boms`, `bom_packaging_lines`; models `Recipe, RecipeLine, Bom, BomPackagingLine`.

- [ ] **Step 1: Write failing tests** — `test_recipe_line_rejects_packaging_item`; `test_bom_packaging_rejects_raw_item`; `test_bom_sku_must_be_finished`; `test_bom_rejects_recipe_of_other_product` (SKU product ≠ recipe product → FK violation); `test_second_active_bom_for_sku_rejected`; `test_retired_bom_allows_new_active`; `test_second_pack_size_shares_recipe` (a 500 g SKU with its own BOM referencing the same recipe inserts with no schema change).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** per §4.3 typed composite FKs. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(db): recipes and BOMs with typed foreign keys`.

### Task 8: Migration 0003 — orders, invoices, credit notes

**Files:** Create `backend/alembic/versions/0003_sales_documents.py`, `backend/app/models/sales.py`, `backend/tests/db/test_sales_constraints.py`

**Interfaces:**
- Produces: tables `orders`, `order_lines`, `invoices`, `invoice_lines`, `credit_notes`; models `Order, OrderLine, Invoice, InvoiceLine, CreditNote`; triggers `enforce_status_transition` on `orders`, `invoices`; `guard_posted_row` on `invoices`; `forbid_mutation` on `invoice_lines`, `credit_notes`.

- [ ] **Step 1: Write failing tests** — `test_order_line_rejects_raw_item`; `test_duplicate_client_uuid_rejected`; `test_invoice_intra_requires_cgst_equals_sgst_and_no_igst`; `test_invoice_inter_requires_igst_only`; `test_invoice_supply_type_must_match_state_codes`; `test_invoice_total_must_equal_components`; `test_invoice_line_tax_math_exact`: line `taxable 7500.00, rate 5, cgst 187.50, sgst 187.50` accepted and `cgst 187.00` rejected; `test_invoice_customer_must_match_order_customer`; `test_invoice_number_unique_per_fy`; `test_invoice_no_longer_than_16_chars`; `test_posted_invoice_amounts_immutable`; `test_invoice_cannot_be_deleted`; `test_issued_to_cancelled_allowed_and_back_rejected`; `test_one_credit_note_per_invoice`; `test_order_illegal_transition_rejected` (`submitted → dispatched`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** per §4.6 and §5.1. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(db): orders, invoices and credit notes with tax CHECKs`.

### Task 9: Migration 0004 — production runs, run lines, overheads

**Files:** Create `backend/alembic/versions/0004_production.py`, `backend/app/models/production.py`, `backend/tests/db/test_production_constraints.py`

**Interfaces:**
- Produces: tables `production_runs`, `production_run_lines`, `production_overheads`; models `ProductionRun, ProductionRunLine, ProductionOverhead`; transition trigger and `guard_posted_row` on `production_runs`.

- [ ] **Step 1: Write failing tests** — `test_run_sku_must_match_bom`; `test_run_line_rejects_finished_item`; `test_completed_requires_output_and_timestamp`; `test_in_progress_with_output_rejected`; `test_completed_run_is_frozen` (any `UPDATE` rejected); `test_completed_to_in_progress_rejected`; `test_overhead_direct_xor_shared` (both set or both null rejected); `test_overhead_period_must_be_first_of_month`; `test_zero_output_completion_requires_completion_note` (CHECK rejects `completed` with output 0 and a blank note, accepts it with a note).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** per §4.8 (no void columns). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(db): production runs and overheads`.

### Task 10: Migration 0005 — purchasing and the stock ledger

**Files:** Create `backend/alembic/versions/0005_inventory.py`, `backend/app/models/{purchasing,inventory}.py`, `backend/tests/db/test_inventory_constraints.py`

**Interfaces:**
- Consumes: `order_lines`, `invoice_lines`, `production_runs` (Tasks 8–9).
- Produces: tables `purchases`, `purchase_lines`, `stock_adjustments`, `stock_movements`, `stock_reservations`; function `assert_stock_invariants()` as a deferred constraint trigger on `stock_movements` and `stock_reservations`; models `Purchase, PurchaseLine, StockAdjustment, StockMovement, StockReservation`.

- [ ] **Step 1: Write failing tests** (each commits for real; assertions on `IntegrityError`/`DBAPIError` raised at commit where deferred) — `test_purchase_line_wrong_qty_base_rejected` (250 g stored as `250` for a kg item); `test_purchase_line_incompatible_unit_rejected`; `test_duplicate_supplier_invoice_rejected`; `test_movement_requires_exactly_one_source`; `test_finished_movement_must_have_no_value`; `test_raw_movement_must_have_value`; `test_issue_must_be_negative_and_receipt_positive`; `test_movement_item_must_match_source_line_item`; `test_duplicate_posting_key_rejected`; `test_second_dispatch_for_line_rejected`; `test_movement_update_and_delete_rejected`; `test_reversal_is_unique` and `test_reversal_must_be_same_item`; `test_trigger_catches_unchecked_insert`: inserting a −10 movement on an empty item commits with error `negative stock`; `test_reservation_trigger_rejects_overcommit`: reserving 100 against on-hand 88 fails at commit; `test_reservation_unique_per_order_line`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** per §4.4–4.5, including the four partial unique indexes and the covering index. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(db): purchasing and append-only stock ledger with invariant triggers`.

### Task 11: Migration 0006 — receivables and audit log

**Files:** Create `backend/alembic/versions/0006_receivables_audit.py`, `backend/app/models/{receivables,audit}.py`, `backend/tests/db/test_receivables_constraints.py`

**Interfaces:**
- Produces: tables `payments`, `payment_allocations`, `audit_log`; deferred trigger `assert_allocation_limits()`; transition and posted-row guards on `payments`; models `Payment, PaymentAllocation, AuditLog`.

- [ ] **Step 1: Write failing tests** — `test_allocation_exceeding_payment_rejected` (allocating 4,001 of a 4,000 payment); `test_allocation_exceeding_invoice_rejected`; `test_allocation_other_customer_rejected`; `test_allocation_to_cancelled_invoice_rejected`; `test_reversed_payment_frees_invoice_capacity`; `test_duplicate_payment_client_uuid_rejected`; `test_payment_amount_must_be_positive`; `test_audit_log_is_append_only`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** per §4.7, §4.9, §5 rule 7. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(db): payments, allocations and audit log`.

### Obligations carried from the Task 10 review (binding on the tasks named; the database does not provide them)

The ledger migration (0005) enforces shape, sign, source agreement, one-shot postings, immutability and a commit-time balance check. It does NOT replace these service duties, and each task below must have a test for its item:

| Task | Obligation | Required test (name) |
|------|------------|----------------------|
| 12 / 13 | Lock `items` rows in ascending id with `SELECT ... FOR UPDATE` before reading any balance (design 6); post issue, scrap and count values from the single WAC function, and write the resolved WAC as `unit_cost` on every positive raw/packaging count | `test_items_are_locked_in_ascending_id_order`; `test_count_adjustment_carries_resolved_wac` |
| 16 | A purchase may be voided only if each receipt is the item's latest movement (design 9.6 rule R2); otherwise refuse with a clear error | `test_void_refused_when_later_movements_exist` |
| 18 (sales cancellation) | Cancelling or rejecting an order releases its active reservations in the same transaction | `test_cancelling_an_order_releases_its_reservations` |
| 19 (invoice cancellation) | Post a dispatch reversal exactly when the credit note's `restock` is true, and none when false | `test_restock_flag_decides_dispatch_reversal` |
| 14 / 20 (audit and payment services) | Write the `audit_log` entry in the SAME transaction as the audited change; lock the payment and invoice rows `FOR UPDATE` in ascending id order before allocating; reverse a payment (never delete) before cancelling an invoice that has allocations | `test_audit_entry_is_written_in_the_same_transaction`; `test_payment_service_locks_payment_then_invoice_in_id_order`; `test_invoice_cancel_requires_payment_reversal_first` |
| 17 (production service) | Completing a run posts the finished-goods output movement in the same transaction (a completed run without its output must be impossible) | `test_completing_a_run_posts_output_atomically` |

### Task 12 as built (rulings; the interface above is otherwise unchanged)

- `post(qty=...)` takes a **positive magnitude** for every movement type; the service applies the sign (issue, scrap and dispatch reduce stock). Only `count_adjustment` takes a signed quantity.
- `lock_items` uses `FOR NO KEY UPDATE` (not `FOR UPDATE`): it still excludes every other stock writer, including the commit-time triggers, but does not block unrelated foreign-key checks on the item.
- A repeated posting key is checked **before** balances, so replaying an issue after the stock has run out returns the original movement instead of failing.
- Extra exceptions: `CostRequired`, `InvalidPosting`, `UnknownItem`; `InsufficientStock` is the one class from `domain.costing`. Quantities with more than 3 decimals, non-finite values, and fractional finished goods are refused before any SQL runs.
- `tests/factories.py` was not created: the existing builders in `tests/db` already provide items and users.
- A purchase receipt takes its quantity and **value from the purchase line** (its amount is in the purchase unit, and no 4-decimal cost can reproduce it for, say, 1 kg bought at 42.37 for an item counted in grams). `unit_cost` is optional for receipts and, if given, must reproduce the line amount.
- Movement type must suit the item kind, a source can be posted once (a second posting under another key is a `PostingMismatch`), values beyond NUMERIC(14,2) and non-integer ids or non-date dates are refused in the service, and posting requires READ COMMITTED.
- Task 13 additions: `reserve(..., user_id)` (the ledger needs `created_by`); a **dispatch consumes its own order's reservation inside `post`** (it may count it as free, so it must also close it); `post` refuses reductions of finished goods that would eat into other orders' reservations (`InsufficientStock`); `reverse` always applies the "latest movement" rule to purchase receipts, whatever `require_latest` says.
- **Still the services' job (database backs it up at COMMIT):** the service does not re-read the source document (adjustment qty/cost, invoice status, run state, order-line qty). Tasks 16–19 create those rows in the same transaction with matching values; a mismatch fails at commit with a database error rather than a domain error. `reverse()` has no ON CONFLICT handling for the same key used concurrently on another item, and its date is the server's `current_date`.

### Task 12: StockService — core posting, balances, WAC, idempotency

**Files:** Create `backend/app/services/stock.py`, `backend/app/services/idempotency.py`, `backend/tests/services/test_stock_core.py`, `backend/tests/services/test_idempotency.py`, `backend/tests/factories.py` (`make_item`, `make_user`)

**Interfaces:**
- Consumes: Task 4 `wac_issue_value`; models from Tasks 6–11.
- Produces: `class InsufficientStock(Exception)`, `class PostingMismatch(Exception)`; `@dataclass(frozen=True) Source(kind: Literal["purchase_line","production_run","invoice_line","adjustment"], id: int)`; `lock_items(s: Session, item_ids: Iterable[int]) -> None`; `on_hand(s, item_id) -> Decimal`; `reserved(s, item_id) -> Decimal`; `available(s, item_id) -> Decimal`; `average_cost(s, item_id) -> Decimal | None`; `post(s, *, key: str, item_id: int, movement_type: str, qty: Decimal, source: Source, movement_date: date, user_id: int, unit_cost: Decimal | None = None, batch_no: str | None = None) -> StockMovement`.

- Also produces (`app/services/idempotency.py`): `class IdempotencyConflict(Exception)`; `fingerprint(action: str, payload: Mapping) -> str` (SHA-256 of canonical JSON: sorted keys, Decimals as strings, line lists sorted by `item_id`); `@dataclass(frozen=True) Replay(entity_type: str, entity_id: int)`; `begin_request(s: Session, *, request_id: UUID, action: str, payload: Mapping) -> Replay | None` (inserts into `request_log`; returns the stored result on an identical replay; raises `IdempotencyConflict` for another action or payload); `finish_request(s: Session, request_id: UUID, entity_type: str, entity_id: int) -> None`.

- [ ] **Step 1: Write failing tests** — `test_receipt_then_issue_values_at_wac`: receipt 100 kg @42 then 60 kg @45, issue 40 → movement `value == Decimal("-1725.00")` and `average_cost == Decimal("43.1250")`; `test_issue_over_on_hand_raises_insufficient_stock`; `test_finished_movement_has_no_value`; `test_posting_same_key_twice_returns_original_and_adds_no_row`; `test_replay_with_different_qty_raises_posting_mismatch`; `test_lock_items_locks_in_ascending_id_order` (inspect `pg_locks` or statement order via a recording session); `test_available_is_on_hand_minus_active_reservations`; `test_positive_count_adjustment_defaults_to_current_wac`; `test_positive_adjustment_on_empty_stock_requires_cost`. In `test_idempotency.py`: `test_first_call_returns_none`; `test_same_request_same_payload_returns_replay`; `test_same_request_different_payload_raises_conflict`; `test_request_id_reused_for_other_action_raises_conflict`; `test_failed_transaction_leaves_no_request_row` (commit-level, real PostgreSQL); `test_fingerprint_ignores_line_order_and_decimal_scale` (`Decimal("40")` and `Decimal("40.000")` hash equal).
- [ ] **Step 2: Run** `pytest tests/services/test_stock_core.py -v` → FAIL.
- [ ] **Step 3: Implement.** `post()` locks the item first, reads balances in a later statement (§6), derives `value` via `wac_issue_value` for valued items, and uses `INSERT … ON CONFLICT (posting_key) DO NOTHING RETURNING`; on conflict it compares the stored row with the request.
- [ ] **Step 4: Run** → PASS. **Step 5: Commit checkpoint** — `feat(stock): ledger posting, balances, WAC, idempotency`.

### Task 13: StockService — concurrency and reversal rules

**Files:** Modify `backend/app/services/stock.py`; Create `backend/tests/services/test_stock_concurrency.py`

**Interfaces:**
- Consumes: Task 12.
- Produces: `class ReversalNotAllowed(Exception)`; `reserve(s, *, order_line_id: int, item_id: int, qty: Decimal) -> StockReservation` (raises `InsufficientStock`); `release_reservation(s, reservation_id: int) -> None`; `fulfil_reservation(s, reservation_id: int) -> None`; `reverse(s, movement_id: int, *, key: str, user_id: int, require_latest: bool) -> StockMovement`.

- [ ] **Step 1: Write failing tests** (use separate sessions and `threading`) — `test_concurrent_reservations_never_oversell`: 8 threads each reserve 20 of an item with on-hand 88 → exactly 4 succeed, the rest raise `InsufficientStock`, and `reserved <= on_hand`; `test_concurrent_issues_never_go_negative`; `test_receipt_and_issue_serialise_for_wac`; `test_reverse_dispatch_restocks_without_value`; `test_reverse_purchase_allowed_when_latest`; `test_reverse_purchase_refused_when_later_movement_exists` (the flour purchase after the run issue); `test_reverse_twice_rejected`; `test_reverse_is_exact_mirror`; `test_adjustment_cannot_drop_below_reserved`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `reverse` with the R2 rule: `require_latest=True` checks `NOT EXISTS movement with id > original` for the item under lock (§9). **Step 4: Run** → PASS (repeat the concurrency tests 5 times to expose flakiness).
- [ ] **Step 5: Commit checkpoint** — `feat(stock): reservations, concurrency safety and restricted reversal`.

### Task 14: Audit service, authentication and RBAC

**Files:** Create `backend/app/services/{audit,auth}.py`, `backend/app/api/deps.py`, `backend/tests/services/test_audit.py`, `backend/tests/services/test_auth.py`

**Interfaces:**
- Produces: `audit.record(s, *, user_id: int | None, action: str, entity_type: str, entity_id: int, before: dict | None = None, after: dict | None = None, request_id: UUID | None = None) -> None`; `auth.hash_password(p: str) -> str`; `auth.verify_password(p: str, h: str) -> bool`; `auth.create_access_token(user: User) -> str`; `api.deps.current_user`; `api.deps.require_roles(*roles: str)`; `PERMISSIONS: dict[str, set[str]]` mapping role → allowed actions (`orders:create`, `orders:approve`, `invoices:create`, `dispatch:create`, `payments:create`, `purchases:create`, `runs:write`, `runs:complete_zero_output`, `masters:write`, `reports:read`, `dashboard:read`).

- [ ] **Step 1: Write failing tests** — `test_audit_row_written_in_same_transaction_and_rolled_back_with_it`; `test_audit_stores_before_after_json`; `test_password_hash_verifies_and_is_not_plaintext`; `test_login_rejects_inactive_user`; `test_permission_matrix`: salesman may `orders:create` only; admin may approve, invoice, dispatch, record payments, write masters and `runs:complete_zero_output`; production may write purchases and runs but **not** complete a run with zero output; auditor has only `reports:read`; owner only `dashboard:read` and `reports:read`; `test_require_roles_returns_403`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (JWT bearer for the API; roles from the user row). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(auth): audit service, authentication and role permissions`.

### Task 15: Master and BOM services, benchmark seed data

**Files:** Create `backend/app/services/masters.py`, `backend/app/seed.py`, `backend/tests/services/test_masters.py`, `backend/tests/services/test_seed.py`

**Interfaces:**
- Consumes: Tasks 12, 14.
- Produces: `create_item(s, ...) -> Item`, `create_customer(...)`, `create_supplier(...)`, `create_recipe(...)`, `create_bom(...)`, `activate_bom(s, bom_id: int, user_id: int) -> Bom` (requires ≥1 recipe line and ≥1 packaging line; retires the previous active BOM of the SKU); `seed.load_benchmark(s: Session) -> SeedResult` (dataclass of ids) — loads §13 step 0 master data and the six opening adjustments through `StockService.post`.

- [ ] **Step 1: Write failing tests** — `test_activate_bom_requires_lines`; `test_activate_bom_retires_previous`; `test_every_master_edit_writes_audit_row`; `test_seed_opening_balances`: after `load_benchmark`, `on_hand` is flour 100, oil 30, spice 5, pouches 500, cartons 30, Murukku 50 and flour `average_cost == Decimal("42.0000")`; `test_seed_is_rerunnable_on_empty_db`; `test_seed_creates_all_six_roles`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement**; the HSN code in the seed is a placeholder marked in a comment. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(masters): masters, BOM activation, benchmark seed`.

### Task 16: Purchasing service

**Files:** Create `backend/app/services/purchasing.py`, `backend/tests/services/test_purchasing.py`

**Interfaces:**
- Consumes: Tasks 12, 13, 14, 2.
- Produces: `@dataclass PurchaseLineIn(item_id: int, qty: Decimal, unit: str, rate: Decimal, gst_rate: Decimal = Decimal(0))`; `post_purchase(s, *, supplier_id: int, supplier_invoice_no: str, purchase_date: date, lines: list[PurchaseLineIn], user_id: int) -> Purchase`; `void_purchase(s, purchase_id: int, *, reason: str, user_id: int) -> Purchase` (cut-line: built last, uses `reverse(require_latest=True)`).

- [ ] **Step 1: Write failing tests** — `test_benchmark_step_1_flour_purchase`: 60 kg @ ₹45 → line `amount == Decimal("2700.00")`, `on_hand(flour) == 160`, `average_cost == Decimal("43.1250")`; `test_purchase_in_grams_converts`: `250 g` of flour posts `qty_base == Decimal("0.250")`; `test_litres_for_kg_item_rejected_with_message`; `test_duplicate_supplier_invoice_rejected`; `test_purchase_of_finished_item_rejected`; `test_purchase_writes_audit`; `test_void_purchase_allowed_when_nothing_followed`; `test_void_purchase_refused_after_issue`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (document number from `document_sequences`, scope `PUR:<fy>`). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(purchasing): purchases posting receipts to the ledger`.

### Task 17: Production service — start, issue, complete, variance view

**Files:** Create `backend/alembic/versions/0007_views.py`, `backend/app/services/production.py`, `backend/tests/services/test_production.py`, `backend/tests/db/test_views.py`

**Interfaces:**
- Consumes: Tasks 4, 5, 12, 15.
- Produces: views `v_stock_on_hand`, `v_stock_available`, `v_run_consumption` (columns per §10, including `cost_impact`, `scrap_qty`, `uncartoned_packets`); `@dataclass IssueLine(item_id: int, qty: Decimal, unit: str)`; `@dataclass ConsumeLine(item_id: int, qty: Decimal, unit: str, scrap_qty: Decimal = Decimal(0))`; `start_run(s, *, bom_id: int, planned_batches: Decimal, issues: list[IssueLine], request_id: UUID, user_id: int) -> ProductionRun` (takes `SELECT … FROM settings WHERE id = 1 FOR SHARE` before reading the tolerance, then locks items in ascending id; design §6.1); `issue_more(s, run_id: int, issues: list[IssueLine], request_id: UUID, user_id: int) -> None`; `complete_run(s, run_id: int, *, good_output: Decimal, consumption: list[ConsumeLine], request_id: UUID, user_id: int, completion_note: str | None = None) -> ProductionRun`; exceptions `RunAlreadyCompleted`, `ZeroOutputNotPermitted` (and `IdempotencyConflict` from Task 12). All three of `start_run`, `issue_more`, `complete_run` call `begin_request` first; `get_run_consumption(s, run_id: int) -> list[RunConsumptionRow]`.

- [ ] **Step 1: Write failing tests** (benchmark steps 2–3, values exact) — `test_start_run_locks_settings_row_for_share_before_reading_tolerance` (the settings row appears in `pg_locks` as a share lock held by the starting transaction until commit); `test_concurrent_settings_update_cannot_produce_inconsistent_run_snapshot` (two real sessions: the run-start transaction holds the lock, a concurrent tolerance update blocks until the start commits, and the committed run's `tolerance_pct` equals the settings value in force when the lock was taken; design §6.1); `test_start_run_snapshots_bom_and_issues_materials`: after issuing flour 40, oil 10.5, spice 1.2: `on_hand` flour 120, oil `Decimal("19.5")`, spice `Decimal("3.8")`, run `expected_output_packets == 200`, `tolerance_pct == 3`; `test_complete_run_posts_packaging_scrap_and_output`: good 188, pouches `qty=188, scrap_qty=6`, cartons 9 → pouches `306`, cartons `21`, finished `238`; `test_yield_variance_minus_6_flagged`; `test_oil_variance_plus_5_flagged_with_cost_impact_70`; `test_pouch_variance_minus_3_not_flagged_scrap_6`; `test_carton_variance_minus_10_flagged_cartons_used_9_uncartoned_8`; `test_no_favourable_field_in_view` (view columns contain no `favour`); `test_start_run_replay_same_request_same_payload_returns_original` (no new rows, Review Focus 1); `test_start_run_reused_request_id_different_payload_rejected`; `test_complete_replay_same_request_id_same_payload_returns_original` (same run, ledger row count unchanged, no second output posting); `test_complete_second_request_after_completion_fails` (new request id → `RunAlreadyCompleted`, no postings); `test_complete_reused_request_id_different_payload_rejected` (`IdempotencyConflict`, ledger unchanged); `test_failed_completion_rolls_back_request_log_so_corrected_retry_works`; `test_zero_output_requires_admin_permission_and_note`; `test_zero_output_run_is_flagged_exception_in_view_and_audit`; `test_complete_with_insufficient_packaging_rolls_back_everything`; `test_completed_run_cannot_be_edited_or_voided`; `test_unplanned_material_adds_zero_expected_line`; `test_run_line_item_not_in_bom_requires_unplanned_flag`; `test_issue_in_grams_converts_to_base`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** migration 0007 for the views and the service per §8.1, §9, §10. `complete_run` does the compare-and-set to `completed` first, then posts packaging issue/scrap and `production_output` (`batch_no=run_no`) in one transaction. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(production): two-stage runs, ledger postings and variance view`.

### Task 18: Sales — orders, availability, approval and hold

**Files:** Create `backend/app/services/sales.py`, `backend/tests/services/test_sales.py`

**Interfaces:**
- Consumes: Tasks 12, 13, 14.
- Produces: `@dataclass OrderLineIn(item_id: int, qty: Decimal, unit_price: Decimal | None = None)` (None → `items.list_price`); `@dataclass ApprovalResult(status: Literal["approved","on_hold"], shortages: dict[int, Decimal])`; `submit_order(s, *, client_uuid: UUID, customer_id: int, salesman_id: int, lines: list[OrderLineIn], order_date: date) -> Order`; `stock_coverage(s, order_id: int) -> dict[int, Decimal]` (available minus requested per item; read-only); `edit_order(s, order_id: int, lines: list[OrderLineIn], user_id: int) -> Order` (only `submitted`/`on_hold`); `approve_order(s, order_id: int, user_id: int) -> ApprovalResult`; `reject_order(s, order_id: int, reason: str, user_id: int) -> Order`; `list_orders(s, *, salesman_id: int | None, status: str | None) -> list[Order]`.

- [ ] **Step 1: Write failing tests** (benchmark steps 4 and 6) — `test_submit_order_uses_list_price`; `test_submit_order_is_idempotent`: same `client_uuid` twice returns the same order, one row (Review Focus 1); `test_approve_with_stock_reserves_150`: on-hand 238 → reservation 150 `active`, status `approved`; `test_approve_insufficient_goes_on_hold_with_shortage_32`: on-hand 88, order 120 → status `on_hold`, `shortages == {sku: Decimal("32")}`, **no** reservation row, `hold_reason` mentions 32; `test_on_hold_order_can_be_edited_down_and_approved`; `test_two_concurrent_approvals_for_last_88_packets`: orders of 88 each from two threads → exactly one `approved`, one `on_hold` (Review Focus 2); `test_multi_line_order_is_all_or_nothing`; `test_edit_refused_after_approval`; `test_reject_releases_nothing_when_no_reservation`; `test_salesman_list_orders_returns_only_own`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (T6/T7 in §11; the hold outcome commits). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(sales): orders, availability, approval with hold`.

### Task 19: Invoicing, dispatch, credit notes and PDF

**Files:** Create `backend/app/services/invoicing.py`, `backend/app/templates/invoice.html`, `backend/tests/services/test_invoicing.py`

**Interfaces:**
- Consumes: Tasks 3, 12, 13, 18.
- Produces: `raise_invoice(s, order_id: int, user_id: int, invoice_date: date | None = None) -> Invoice` (`None` → today in IST); `dispatch_invoice(s, invoice_id: int, user_id: int) -> Invoice`; `cancel_invoice(s, invoice_id: int, *, reason: str, restock: bool, user_id: int) -> CreditNote`; `render_invoice_pdf(s, invoice_id: int) -> bytes`; `whatsapp_share_link(invoice: Invoice, public_url: str) -> str`.

- [ ] **Step 1: Write failing tests** (benchmark step 5) — `test_retailer_a_invoice_7875`: taxable `7500.00`, cgst `187.50`, sgst `187.50`, igst `0`, grand total `7875.00`, `supply_type == "intra"`, `invoice_no == "INV/26-27/0001"`; `test_dispatch_reduces_fg_to_88_and_fulfils_reservation`; `test_distributor_b_invoice_would_be_inter_state_igst_300_for_120` (build on an order with sufficient stock in a fixture: taxable 6000, igst 300, total 6300); `test_unregistered_customer_without_gstin_can_be_invoiced`; `test_invoice_requires_approved_order`; `test_invoice_prices_are_snapshot_after_master_price_change`; `test_second_invoice_for_order_rejected`; `test_invoice_numbers_restart_per_fy`: 2027-03-31 gets `INV/26-27/000N`, 2027-04-01 gets `INV/27-28/0001`; `test_invoice_at_0030_ist_on_1_april_is_new_fy` (Review Focus 4); `test_invoice_total_keeps_paise_by_default` (3 × ₹33.33: taxable `99.99`, CGST `2.50`, SGST `2.50`, grand total `104.99`, `round_off == 0.00`); `test_whole_rupee_rounding_only_when_setting_enabled` (same order with `invoice_round_to_rupee` on: total `105.00`, `round_off == 0.01`); `test_dispatch_twice_rejected`; `test_cancel_before_dispatch_releases_reservation_and_issues_credit_note`; `test_cancel_after_dispatch_with_restock_reverses_dispatch`; `test_cancel_blocked_while_payment_allocated`; `test_pdf_contains_gstin_hsn_cgst_sgst_and_number`: PDF bytes start with `%PDF` and extracted text contains `INV/26-27/0001`, `CGST`, `SGST`, `7,875.00`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (T8, T9, T12 in §11; PDF via WeasyPrint with the HTML template). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(invoicing): GST invoices, dispatch, credit notes, PDF`.

### Task 20: Receivables — payments, outstanding, ageing

**Files:** Create `backend/alembic/versions/0008_receivable_views.py`, `backend/app/services/receivables.py`, `backend/tests/services/test_receivables.py`

**Interfaces:**
- Consumes: Tasks 11, 19.
- Produces: views `v_invoice_outstanding`, `v_receivables_ageing`; `@dataclass AllocationIn(invoice_id: int, amount: Decimal)`; `record_payment(s, *, client_uuid: UUID, customer_id: int, amount: Decimal, payment_date: date, method: str, reference: str | None, allocations: list[AllocationIn], user_id: int) -> Payment`; `reverse_payment(s, payment_id: int, reason: str, user_id: int) -> Payment`; `invoice_outstanding(s, invoice_id: int) -> Decimal`; `customer_outstanding(s, customer_id: int) -> Decimal`.

- [ ] **Step 1: Write failing tests** (benchmark step 7) — `test_retailer_a_outstanding_3875`: pay 4,000 against the 7,875 invoice → `invoice_outstanding == Decimal("3875.00")` and `customer_outstanding == Decimal("3875.00")`; `test_payment_retry_with_same_client_uuid_is_idempotent` (Review Focus 1); `test_allocation_over_invoice_total_rejected`; `test_allocation_over_payment_rejected`; `test_unallocated_remainder_kept_as_advance`; `test_reversed_payment_restores_outstanding`; `test_cancelled_invoice_has_zero_outstanding`; `test_ageing_buckets`: invoices 10, 45, 75 and 120 days old land in 0–30, 31–60, 61–90, 90+.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (T10 in §11; lock payment then invoices, ascending id). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(receivables): payments, outstanding, ageing`.

### Task 21: Costing and overheads

**Files:** Create `backend/app/services/costing.py`, `backend/tests/services/test_costing.py`

**Interfaces:**
- Consumes: Tasks 4, 9, 12, 17.
- Produces: `add_overhead(s, *, run_id: int | None, period_month: date | None, kind: str, amount: Decimal, description: str, user_id: int) -> ProductionOverhead`; `void_overhead(s, overhead_id: int, user_id: int) -> None`; `get_run_cost(s, run_id: int) -> RunCostReport` (materials from frozen issue/scrap values, direct overheads, allocated shared overheads, `RunCost`, `margin: Decimal`, `margin_pct: Decimal`); `allocate_shared_overheads(s, month: date) -> dict[int, Decimal]`.

- [ ] **Step 1: Write failing tests** (benchmark step 8) — `test_benchmark_cost_per_packet`: materials `Decimal("4055.00")`, direct overheads `Decimal("1800.00")`, total `5855.00`, `per_packet == Decimal("31.1436")`, margin against ₹50 `== Decimal("18.8564")`; `test_run_cost_unchanged_by_later_flour_purchase` (invariant 24); `test_voided_overhead_excluded_and_new_one_counted`; `test_shared_overhead_allocated_by_material_cost_and_sums_to_pool`; `test_cost_of_run_with_zero_output_raises_clear_error`; `test_overhead_on_completed_run_allowed` (overheads are accounting rows, not run edits).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(costing): per-run costing with direct and shared overheads`.

### Task 22: Benchmark acceptance test and runnable seed command

**Files:** Create `backend/tests/acceptance/test_benchmark.py`, modify `backend/app/seed.py` (add CLI entry `python -m app.seed`), modify `docker-compose.yml` (api command runs `alembic upgrade head && python -m app.seed --if-empty`)

**Interfaces:**
- Consumes: Tasks 15–21.
- Produces: `test_nine_step_benchmark` — the primary acceptance criterion.

- [ ] **Step 1: Write the failing test** that replays steps 1–9 using only service calls and asserts, in order, with `Decimal`: flour `160` then `120`; oil `Decimal("19.5")`; spice `Decimal("3.8")`; pouches `306`; cartons `21`; finished `238` then `88`; yield variance `Decimal("-6.00")` flagged; oil variance `Decimal("5.00")` flagged; carton line numeric variance `Decimal("-10.00")`, `uncartoned_packets == 8`; invoice taxable `7500.00`, CGST `187.50`, SGST `187.50`, total `7875.00`; Retailer A outstanding `3875.00`; Distributor B order `on_hold` with shortage `32`; run `per_packet == Decimal("31.1436")`; raw + packaging stock value `Decimal("9745.00")`; then the identity *opening + receipts − issues ± adjustments = closing* for every non-finished item; finally `assert no source file under app/ contains the literals "7875" or "3875"`.
- [ ] **Step 2: Run** `pytest tests/acceptance -v` → FAIL or partially pass if earlier tasks are complete; fix any gap in the owning task, not in the test.
- [ ] **Step 3: Implement** the seed CLI and compose command. **Step 4: Run** `docker compose up --build`, then `python -m app.seed` against an empty database twice (second run is a no-op) → PASS.
- [ ] **Step 5: Commit checkpoint** — `test: nine-step benchmark acceptance and runnable seed`.

### Task 23: Reports and exports

**Files:** Create `backend/app/services/{reports,exports}.py`, `backend/tests/services/test_reports.py`

**Interfaces:**
- Consumes: Tasks 17, 19–21.
- Produces: `sales_register(s, start: date, end: date) -> list[dict]`; `sales_by_customer(...)`; `sales_by_product(...)`; `gst_summary(s, start, end) -> list[dict]` (taxable by rate and supply type, credit notes netted, GSTR-1-shaped columns); `purchase_register(...)`; `stock_summary(s, as_of: date) -> list[dict]`; `stock_ledger(s, item_id: int, start: date, end: date) -> list[dict]` (opening, each movement with source document number, closing); `production_report(s, start, end) -> list[dict]`; `collections_and_outstanding(s, as_of: date) -> list[dict]` (with ageing); `export_csv(rows: list[dict], columns: list[str]) -> bytes`; `export_xlsx(rows, columns, title: str) -> bytes`; `export_pdf(rows, columns, title: str) -> bytes`.

- [ ] **Step 1: Write failing tests** (benchmark step 9, on the benchmark dataset) — `test_stock_ledger_ties_out`: per item `opening + receipts − issues ± adjustments == closing == on_hand`; `test_stock_ledger_rows_name_their_source_document`; `test_sales_register_total_equals_invoice_totals`: `7875.00`; `test_gst_summary_splits_cgst_sgst_for_tn`: CGST `187.50`, SGST `187.50`, IGST `0`; `test_gst_summary_nets_credit_notes`; `test_production_report_shows_planned_vs_actual_and_flags`; `test_outstanding_report_shows_3875_for_retailer_a`; `test_date_range_excludes_outside_rows`; `test_csv_roundtrip_preserves_decimals_as_text`; `test_xlsx_opens_and_has_headers` (openpyxl load); `test_pdf_export_starts_with_pdf_header`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (reports read views and the ledger only; no writes). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(reports): auditor reports with CSV, XLSX and PDF export`.

### Task 24: API routers (delivered per slice) and role-scoped access

Routers are added **with their slice** (see *Execution sequence*): 24a auth + masters after Task 15; 24b purchasing + production after Task 17; 24c sales + invoicing after Task 19; 24d receivables + costing after Task 21; 24e reports + dashboard after Task 23. The cross-cutting tests below are written in 24a and extended by every slice; the final run covers all of them.

**Files:** Create `backend/app/api/{auth,masters,purchasing,production,sales,receivables,reports,dashboard}.py`, modify `backend/app/main.py`, create `backend/tests/api/test_{access,orders,benchmark_api}.py`

**Interfaces:**
- Consumes: all services; Task 14 `require_roles`.
- Produces: REST endpoints (JSON; decimals as strings), including `POST /orders` (idempotent via `client_uuid`), `GET /orders` (salesman: own only), `POST /orders/{id}/approve`, `POST /orders/{id}/invoice`, `POST /invoices/{id}/dispatch`, `GET /invoices/{id}/pdf`, `POST /payments`, `POST /production-runs` and `POST /production-runs/{id}/complete` (both require `request_id`), `GET /reports/{name}?format=csv|xlsx|pdf`, `GET /dashboard` (today's orders, stock levels, flagged variances, cost per packet, outstanding).

- [ ] **Step 1: Write failing tests** — `test_salesman_cannot_see_other_salesmans_orders` (Review Focus 5); `test_salesman_cannot_approve_or_open_reports` (403); `test_auditor_cannot_write_anything` (every POST returns 403); `test_owner_dashboard_has_required_fields`; `test_production_cannot_create_invoice`; `test_production_role_cannot_complete_run_with_zero_output` (403); `test_run_complete_endpoint_replay_returns_original_and_conflict_returns_409`; `test_decimals_serialised_as_strings`; `test_validation_errors_are_meaningful` (422 body names the field; insufficient stock returns 409 with the shortage); `test_benchmark_via_api`: the nine steps through HTTP as the real roles, asserting `7875.00`, `3875.00`, shortage `32`, FG `88`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** thin routers that open one transaction per request and map domain exceptions to 409/422. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit checkpoint** — `feat(api): role-scoped REST API` (one checkpoint per slice, 24a–24e).

### Task 25: Frontend integration track (gated by Gate G, interleaved with the backend)

**Blocked until the visual-identity discussion is complete and approved (Gate G).** It is not a final phase. After approval a separate frontend plan details each slice; their positions in the order are fixed by *Execution sequence*: F0 scaffold, design tokens, login and role shell; F1 masters; F2 production floor (run start and completion with variance display, and the numeric-versus-wording split of §10); F3 salesman mobile order entry (under a minute) and tracking; F4 admin approval queue with stock coverage and hold review, invoice, dispatch, PDF; F5 payments, outstanding, run cost; F6 auditor reports with export, and the owner dashboard. Offline order queue is a stretch after F3, built on the existing idempotent `client_uuid`.

- [ ] **Step 1:** Hold the design discussion with the user, write the approved design direction, then write the frontend plan. No frontend code before approval.

### Task 26: Documentation and delivery

**Files:** Create `README.md`, `docs/design/02-design-note.md` (maximum two pages: ER diagram, stock and BOM computation, costing method), `docs/demo-script.md`; modify `docker-compose.yml` if needed

**Interfaces:** Consumes everything above.

- [ ] **Step 1: Write the README** covering one-command setup (`docker compose up --build`), stack and justification, features against the section 6 tiers, assumptions (§15), known gaps (§16), test credentials per role, the benchmark table with explanations of any difference (including the carton quirk), and how to run the tests.
- [ ] **Step 2: Write the design note** (two pages, derived from `01-database-design.md`).
- [ ] **Step 3: Write the demo script** that follows the nine steps in order, with the mobile salesman view and the admin view, within eight minutes.
- [ ] **Step 4: Verify** a clean clone runs: `docker compose up --build`, log in with each seeded role, run the acceptance test inside the container. Record actual results.
- [ ] **Step 5: Commit checkpoint** — `docs: README, design note, demo script`.

---

## Self-review (completed)

- **Spec coverage:** §4 tables → Tasks 6–11; §5 integrity matrix → tests in Tasks 6–11 and 12–13; §6 concurrency → Tasks 12–13, 18; §7 idempotency and numbering → Tasks 3, 12, 17–20; §8–9 lifecycles, valuation, reversals → Tasks 6, 13, 16, 19, 21; §10 variance → Tasks 5, 17; §11 transactions → Tasks 16–21; §12 invariants → covered across DB and service tests; §13 benchmark → Tasks 22 and 24; assumptions and deferred items are documented, not built.
- **Type consistency:** `Source`, `lock_items`, `post`, `reserve`, `reverse`, `IssueLine`, `ConsumeLine`, `OrderLineIn`, `AllocationIn` and `RunCost` are defined once and consumed with the same signatures.
- **Cut-line:** `void_purchase` (Task 16) and the R2 branch of `reverse` (Task 13) are built last within their tasks; dropping them does not affect the benchmark.
- **Proportion:** tasks carry signatures, test names and exact values; implementation bodies are left to the implementer.
