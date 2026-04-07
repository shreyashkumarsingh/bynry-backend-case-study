"""
StockFlow — Runnable FastAPI Demo
==================================
A self-contained FastAPI application that demonstrates the core endpoints
from this case study using SQLite (in-memory) for zero-setup running.

For production use, swap the DATABASE_URL to a PostgreSQL connection string.

Usage:
    pip install -r requirements.txt
    uvicorn app:app --reload --port 8000

    # Then visit: http://localhost:8000/docs  (auto-generated Swagger UI)
"""

from __future__ import annotations

import math
import logging
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
)
logger = logging.getLogger("stockflow")

# ── Database setup ────────────────────────────────────────────────────────────

# SQLite for local demo; replace with postgresql+psycopg2://... for production
DATABASE_URL = "sqlite:///./stockflow_demo.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # SQLite-specific; remove for PostgreSQL
    echo=False,
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    """Dependency that provides a database session and ensures cleanup."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Schema bootstrapping ──────────────────────────────────────────────────────

BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    slug       TEXT    NOT NULL UNIQUE,
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS warehouses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    name       TEXT    NOT NULL,
    is_active  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS suppliers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    name            TEXT    NOT NULL,
    contact_email   TEXT,
    contact_phone   TEXT,
    lead_time_days  INTEGER,
    is_active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  INTEGER NOT NULL REFERENCES companies(id),
    supplier_id INTEGER REFERENCES suppliers(id),
    name        TEXT    NOT NULL,
    sku         TEXT    NOT NULL,
    price       TEXT    NOT NULL,   -- stored as string to preserve decimal precision in SQLite demo
    is_bundle   INTEGER NOT NULL DEFAULT 0,
    is_active   INTEGER NOT NULL DEFAULT 1,
    UNIQUE (company_id, sku)
);

CREATE TABLE IF NOT EXISTS inventory (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    INTEGER NOT NULL REFERENCES products(id),
    warehouse_id  INTEGER NOT NULL REFERENCES warehouses(id),
    quantity      INTEGER NOT NULL DEFAULT 0,
    reorder_point INTEGER NOT NULL DEFAULT 10,
    reorder_qty   INTEGER NOT NULL DEFAULT 50,
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (product_id, warehouse_id)
);

CREATE TABLE IF NOT EXISTS inventory_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    inventory_id   INTEGER NOT NULL REFERENCES inventory(id),
    product_id     INTEGER NOT NULL,
    warehouse_id   INTEGER NOT NULL,
    delta          INTEGER NOT NULL,
    quantity_after INTEGER NOT NULL,
    reason         TEXT    NOT NULL DEFAULT 'initial_stock',
    reference_id   TEXT,
    actor_id       INTEGER,
    note           TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sales_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    INTEGER NOT NULL REFERENCES products(id),
    warehouse_id  INTEGER NOT NULL REFERENCES warehouses(id),
    quantity_sold INTEGER NOT NULL,
    sold_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    order_id      TEXT
);
"""

