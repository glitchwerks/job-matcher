# job-matcher 2.0 — Architecture Decision Record

> **Status:** LOCKED 2026-05-29 (brainstorming session + OpenDesign handoff reconciliation).
> **Scope:** cross-cutting architectural decisions for the 2.0 initiative; they constrain every cycle.
> **Companion:** `2026-05-29-job-matcher-2.0-roles-foundation-design.md` (Cycle 0+1 spec).
> **Tracking:** Epic glitchwerks/job-matcher#751 · milestone #12 · cycles #747–#750.
>
> These decisions answer a series of "is the core architecture still correct, and what should change
> *now* while the schema/UI are being reset?" questions. Where a decision overrides the existing
> CLAUDE.md § Key design decisions, that is called out explicitly.

---

## Decision index

| ADR | Decision | Affects |
|---|---|---|
| 001 | All config in PostgreSQL (departs from flat-file profile) | Cycle 0 |
| 002 | Keep no-ORM (psycopg2); split `db.py` into a `db/` package | Cycle 0 |
| 003 | Money as `NUMERIC`/minor-units, not `REAL`; normalize currency+period | Cycle 0 |
| 004 | `TIMESTAMPTZ` for all timestamps | Cycle 0 |
| 005 | `JSONB` for JSON columns, not JSON-in-`TEXT` | Cycle 0 |
| 006 | ID strategy: serial-int internal PKs; stable slug keys where cross-referenced | Cycle 0 |
| 007 | Single-user; multi-user deferred to v3 (no tenancy now) | all |
| 008 | Keep HTMX; make it reversible via an API-first service layer | Cycle 2 |
| 009 | Rendering discipline made structural: one render chokepoint + CI contract tests | Cycle 2 |
| 010 | Explicit write contract: PATCH semantics (absent/null/value); never echo secrets | Cycle 0/2 |
| 011 | Secrets boundary: credentials in file/secret-store; DB holds routing/refs only | Cycle 0/2 |
| 012 | Migrations: stay hand-rolled for Cycle 0; adopt tooling only if pulled | Cycle 0 |
| 013 | Payload validation (pydantic) with the Cycle 2 API, not upfront | Cycle 2 |
| 014 | Resume file storage = local mounted volume, no object store | Cycle 3 |
| 015 | SSE live ingest stream: document proxy no-buffer; no new service | Cycle 2 |

---

## ADR-001 — All configuration moves into PostgreSQL

**Context.** Today the candidate/search config lives in flat files (`config/profile.json`,
`config/config.json`); CLAUDE.md § Key design decisions chose the flat file because it was "edited
manually as a whole unit; easier to version-control." 2.0 introduces Roles (need stable IDs + FK from
the `matches` join + live UI editing) and Job Preferences.

**Decision.** Profile, Job Preferences, Roles, Skills, and `matches` all become PostgreSQL tables.
The `/profile` and `/settings` surfaces become DB-backed CRUD. A one-time migration seeds the DB from
the existing flat files (see Cycle 0 spec §3.3). **Overrides the flat-file decision deliberately.**

**Consequences.** Enables relational integrity for many-to-many scoring and role lifecycle; removes
hand-edited JSON onboarding (replaced by UI — see ADR-009/deployment note). Credentials are the
exception (ADR-011).

## ADR-002 — Keep no-ORM (psycopg2); split `db.py` into a `db/` package

**Context.** CLAUDE.md chose "PostgreSQL, no ORM" because "schema is small and stable." 2.0 makes it
larger and relational (5 new tables, a join, sparse-override resolution, combined-view joins). `db.py`
already hand-serializes JSON and is 640+ lines (`db.py:639` `insert_listing`).

**Decision.** Stay on psycopg2 (no ORM). Manage growth by **splitting `db.py` into a `db/` package
by entity** (`db/profile.py`, `db/roles.py`, `db/matches.py`, …) with a thin row→dict mapping helper.
SQLAlchemy Core (query builder, not the full ORM) is the documented fallback **only if** hand-written
SQL becomes error-prone — not adopted pre-emptively (YAGNI).

**Consequences.** Avoids ORM dependency + migration; risk is module sprawl, mitigated by the package split.

## ADR-003 — Money as `NUMERIC`/minor-units, not `REAL`

**Context.** `listings.salary_min/max` are `REAL` (`db.py:339-340`). 2.0 adds salary math
(delta-vs-base, range midpoints), where float drift is a latent bug.

