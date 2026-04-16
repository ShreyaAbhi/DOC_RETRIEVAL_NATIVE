# POD Automation System
## Product Specification Document

**Document Version:** 1.0
**Classification:** Customer Distribution
**Standard:** Aligned with ISO/IEC 25010 (Software Product Quality)

---

## Table of Contents

1. [Product Overview](#1-product-overview)
2. [System Architecture](#2-system-architecture)
3. [Technology Stack](#3-technology-stack)
4. [Core Features & Capabilities](#4-core-features--capabilities)
5. [System Workflows](#5-system-workflows)
6. [Integration & API Reference](#6-integration--api-reference)
7. [System Requirements](#7-system-requirements)
8. [Security & Compliance](#8-security--compliance)
9. [Deployment Architecture](#9-deployment-architecture)
10. [Administration & Configuration](#10-administration--configuration)
11. [Performance Characteristics](#11-performance-characteristics)
12. [Glossary](#12-glossary)

---

## 1. Product Overview

### 1.1 Product Name
**POD Automation System** (POD System v2)

### 1.2 Purpose
The POD Automation System is a logistics document management platform designed to automate the receipt, classification, matching, and distribution of Proof of Delivery (POD) documents, packing slips, and invoices. It eliminates manual document chasing by integrating email monitoring, AI-powered classification, carrier API connectivity, and a structured approval workflow into a single managed platform.

### 1.3 Target Users

| User Type | Role |
|---|---|
| **Logistics / Operations Teams** | Monitor document status, manage carrier requests, review approvals |
| **Accounts / Finance Teams** | Retrieve invoices and packing slips linked to orders |
| **System Administrators** | Configure the platform, manage users, monitor pipeline health |
| **External Integrators** | Submit and query documents via the REST API using API key authentication |

### 1.4 Key Business Outcomes

- Reduce manual effort in chasing PODs and shipping documents from carriers
- Provide a single searchable record of document status across all deliveries
- Automate email responses to document requests with AI-generated drafts
- Support compliance and audit requirements through full activity logging
- Enable ERP and WMS integration via a documented external REST API

---

## 2. System Architecture

### 2.1 Architectural Overview

The POD System follows a **containerised microservices architecture** deployed via Docker Compose. All components communicate over an internal Docker network; only the reverse proxy is exposed externally.

```
                    ┌─────────────────────────────────────────┐
                    │              CLIENT BROWSER              │
                    └───────────────────┬─────────────────────┘
                                        │ HTTPS (port 443)
                    ┌───────────────────▼─────────────────────┐
                    │             nginx (Reverse Proxy)        │
                    │         TLS termination · routing        │
                    └──────────┬──────────────────┬───────────┘
                               │                  │
              ┌────────────────▼──┐     ┌─────────▼────────────┐
              │  Frontend Service │     │   Backend API Service │
              │  React · Vite     │     │   FastAPI · Uvicorn   │
              │  (port 3000)      │     │   (port 8000)         │
              └───────────────────┘     └──────┬───────┬────────┘
                                               │       │
                              ┌────────────────▼─┐  ┌──▼──────────────────┐
                              │   PostgreSQL 16   │  │   Redis 7            │
                              │   Primary Store   │  │   Task Queue / Cache │
                              └───────────────────┘  └──────────┬──────────┘
                                                                 │
                                              ┌──────────────────▼──────────┐
                                              │   Celery Worker              │
                                              │   Async Pipeline Processing  │
                                              └──────────────────┬──────────┘
                                                                 │
                                              ┌──────────────────▼──────────┐
                                              │   Ollama (LLM Runtime)       │
                                              │   qwen2.5:3b · local         │
                                              └─────────────────────────────┘
```

### 2.2 Component Responsibilities

| Component | Technology | Responsibility |
|---|---|---|
| **nginx** | nginx:alpine | TLS termination, reverse proxy, static asset serving |
| **Frontend** | React 18 + Vite 5 | Single-page application; all user interaction |
| **Backend API** | FastAPI (Python 3.12) | REST API, business logic, authentication |
| **Database** | PostgreSQL 16 | Persistent storage for all entities |
| **Task Queue** | Redis 7 + Celery | Asynchronous pipeline task execution |
| **LLM Runtime** | Ollama + qwen2.5:3b | Email classification and response drafting |
| **IMAP Poller** | Background asyncio task | Monitors configured mailboxes for inbound emails |

---

## 3. Technology Stack

### 3.1 Backend

| Category | Technology | Version |
|---|---|---|
| Runtime | Python | 3.12 |
| Web Framework | FastAPI | Latest stable |
| ASGI Server | Uvicorn | Latest stable |
| ORM | SQLAlchemy (async) | 2.x |
| Database Driver | asyncpg | Latest stable |
| Task Queue | Celery | Latest stable |
| Message Broker | Redis | 7 |
| Password Hashing | bcrypt | 4.1.3 |
| Token Authentication | python-jose (JWT) | Latest stable |
| HTTP Client | httpx | Latest stable |
| Email (SMTP) | smtplib (stdlib) | — |
| Email (IMAP) | imaplib (stdlib) | — |
| PDF Generation | reportlab | Latest stable |
| PDF/Image Conversion | Pillow + LibreOffice | Latest stable |
| Encryption (IMAP creds) | cryptography (Fernet) | Latest stable |

### 3.2 Frontend

| Category | Technology | Version |
|---|---|---|
| UI Framework | React | 18.3 |
| Build Tool | Vite | 5.4 |
| Styling | Tailwind CSS | 3.4 |
| Data Fetching | TanStack Query | 5.x |
| HTTP Client | Axios | 1.7 |
| Routing | React Router | 6.x |
| Charts | Recharts | 2.x |
| Notifications | Sonner | 1.5 |
| Icons | Lucide React | 0.454 |
| Spreadsheet I/O | xlsx | 0.18 |
| HTML Sanitisation | DOMPurify | 3.3 |

### 3.3 Infrastructure

| Component | Technology | Notes |
|---|---|---|
| Container Runtime | Docker + Docker Compose | v2 Compose spec |
| Reverse Proxy | nginx:alpine | Handles TLS, routing |
| Database | PostgreSQL 16 Alpine | Persistent volume |
| LLM Runtime | Ollama | Runs natively on host for GPU access |
| LLM Model | qwen2.5:3b | ~2 GB, local inference |
| TLS | Self-signed or custom cert | Cert path configurable |

---

## 4. Core Features & Capabilities

### 4.1 Document Management

| Feature | Description |
|---|---|
| POD Storage | Stores and indexes Proof of Delivery documents with delivery number, order ID, and carrier linkage |
| Packing Slip Tracking | Associates packing slips with orders by delivery number or order ID |
| Invoice Tracking | Associates invoices with orders by invoice number or delivery number |
| Document Status Dashboard | Single-table view of POD, packing slip, and invoice status per delivery |
| Manual Upload (UI) | Drag-and-drop or file-picker upload for all document types |
| Folder Scanning | Automatic ingestion of documents placed in configured storage folders |
| FTP Polling | Scheduled retrieval of documents from FTP sources |
| PDF Conversion | Automatic conversion of images and Office documents to PDF on ingest |

### 4.2 Email Pipeline

| Feature | Description |
|---|---|
| IMAP Monitoring | Polls one or more configured mailboxes at configurable intervals |
| Subject Filtering | Only emails matching configured subject filters are ingested |
| AI Classification | LLM classifies email intent (POD request, packing slip, invoice, general) with confidence scoring |
| Prompt Injection Protection | Pre-screening and output validation prevent malicious email content from manipulating AI behaviour |
| Multi-Order Detection | Single email referencing multiple order numbers is handled as a multi-order request |
| Carrier API Lookup | Automatic POD retrieval from UPS, FedEx, and DHL APIs when tracking numbers are available |
| Unknown Carrier Handling | Automated email request sent to configured carrier contact; request paused awaiting reply |
| Response Drafting | AI-generated email response drafts with document status table |
| Approval Workflow | Responses queue for human review and approval before sending |
| Guidance Queue | Low-confidence classifications or unresolvable orders route to a human review queue |
| Retry & Recovery | Failed pipeline tasks retry up to 3 times; permanent failures are flagged for manual review |

### 4.3 Order Management

| Feature | Description |
|---|---|
| Order Import | Bulk import via Excel template |
| Auto-Poll Import | Automatic ingestion of order files placed in a configured import folder |
| Order CRUD | Full create, read, update, delete via UI |
| Material Master | Material/SKU reference data management |
| Carrier Registry | Carrier records with contact details and API credentials |

### 4.4 User & Access Management

| Feature | Description |
|---|---|
| Role-Based Access | Two roles: **Admin** (full access) and **Reviewer** (operational access, no admin functions) |
| JWT Authentication | Stateless token-based authentication; 4-hour session lifetime |
| User Management | Admin can create, deactivate, and manage users |
| API Key Management | Admins can issue, list, and revoke API keys for external integrations |
| Audit Log | Full activity trail: email events, classification, approvals, document actions, admin operations |

### 4.5 Reporting & Administration

| Feature | Description |
|---|---|
| Dashboard | Real-time pipeline status chart, request counts by status |
| Reports Page | Filterable request history with export capability |
| DB Explorer | Admin-only raw table viewer with paginated data and bulk delete (password-confirmed) |
| System Configuration | Key-value config store for SMTP, IMAP, carrier credentials, email templates, and system settings |
| Email Signature | Admin-configurable HTML email signature appended to all outbound emails |

---

## 5. System Workflows

### 5.1 Inbound Email Processing Pipeline

```
IMAP Poller / Manual Submission
          │
          ▼
  EmailRequest created (status: received)
          │
          ▼
  ┌───────────────────┐
  │  1. CLASSIFY       │  LLM classifies intent + extracts order refs
  │                   │  Pre-injection screening applied
  └────────┬──────────┘
           │
     Confidence < 75%? ──► Guidance Queue (human review)
           │
           ▼
  ┌───────────────────┐
  │  2. DB LOOKUP      │  Search pod_documents + pod_registry
  └────────┬──────────┘
           │
     Documents found? ──No──► Carrier API Lookup
           │                        │
           │                   Still none? ──► Email carrier / Guidance Queue
           │
           ▼
  ┌───────────────────┐
  │  3. COMPOSE        │  AI drafts response email with document status
  └────────┬──────────┘
           │
           ▼
  ┌───────────────────┐
  │  4. APPROVAL QUEUE │  Human reviews, edits, and approves draft
  └────────┬──────────┘
           │
           ▼
  ┌───────────────────┐
  │  5. SEND & COMPLETE│  Email sent with attachments; status = completed
  └───────────────────┘
```

### 5.2 Document Upload Flow (External API)

```
External System  ──POST /api/v1/documents/upload/pod──►  Validate (type, size, magic)
                                                               │
                                                    Resolve registry entry
                                                               │
                                                    Save to POD storage
                                                               │
                                                    Update pod_registry → have_pod
                                                               │
                                                    Return filename + status
```

### 5.3 Order Import Flow

```
Excel file placed in import folder  OR  uploaded via UI
              │
              ▼
        Parse + validate rows
              │
              ▼
        Dedup (delivery number → order number)
              │
              ▼
        Create / update Order + OrderLine records
              │
              ▼
        Trigger document scan for new order IDs
```

---

## 6. Integration & API Reference

### 6.1 External REST API

Base path: `/api/v1/documents/`
Authentication: `X-API-Key` header (issued by admin)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/lookup` | Query document status by delivery number, customer PO, or order number |
| `POST` | `/upload/pod` | Upload a POD file and associate with a delivery record |
| `GET` | `/download/pod/{filename}` | Download a POD PDF |
| `GET` | `/download/packing-slip/{filename}` | Download a packing slip |
| `GET` | `/download/invoice/{filename}` | Download an invoice |

**Lookup Query Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `delivery_number` | string | Internal delivery / despatch number |
| `customer_po` | string | Customer purchase order number |
| `order_number` | string | Sales or internal order number |
| `request_if_missing` | boolean | If `true`, triggers a carrier POD request when no document is found |

**Upload Form Fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | Yes | PDF or image (PNG, JPEG, TIFF). Max 25 MB. |
| `delivery_number` | string | Conditional | At least one identifier required |
| `customer_po` | string | Conditional | |
| `order_number` | string | Conditional | |

### 6.2 Carrier API Integrations

| Carrier | Integration Type | Credentials Required |
|---|---|---|
| UPS | REST API (Tracking v3) | Client ID + Client Secret (OAuth 2.0) |
| FedEx | REST API (Track v1) | Client ID + Client Secret (OAuth 2.0) |
| DHL | REST API | API Key |

Carrier credentials are stored in the system configuration store and are not exposed via any API response.

### 6.3 Email Integration

| Protocol | Purpose | Configuration |
|---|---|---|
| SMTP | Outbound email (responses, carrier requests) | Host, port, username, App Password |
| IMAP | Inbound email monitoring | Host, port, username, encrypted password, SSL |

Supported providers: any SMTP/IMAP-compatible provider (verified with Gmail).

---

## 7. System Requirements

### 7.1 Server / Host Requirements

| Resource | Minimum | Recommended |
|---|---|---|
| CPU | 4 cores | 8 cores |
| RAM | 8 GB | 16 GB |
| Storage | 40 GB | 100 GB+ (scales with document volume) |
| OS | Windows 10/11, Ubuntu 20.04+, macOS 12+ | Ubuntu 22.04 LTS |
| GPU | Not required | DirectML / CUDA (improves LLM inference speed) |

> **Note:** The LLM component (`qwen2.5:3b`) requires approximately 2–4 GB of RAM during inference. On systems without a GPU, inference latency is typically 30–120 seconds per classification. A GPU reduces this to 2–10 seconds.

### 7.2 Software Dependencies (Host)

| Software | Version | Purpose |
|---|---|---|
| Docker Engine | 24.0+ | Container runtime |
| Docker Compose | v2.0+ | Service orchestration |
| Ollama | Latest | Local LLM runtime |
| Git | Any | Optional — version control |

### 7.3 Network Requirements

| Port | Direction | Protocol | Purpose |
|---|---|---|---|
| 443 | Inbound | HTTPS | Primary application access |
| 80 | Inbound | HTTP | Redirects to HTTPS |
| 11434 | Internal | HTTP | Ollama LLM API (localhost only) |
| 587 | Outbound | SMTP/TLS | Email sending |
| 993 | Outbound | IMAP/SSL | Email monitoring |
| 443 | Outbound | HTTPS | Carrier API calls (UPS, FedEx, DHL) |

> No inbound ports other than 80 and 443 are required. All backend services communicate on the internal Docker network.

### 7.4 Browser Requirements

| Browser | Minimum Version |
|---|---|
| Google Chrome | 100+ |
| Mozilla Firefox | 100+ |
| Microsoft Edge | 100+ |
| Safari | 15+ |

JavaScript must be enabled. No browser plugins or extensions are required.

### 7.5 Storage Sizing Guidance

| Data Type | Estimated Size Per Record |
|---|---|
| POD PDF (generated) | 50–200 KB |
| POD PDF (uploaded) | 100 KB – 5 MB |
| Packing slip / invoice | 50 KB – 2 MB |
| Database record | < 10 KB |

A deployment processing 500 deliveries per month with all document types would consume approximately 2–5 GB of storage per year.

---

## 8. Security & Compliance

### 8.1 Authentication & Authorisation

| Control | Implementation |
|---|---|
| User authentication | Username + password with bcrypt hashing (cost factor 12) |
| Session management | Stateless JWT with 4-hour expiry |
| Role-based access control | Admin and Reviewer roles with enforced permission boundaries |
| API authentication | SHA-256 hashed API keys; raw key shown once at creation only |
| Admin action confirmation | Destructive operations require password re-verification |

### 8.2 Transport Security

| Control | Implementation |
|---|---|
| TLS | HTTPS enforced; HTTP redirects to HTTPS |
| Certificate | Configurable; self-signed provided for development |
| Header protection | HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy |

### 8.3 Input & Content Security

| Control | Implementation |
|---|---|
| Prompt injection protection | Pre-screening regex + LLM output validation + input isolation |
| XSS prevention | DOMPurify sanitisation on all rendered HTML content |
| File upload validation | Extension check + magic number (binary signature) check + 25 MB limit |
| SQL injection prevention | Parameterised queries throughout; table names from hardcoded whitelist only |
| Login brute force protection | 10 attempts per 60-second window per IP address |

### 8.4 Credential Handling

| Credential | Storage Method |
|---|---|
| User passwords | bcrypt hash — plaintext never stored |
| API keys | SHA-256 hash — raw key never stored after creation |
| IMAP passwords | Fernet symmetric encryption at rest |
| JWT secret key | Environment variable (not in source code) |
| Carrier API credentials | System configuration store (database) |

### 8.5 Audit Logging

All significant system events are recorded in the audit log, including:
- Email received and classification outcome
- Document lookup and retrieval
- Approval and rejection actions
- Email send events (including attachment list and delivery status)
- Administrative operations (user changes, bulk deletes)

---

## 9. Deployment Architecture

### 9.1 Docker Services

| Service Name | Image | Purpose |
|---|---|---|
| `pod_nginx` | nginx:alpine | Reverse proxy, TLS |
| `pod_frontend` | Custom (Node 20) | Vite dev server / built assets |
| `pod_backend` | Custom (Python 3.12) | FastAPI application |
| `pod_worker` | Custom (Python 3.12) | Celery pipeline worker |
| `pod_beat` | Custom (Python 3.12) | Celery scheduled task runner |
| `pod_postgres` | postgres:16-alpine | Primary database |
| `pod_redis` | redis:7-alpine | Task broker and cache |

### 9.2 Persistent Volumes

| Volume | Contents |
|---|---|
| `postgres_data` | Database files |
| `./pod_storage` | POD PDF documents |
| `./packing_slips` | Packing slip documents |
| `./invoices` | Invoice documents |
| `./order_import` | Auto-poll order import folder |

### 9.3 Environment Configuration

All sensitive configuration is supplied via environment variables at deployment time. The following must be set before production deployment:

| Variable | Description |
|---|---|
| `SECRET_KEY` | JWT signing key — must be a cryptographically random string |
| `ADMIN_SEED_EMAIL` | Initial administrator email address |
| `ADMIN_SEED_PASSWORD` | Initial administrator password |
| `CORS_ALLOWED_ORIGINS` | Comma-separated list of permitted frontend origins |
| `DATABASE_URL` | PostgreSQL connection string |
| `REDIS_URL` | Redis connection string |
| `OLLAMA_BASE_URL` | Ollama API endpoint |
| `OLLAMA_MODEL` | LLM model name |

---

## 10. Administration & Configuration

### 10.1 System Configuration Keys

Operational settings are managed through the admin Settings interface and stored in the `system_config` table.

| Key | Description |
|---|---|
| `smtp_host` / `smtp_port` | Outbound email server settings |
| `smtp_user` / `smtp_password` | SMTP authentication credentials |
| `imap_subject_filters` | Comma-separated list of subject keywords for email ingestion |
| `email_signature` | HTML signature appended to all outgoing emails |
| `pod_folder_path` | Internal path for POD storage |
| `default_pod_request_email` | Fallback carrier email for unknown carriers |
| `autopoll_path` | Folder path for automatic order file import |
| `fedex_client_id` / `fedex_client_secret` | FedEx API credentials |
| `dhl_api_key` | DHL API key |

### 10.2 Common Operational Tasks

| Task | Method |
|---|---|
| Reset pipeline for a stuck request | Requests page → select request → Retrigger |
| Force immediate IMAP poll | Set `last_checked_at = NULL` on monitored email record |
| Purge queued Celery tasks | `docker exec pod_worker celery -A app.core.celery_app purge -f` |
| Restart pipeline worker after code change | `docker restart pod_worker` |
| Apply configuration changes | Settings → save; backend reloads automatically |

---

## 11. Performance Characteristics

### 11.1 Processing Times

| Operation | Typical Duration | Notes |
|---|---|---|
| Email classification (CPU) | 30–120 seconds | Depends on host CPU; no GPU |
| Email classification (GPU) | 2–10 seconds | With compatible GPU via Ollama |
| Document lookup (DB) | < 100 ms | Indexed queries |
| API document query | < 200 ms | Including DB lookup |
| File upload (25 MB) | 1–5 seconds | Depends on network and disk speed |
| PDF conversion (image) | 1–3 seconds | Via Pillow |
| PDF conversion (Office doc) | 5–15 seconds | Via LibreOffice headless |

### 11.2 Concurrency

| Parameter | Value | Notes |
|---|---|---|
| Celery worker concurrency | 1 | Single-threaded to serialise LLM access |
| Task retry limit | 3 attempts | 30-second delay between retries |
| IMAP poll interval | 60 seconds | Configurable per mailbox |
| Autopoll interval | 30 seconds | Fixed |

### 11.3 Scalability Notes

The current deployment is optimised for single-server operation handling hundreds of requests per day. For higher throughput, the worker concurrency can be increased once dedicated GPU resources are available to support parallel LLM inference. The database and API tiers are horizontally scalable independently of the LLM component.

---

## 12. Glossary

| Term | Definition |
|---|---|
| **POD** | Proof of Delivery — a document confirming that goods were delivered and received |
| **Packing Slip** | A document listing the contents of a shipment |
| **Pipeline** | The automated sequence of steps that processes an inbound email request end-to-end |
| **LLM** | Large Language Model — the AI component used for email classification and response drafting |
| **Guidance Queue** | A holding queue for requests the system cannot resolve automatically; requires human input |
| **Approval Queue** | A queue of AI-drafted email responses awaiting human review before sending |
| **IMAP** | Internet Message Access Protocol — used to monitor inbound email mailboxes |
| **SMTP** | Simple Mail Transfer Protocol — used to send outbound emails |
| **JWT** | JSON Web Token — the authentication token issued on login |
| **API Key** | A credential used by external systems to authenticate with the External API v1 |
| **Registry** | The `pod_registry` table — the master record of document status per delivery number |
| **Celery** | An asynchronous task queue used to process pipeline tasks in the background |
| **Redis** | An in-memory data store used as the Celery task broker |
| **Ollama** | The local LLM runtime that hosts the classification and drafting model |
| **DOMPurify** | A JavaScript library that sanitises HTML content to prevent XSS attacks |
| **Magic Number** | The binary signature at the start of a file that identifies its true type |

---

*POD Automation System — Product Specification v1.0*
*For technical integration queries, please contact your account representative.*