SEED_SQL = """
-- Seed demo data only if company table is empty
INSERT OR IGNORE INTO companies (id, name, slug) VALUES (1, 'Acme Corp', 'acme-corp');

INSERT OR IGNORE INTO warehouses (id, company_id, name) VALUES
    (1, 1, 'Main Warehouse'),
    (2, 1, 'East Coast DC');

INSERT OR IGNORE INTO suppliers (id, company_id, name, contact_email, contact_phone, lead_time_days) VALUES
    (1, 1, 'Acme Parts Co.', 'orders@acmeparts.com', '+1-800-555-0199', 5),
    (2, 1, 'Global Supply Ltd', 'supply@globalsupply.io', '+1-800-555-0177', 10);

INSERT OR IGNORE INTO products (id, company_id, supplier_id, name, sku, price) VALUES
    (1, 1, 1, 'Widget Pro',             'WGT-001', '29.99'),
    (2, 1, 2, 'Deluxe Mounting Bracket','BRK-DLX', '12.50'),
    (3, 1, NULL, 'Standard Bolt Pack',  'BLT-STD', '4.99');

-- inventory: Widget Pro is critically low in both warehouses
INSERT OR IGNORE INTO inventory (id, product_id, warehouse_id, quantity, reorder_point, reorder_qty) VALUES
    (1, 1, 1, 3,  10, 100),   -- Widget Pro / Main     → LOW (3 <= 10)
    (2, 1, 2, 4,  10, 100),   -- Widget Pro / East DC  → LOW (4 <= 10)
    (3, 2, 1, 8,  15,  50),   -- Bracket    / Main     → LOW (8 <= 15)
    (4, 3, 1, 200, 20, 100);  -- Bolts      / Main     → OK  (200 > 20)

-- sales in last 30 days (so these products qualify for the alert)
INSERT OR IGNORE INTO sales_events (product_id, warehouse_id, quantity_sold, sold_at, order_id) VALUES
    (1, 1, 20, datetime('now', '-5 days'),  'ORD-001'),
    (1, 1, 15, datetime('now', '-12 days'), 'ORD-002'),
    (1, 1, 25, datetime('now', '-20 days'), 'ORD-003'),
    (1, 2, 18, datetime('now', '-3 days'),  'ORD-004'),
    (1, 2, 24, datetime('now', '-15 days'), 'ORD-005'),
    (1, 2, 18, datetime('now', '-25 days'), 'ORD-006'),
    (2, 1,  3, datetime('now', '-7 days'),  'ORD-007'),
    (2, 1,  3, datetime('now', '-18 days'), 'ORD-008'),
    (2, 1,  3, datetime('now', '-28 days'), 'ORD-009'),
    (3, 1, 50, datetime('now', '-2 days'),  'ORD-010');  -- Bolts sell well; not low-stock
"""