**Decision.** Store money as `NUMERIC` (or integer minor units) with explicit `currency` + `period`
(`base_salary`/`target_salary` carry `{amount, currency, period}`). Normalize currency/period before
computing deltas; ranges use the midpoint. No-salary postings are a red flag, never an auto-drop.

**Consequences.** Correct money math; a column-type choice that is free now, a data migration later.

## ADR-004 — `TIMESTAMPTZ` for all timestamps

**Context.** The schema is inconsistent — `created_at/fetched_at/posted_at/opened_at` are `TEXT`
(`db.py:346-362`) while `ingest_runs` uses `TIMESTAMPTZ` (`db.py:424-425`). Freshness gates
(hours filter, `max_days_old`) depend on correct, TZ-aware comparison.

**Decision.** All timestamp columns are `TIMESTAMPTZ`. Standardize during the Cycle 0 schema build.

**Consequences.** Correct sorting/filtering; removes a class of TEXT-timestamp footguns.

## ADR-005 — `JSONB` for JSON columns, not JSON-in-`TEXT`

**Context.** `db.py` hand-`json.dumps`/`loads` arrays into `TEXT`. 2.0 adds queryable arrays
(`role.applicable_skills`, `matches`, `overrides`, `preferences.locations`).

**Decision.** Use `JSONB` for these columns. Enables native validation + GIN-indexed containment
queries (combined view, "roles with skill X") that `TEXT` cannot.

**Consequences.** Better query power and integrity; trivial now, a conversion migration later.

## ADR-006 — ID strategy

**Decision.** Internal primary keys are serial integers (consistent with `listings.id`). Use stable
**slug-style string IDs only where an entity is cross-referenced by humans or other rows** — Skills
(referenced by `role.applicable_skills`) and Roles (referenced by `matches.role_id`, `default_resume_id`).
Don't mix conventions ad hoc.

**Consequences.** Predictable references; aligns with single-user (ADR-007) — no UUIDs needed.

## ADR-007 — Single-user; multi-user deferred to v3

**Context.** `api-surface.md §6.4` flags single-user-vs-multi-user as the most expensive
retrofit-if-wrong decision (everything would re-root under an account).

**Decision.** **Single-user.** Candidate and Job Preferences are singletons. **Multi-user is a v3
concern** — do NOT build account scoping, auth, or tenancy now. The value is the explicit decision,
not a feature. (Pushed back on building a speculative `owner_id` seam — omitted as YAGNI.)

**Consequences.** Simplest model; a future v3 multi-user effort is a known, accepted re-root cost.

## ADR-008 — Keep HTMX; make it reversible via an API-first service layer

**Context.** CLAUDE.md chose "HTMX, no JS framework" for a "read-mostly UI with two write actions."
2.0's UI is far richer (CRUD across many entities, inspector, combined view, live SSE stream), so the
original premise no longer holds. HTMX's front/back contract is inherently implicit (template names,
swap targets, trigger strings) — weaker than a typed-API + component framework.

**Decision.** **Keep HTMX** (the OpenDesign Shell-B prototype validates it; a framework would add
build tooling + a JSON API + a second render model). De-risk the choice by building the Cycle 2
backend as a **resource/service layer (JSON-capable) with HTMX as a thin server-side renderer over the
same layer.** A later pivot to a JS framework then reuses the API and discards only the Jinja layer —
bounded blast radius, decision stays reversible.

**Consequences.** No build tooling now; the service layer is the seam that keeps the option open and
is also the chokepoint for ADR-009/010.

## ADR-009 — Rendering discipline made structural (not a checklist)

**Context.** The v1 feed had a documented front/back rendering bug (`glitchwerks/job-matcher-pr#223`,
fixed by `#224`) that introduced a dual render-path (`/` vs `/feed/fragment`), leaving open coupling
debt: duplicated card markup (#580), filters lost on refresh (#581), duplicated query parsing (#582,
`web/feed.py:77-94` vs `:136-153`). "Discipline" as a doc checklist is fragile — newcomers don't see
it, shortcuts bypass it.

**Decision.** Make it **structural + tested**, not remembered:
1. **One render chokepoint** — a single feed-render service/function is the *only* way to produce feed
   HTML; one shared card partial; one shared query parser covering new `role`/`combined`/`state` dims.
2. **CI contract tests** — full-page vs fragment markup byte-identical for the same query; filters/role/
   view survive a refresh; card markup string exists in exactly one template.
Supersedes #580/#581/#582 (close as superseded when Cycle 2 lands — tracked in #749).

**Consequences.** Violations fail CI instead of shipping; converts ~80% of the fragility from human
memory to mechanical enforcement.

