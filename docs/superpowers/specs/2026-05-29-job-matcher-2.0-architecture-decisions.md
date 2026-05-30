# job-matcher 2.0 — Architecture Decision Record

> **Status:** REVISED v2 2026-05-29 — reviewed on PR #752 (`project-reviewer` + `inquisitor`); ADR-003/004/005/006/009
> updated below; re-review pending. (v1 "LOCKED" was premature.)
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

## v2 changes (from the PR #752 review — `project-reviewer` + `inquisitor`)

The aggressive review of v1 produced 3 blockers + a contradiction, all resolved here and in the design spec:

- **B1 — lifecycle vs `description_source` collision** → design §2.5 renames the new axis to
  `lifecycle` (`discovered`\|`scored`), orthogonal to the untouched `description_source`. (design D11)
- **B2 — dedup defeated the many-to-many fan-out** → design §4.1 makes dedup match-aware
  (`(listing, role)`), upsert-then-check. (design D12)
- **B3 — non-atomic migration on the autocommit pool** → design §3.3 wraps the seed in one explicit
  transaction gated by a `schema_version` sentinel + a post-commit assertion. (design D13)
- **PK contradiction** → ADR-006 (above) resolves to serial-int PK + separate UNIQUE `slug`; FKs target
  the int. (design D16)
- Plus resolved concerns: Cycle-1 feed reads a `MAX(matches.score)` roll-up (design D14); roles are
  soft-delete-only to remove the mid-run FK crash path (design D15); B-mode worst case = K scoring calls,
  accepted + budget-logged (design D17); ADR-003/004/005 scoped to new tables only (note under ADR-005);
  ADR-009 chokepoint + contract tests specified concretely and #580/#581/#582 stay open until they exist.

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

**Consequences.** Better query power and integrity on the new tables.

> **Scope note for ADR-003/004/005 (REVISED v2 — resolves the ADR-012 tension the `inquisitor` raised).**
> These type choices apply to **new tables/columns only** (Profile/Role/JobPreferences/Match, `base_salary`,
> `target_salary`, the JSONB arrays). **Existing `listings` columns (`REAL` salary, `TEXT` timestamps,
> `TEXT`-JSON) are explicitly OUT OF SCOPE for Cycle 0** — they are not converted in-place. Mixed-type
> reads are handled by per-table row mappers (design §3.2). IF a future cycle ever converts the legacy
> `listings` columns, that lossy in-place `ALTER ... USING` migration is exactly the "error-prone" trigger
> ADR-012 names — and would adopt migration tooling at that point. No lossy conversion of populated legacy
> columns ships in 2.0, so the deferred-conversion risk the review flagged does not materialize here.

## ADR-006 — ID strategy (REVISED v2 — resolves the PK/slug contradiction)

**Context.** v1 was self-contradictory: this ADR said "serial-int PKs" while the data-model §2 used a
string slug `id` as the `matches.role_id` FK target. `roles.id` cannot be both. The `inquisitor` flagged
this as the FK the whole join hangs on.

**Decision (resolved).** **The PK is the serial integer `id`, and it is the FK target.** A separate
**`slug TEXT UNIQUE`** column carries the human-stable display/URL key — it is **never** an FK target.
- `skills.id` (serial int) is what `roles.applicable_skills` stores (an `int[]`).
- `roles.id` (serial int) is what `matches.role_id` and `roles.default_resume_id` reference.
- `slug` is mutable-but-stable for display; renaming a skill/role changes `name`, not `slug`, and never
  touches an FK. Design spec §2.2/§2.3/§2.5/§3.2 are aligned to this (D16).

**Consequences.** One unambiguous FK target; predictable references; aligns with single-user (no UUIDs).

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

**Decision.** Make it **structural + tested**, not remembered. Concretely (REVISED v2 — the `inquisitor`
charged that v1 stated this as a checklist, not a testable contract):
1. **One render chokepoint** — a single function is the *only* way to produce feed HTML. Indicative
   signature: `render_feed(query: FeedQuery, *, fragment: bool) -> str` (full page wraps the same body
   the fragment returns). One shared `_feed_cards.html` partial `{% include %}`d by both; one shared
   `parse_feed_query(request) -> FeedQuery` covering the new `role` / `combined` / `lifecycle` dims.
2. **Literal CI contract tests:**
   - `test_feed_fullpage_and_fragment_card_markup_identical` — for a fixed `FeedQuery` fixture, the
     `#feed-content` subtree of `GET /` equals the entire body of `GET /feed/fragment` (byte-for-byte
     after normalizing whitespace).
   - `test_feed_card_markup_single_source` — the card markup string/macro appears in exactly one
     template file (grep assertion).
   - `test_fragment_refresh_preserves_query` — a fragment fetch with filters/role/view params returns the
     same filtered set as the full page with those params.

**Do NOT close #580/#581/#582 as "superseded" on the strength of this ADR** (the `inquisitor` flagged
this as laundering unsolved debt). Close them only when the chokepoint + the three tests above **exist in
code** (Cycle 2, #749).

**Consequences.** Violations fail CI instead of shipping; converts the fragility from human memory to
mechanical enforcement — but only once the tests land, which is why the issues stay open until then.

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
