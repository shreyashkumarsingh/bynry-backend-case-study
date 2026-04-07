# Entity-Relationship Diagram — StockFlow

## ERD (ASCII)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            StockFlow ERD                                    │
└─────────────────────────────────────────────────────────────────────────────┘

┌───────────────────┐
│    companies      │
├───────────────────┤
│ PK id             │
│    name           │
│    slug  (UNIQUE) │
│    plan           │
│    is_active      │
│    created_at     │
│    updated_at     │
└────────┬──────────┘
         │ 1
         │
         ├──────────────────────────────────┐
         │                                  │
         │ N                                │ N
┌────────▼──────────┐             ┌─────────▼──────────┐
│    warehouses     │             │    suppliers        │
├───────────────────┤             ├────────────────────-┤
│ PK id             │             │ PK id               │
│ FK company_id ────┼─────────────┼─── company_id       │
│    name           │             │    name             │
│    location       │             │    contact_email    │
│    timezone       │             │    contact_phone    │
│    is_active      │             │    lead_time_days   │
│    created_at     │             │    is_active        │
│    updated_at     │             │    created_at       │
└────────┬──────────┘             │    updated_at       │
         │ 1                      └────────┬────────────┘
         │                                 │ 0..1
         │ N                               │ N
┌────────▼──────────────────────────────── ▼────────────┐
│                     products                          │
├───────────────────────────────────────────────────────┤
│ PK id                                                 │
│ FK company_id  ─────────────────────── companies.id   │
│ FK supplier_id ─────────────────────── suppliers.id   │
│    name                                               │
│    sku          (UNIQUE per company)                  │
│    description                                        │
│    price        NUMERIC(12,4)                         │
│    cost         NUMERIC(12,4)                         │
│    unit                                               │
│    is_bundle                                          │
│    is_active                                          │
│    created_at                                         │
│    updated_at                                         │
└───────┬──────────────────────────────────┬────────────┘
        │ 1                                │ 1 (when is_bundle=TRUE)
        │                                  │
        │ N                                │ N (components)
┌───────▼──────────┐             ┌─────────▼──────────────┐
│    inventory     │             │   bundle_components     │
├──────────────────┤             ├────────────────────────-┤
│ PK id            │             │ PK id                   │
│ FK product_id ───┼─────────────┼─── bundle_id (→products)│
│ FK warehouse_id  │             │    component_id (→prods)│
│    quantity      │             │    quantity             │
│    reorder_point │             │                         │
│    reorder_qty   │             │ UNIQUE(bundle_id,        │
│    updated_at    │             │        component_id)     │
│                  │             └─────────────────────────┘
│ UNIQUE(product_id│
│  ,warehouse_id)  │
└───────┬──────────┘
        │ 1
        │
        │ N
┌───────▼──────────────────────────┐
│         inventory_log            │
├──────────────────────────────────┤
│ PK id            (BIGSERIAL)     │
│ FK inventory_id ─── inventory.id │
│    product_id    (denormalized)  │
│    warehouse_id  (denormalized)  │
│    delta                         │
│    quantity_after                │
│    reason        (ENUM)          │
│    reference_id                  │
│    actor_id                      │
│    note                          │
│    created_at    (immutable)     │
└──────────────────────────────────┘

        ┌──────────────────────────┐
        │       sales_events       │
        ├──────────────────────────┤
        │ PK id       (BIGSERIAL)  │
        │ FK product_id            │
        │ FK warehouse_id          │
        │    quantity_sold         │
        │    sold_at               │
        │    order_id              │
        │    created_at            │
        └──────────────────────────┘
```

---

## Relationship Summary

| Relationship | Cardinality | Notes |
|---|---|---|
| `companies` → `warehouses` | 1:N | A company owns multiple warehouses |
| `companies` → `suppliers` | 1:N | Suppliers are scoped per company |
| `companies` → `products` | 1:N | Products are scoped per company |
| `suppliers` → `products` | 1:N (optional) | A product may have no supplier |
| `products` × `warehouses` → `inventory` | M:N (join with state) | One inventory record per product-warehouse pair |
| `inventory` → `inventory_log` | 1:N | Every change to a balance produces a log entry |
| `products` → `bundle_components` | 1:N (self-ref) | Bundle products reference component products |
| `products` × `warehouses` → `sales_events` | M:N | Sales are recorded per product-warehouse |

---

## Column Type Rationale

| Column | Type | Why |
|--------|------|-----|
| `price`, `cost` | `NUMERIC(12,4)` | Exact decimal — never `FLOAT` for money |
| `inventory_log.id` | `BIGSERIAL` | High-volume table; `INT` max (~2B) could overflow |
| `sales_events.id` | `BIGSERIAL` | Same as above |
| `reason` | `ENUM` | Constrains to known values; prevents free-text inconsistency |
| `*_at` columns | `TIMESTAMPTZ` | Stores timezone offset; correct for multi-region ops |
