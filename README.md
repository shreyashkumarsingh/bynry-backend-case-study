# Backend Engineering Case Study Submission for Bynry – Shreyash Singh

# StockFlow — B2B SaaS Inventory Management System

> A production-grade backend case study demonstrating system design, API development, and database architecture for a multi-tenant inventory management platform.

---

## Table of Contents

- [Overview](#overview)
- [Case Study Structure](#case-study-structure)
- [Technical Approach](#technical-approach)
- [Architecture](#architecture)
- [Repository Structure](#repository-structure)
- [Running the Code](#running-the-code)
- [API Reference](#api-reference)
- [Design Decisions](#design-decisions)
- [Future Improvements](#future-improvements)

---

## Overview

StockFlow is a B2B SaaS platform that enables companies to manage inventory across multiple warehouses, track stock movements, configure supplier relationships, and receive automated low-stock alerts. This case study covers three core areas:

| Part | Focus | Key Skills Demonstrated |
|------|-------|------------------------|
| [Part 1](part1_debugging.md) | Code Review & Debugging | Error handling, transactions, validation, security |
| [Part 2](part2_database.md) | Database Design | Schema design, indexing, normalization, audit logging |
| [Part 3](part3_api.md) | API Implementation | Clean REST APIs, complex queries, edge case handling |

---

## Design Philosophy

This solution prioritizes:
- Data consistency using transactions
- Scalability via normalized schema and indexing
- Real-world constraints like multi-warehouse inventory
- Clean separation of concerns in API design

If scaled further, this system can be extended using microservices with async processing (Celery/Kafka) and Redis caching for high-throughput inventory queries.


## Case Study Structure

```
stockflow/
├── README.md                   # This file
├── part1_debugging.md          # Code review and bug analysis
├── part2_database.md           # Database schema design
├── part3_api.md                # API implementation walkthrough
├── diagrams/
│   ├── architecture.md         # System architecture (ASCII)
│   └── erd.md                  # Entity-relationship diagram
└── optional-code/
    ├── app.py                  # Runnable FastAPI implementation
    └── requirements.txt        # Python dependencies
```

---

## Technical Approach

### Core Principles

**Correctness over cleverness.** Every piece of logic in this codebase is explicit, intentional, and easy to audit. I prioritize readable code that a new engineer can understand in 5 minutes over micro-optimizations.

**Fail loudly, recover gracefully.** The system validates inputs at the boundary, uses database transactions for multi-step operations, and returns structured error responses that clients can act on.

**Design for change.** The schema and API are designed to accommodate future requirements (e.g., bundle products, multi-currency, demand forecasting) without breaking existing contracts.

### Backend Strengths Highlighted

- **Atomic transactions** — Multi-step operations (create product + initialize inventory) are wrapped in a single database transaction to prevent partial writes
- **Input validation** — All external data is validated before touching the database; missing or malformed fields return `400` with actionable messages
- **Proper decimal handling** — Financial values use `NUMERIC(12,4)` in the database and Python's `Decimal` type to avoid IEEE 754 floating-point errors
- **Indexed queries** — All foreign keys and common filter columns carry indexes; composite indexes are placed on high-cardinality query patterns
- **Audit trail** — Every inventory change writes an immutable log entry with actor, timestamp, and delta — enabling full traceability
- **Multi-tenancy** — All queries are scoped by `company_id` to prevent data leakage between tenants
- **Pagination** — List endpoints accept `page` / `page_size` to prevent unbounded result sets

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                        Clients                           │
│              (Web App / Mobile / Partner API)            │
└─────────────────────────┬────────────────────────────────┘
                          │ HTTPS
┌─────────────────────────▼────────────────────────────────┐
│                    Load Balancer                          │
│                  (nginx / AWS ALB)                        │
└──────────┬───────────────────────────┬───────────────────┘
           │                           │
┌──────────▼──────────┐   ┌────────────▼──────────────────┐
│   API Servers        │   │      Background Workers        │
│   (FastAPI / uvicorn)│   │   (Celery + Redis)             │
│   Stateless, N nodes │   │   - Alert processing           │
└──────────┬──────────┘   │   - Report generation          │
           │               └────────────┬──────────────────┘
           │                            │
┌──────────▼────────────────────────────▼──────────────────┐
│                   PostgreSQL (Primary)                    │
│              + Read Replica for analytics                 │
└──────────────────────────────────────────────────────────┘
           │
┌──────────▼──────────┐
│   Redis Cache        │
│   - Session tokens   │
│   - Low-stock cache  │
│   - Rate limiting    │
└─────────────────────┘
```

See [diagrams/architecture.md](diagrams/architecture.md) for the full diagram.

---

## Running the Code

### Prerequisites

- Python 3.11+
- PostgreSQL 14+

### Setup

```bash
# Clone and enter the repo
git clone https://github.com/your-username/stockflow.git
cd stockflow/optional-code

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env and set DATABASE_URL

# Run the app (SQLite in-memory for demo)
uvicorn app:app --reload --port 8000
```

### Verify

```bash
# Health check
curl http://localhost:8000/health

# Create a product
curl -X POST http://localhost:8000/api/products \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Widget Pro",
    "sku": "WGT-001",
    "price": "29.99",
    "warehouse_id": 1,
    "initial_quantity": 150
  }'

# Get low-stock alerts
curl "http://localhost:8000/api/companies/1/alerts/low-stock"
```

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/products` | Create product and initialize inventory |
| `GET` | `/api/companies/{id}/alerts/low-stock` | Get low-stock alerts with supplier info |
| `GET` | `/health` | Health check |

Full request/response examples are in [part3_api.md](part3_api.md).

---

## Design Decisions

**Why FastAPI over Flask?**
FastAPI provides request/response schema validation via Pydantic, async support, and auto-generated OpenAPI docs out of the box. For a new production service, this eliminates a large class of validation bugs at zero cost.

**Why PostgreSQL?**
Inventory systems require ACID compliance. PostgreSQL's `NUMERIC` type handles financial precision correctly, advisory locks can prevent oversell races, and window functions simplify analytics queries (e.g., days_until_stockout).

**Why a separate `inventory_log` table?**
Mutable inventory balances without a changelog are an operational nightmare. The log provides: debugging capability, customer-facing transaction history, replayability, and compliance audit trails.

**Why `NUMERIC(12,4)` for prices?**
`FLOAT` is fundamentally wrong for money — it cannot represent many decimal fractions exactly. `NUMERIC(12,4)` stores up to $99,999,999.9999 with exact precision.

---

## Future Improvements

### Short-term (Next Sprint)
- **Redis caching** for low-stock alert results (TTL: 5 minutes); invalidate on inventory update
- **Webhook notifications** when stock drops below threshold — push to Slack/email rather than requiring polling
- **Optimistic locking** on inventory updates to handle concurrent stock adjustments safely

### Medium-term
- **Celery task queue** for heavy operations: report generation, bulk imports, alert fan-out
- **Rate limiting** per API key using Redis sliding window counters
- **Cursor-based pagination** to replace offset pagination for large result sets

### Long-term
- **ML-based demand forecasting** — train on `sales_events` history to dynamically adjust reorder thresholds per product/season
- **Event sourcing** for inventory — replace mutable balance + log with pure event stream; current balance is a projection
- **Read replicas** — route analytics queries (low-stock scan, reporting) to replica; writes go to primary
- **Multi-region** — geo-distribute warehouse data closer to operations for latency-sensitive stock updates

---

## Author

Built as a backend engineering case study. All code is written to production standards with real-world edge cases in mind.
