# Security Policy

## Supported Versions

Only the current `main` branch is actively maintained. Older releases and forks are not supported.

| Version | Supported |
|---|---|
| `main` (latest) | ✅ |
| Older commits / forks | ❌ |

---

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security bugs.** Public issues are visible to everyone before a fix is available.

Use GitHub's private vulnerability reporting instead:

1. Go to the [Security tab](https://github.com/cbeaulieu-gt/job-matcher/security) of this repository
2. Click **"Report a vulnerability"**
3. Fill in the details and submit

This creates a private security advisory visible only to the repository maintainer.

### What to include

A useful report includes:

- A description of the vulnerability and the potential impact
- Steps to reproduce the issue
- The affected component (e.g. `app.py`, `config/`, a specific route)
- Any relevant configuration or environment details (with credentials removed)

### Response timeline

- **Acknowledgement**: within 7 days of receiving the report
- **Fix and coordinated disclosure**: within 90 days, depending on severity and complexity

If the issue is critical, a fix will be prioritised and released as soon as possible. You will be credited in the release notes unless you prefer to remain anonymous.

---

## Out of Scope

The following are generally not considered in-scope security issues for this project:

- Findings from automated scanners with no demonstrated exploitability in this specific project
- Vulnerabilities in third-party dependencies that are not exploitable through Job Matcher's usage of that dependency
- Issues that require physical access to the machine running the app
- Self-XSS or attacks that require the victim to already have admin access

---

## Notes on Threat Model

Job Matcher is designed as a **single-user, locally-hosted tool**. It is not intended to be exposed to the public internet. The Origin-based CSRF guard (`allowed_origins` in `config/config.json`) is the primary protection against LAN-adjacent misuse; the app assumes a trusted local environment.

If you are running Job Matcher in a multi-user or internet-exposed context, be aware that this is outside the supported deployment model.
