# Part 2: Database Design

## Requirements Summary

Design a scalable relational schema for StockFlow that supports:
- Multi-tenant companies with multiple warehouses
- Products tracked across multiple warehouses via an inventory join table
- Inventory change audit log
- Supplier relationships on products
- Bundle products (a product composed of other products)

---

## Schema (PostgreSQL)

```sql
-- ============================================================
-- EXTENSIONS
-- ============================================================
CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- for gen_random_uuid()


-- ============================================================
-- COMPANIES (top-level tenant)
-- ============================================================
CREATE TABLE companies (
    id            SERIAL          PRIMARY KEY,
    name          VARCHAR(255)    NOT NULL,
    slug          VARCHAR(100)    NOT NULL UNIQUE,   -- used in URLs / API keys
    plan          VARCHAR(50)     NOT NULL DEFAULT 'starter',
    is_active     BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_companies_slug ON companies (slug);


-- ============================================================
-- WAREHOUSES
-- ============================================================
CREATE TABLE warehouses (
    id            SERIAL          PRIMARY KEY,
    company_id    INT             NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    name          VARCHAR(255)    NOT NULL,
    location      TEXT,                                -- free-text address or geo coordinates
    timezone      VARCHAR(100)    NOT NULL DEFAULT 'UTC',
    is_active     BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (company_id, name)   -- warehouse names are unique within a company
);

CREATE INDEX idx_warehouses_company_id ON warehouses (company_id);


-- ============================================================
-- SUPPLIERS
-- ============================================================
CREATE TABLE suppliers (
    id            SERIAL          PRIMARY KEY,
    company_id    INT             NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    name          VARCHAR(255)    NOT NULL,
    contact_email VARCHAR(320),
    contact_phone VARCHAR(50),
    lead_time_days INT            CHECK (lead_time_days >= 0),  -- average reorder lead time
    is_active     BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (company_id, name)
);

CREATE INDEX idx_suppliers_company_id ON suppliers (company_id);


-- ============================================================
-- PRODUCTS
-- ============================================================
CREATE TABLE products (
    id            SERIAL          PRIMARY KEY,
    company_id    INT             NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    supplier_id   INT             REFERENCES suppliers (id) ON DELETE SET NULL,
    name          VARCHAR(255)    NOT NULL,
    sku           VARCHAR(100)    NOT NULL,
    description   TEXT,
    price         NUMERIC(12, 4)  NOT NULL CHECK (price >= 0),   -- sale price
    cost          NUMERIC(12, 4)  CHECK (cost >= 0),             -- purchase cost from supplier
    unit          VARCHAR(50)     NOT NULL DEFAULT 'each',        -- each, kg, litre, etc.
    is_bundle     BOOLEAN         NOT NULL DEFAULT FALSE,
    is_active     BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- SKU is unique within a company (different companies can reuse the same SKU)
    UNIQUE (company_id, sku)
);

CREATE INDEX idx_products_company_id  ON products (company_id);
CREATE INDEX idx_products_supplier_id ON products (supplier_id);
CREATE INDEX idx_products_sku         ON products (company_id, sku);   -- composite: most lookups filter by company first


-- ============================================================
-- BUNDLE COMPONENTS
-- A bundle product is composed of one or more component products.
-- Example: "Starter Kit" (bundle) contains "Widget" x2 + "Manual" x1.
-- ============================================================
CREATE TABLE bundle_components (
    id              SERIAL      PRIMARY KEY,
    bundle_id       INT         NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    component_id    INT         NOT NULL REFERENCES products (id) ON DELETE RESTRICT,
    quantity        INT         NOT NULL CHECK (quantity > 0),

    -- A component can appear only once per bundle; adjust quantity instead
    UNIQUE (bundle_id, component_id),

    -- A bundle cannot be its own component (self-referencing loop prevention)
    CHECK (bundle_id <> component_id)
);

CREATE INDEX idx_bundle_components_bundle_id    ON bundle_components (bundle_id);
CREATE INDEX idx_bundle_components_component_id ON bundle_components (component_id);


-- ============================================================
-- INVENTORY
-- Tracks current stock level for a (product, warehouse) pair.
-- ============================================================
CREATE TABLE inventory (
    id              SERIAL          PRIMARY KEY,
    product_id      INT             NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    warehouse_id    INT             NOT NULL REFERENCES warehouses (id) ON DELETE CASCADE,
    quantity        INT             NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    reorder_point   INT             NOT NULL DEFAULT 10 CHECK (reorder_point >= 0),  -- alert threshold
    reorder_qty     INT             NOT NULL DEFAULT 50 CHECK (reorder_qty > 0),     -- suggested order qty
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- A product is tracked once per warehouse
    UNIQUE (product_id, warehouse_id)
);

CREATE INDEX idx_inventory_product_id   ON inventory (product_id);
CREATE INDEX idx_inventory_warehouse_id ON inventory (warehouse_id);

-- Partial index: quickly find all low-stock entries without scanning full table
CREATE INDEX idx_inventory_low_stock
    ON inventory (warehouse_id, product_id)
    WHERE quantity <= reorder_point;


-- ============================================================
-- INVENTORY LOG (immutable audit trail)
-- Every change to inventory.quantity writes a record here.
-- The current balance is always derivable by summing deltas,
-- but we also keep the denormalized snapshot for performance.
-- ============================================================
CREATE TYPE inventory_change_reason AS ENUM (
    'initial_stock',
    'purchase_order',
    'sale',
    'return',
    'adjustment',
    'transfer_in',
    'transfer_out',
    'shrinkage',
    'bundle_fulfillment'
);

CREATE TABLE inventory_log (
    id              BIGSERIAL       PRIMARY KEY,                  -- BIGSERIAL: high-volume table
    inventory_id    INT             NOT NULL REFERENCES inventory (id) ON DELETE CASCADE,
    product_id      INT             NOT NULL,                     -- denormalized for query performance
    warehouse_id    INT             NOT NULL,                     -- denormalized for query performance
    delta           INT             NOT NULL,                     -- positive = stock in, negative = stock out
    quantity_after  INT             NOT NULL CHECK (quantity_after >= 0),  -- snapshot after this change
    reason          inventory_change_reason NOT NULL,
    reference_id    VARCHAR(100),                                 -- e.g. order_id, PO number, transfer_id
    actor_id        INT,                                          -- user or system that triggered the change
    note            TEXT,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- Querying recent changes for a specific product/warehouse is the most common access pattern
CREATE INDEX idx_inventory_log_inventory_id ON inventory_log (inventory_id, created_at DESC);
CREATE INDEX idx_inventory_log_product_id   ON inventory_log (product_id, created_at DESC);
CREATE INDEX idx_inventory_log_warehouse_id ON inventory_log (warehouse_id, created_at DESC);
CREATE INDEX idx_inventory_log_reference    ON inventory_log (reference_id) WHERE reference_id IS NOT NULL;


-- ============================================================
-- SALES EVENTS
-- Lightweight record of units sold, used to compute sales velocity
-- for the days_until_stockout calculation in Part 3.
-- ============================================================
CREATE TABLE sales_events (
    id              BIGSERIAL       PRIMARY KEY,
    product_id      INT             NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    warehouse_id    INT             NOT NULL REFERENCES warehouses (id) ON DELETE CASCADE,
    quantity_sold   INT             NOT NULL CHECK (quantity_sold > 0),
    sold_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    order_id        VARCHAR(100),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sales_events_product_warehouse ON sales_events (product_id, warehouse_id, sold_at DESC);
CREATE INDEX idx_sales_events_sold_at           ON sales_events (sold_at DESC);


-- ============================================================
-- FUNCTION: update updated_at automatically
-- ============================================================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

-- Apply to all tables with updated_at
CREATE TRIGGER trg_companies_updated_at   BEFORE UPDATE ON companies   FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_warehouses_updated_at  BEFORE UPDATE ON warehouses  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_suppliers_updated_at   BEFORE UPDATE ON suppliers   FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_products_updated_at    BEFORE UPDATE ON products    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_inventory_updated_at   BEFORE UPDATE ON inventory   FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

---

## Design Decisions

### Multi-Tenancy via `company_id`

Every table that contains business data carries a `company_id` foreign key. All application-layer queries must filter on `company_id` first. This pattern (sometimes called "tenant scoping") is simpler to implement and operate than separate schemas or separate databases per tenant, while still providing strong data isolation when enforced correctly via Row-Level Security (RLS) or ORM-layer policies.

> **Future:** Enable PostgreSQL Row-Level Security on `products`, `inventory`, and `inventory_log` to enforce tenant isolation at the database level, eliminating the risk of a missing `WHERE company_id = ?` clause leaking data.

### `NUMERIC(12,4)` for Money

`FLOAT` and `DOUBLE` use binary fractions and cannot exactly represent most decimal values. `NUMERIC(12,4)` provides exact decimal arithmetic up to 8 integer digits and 4 decimal places — sufficient for virtually all inventory pricing scenarios.

### Inventory Table as Junction with State

`inventory` is not just a join table — it carries denormalized state (`quantity`, `reorder_point`, `reorder_qty`). This is a deliberate trade-off: storing the current balance here avoids summing the full log on every read (which would be O(n) at scale). The log remains the source of truth for auditing and can reconstruct the balance if needed.

### `inventory_log` as Immutable Append-Only Table

The log uses `BIGSERIAL` (not `SERIAL`) because it is the highest-volume table — every stock movement writes here. It stores `product_id` and `warehouse_id` redundantly (denormalized) to avoid joins on hot analytical queries like "show all changes for warehouse X in the last 7 days." Rows are never updated or deleted.

### Bundle Products via Self-Referencing Join

Rather than creating a separate `bundles` table, a `is_bundle = TRUE` flag on `products` plus a `bundle_components` self-referencing join supports the requirement without duplicating product metadata. A CHECK constraint prevents direct self-references; deeper cycles (A→B→A) would need a trigger or application-layer validation.

### Partial Index on `inventory`

```sql
CREATE INDEX idx_inventory_low_stock ON inventory (warehouse_id, product_id)
WHERE quantity <= reorder_point;
```

The low-stock alert query (Part 3) is a frequent, read-heavy operation that only cares about a small subset of rows. A partial index dramatically reduces its scan size and keeps it fast as the `inventory` table grows.

### `sales_events` for Velocity

Rather than computing sales velocity from `inventory_log` (which mixes sales, adjustments, returns, etc.), a dedicated `sales_events` table provides a clean, queryable signal. The `days_until_stockout` formula is `current_quantity / avg_daily_sales`, where `avg_daily_sales` is derived from this table over a rolling window.

---

## Missing Requirements / Open Questions

The following questions would need answers before this schema goes to production:

1. **Currency** — Is pricing always in a single currency per company, or do products have multi-currency prices? If multi-currency: add a `currency` column to `products` or a separate `product_prices` table.

2. **Negative stock / oversell** — Should the DB enforce `quantity >= 0`, or are negative balances permitted (e.g., for pre-orders or fulfilment from a supplier before goods arrive)? The current schema enforces `>= 0` but this may be too strict.

3. **Product variants** — Are products expected to have variants (size, colour)? If yes, a `product_variants` table with a FK to `products` would be needed before launch.

4. **Purchase orders** — Is there a formal PO workflow (raise PO → receive goods → update inventory)? This would require a `purchase_orders` table referencing `suppliers`.

5. **Transfers between warehouses** — Should stock transfers between warehouses be atomic (deduct from A and add to B in one transaction)? This is supported by the log's `transfer_in` / `transfer_out` reasons but no `transfers` table exists yet.

6. **User roles** — What granularity of RBAC is needed? Company admin vs. warehouse manager vs. read-only viewer would each have different write permissions.

7. **Soft-delete vs. hard-delete** — Should deleted products and warehouses be hard-deleted or archived? The current `is_active` flag supports soft-deletes for warehouses and products, but cascade rules need revisiting if hard deletes are required.

8. **Data retention** — How long should `inventory_log` and `sales_events` rows be retained? At high volume these tables grow quickly. Consider partitioning by month or archiving to cold storage after 12 months.