def init_db():
    with engine.connect() as conn:
        for statement in BOOTSTRAP_SQL.strip().split(";"):
            stmt = statement.strip()
            if stmt:
                conn.execute(text(stmt))
        for statement in SEED_SQL.strip().split(";"):
            stmt = statement.strip()
            if stmt:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass  # seed failures are non-fatal
        conn.commit()
    logger.info("Database initialized.")


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(
    title="StockFlow API",
    description="B2B SaaS Inventory Management — Backend Case Study",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class CreateProductRequest(BaseModel):
    name: str
    sku: str
    price: str   # String to preserve decimal precision; validated below
    warehouse_id: int
    initial_quantity: int
    supplier_id: Optional[int] = None

    @field_validator("name", "sku")
    @classmethod
    def must_be_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must be a non-empty string")
        return v

    @field_validator("sku")
    @classmethod
    def sku_to_upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("price")
    @classmethod
    def validate_price(cls, v: str) -> str:
        try:
            price = Decimal(v)
            if price < 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ValueError("must be a non-negative decimal number (e.g. '29.99')")
        return v

    @field_validator("warehouse_id")
    @classmethod
    def warehouse_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be a positive integer")
        return v

    @field_validator("initial_quantity")
    @classmethod
    def quantity_must_be_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be >= 0")
        return v


class CreateProductResponse(BaseModel):
    message: str
    product_id: int
    inventory_id: int


class SupplierInfo(BaseModel):
    id: int
    name: str
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    lead_time_days: Optional[int] = None


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
    last_sale_at: str
    days_until_stockout: Optional[float] = None
    supplier: Optional[SupplierInfo] = None


class LowStockResponse(BaseModel):
    company_id: int
    total_alerts: int
    page: int
    page_size: int
    total_pages: int
    alerts: list[LowStockAlert]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
def health_check():
    """Simple liveness probe."""
    return {"status": "ok", "service": "stockflow-api"}


@app.post(
    "/api/products",
    response_model=CreateProductResponse,
    status_code=201,
    tags=["products"],
    summary="Create a product and initialize warehouse inventory",
)
def create_product(
    payload: CreateProductRequest,
    db: Session = Depends(get_db),
):
    """
    Create a new product record and initialize its inventory in the specified
    warehouse. Both writes occur in a single atomic transaction.

    Raises:
        400 — Validation failure (handled by FastAPI/Pydantic)
        404 — Warehouse not found
        409 — Duplicate SKU within the company
        500 — Unexpected database error
    """
    # Verify the warehouse exists. For demo purposes we derive company_id from
    # the warehouse; in production the company_id comes from the auth token.
    warehouse_row = db.execute(
        text("SELECT id, company_id FROM warehouses WHERE id = :id AND is_active = 1"),
        {"id": payload.warehouse_id},
    ).fetchone()

    if not warehouse_row:
        raise HTTPException(
            status_code=404,
            detail=f"Warehouse {payload.warehouse_id} not found or inactive.",
        )

    company_id = warehouse_row.company_id

    try:
        # --- Insert product ---
        result = db.execute(
            text("""
                INSERT INTO products (company_id, supplier_id, name, sku, price)
                VALUES (:company_id, :supplier_id, :name, :sku, :price)
            """),
            {
                "company_id": company_id,
                "supplier_id": payload.supplier_id,
                "name": payload.name,
                "sku": payload.sku,
                "price": payload.price,
            },
        )
        product_id = result.lastrowid

        # --- Initialize inventory (same transaction) ---
        inv_result = db.execute(
            text("""
                INSERT INTO inventory (product_id, warehouse_id, quantity)
                VALUES (:product_id, :warehouse_id, :quantity)
            """),
            {
                "product_id": product_id,
                "warehouse_id": payload.warehouse_id,
                "quantity": payload.initial_quantity,
            },
        )
        inventory_id = inv_result.lastrowid

        # --- Write initial audit log entry ---
        db.execute(
            text("""
                INSERT INTO inventory_log
                    (inventory_id, product_id, warehouse_id, delta, quantity_after, reason)
                VALUES
                    (:inventory_id, :product_id, :warehouse_id, :delta, :qty_after, 'initial_stock')
            """),
            {
                "inventory_id": inventory_id,
                "product_id": product_id,
                "warehouse_id": payload.warehouse_id,
                "delta": payload.initial_quantity,
                "qty_after": payload.initial_quantity,
            },
        )

        db.commit()  # Single commit: all three inserts atomically succeed or fail

    except IntegrityError as exc:
        db.rollback()
        err_str = str(exc).lower()
        if "unique" in err_str and "sku" in err_str:
            raise HTTPException(
                status_code=409,
                detail=f"SKU '{payload.sku}' already exists for this company.",
            )
        logger.error("IntegrityError creating product: %s", exc)
        raise HTTPException(status_code=409, detail="A database constraint was violated.")

    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Database error creating product: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Unexpected database error.")

    logger.info("Created product id=%d sku=%s qty=%d", product_id, payload.sku, payload.initial_quantity)
    return CreateProductResponse(
        message="Product created.",
        product_id=product_id,
        inventory_id=inventory_id,
    )


@app.get(
    "/api/companies/{company_id}/alerts/low-stock",
    response_model=LowStockResponse,
    tags=["alerts"],
    summary="Get low-stock alerts for a company",
)
def get_low_stock_alerts(
    company_id: int,
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=50, ge=1, le=200, description="Results per page (max 200)"),
    db: Session = Depends(get_db),
):
    """
    Return products that are at or below their reorder threshold AND have had
    at least one sale in the last 30 days. Includes supplier info and a
    days_until_stockout estimate based on 30-day sales velocity.

    Sorted by days_until_stockout ASC (most urgent first), nulls last.
    """
    # Validate company exists
    company = db.execute(
        text("SELECT id FROM companies WHERE id = :id AND is_active = 1"),
        {"id": company_id},
    ).fetchone()

    if not company:
        raise HTTPException(status_code=404, detail=f"Company {company_id} not found.")

    # -- Count total matching alerts for pagination metadata --
    # SQLite uses datetime arithmetic slightly differently; 'now', '-30 days' works in both
    count_sql = text("""
        WITH recent_sales AS (
            SELECT product_id, warehouse_id
            FROM sales_events
            WHERE sold_at >= datetime('now', '-30 days')
            GROUP BY product_id, warehouse_id
        )
        SELECT COUNT(*) AS total
        FROM inventory inv
        JOIN products   p ON p.id = inv.product_id
        JOIN warehouses w ON w.id = inv.warehouse_id
        JOIN recent_sales rs
             ON rs.product_id   = inv.product_id
            AND rs.warehouse_id = inv.warehouse_id
        WHERE w.company_id = :company_id
          AND p.company_id = :company_id
          AND p.is_active  = 1
          AND w.is_active  = 1
          AND inv.quantity <= inv.reorder_point
    """)
    total_alerts = db.execute(count_sql, {"company_id": company_id}).scalar()
    total_pages = math.ceil(total_alerts / page_size) if total_alerts > 0 else 1
    offset = (page - 1) * page_size

    if page > total_pages and total_alerts > 0:
        return LowStockResponse(
            company_id=company_id,
            total_alerts=total_alerts,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            alerts=[],
        )

    # -- Fetch paginated alert rows --
    alerts_sql = text("""
        WITH recent_sales AS (
            SELECT
                product_id,
                warehouse_id,
                SUM(quantity_sold)        AS total_sold_30d,
                SUM(quantity_sold) / 30.0 AS avg_daily_sold,
                MAX(sold_at)              AS last_sale_at
            FROM sales_events
            WHERE sold_at >= datetime('now', '-30 days')
            GROUP BY product_id, warehouse_id
        )
        SELECT
            p.id                                                    AS product_id,
            p.name                                                  AS product_name,
            p.sku,
            p.is_bundle,
            w.id                                                    AS warehouse_id,
            w.name                                                  AS warehouse_name,
            inv.quantity                                            AS current_stock,
            inv.reorder_point,
            inv.reorder_qty,
            rs.total_sold_30d,
            rs.avg_daily_sold,
            rs.last_sale_at,
            CASE
                WHEN rs.avg_daily_sold > 0
                THEN ROUND(CAST(inv.quantity AS REAL) / rs.avg_daily_sold, 1)
                ELSE NULL
            END                                                     AS days_until_stockout,
            s.id                                                    AS supplier_id,
            s.name                                                  AS supplier_name,
            s.contact_email                                         AS supplier_email,
            s.contact_phone                                         AS supplier_phone,
            s.lead_time_days                                        AS supplier_lead_time_days
        FROM inventory inv
        JOIN products    p  ON p.id  = inv.product_id
        JOIN warehouses  w  ON w.id  = inv.warehouse_id
        JOIN recent_sales rs
             ON rs.product_id   = inv.product_id
            AND rs.warehouse_id = inv.warehouse_id
        LEFT JOIN suppliers s ON s.id = p.supplier_id
        WHERE w.company_id = :company_id
          AND p.company_id = :company_id
          AND p.is_active  = 1
          AND w.is_active  = 1
          AND inv.quantity <= inv.reorder_point
        ORDER BY days_until_stockout ASC NULLS LAST, p.name ASC
        LIMIT  :page_size
        OFFSET :offset
    """)

    rows = db.execute(
        alerts_sql,
        {"company_id": company_id, "page_size": page_size, "offset": offset},
    ).fetchall()

    alerts = []
    for row in rows:
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
                is_bundle=bool(row.is_bundle),
                warehouse_id=row.warehouse_id,
                warehouse_name=row.warehouse_name,
                current_stock=row.current_stock,
                reorder_point=row.reorder_point,
                reorder_qty=row.reorder_qty,
                total_sold_30d=row.total_sold_30d,
                avg_daily_sold=float(row.avg_daily_sold),
                last_sale_at=str(row.last_sale_at),
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


# ── Global exception handler ──────────────────────────────────────────────────

@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "An unexpected error occurred. Please try again."},
    )
