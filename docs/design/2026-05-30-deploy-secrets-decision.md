# Deploy Secrets Decision

**Date:** 2026-05-30
**Issue:** #632
**Status:** Decided — no further action for now

## Context

The project's self-hosted Docker deployment uses two env files, `.env.prod`
and `.env.dev`, copied manually to the server via `scripts/deploy-remote-linux.sh`.
These files hold two categories of sensitive value:

1. **DB password** (`POSTGRES_PASSWORD` / `DATABASE_URL`): as of PR #742 (issue
   #644), the DB password is delivered to the web container exclusively as a
   Docker file-based secret mounted at `/run/secrets/db_password`
   (`docker-compose.prod.yml:L39-41, L75-77`; `docker-compose.dev.yml:L51-53,
   L87-89`). The compose comment at `docker-compose.prod.yml:L44-45` states:
   "DATABASE_URL is constructed by entrypoint.sh from POSTGRES_USER +
   /run/secrets/db_password + POSTGRES_DB. Do not set DATABASE_URL here."
   `POSTGRES_PASSWORD` remains in the env file for the `db` service, which
   cannot yet consume Docker secrets directly.

2. **Flask secret key** (`SECRET_KEY`): still set via env-file interpolation
   (`docker-compose.prod.yml:L46`):
   ```
   SECRET_KEY: '${SECRET_KEY:?SECRET_KEY must be set in .env.prod ...}'
   ```
   The live value lives in `.env.prod` on the server — a plaintext file
   protected only by filesystem permissions. `.env.prod.example` ships the
   placeholder `changeme_generate_with_python_secrets_token_hex_32`
   (`.env.prod.example:L12`), and the `deploy-prod` GHA preflight
   (`.github/workflows/deploy.yml:L204`) rejects that placeholder at CI time,
   preventing accidental deploys with an unset key.

The residual risk that motivated #632: both env files are plaintext on disk.
A compromised server account with read access to `/opt/job-matcher-pr/` can
read them directly.

## Options Considered

**(a) Status quo — Docker file-based secrets (already in place for DB password)**
Low operational weight. DB password is no longer in the interpolated env
string. `SECRET_KEY` remains in the env file; server-side filesystem
permissions are the only guard.

**(b) sops-encrypted env files**
Encrypts env files at rest; decrypts at deploy time using a key stored in a
secret manager or GPG keyring. Adds meaningful ops complexity for a
single-maintainer, self-hosted tool and requires key distribution.

**(c) Azure Key Vault + workload identity**
Secrets stored in Key Vault; VMs or containers authenticate with managed
identity (no credential in the env file). The natural fit if the project
migrates to Azure App Service or ACI. Eliminates plaintext secrets entirely
but requires the Azure deployment target.

## Decision

Stay on Docker file-based secrets for now.

- The DB password — the highest-value credential — is already off the plaintext
  env path (PR #742).
- `SECRET_KEY` remains in `.env.prod`; that is an accepted residual risk. The
  key signs Flask sessions; it does not gate database access. The GHA preflight
  ensures it is always explicitly set before a deploy succeeds.
- This is a single-user, self-hosted tool. The operational cost of sops or a
  cloud secret manager is not justified at current scale.

**Designated future path:** Azure Key Vault with managed identity, if and when
the deployment moves to Azure infrastructure. At that point the env-file
pattern should be retired entirely in favor of Key Vault references.

## Residual Gap

`SECRET_KEY` is still plaintext in `.env.prod` / `.env.dev` on the server.
This is acknowledged and accepted for the current deployment model. Mitigation
in place: the file is not committed to version control, server filesystem
permissions restrict access, and the GHA preflight blocks placeholder values.

## Revisit Trigger

- Migration to Azure (managed identity makes Key Vault nearly free to adopt).
- Going multi-user or multi-tenant (widens the blast radius of a compromised
  `SECRET_KEY`).
