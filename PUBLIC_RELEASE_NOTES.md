# Public preview notes — 2026-11-07

**Release type:** sanitized snapshot of the 1.1.0-rc.1 *laboratory branch*. This is **not** a `v1.1.0` release nor a production SaaS.

## Included in this preview
- FastAPI + SQLAlchemy + Alembic 001–012 + PostgreSQL RLS and least-privilege runtime role.
- Browser UI: Migration Studio, CRM/Kanban and Focus, plus Deal Workspace and individual deal URLs.
- Synthetic CSV fixtures, tests SQLite/PostgreSQL/Chromium, security and a public CI workflow adapted to a fresh repository.
- Docker Compose and the MFA/admin bootstrap sequence for local isolated environments.

## Intentional differences from private development
- New **public Git history only**; no private commit/PR history, internal development logs or non-public project documents.
- CI does **not** rely on the original repository's historical `v1.0.0` tag or private review scripts.
- No `.env` or API keys, backups, user data, local database or private pipeline configuration copied.
- LLMs, live CRM sync, email/calendar/Slack and phone calls are **not** validated integrations in this preview.

## Testing
Source snapshot came from the 2026-11-07 development release candidate. The public export uses its own CI and must be judged by the checks on **this repository**. Successful private CI cannot substitute for tests of the exported snapshot.

## Roadmap / go-to-market
Initial release is for architectural review, product feedback and synthetic demos only. A beta with customer data requires separate privacy, legal, security, operating and connector readiness checks.
