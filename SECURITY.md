# Security policy — Public Technical Preview

Revenue OS public preview is for **local evaluation with synthetic data only**, not production.

## Reporting a vulnerability
Please **do not publish exploit details, credentials or sensitive data in a public issue**. Use GitHub's **Report a vulnerability** / private vulnerability reporting if enabled in this repository, or contact the maintainer privately through their GitHub profile. Avoid uploading real customer records, tokens, login traces or session identifiers.

## Current scope
- FastAPI/Python application; PostgreSQL RLS, RBAC, per-record grants and admin MFA.
- Dependencies are scanned using pip-audit and Bandit in public CI.
- Inactive CRM integration adapters are experimental and must not be enabled with customer credentials.
- No pentest, bug bounty, SLO/SLA, SOC 2/ISO certification or GDPR compliance claim.

## If you accidentally commit a secret
Revoke/rotate it with its issuer immediately. Removing a line from a later commit does **not** erase the old Git history. Treat the full leaked history as exposed.
