# System Architecture — StockFlow

## High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              CLIENTS                                     │
│                                                                          │
│   ┌─────────────┐    ┌──────────────────┐    ┌───────────────────────┐  │
│   │  Web App    │    │  Mobile App      │    │  Partner API / EDI    │  │
│   │  (React)    │    │  (iOS/Android)   │    │  (3PL, ERP systems)   │  │
│   └──────┬──────┘    └────────┬─────────┘    └───────────┬───────────┘  │
└──────────┼───────────────────┼──────────────────────────┼───────────────┘
           │                   │                          │
           │         HTTPS / REST                         │
           ▼                   ▼                          ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                         LOAD BALANCER                                    │
│                    (nginx / AWS ALB / Cloudflare)                        │
│                TLS termination · Rate limiting · WAF                     │
└────────────────────────┬───────────────────────────────────────────────┘
                         │
           ┌─────────────┴──────────────┐
           │                            │
┌──────────▼──────────┐     ┌───────────▼──────────┐
│    API SERVER #1     │     │    API SERVER #2      │
│   FastAPI / uvicorn  │     │   FastAPI / uvicorn   │
│   Stateless          │     │   Stateless           │
│                      │     │                       │
│  ┌────────────────┐  │     │  ┌─────────────────┐  │
│  │  Auth Middleware│ │     │  │ Auth Middleware  │  │
│  │  (JWT / API Key)│ │     │  │ (JWT / API Key)  │  │
│  └────────────────┘  │     │  └─────────────────┘  │
└──────────┬───────────┘     └────────────┬──────────┘
           │                              │
           └──────────────┬───────────────┘
                          │
         ┌────────────────┴────────────────┐
         │                                 │
┌────────▼──────────┐          ┌───────────▼──────────────┐
│   PostgreSQL       │          │   Redis                   │
│   (Primary)        │          │   Cache / Session Store   │
│                    │          │                           │
│  ┌──────────────┐  │          │  ┌─────────────────────┐ │
│  │  companies   │  │          │  │  low-stock cache     │ │
│  │  warehouses  │  │          │  │  (TTL: 5 min)        │ │
│  │  products    │  │          │  └─────────────────────┘ │
│  │  inventory   │◄─┼──────────┤                           │
│  │  inv_log     │  │          │  ┌─────────────────────┐ │
│  │  suppliers   │  │          │  │  rate limit counters │ │
│  │  sales_events│  │          │  └─────────────────────┘ │
│  └──────────────┘  │          │                           │
└────────┬───────────┘          └───────────────────────────┘
         │
         │  Streaming replication
         ▼
┌────────────────────┐
│  PostgreSQL Replica │
│  (Read-only)        │
│  Analytics queries  │
│  Report generation  │
└────────────────────┘
         │
         │  Async / scheduled
         ▼
┌────────────────────────────────────┐
│        BACKGROUND WORKERS          │
│        (Celery + Redis Broker)     │
│                                    │
│  ┌──────────────────────────────┐  │
│  │  low_stock_alert_fan_out     │  │
│  │  → Email / Slack / Webhook   │  │
│  └──────────────────────────────┘  │
│  ┌──────────────────────────────┐  │
│  │  bulk_import_processor       │  │
│  │  → CSV / EDI file ingestion  │  │
│  └──────────────────────────────┘  │
│  ┌──────────────────────────────┐  │
│  │  report_generator            │  │
│  │  → PDF / XLSX exports        │  │
│  └──────────────────────────────┘  │
└────────────────────────────────────┘
```

---

## Request Lifecycle

```
Client Request
     │
     ▼
Load Balancer ──── (rejects oversized/malformed requests)
     │
     ▼
API Server
  │
  ├─ Auth Middleware
  │    ├── Validate Bearer token / API key
  │    ├── Resolve company_id from token
  │    └── Attach to request context
  │
  ├─ Route Handler
  │    ├── Validate path/query params (Pydantic)
  │    ├── Check Redis cache (read endpoints only)
  │    │       └── HIT  → return cached response
  │    │       └── MISS → query database
  │    ├── Execute SQL (SQLAlchemy / raw text)
  │    ├── Build response model
  │    └── Write to Redis cache (if applicable)
  │
  └─ Return JSON response
```

---

## Deployment Topology

```
Production (AWS)
├── ECS Fargate — API containers (auto-scaled 2–10 instances)
├── RDS PostgreSQL 15 — Multi-AZ, automated backups
├── ElastiCache Redis — Cluster mode, 2 shards
├── ALB — HTTPS termination, health checks
└── CloudWatch — Metrics, structured log aggregation

CI/CD
├── GitHub Actions → run tests + lint
├── Docker build + push to ECR
└── ECS rolling deployment (zero-downtime)
```
