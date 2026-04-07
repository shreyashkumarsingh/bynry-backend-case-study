# Part 3: API Implementation

## Endpoint

```
GET /api/companies/{company_id}/alerts/low-stock
```

Returns a list of products that are at or below their reorder threshold, have had recent sales activity, and includes supplier contact info and a computed `days_until_stockout` estimate.

---

## Requirements Breakdown

| Requirement | Implementation |
|-------------|----------------|
| Multi-warehouse support | Aggregate across all warehouses; one alert per (product, warehouse) pair |
| Product-specific thresholds | Use `inventory.reorder_point` per row |
| Only include products with recent sales | Filter via `sales_events` JOIN with a 30-day lookback window |
| Include supplier info | LEFT JOIN `suppliers` on `products.supplier_id` |
| Compute `days_until_stockout` | `current_quantity / avg_daily_sales` (30-day rolling window) |

---

## Assumptions

1. "Recent sales" means at least one sale in the last 30 days. Products with no recent sales are excluded (they're not actively moving and wouldn't warrant an urgent alert).
2. `days_until_stockout` is computed as: `current_quantity / avg_daily_sold_last_30_days`. If avg is 0 but stock is low, we return `null` (cannot estimate).
3. A product can appear multiple times in the response — once per warehouse where it is below threshold. The client is expected to handle this.
4. The `company_id` in the URL path is the tenant scope. We validate the company exists before querying.
5. Results are sorted by `days_until_stockout ASC NULLS LAST` — most urgent alerts first.
6. Pagination is supported via `?page=1&page_size=50` query parameters.

---

## SQL Query

The core query that powers this endpoint:

```sql
WITH recent_sales AS (
    -- Compute average daily sales per (product, warehouse) over the last 30 days.
    -- Only include products that have had at least one sale in this window.
    SELECT
        se.product_id,
        se.warehouse_id,
        SUM(se.quantity_sold)                           AS total_sold_30d,
        SUM(se.quantity_sold) / 30.0                    AS avg_daily_sold,
        MAX(se.sold_at)                                 AS last_sale_at
    FROM sales_events se
    WHERE se.sold_at >= NOW() - INTERVAL '30 days'
    GROUP BY se.product_id, se.warehouse_id
)
SELECT
    p.id                                                AS product_id,
    p.name                                             AS product_name,
    p.sku,
    p.is_bundle,
    w.id                                               AS warehouse_id,
    w.name                                             AS warehouse_name,
    inv.quantity                                       AS current_stock,
    inv.reorder_point,
    inv.reorder_qty,
    rs.total_sold_30d,
    rs.avg_daily_sold,
    rs.last_sale_at,
    -- days_until_stockout: null when avg_daily_sold is 0 (no velocity to extrapolate from)
    CASE
        WHEN rs.avg_daily_sold > 0
        THEN ROUND(inv.quantity / rs.avg_daily_sold, 1)
        ELSE NULL
    END                                                AS days_until_stockout,
    s.id                                               AS supplier_id,
    s.name                                             AS supplier_name,
    s.contact_email                                    AS supplier_email,
    s.contact_phone                                    AS supplier_phone,
    s.lead_time_days                                   AS supplier_lead_time_days
FROM inventory inv
JOIN products    p  ON p.id  = inv.product_id
JOIN warehouses  w  ON w.id  = inv.warehouse_id
JOIN recent_sales rs
    ON rs.product_id   = inv.product_id
    AND rs.warehouse_id = inv.warehouse_id
LEFT JOIN suppliers s ON s.id = p.supplier_id
WHERE
    w.company_id    = :company_id          -- tenant scope
    AND p.company_id = :company_id         -- belt-and-suspenders; products are also scoped
    AND p.is_active  = TRUE
    AND w.is_active  = TRUE
    AND inv.quantity <= inv.reorder_point  -- at or below threshold
ORDER BY
    days_until_stockout ASC NULLS LAST,    -- most urgent first
    p.name ASC
LIMIT  :page_size
OFFSET :offset;
```

---

## FastAPI Implementation