## ADR-010 — Explicit write contract: PATCH semantics; never echo secrets

**Context.** Full-page form POSTs over form-encoding can't distinguish "cleared this field" from
"unchanged / absent" — the LLM/Sources settings pages needed sentinel workarounds. 2.0 makes this
central: `Role.overrides` is a **sparse map** (absent = inherit, present = override, removed = revert)
— the exact absent/null/value trichotomy, now a core modeling primitive.

**Decision.** Writes use **explicit partial-update (PATCH) semantics** with a typed payload where
**absent = no change, explicit null = clear, value = set** (JSON expresses this; form-encoding can't).
Merge policy defined **once per resource** in the service layer and tested. `Role.overrides` stores
only overridden keys; "revert to shared" deletes the key. Secrets are **never echoed** to the client
(presence indicator only); omit = unchanged, explicit clear = remove, value = rotate.

**Consequences.** Eliminates the cleared-vs-empty bug structurally; makes the override UX correct;
drives scoped PATCH endpoints (HTMX still fine) rather than one giant full-form POST.

## ADR-011 — Secrets boundary

**Context.** Keys live in `config/providers.json` (LLM + source creds) with env overrides; the DB
password moved to Docker file-based secrets (commit #742). 2.0 moves `models`/`sources` *config* to DB.

**Decision.** **Keep credentials OUT of the DB.** DB stores operational/routing config and references
providers **by name**; actual keys stay in file/Docker-secret/env (resolution order: env/Docker-secret
→ `providers.json` fallback). A provider's full credential bundle stays together in the secret store
(no id-in-DB / key-in-file split). No encryption-at-rest, no external secret-store dependency (YAGNI).

**Consequences.** DB compromise ≠ key compromise; keys never in backups/dumps/logs; consistent with #742.

## ADR-012 — Migrations stay hand-rolled for Cycle 0

**Decision.** Continue the existing idempotent pattern (`CREATE TABLE IF NOT EXISTS` /
`ADD COLUMN IF NOT EXISTS` in `db.init_db()`, `db.py:368-405`). Adopt a migration tool (Alembic/yoyo)
**only if** the flat-file→DB migration or later schema changes prove error-prone — a watch item, not a
pre-emptive adoption.

**Consequences.** No new tooling now; revisit if hand-rolled migration against the production DB hurts.

## ADR-013 — Payload validation adopted with the Cycle 2 API

**Decision.** Introduce a validation layer (pydantic; precedent in `services/provider_schemas.py`)
**alongside** the Cycle 2 resource/PATCH API — it provides the contract + validation + the absent/null/
value distinction in one place. Not an upfront standalone project.

## ADR-014 — Resume file storage = local mounted volume

**Decision.** `Resume.content_ref` (Cycle 3) points at files on a **local Docker-mounted volume**, not
object storage / S3. Add the volume to the backup set. For a single-user tool this is the right weight;
no blob-store dependency.

## ADR-015 — SSE live ingest stream: document, don't add a service

**Decision.** The Cycle 2 live ingest stream uses SSE over the existing WSGI server (waitress in
Docker per CLAUDE.md § Deployment). **Document the required proxy setting** (`proxy_buffering off` /
streaming) and note the per-connection worker cost. No queue, worker container, or message broker —
single-user concurrency does not warrant them.

---

## Deployment posture (summary)

2.0 adds **no new infra services** — the topology stays Postgres + Flask + ingest CLI in the existing
Docker stack. Added surface is UX (a first-run/empty-state to replace hand-edited JSON onboarding —
helped by the existing PDF import, `services/pdf_import.py`), one resume volume (ADR-014), and one SSE
proxy note (ADR-015). New-user setup time is unchanged (acquiring API keys remains the real cost);
complexity moves from ~5 to ~5–6 on a 10-point scale. Update README onboarding steps when these land.

## References

- Current schema/types: `db.py:339-340` (REAL salary), `:346-362` (TEXT timestamps), `:368-405`
  (migration pattern + JSON-in-TEXT), `:424-425` (TIMESTAMPTZ precedent), `:639` (`insert_listing`).
- Feed render coupling: `web/feed.py:77-94` / `:136-153` (duplicated parsing); history
  `glitchwerks/job-matcher-pr#223` / `#224`; open `glitchwerks/job-matcher#580` / `#581` / `#582`.
- Secrets direction: commit #742 (Docker file-based secrets); CLAUDE.md § Config & profile.
- Open API decisions: `api-surface.md §6` (OpenDesign handoff, captured 2026-05-29).
- Tracking: epic #751, milestone #12, cycles #747–#750.
