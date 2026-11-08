# Revenue OS

[![Public Preview CI](https://github.com/miguelmoga2002-alt/revenue-os-public-preview/actions/workflows/ci.yml/badge.svg)](https://github.com/miguelmoga2002-alt/revenue-os-public-preview/actions/workflows/ci.yml)

**Public Technical Preview** · `1.1.0-rc.1` · Sales Operating System for B2B and B2C teams

Deterministic sales execution platform built around pipeline management, account intelligence, task prioritization and auditable commercial workflows.

> **Release status:** Laboratory release candidate. Validated with synthetic data. Not approved for production deployment or customer-data pilots.

## Why I built this

I've spent four years as a full-cycle Account Executive, and I know where a rep's day goes: updating the CRM instead of following up. I'm building Revenue OS as the sales system for our two family businesses, an aluminium carpentry company and an industrial machinery and real estate business: one place that says what to do next and keeps every deal traceable. The goal is one system where calls, transcripts, emails and notes sit next to the pipeline, so a rep gets back 20-30% of the time that admin takes today. This preview covers the core; AI and telephony are on the roadmap.

I designed the product, its rules and its architecture, and I write the code with AI assistants under a test-first, verify-before-done protocol. Next, I want to know whether other sales teams, B2B and B2C, have the same problem, so I'm talking to sales leaders (see section 06).

## 01. Technical Overview

| Layer | Technology |
|---|---|
| Backend | Python 3.12 · FastAPI · Pydantic v2 |
| Frontend | HTML5 · Vanilla JavaScript · Native CSS |
| Persistence | PostgreSQL 16 · SQLAlchemy 2 |
| Database Migrations | Alembic |
| Database Security | Native PostgreSQL Row Level Security |
| Authentication | Server-side Sessions · PBKDF2 · TOTP MFA |
| Authorization | Tenant Isolation · RBAC · Record-level ACL |
| Infrastructure | Docker · Docker Compose |
| Testing | pytest · Playwright · Chromium |
| CI / Security | GitHub Actions · Bandit · pip-audit · axe-core |
| Decision Engine | Deterministic Business Rules |
| LLM Integration | Technical Roadmap — Not Implemented |

### Implementation Status

| Component | Status | Scope |
|---|---|---|
| Pipeline / Kanban | Implemented | Deals, configurable stages and state transitions |
| Account 360 | Implemented | Relationships, persistent notes and activity timeline |
| Focus / Today | Implemented | Explainable, rule-based task prioritization |
| Sales Execution | Implemented | Assignments, rescheduling, outcomes and idempotency |
| Migration Studio | Implemented | CSV/XLSX import, mapping, relationships and conditional rollback |
| Multi-tenant Security | Implemented | PostgreSQL RLS, RBAC, record grants and audit events |
| Authentication | Implemented | Revocable sessions and administrator TOTP MFA |
| CRM Connectors | Experimental | Pipedrive, HubSpot and Salesforce adapters; disabled by default |
| Generative AI | Roadmap | LLM-assisted extraction and follow-up generation |
| External Integrations | Roadmap | Email, Calendar, Slack and Telephony |

**Current decision model: 100% deterministic.**

Prioritization is calculated from persisted business data, explicit conditions and configurable rules.

No LLM inference is used for scoring, recommendations or workflow execution in this release.

## 02. System Architecture

```text
┌───────────────────────────────────────────┐
│                 Browser UI                │
│           HTML · Vanilla JS · CSS         │
└────────────────────┬──────────────────────┘
                     │
                Fetch API
                     │
┌────────────────────▼──────────────────────┐
│                  FastAPI                  │
│                                           │
│    Authentication · RBAC · Record ACL     │
│    Pipeline · Focus · Migration Studio    │
│    Deterministic Decision Engine          │
│    Audit Events                           │
└────────────────────┬──────────────────────┘
                     │
               SQLAlchemy 2
                     │
┌────────────────────▼──────────────────────┐
│               PostgreSQL 16               │
│                                           │
│    Native Row Level Security (RLS)        │
│    Tenant-scoped Policies                 │
│    Transactions · Persistent Storage      │
└───────────────────────────────────────────┘

Alembic        → Schema migrations
Docker Compose → Local orchestration
GitHub Actions → Automated validation
```

### Backend

Python/FastAPI application with explicit authorization boundaries and transactional persistence.

- **FastAPI + Pydantic v2:** REST API, request validation and typed contracts.
- **SQLAlchemy 2 + psycopg 3:** Transactional database operations.
- **Alembic:** Version-controlled schema migrations.
- **Deterministic Decision Engine:** Explainable task selection and business-rule enforcement.
- **Audit Trail:** Persistent records of relevant commercial operations.
- **Idempotency:** Protection against duplicate operations in supported workflows.

### Frontend

**Vanilla JavaScript + Native CSS — Zero-dependency frontend architecture to prevent framework bloat.**

No React, Next.js, Vue, Angular, Tailwind or client-side framework runtime.

| Design Decision | Engineering Rationale |
|---|---|
| Vanilla JavaScript | Direct DOM manipulation without framework overhead |
| Native CSS | Styling without external CSS frameworks |
| Fetch API | Native HTTP communication |
| History API | Browser navigation and deep linking |
| Standalone HTML Views | Reduced build and deployment complexity |
| No Bundler | Minimal frontend toolchain |

The architecture minimizes runtime overhead and dependency surface. Sub-millisecond execution remains a performance optimization target, not a measured guarantee.

**Frontend entry points:**

```text
web/
├── index.html      # Migration Studio
├── crm.html        # Pipeline / Account 360
└── focus.html      # Guided Sales Execution
```

## 03. Security Model

**Tenant-level isolation via Native PostgreSQL Row Level Security (RLS).**

Tenant isolation is enforced through database-level policies, supplemented by application-level authorization and record-specific access controls.

| Security Control | Implementation |
|---|---|
| Tenant Isolation | Native PostgreSQL RLS |
| Runtime Database Role | Restricted `revenue_app` |
| Database Privileges | Non-owner · `NOBYPASSRLS` |
| Application Authorization | RBAC and Record-level Grants |
| Authentication | Revocable Server-side Sessions |
| Administrator MFA | TOTP — RFC 6238 |
| Password Hashing | PBKDF2-HMAC-SHA256 |
| Secret Encryption | Fernet |
| Auditability | Persistent Audit Events |
| HTTP Security | Application-level Hardening |

### Database Isolation

PostgreSQL RLS policies restrict tenant-scoped records using database session context.

```sql
tenant_id = NULLIF(
    current_setting('revenue_os.tenant_id', true),
    ''
)
```

The application operates under a restricted database role instead of the migration owner.

Cross-tenant isolation and direct SQL access scenarios are included in the PostgreSQL test suite.

### Container Hardening

- Read-only application filesystem.
- Dropped Linux capabilities.
- `no-new-privileges` enforcement.
- Restricted temporary storage.
- Process limits.
- Loopback-only development port bindings.

### Quality Assurance

| Validation | Tooling |
|---|---|
| Unit & Integration Tests | pytest |
| PostgreSQL Isolation | RLS and Cross-tenant Tests |
| Transactional Consistency | Concurrency and Idempotency Tests |
| Browser E2E | Playwright · Chromium |
| Accessibility Checks | axe-core |
| Dependency Auditing | pip-audit |
| Static Security Analysis | Bandit |
| CI Automation | GitHub Actions |

**Security scope:** Implemented controls and automated tests do not constitute a penetration test, SOC 2/ISO certification, formal GDPR compliance assessment or production SLA.

## 04. Local Installation

### Requirements

- Git
- Docker Engine / Docker Desktop
- Docker Compose
- Python 3.12 for local tooling

### Step 1 — Clone Repository

```bash
git clone https://github.com/miguelmoga2002-alt/revenue-os-public-preview.git
cd revenue-os-public-preview
cp .env.example .env
```

**Windows PowerShell:**

```powershell
Copy-Item .env.example .env
```

### Step 2 — Environment Configuration

Configure `.env` with unique local credentials:

```dotenv
POSTGRES_PASSWORD=<unique-random-secret>
POSTGRES_APP_PASSWORD=<different-random-secret>

REVENUE_OS_MFA_ENCRYPTION_KEY=<private-fernet-key>
REVENUE_OS_REQUIRE_ADMIN_MFA=1
REVENUE_OS_ENV=development

REVENUE_OS_ENABLE_PIPEDRIVE_SYNC=0
REVENUE_OS_ENABLE_CRM_OAUTH=0
```

Use different randomly generated database passwords of at least 20 characters.

**Generate a Fernet encryption key:**

```bash
python -m pip install cryptography
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Replace the environment placeholders with the generated secrets.

Never commit `.env`, encryption keys or MFA provisioning secrets.

### Step 3 — Start Docker Compose

```bash
docker compose config --quiet
docker compose up -d --build app
```

**Verify services:**

```bash
docker compose ps
docker compose logs --tail 30 app
```

| Service | Responsibility |
|---|---|
| `postgres` | PostgreSQL 16 persistence |
| `migrate` | Alembic migrations and restricted-role provisioning |
| `app` | FastAPI runtime |

### Step 4 — Administrator Bootstrap

Create the initial development tenant and administrator:

```bash
docker compose run --rm migrate python bootstrap.py demo admin-demo
```

Enter an administrator password of at least 14 characters when prompted.

**Enroll administrator MFA:**

```bash
docker compose run --rm migrate python -m scripts.enroll_admin_mfa admin-demo
```

The enrollment command generates a secret TOTP provisioning URI (`otpauth://`) directly in the terminal.

Import the URI into an authenticator application or convert it locally into a QR code for scanning with Google Authenticator, Microsoft Authenticator or another TOTP-compatible application. The current script prints the URI; it does not render the QR code itself.

Enter the requested six-digit verification code to complete enrollment. Existing administrator sessions are revoked after successful setup.

**Security:** Treat the provisioning URI as a credential. Never share it in screenshots, logs, repositories or support tickets.

### Step 5 — Access the Development Environment

| Interface | Local URL |
|---|---|
| **CRM / Kanban** | http://127.0.0.1:8000/crm |
| **Focus / Sales Workspace** | http://127.0.0.1:8000/focus |
| **Migration Studio** | http://127.0.0.1:8000/ |
| **OpenAPI / Swagger UI** | http://127.0.0.1:8000/docs |
| **Health Check** | http://127.0.0.1:8000/health |

Application and PostgreSQL ports are bound to `127.0.0.1` by default.

The development Compose configuration must not be exposed directly to the public Internet.

### Step 6 — Run Tests

Install development dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Execute the test suite:

```bash
python -m pytest -q tests
```

PostgreSQL integration tests require an isolated database and the test configuration defined in the repository's CI workflow.

**CI Configuration:** [.github/workflows/ci.yml](.github/workflows/ci.yml)

### Current Technical Constraints

| Area | Current Limitation |
|---|---|
| Customer Data | Synthetic evaluation data only |
| AI / LLMs | Not integrated |
| Decision Engine | Deterministic business rules only |
| CRM Synchronization | Experimental adapters; no validated production sync |
| Email / Calendar | Not integrated |
| Slack | Not integrated |
| Call Intelligence | No production recording or transcription |
| Outbound Communication | No autonomous message sending |
| Performance | No published production-scale benchmarks |
| Compliance | Formal privacy and security validation pending |
| Operations | Production monitoring, backup restoration and SLA not established |

**Deployment classification:** Public Technical Preview — local technical evaluation only.

## 05. Growth Roadmap

Development priorities are tied to product validation, integration reliability and measurable commercial outcomes.

- **ICP Validation:** Interview Spanish B2B and B2C sales teams with 10–50 representatives. Validate administrative bottlenecks, CRM adoption friction and willingness to pay.
- **Sandbox Integrations:** Validate one CRM connector first, followed by permissioned email/calendar ingestion and Slack workflows. Require reliable synchronization, deduplication and audit trails.
- **Deterministic Decision Intelligence:** Retain explicit, explainable business rules as the authoritative decision layer. Evaluate future LLM-assisted signals using source attribution, timestamps, confidence metadata and strict human-in-the-loop validation.
- **Follow-up Automation:** Introduce evidence-grounded message drafts with mandatory human approval before external delivery.
- **Operational Readiness:** Verify tenant isolation, backup restoration, security controls, observability and legal requirements before customer-data pilots.
- **Commercial Validation:** Measure administrative minutes per lead (target: 20-30% less admin time per rep), time to follow-up, recommendation acceptance, CRM data completeness and weekly product adoption.

### AI Integration Policy

LLMs are reserved for future signal extraction, contextual summarization and draft generation.

They will not autonomously modify commercial commitments, fabricate CRM facts or bypass approval controls.

Every LLM-assisted workflow must support source traceability, evaluation and human correction.

**Current release: deterministic execution only. Generative AI remains on the technical roadmap.**

## 06. Feedback & Contributions

Revenue OS is looking for **Spanish sales teams, B2B or B2C, with 10–50 sales representatives** interested in product discovery interviews and technical demonstrations using synthetic data.

The objective is to validate actual sales workflows, CRM administration overhead and the operational value of deterministic next-action recommendations.

### Design Partner Interviews

Relevant profiles include:

- Sales Managers and Heads of Sales.
- Revenue Operations teams.
- SDR / BDR and Account Executive teams.
- B2B and B2C sales organizations with established CRM workflows.

To request a conversation, [open a GitHub Issue](https://github.com/miguelmoga2002-alt/revenue-os-public-preview/issues/new) with the title:

`Design partner`

Describe your sales workflow, current CRM stack and primary operational bottlenecks.

**Do not include customer records, personal data, API credentials or confidential CRM exports.**

### Technical Contributions

Reproducible bug reports, architecture feedback and technical review are welcome.

- Review [CONTRIBUTING.md](CONTRIBUTING.md) before submitting changes.
- Review [SECURITY.md](SECURITY.md) for private vulnerability reporting.
- Do not disclose vulnerabilities or credentials through public issues.

Repository visibility does not imply permission to reuse or redistribute proprietary code.

## 07. License

**Proprietary preview. No redistribution or commercial license granted at this stage.**

Revenue OS is publicly accessible for technical evaluation, architectural review and product feedback.

All rights are reserved unless an explicit license is published.

Public access to this repository does not grant rights to redistribute, sublicense, modify for redistribution or commercially exploit its source code.

---

**Release:** `1.1.0-rc.1` · Public Technical Preview

**Repository:** [revenue-os-public-preview](https://github.com/miguelmoga2002-alt/revenue-os-public-preview)

**Release Notes:** [PUBLIC_RELEASE_NOTES.md](PUBLIC_RELEASE_NOTES.md)

**Intended Use:** Local evaluation, architectural review and product discovery using synthetic data only.

---

Built by [Miguel Morales García](https://miguelmoralesgarcia.es). More projects: [automation-portfolio](https://github.com/miguelmoga2002-alt/automation-portfolio).