```python
# This is the key section from optional-code/app.py.
# See that file for the full runnable application.

from __future__ import annotations

import math
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

router = APIRouter()


# ── Response schemas ─────────────────────────────────────────────────────────

class SupplierInfo(BaseModel):
    id: int
    name: str
    contact_email: Optional[str]
    contact_phone: Optional[str]
    lead_time_days: Optional[int]


class LowStockAlert(BaseModel):
    product_id: int
    product_name: str
    sku: str
    is_bundle: bool
    warehouse_id: int
    warehouse_name: str
    current_stock: int
    reorder_point: int
    reorder_qty: int
    total_sold_30d: int
    avg_daily_sold: float
    last_sale_at: str           # ISO 8601 string
    days_until_stockout: Optional[float]   # null when no sales velocity
    supplier: Optional[SupplierInfo]


class LowStockResponse(BaseModel):
    company_id: int
    total_alerts: int
    page: int
    page_size: int
    total_pages: int
    alerts: list[LowStockAlert]


# ── Query ─────────────────────────────────────────────────────────────────────

LOW_STOCK_QUERY = text("""
    WITH recent_sales AS (
        SELECT
            se.product_id,
            se.warehouse_id,
            SUM(se.quantity_sold)        AS total_sold_30d,
            SUM(se.quantity_sold) / 30.0 AS avg_daily_sold,
            MAX(se.sold_at)              AS last_sale_at
        FROM sales_events se
        WHERE se.sold_at >= NOW() - INTERVAL '30 days'
        GROUP BY se.product_id, se.warehouse_id
    )
    SELECT
        p.id                                                     AS product_id,
        p.name                                                   AS product_name,
        p.sku,
        p.is_bundle,
        w.id                                                     AS warehouse_id,
        w.name                                                   AS warehouse_name,
        inv.quantity                                             AS current_stock,
        inv.reorder_point,
        inv.reorder_qty,
        rs.total_sold_30d,
        rs.avg_daily_sold,
        rs.last_sale_at,
        CASE
            WHEN rs.avg_daily_sold > 0
            THEN ROUND(inv.quantity / rs.avg_daily_sold, 1)
            ELSE NULL
        END                                                      AS days_until_stockout,
        s.id                                                     AS supplier_id,
        s.name                                                   AS supplier_name,
        s.contact_email                                          AS supplier_email,
        s.contact_phone                                          AS supplier_phone,
        s.lead_time_days                                         AS supplier_lead_time_days
    FROM inventory inv
    JOIN products    p  ON p.id   = inv.product_id
    JOIN warehouses  w  ON w.id   = inv.warehouse_id
    JOIN recent_sales rs
        ON rs.product_id    = inv.product_id
       AND rs.warehouse_id  = inv.warehouse_id
    LEFT JOIN suppliers s ON s.id = p.supplier_id
    WHERE
        w.company_id    = :company_id
        AND p.company_id = :company_id
        AND p.is_active  = TRUE
        AND w.is_active  = TRUE
        AND inv.quantity <= inv.reorder_point
    ORDER BY days_until_stockout ASC NULLS LAST, p.name ASC
    LIMIT  :page_size
    OFFSET :offset
""")

COUNT_QUERY = text("""
    WITH recent_sales AS (
        SELECT product_id, warehouse_id
        FROM sales_events
        WHERE sold_at >= NOW() - INTERVAL '30 days'
        GROUP BY product_id, warehouse_id
    )
    SELECT COUNT(*) AS total
    FROM inventory inv
    JOIN products   p ON p.id  = inv.product_id
    JOIN warehouses w ON w.id  = inv.warehouse_id
    JOIN recent_sales rs
        ON rs.product_id   = inv.product_id
       AND rs.warehouse_id = inv.warehouse_id
    WHERE
        w.company_id    = :company_id
        AND p.company_id = :company_id
        AND p.is_active  = TRUE
        AND w.is_active  = TRUE
        AND inv.quantity <= inv.reorder_point
""")


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.get(
    "/api/companies/{company_id}/alerts/low-stock",
    response_model=LowStockResponse,
    summary="Get low-stock alerts for a company",
    responses={
        200: {"description": "List of low-stock alerts"},
        404: {"description": "Company not found"},
        422: {"description": "Invalid query parameters"},
    },
)
def get_low_stock_alerts(
    company_id: int,
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=50, ge=1, le=200, description="Results per page"),
    db: Session = Depends(get_db),
):
    """
    Return all products that are at or below their reorder threshold
    AND have had at least one sale in the last 30 days.

    Results include:
    - Current stock and reorder threshold
    - 30-day sales velocity and computed days_until_stockout
    - Supplier contact info (nullable if no supplier is assigned)

    Sorted by days_until_stockout ASC (most urgent first), nulls last.
    """
    # Validate company exists — prevents leaking whether other company IDs exist
    # by returning the same 404 regardless.
    company = db.execute(
        text("SELECT id FROM companies WHERE id = :id AND is_active = TRUE"),
        {"id": company_id},
    ).fetchone()

    if not company:
        raise HTTPException(status_code=404, detail=f"Company {company_id} not found.")

    # Compute pagination offsets
    offset = (page - 1) * page_size

    # Fetch total count (for pagination metadata) — uses the lighter CTE query
    total_alerts = db.execute(
        COUNT_QUERY, {"company_id": company_id}
    ).scalar_one()

    total_pages = math.ceil(total_alerts / page_size) if total_alerts > 0 else 1

    # Guard: if requested page is beyond available data, return empty but valid response
    if page > total_pages and total_alerts > 0:
        return LowStockResponse(
            company_id=company_id,
            total_alerts=total_alerts,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            alerts=[],
        )

    # Fetch paginated alert rows
    rows = db.execute(
        LOW_STOCK_QUERY,
        {"company_id": company_id, "page_size": page_size, "offset": offset},
    ).fetchall()

    alerts = []
    for row in rows:
        # Build supplier sub-object only when supplier data is present
        supplier = None
        if row.supplier_id is not None:
            supplier = SupplierInfo(
                id=row.supplier_id,
                name=row.supplier_name,
                contact_email=row.supplier_email,
                contact_phone=row.supplier_phone,
                lead_time_days=row.supplier_lead_time_days,
            )

        alerts.append(
            LowStockAlert(
                product_id=row.product_id,
                product_name=row.product_name,
                sku=row.sku,
                is_bundle=row.is_bundle,
                warehouse_id=row.warehouse_id,
                warehouse_name=row.warehouse_name,
                current_stock=row.current_stock,
                reorder_point=row.reorder_point,
                reorder_qty=row.reorder_qty,
                total_sold_30d=row.total_sold_30d,
                avg_daily_sold=float(row.avg_daily_sold),
                last_sale_at=row.last_sale_at.isoformat(),
                days_until_stockout=(
                    float(row.days_until_stockout)
                    if row.days_until_stockout is not None
                    else None
                ),
                supplier=supplier,
            )
        )

    return LowStockResponse(
        company_id=company_id,
        total_alerts=total_alerts,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        alerts=alerts,
    )
```

---

## Example Response

```json
{
  "company_id": 1,
  "total_alerts": 2,
  "page": 1,
  "page_size": 50,
  "total_pages": 1,
  "alerts": [
    {
      "product_id": 42,
      "product_name": "Widget Pro",
      "sku": "WGT-001",
      "is_bundle": false,
      "warehouse_id": 3,
      "warehouse_name": "East Coast DC",
      "current_stock": 4,
      "reorder_point": 10,
      "reorder_qty": 100,
      "total_sold_30d": 60,
      "avg_daily_sold": 2.0,
      "last_sale_at": "2026-04-06T14:22:00+00:00",
      "days_until_stockout": 2.0,
      "supplier": {
        "id": 7,
        "name": "Acme Parts Co.",
        "contact_email": "orders@acmeparts.com",
        "contact_phone": "+1-800-555-0199",
        "lead_time_days": 5
      }
    },
    {
      "product_id": 17,
      "product_name": "Deluxe Mounting Bracket",
      "sku": "BRK-DLX",
      "is_bundle": false,
      "warehouse_id": 1,
      "warehouse_name": "Main Warehouse",
      "current_stock": 8,
      "reorder_point": 15,
      "reorder_qty": 50,
      "total_sold_30d": 9,
      "avg_daily_sold": 0.3,
      "last_sale_at": "2026-04-01T09:10:00+00:00",
      "days_until_stockout": 26.7,
      "supplier": null
    }
  ]
}
```

---

## Example curl Request

```bash
# Basic request
curl -s "http://localhost:8000/api/companies/1/alerts/low-stock" \
  -H "Accept: application/json" | python -m json.tool

# With pagination
curl -s "http://localhost:8000/api/companies/1/alerts/low-stock?page=2&page_size=20" \
  -H "Accept: application/json"

# With auth header (production)
curl -s "http://localhost:8000/api/companies/1/alerts/low-stock" \
  -H "Authorization: Bearer <token>" \
  -H "Accept: application/json"
```

---

## Edge Cases Handled

| Scenario | Behaviour |
|----------|-----------|
| Company ID does not exist | `404 Not Found` |
| Company has no warehouses | Empty `alerts` array, `total_alerts: 0` |
| Product has no supplier | `"supplier": null` in response |
| Product has recent sales but zero avg (rounding edge) | `days_until_stockout: null` |
| Page number exceeds total pages | Empty `alerts` with correct pagination metadata |
| `page_size` > 200 | FastAPI rejects with `422 Unprocessable Entity` |
| Inactive warehouse or product | Excluded by `is_active = TRUE` filter |
| Bundle products below threshold | Included with `is_bundle: true` flag — clients can handle differently |
