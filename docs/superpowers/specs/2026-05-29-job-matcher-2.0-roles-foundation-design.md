---
title: job-matcher 2.0 — Cycle 0 (Foundation) + Cycle 1 (Roles ingest/scoring)
status: revised-v3 — review complete
touches:
  - db.py
  - ingest.py
  - web/feed.py
  - web/**
  - services/**
  - plugins/sources/**
  - job_sources/base.py
  - templates/**
  - conftest.py
  - tests/**
tracking: { epic: 751, milestone: 12, cycles: [747, 748] }
---

# job-matcher 2.0 — Cycle 0 (Foundation) + Cycle 1 (Roles ingest/scoring) — design spec

> **Status:** REVISED v3 — 2026-05-29, **review complete**. v1 → reviewed (`project-reviewer` +
> `inquisitor`) → v2 fixed the 3 blockers + PK → v2 re-reviewed (both confirmed those PASS) → v3 fixes
> the 3 *new* blockers the v2 fixes exposed (N1 dedup `RETURNING` idiom, N2 best-fit Match-row selection,
> N3 cross-process purge lock) + the concern set. Review thread on PR glitchwerks/job-matcher#752.
> **Tracking:** Epic #751 · milestone #12 · Cycle 0 #747 · Cycle 1 #748.
> **Scope:** Cycles 0 and 1 only. Cycle 2 (API + UI, #749) and Cycle 3 (Resumes, #750) get their own specs.

---

## 1. Background & intent

job-matcher today is a single-profile job *scorer*: one `config/profile.json` + one `config/config.json`
search query drive an ingest pipeline that scores listings against a single profile and stores them in
PostgreSQL. There is **no Role concept** and **no Resume concept** — "applied" is a boolean column on
`listings` (`db.py:359`).

2.0 introduces two new core domain concepts:

1. **Roles** — a single Profile targets **N roles** (e.g. Software Engineer, Data Engineer), each its
   own search + scoring lens. A listing may match multiple roles (**many-to-many**).
2. **Resumes** — a managed entity, tailorable to a job (Cycle 3 — out of scope here).

This spec covers the **foundation** (data model into PostgreSQL + migration) and the **ingest/scoring
overhaul** that makes the pipeline multi-role aware.

### Authority

- **The model in this spec is authoritative** (product of the 2026-05-29 brainstorming session); it
  supersedes the OpenDesign handoff docs where they differ.
- OpenDesign `ui-layout.md` is authoritative for **UI/IA** (Cycle 2).
- OpenDesign `data-model.md`, `api-surface.md`, `design-principles.md`, `roles-editor-rebuild.md` are
  **bridging references**, captured 2026-05-29 at
  `C:\Users\chris\AppData\Roaming\Open Design\namespaces\release-stable-win\data\projects\0115656f-3dfa-45ce-a2d7-e6857d2f2f6a\docs\`.

> **O4 (still open):** copy the four OpenDesign reference docs into the repo (`docs/design/`) so this
> spec cites them durably. External absolute paths are not durable citations. Pending user OK.

---

## 2. The three-category entity model (NORMATIVE)

`?` = optional/nullable. Types indicative; verify exact column types against the repo at build — EXCEPT
where ADR-003/004/005/006 fix a type, which are normative.

### 2.1 Profile (singleton) — "who the candidate is"

```
Profile {
  id:               int        // serial PK; singleton enforced via CHECK (id = 1)
  name:             str
  email:            str
  country:          str
  current_location: { label: str, lat?: num, lng?: num }   // home/base = distance ORIGIN
  current_role:     str
  residency:        { authorized_regions: str[], needs_sponsorship: bool }   // OUR model (gap in handoff)
  education:        Education[]
  primary_skills:   Skill[]        // shared bucket — see 2.2
  anti_preferences: str[]          // baseline (roles ADD to this — D2)
  scoring_notes:    str[]          // baseline (roles ADD to this — D1)
  base_salary:      { amount: num, currency: str, period: "year"|"hour" }   // anchor, NOT a floor
  defaults:         { seniority: str, preferred_industries: str[] }          // Tier-2 shared defaults
}
Education { degree_type: str, degree_field: str, school: str, graduation_year: int }
```

### 2.2 Skill — member of `Profile.primary_skills`

```
Skill {
  id:    int     // SERIAL PK — the cross-reference key (ADR-006). roles reference THIS.
  slug:  str     // UNIQUE, human-stable display key (mutable name → slug regenerated is NOT done;
                 //   slug is set once at create and stays; rename changes `name`, not `slug`)
  name:  str     // display name (was legacy `description`); mutable
  years: int     // (was legacy `years_active`)
}
// legacy `active` boolean DROPPED. No per-role weighting — emphasis via scoring_notes.
```

**PK/FK resolution (was the v1 ADR-006-vs-§2 contradiction):** the FK target is the **serial int `id`**.
`role.applicable_skills` stores **integer skill ids**, not slugs. The `slug` is a separate UNIQUE column
for human-stable display/URLs only — never an FK target. Same pattern for Role (2.3).

### 2.3 Role (N per profile) — "what the candidate is aiming for"

```
Role {
  id:       int          // SERIAL PK — FK target for matches.role_id (ADR-006)
  slug:     str          // UNIQUE display key
  name:     str
  color:    str          // identity color (Cycle 2 shell)
  active:   bool         // false = PAUSED (temporarily excluded from ingest; row & matches retained)
  archived: bool         // true = soft-deleted (hidden from UI + ingest; row & matches retained) — see 2.7

  // Tier 1 — target-defined
  search_what:   str
  prefilter:     { title_include: str[], title_exclude: str[] }
  threshold:     num
  scoring_notes: str[]             // per-role ADDITIONS (appended to Profile baseline)

  // per-role attributes
  anti_preferences:  str[]         // per-role ADDITIONS (appended to Profile baseline)
  target_salary:     num           // seeded from profile.base_salary, then editable
  applicable_skills: int[]         // BINARY refs → skills.id (serial ints)
  default_resume_id: int?          // Cycle 3

  // Tier 2 — sparse override (present key = override; absent = inherit)
  overrides: { seniority?: str, preferred_industries?: str[] }

  updated_at: timestamptz          // bumped on any edit — drives mid-run staleness check (2.7)
}
```

`location` / `radius` / `work_arrangement` / `job_types` are deliberately **absent** — global (2.4).

### 2.4 Job Preferences (singleton, global)

```
JobPreferences {
  id:               int        // serial PK; singleton via CHECK (id = 1)
  locations:        Location[]
  radius_km:        num        // SINGLE global radius
  work_arrangement: ("onsite"|"hybrid"|"remote")[]    // multi-select HARD PULL FILTER
  job_types:        ("contract"|"contract_to_hire"|"full_time"|"part_time"|"internship")[]  // HARD PULL FILTER
  max_days_old:     int?
  salary_mode:      "floor" | "display"
  floor_amount:     num?       // required when salary_mode == "floor"
}
Location { label: str, lat?: num, lng?: num }   // NO per-location radius
```

- `work_arrangement` / `job_types` / `max_days_old` are **hard pull filters** (exclude at ingestion).
- `salary_mode=="floor"` → drop postings below `floor_amount` at ingestion.
- `salary_mode=="display"` → no salary filter; feed shows delta-vs-`base_salary` (Cycle 2): neutral below
  base (`−10% vs base`), green above (`+20% vs base`).
- **No extracted salary** = red flag, never auto-dropped (Cycle 2 may offer an opt-in hide filter).

### 2.5 Listing + Match (the posting and its per-role scores)

```
Listing {                          // existing `listings` table, extended
  ...all existing columns (title, company, location, salary_*, description, source, source_id,
                           description_source, seen, …)
  lifecycle:         "discovered" | "scored"   // NEW — see naming note below
  applied_resume_id: int?                        // Cycle 3 link-only
  // legacy scoring columns (score, verdict, matched_skills, …) are RETAINED but INERT in 2.0 —
  //   read authority moves to `matches` in Cycle 1 (see §4.6); kept for backfill provenance/rollback.
}

Match {                            // NEW join — one row per (listing, role) scored pair
  listing_id:     int  fk → listings.id  ON DELETE CASCADE
  role_id:        int  fk → roles.id     // roles are soft-deleted (2.7), so this never dangles
  score:          num
  matched_skills: jsonb            // ADR-005
  missing_skills: jsonb
  concerns:       jsonb
  verdict:        str
  model_used:     str
  tokens_input:   int
  tokens_output:  int
  overridden_via: jsonb            // Tier-2 overrides applied (Combined-view marker, Cycle 2)
  scored_at:      timestamptz
  PRIMARY KEY (listing_id, role_id)
}
```

**Naming note — `lifecycle`, NOT `state='snippet'` (was a v1 BLOCKER).** The repo already has
`listings.description_source` (`'full'|'snippet'`, `db.py:363`) meaning *the description text came from a
short API snippet vs a full scrape* — and that listing **has been scored**. The live `/snippets` route +
`db.get_snippet_feed()` (`db.py:880-933`) surface `description_source='snippet' AND scored`. The new axis
means something different — *not yet LLM-scored* — so it is a **separate column named `lifecycle`** with
values `discovered`|`scored`. The two axes are orthogonal and both retained:

| `description_source` (existing, unchanged) | `lifecycle` (new) | meaning |
|---|---|---|
| `full` / `snippet` | `discovered` | fetched, not yet scored (no Match rows) |
| `full` | `scored` | scraped full JD, scored |
| `snippet` | `scored` | scored from a short API description (today's `/snippets`) |

The `/snippets` route + `get_snippet_feed()` keep filtering on `description_source='snippet'` (now also
`lifecycle='scored'`). No vocabulary collision. The Cycle-2 "awaiting scrape/score" UI keys off
`lifecycle='discovered'`.

### 2.6 Entity-relationship summary

```
Profile (1) ──owns──> Skill (*)            Profile (1) ──owns──> Role (*)
Role (*) ──applicable_skills (int refs)──> Skill (*)
Listing (1) ──< Match >── (1) Role          (many-to-many; Match is the join)
JobPreferences (1) ──hard-filters──> Listing ingestion
Profile.base_salary ──derived %──> feed delta (Cycle 2)
Resume / Application — DEFERRED to Cycle 3 (applied_resume_id is link-only)
```

### 2.7 Role lifecycle & ingest concurrency (was a v1 CONCERN)

- **Soft-delete only (decision 3A).** UI "delete" sets `archived=true`; the row and `id` **never**
  disappear, so `matches.role_id` never dangles and a mid-run Match write can't hit a missing-FK crash.
  `active=false` = paused (temporary); `archived=true` = retired (permanent-ish, hidden). Archived roles
  are excluded from ingest and the UI but their historical Matches stay valid.
- **Hard purge** (truly delete a role + its Matches, `ON DELETE CASCADE`) is a separate explicit Admin
  action, **gated by a cross-process DB signal — NOT `ingest_control._ingest_running()`** (which only
  sees web-spawned subprocesses and is blind to `python ingest.py` cron/manual runs, the primary entry
  point — N3). Use a **PostgreSQL advisory lock** (`pg_advisory_lock`) that both the ingest run
  (acquired at run start, released at finish) and the hard-purge acquire; the purge refuses if it can't
  get the lock. Equivalently/additionally, check `ingest_runs` for a `status='running'` row (written by
  both CLI and web via `db.create_ingest_run`, `db.py:1207`). Not exposed as routine UI delete.
- **Run-start snapshot + staleness.** Ingest snapshots active roles at run start (§4.7). If a role's
  `updated_at` changes between snapshot and a Match write for that role, ingest logs a staleness warning
  (the run used the snapshot config; the next run picks up edits). No mid-run config reload.

---

## 3. Cycle 0 — Foundation: schema + migration

### 3.1 Current state (captured)

- `listings`: `db.py:332-366`; scoring in columns (`db.py:348-358`); `description_source` at `:363`.
- Idempotent migration pattern: `ADD COLUMN IF NOT EXISTS` loop, `db.py:368-382`; single-statement
  guarded migration precedent (#114) `db.py:388-405`.
- Connection pool is **autocommit=True** (`db.py:297-308`); `_Conn.commit()` exists for explicit txns.
- `ingest_runs`: `db.py:421-435` (per-run `fetched/filtered/scored/failed_count`). Geocache `:410-416`.
- Candidate fields: `config/profile.example.json`. Search/prefilter: `config/config.example.json`
  (`prefilter.require_contract_time` ∈ {full_time, part_time}; `require_contract_type` ∈ {permanent, contract}
  — verified `config.example.json:25-26`, matched in `ingest.py:394-407`).

### 3.2 New tables (added to `db.init_db()`)

| Table | Shape | Notes |
|---|---|---|
| `schema_version` | `(version int PK, applied_at timestamptz)` | NEW — migration sentinel; see §3.3 |
| `profile` | singleton, `CHECK (id = 1)` | Profile (2.1); JSONB for `education/anti_preferences/scoring_notes/defaults/residency/base_salary` |
| `skills` | `id SERIAL PK`, `slug TEXT UNIQUE`, `name`, `years` | FK target = `id` |
| `job_preferences` | singleton, `CHECK (id = 1)` | JobPreferences (2.4); JSONB for arrays/locations |
| `roles` | collection, `id SERIAL PK`, `slug UNIQUE` | Role (2.3); JSONB for `prefilter/scoring_notes/anti_preferences/applicable_skills/overrides`; `active/archived/updated_at` |
| `matches` | join, PK `(listing_id, role_id)` | Match (2.5); FK `listing_id`→`listings.id` ON DELETE CASCADE, `role_id`→`roles.id`; indexes on `role_id`, `listing_id` |

`listings` additions: **`lifecycle TEXT NULL`** (nullable at `ADD COLUMN` — NOT `DEFAULT 'scored'`; see
§3.3 for why) and `applied_resume_id INTEGER NULL`.

**Table-creation order (was a v1 CONCERN):** `init_db()` must create parents before children —
`skills`, `roles`, `job_preferences`, `profile` before `matches` (which FKs `roles` + `listings`).
An integration test asserts `init_db()` succeeds against a **completely empty** DB.

**`db/` package + JSONB mappers (ADR-002/005):** the `db/` package split ships a `db/__init__.py` that
re-exports all existing public symbols (callers' `import db` / `from db import …` keep working — no
caller churn in Cycles 0–1). Each entity module owns its row mapper; the legacy TEXT-JSON
`_deserialise_row` is **not** reused on JSONB tables (psycopg2 returns JSONB as Python objects already) —
mixed-type joins (e.g. `listings` TEXT-JSON × `matches` JSONB) must use per-column-aware mapping.

**`ingest_runs` multi-role accounting (was a v1 CONCERN):** add `matches_written INT`, `roles_processed
INT`; `finish_ingest_run()` reports them. "scored" stays = listings transitioned to `lifecycle='scored'`.

**Legacy scoring columns (O1 — RESOLVED): retained but inert.** Keep `listings.score/verdict/...`
through 2.0 for backfill provenance + rollback; **nothing reads them after Cycle 1** (feed reads
`matches`, §4.6). Dropping them is a post-2.0 cleanup issue. Type conversions of existing `listings`
columns (REAL/TEXT) are **out of scope** here (see ADR-003/004/005 scope note).

### 3.3 Migration (one-time, ATOMIC, idempotent) — was a v1 BLOCKER

Run inside `db.init_db()`, gated by `schema_version`. The seed runs in **one explicit transaction**
(`raw.autocommit=False` for the block; `conn.commit()` once at the end — the seam at `db.py:297-308`):

```
# prereq (schema creation, §3.2): ADD COLUMN lifecycle TEXT NULL   (nullable, NO default — see below)
if schema_version < 1:
  conn._conn.autocommit = False                 # explicit txn on this checked-out connection
  try:
    BEGIN
      1. Seed Profile (skills: description→name + slug + serial id, drop `active`; education/
         anti_preferences/scoring_notes → baselines; seniority/preferred_industries → defaults;
         location.center → current_location.label; base_salary/residency/current_role → empty defaults)
      2. Seed JobPreferences (radius_km; max_days_old; salary_min → salary_mode='floor'+floor_amount;
         require_contract_* → job_types/work_arrangement per the table below)
      3. Seed one Role (search_what/prefilter/threshold; active=true, archived=false;
         applicable_skills = ALL skill ids → behavior unchanged; empty additions/overrides)
      4. Backfill matches: one Match per seen=1 listing vs the seeded Role (copy score/verdict/skills/
         concerns/model/tokens).
         Set lifecycle for ALL rows IN THIS TXN: seen=1 → 'scored'; seen=0 → 'discovered' (O3).
         Then ALTER TABLE listings ALTER COLUMN lifecycle SET NOT NULL   # safe: every row now populated
      5. ASSERT COUNT(matches) == COUNT(listings WHERE seen=1); on mismatch RAISE → rolls the txn back
         (the assertion is INSIDE the txn — a corrupt migration is NEVER marked done)
      6. INSERT schema_version (1, now())
    COMMIT       # all-or-nothing: interrupted run rolls back; re-run repeats cleanly
  finally:
    conn._conn.autocommit = True                # RESTORE before the pool recycles this connection
```

Why these specifics (from the v2 review):
- **`lifecycle` is added nullable, then backfilled + `SET NOT NULL` inside the txn** — not
  `ADD COLUMN … DEFAULT 'scored'` (which would stamp `seen=0` rows `'scored'` via DDL outside the txn;
  a crash before step 4 would leave them permanently mislabeled).
- **The guard is `schema_version`, not "profile row exists"** — an interrupted run (no commit) leaves
  `schema_version` unset, so the next `init_db()` re-runs the whole seed cleanly. No half-migrated lock.
- **The consistency assertion is INSIDE the txn** (before the `schema_version` insert) — a mismatch
  rolls everything back rather than committing a corrupt state and marking it done-forever.
- **`autocommit` is restored to `True` in a `finally`** — the pooled connection (`db.py:297-308`) is
  recycled by other callers that expect autocommit mode; not restoring it silently breaks their writes.
- **Migration #114 ordering:** the existing `#114` reclassify (`db.py:388-405`, bare `UPDATE` under
  autocommit) runs at `init_db()` level outside this txn. On a fresh DB it no-ops (no listings); on an
  upgrade it may run before/after the seed — benign (it only touches `description_source`, never
  `lifecycle`/`matches`). The seed must run after the new tables exist (FK order, §3.2).

**Contract-type mapping (O5 — RESOLVED, enumerated):**

| legacy (`config.json`) | → new | note |
|---|---|---|
| `require_contract_time = "full_time"` | `job_types += [full_time]` | |
| `require_contract_time = "part_time"` | `job_types += [part_time]` | |
| `require_contract_type = "contract"` | `job_types += [contract]` | |
| `require_contract_type = "permanent"` | (no member) | "permanent" = NOT-contract; represented by full/part-time presence, not a `job_types` entry |
| field `null`/absent | no constraint on that axis | |
| any unrecognized value | **log warning, drop (no filter)** | never silently over/under-filter |

Because the legacy two-axis (time × type) model collapses into one `job_types` multiselect (lossy), a
**first-run UI confirmation** (Cycle 2) prompts the user to verify migrated job-type prefs.

### 3.4 Cycle 0 acceptance criteria

- [ ] `init_db()` creates all tables on a **completely empty** DB (FK order correct) and is a no-op on re-run.
- [ ] Migration seeds Profile + JobPreferences + one Role from flat files; backfills `matches` for `seen=1`.
- [ ] **Interrupted-migration test:** kill between step 1 and step 5 (no commit) → re-run completes the seed
      (no half-migrated lock); post-commit assertion `COUNT(matches)==COUNT(seen=1)` holds.
- [ ] `seen=0` → `lifecycle='discovered'`; `seen=1` → `lifecycle='scored'`.
- [ ] Contract-type mapping table applied; unrecognized value logs + drops the filter.
- [ ] JSONB round-trip; `conftest.py` test-DB guard respected (scoped DELETE by `source_id` prefix, no TRUNCATE).

---

## 4. Cycle 1 — Roles ingest/scoring overhaul

### 4.1 Plugin-major loop with match-aware dedup (dedup fix was a v1 BLOCKER)

Outer per-source/plugin loop is **unchanged** (`ingest.py` is DB-aware: `import db` `:41`; dedup `:1383`;
`insert_listing` `:639`). Roles are an inner fan-out. **Dedup is now match-aware** — the old boolean
`listing_exists()` skip (`ingest.py:1383-1394`) would short-circuit the whole listing and never score a
2nd role; it is replaced by upsert-listing-then-check-match-per-role:

```
# upsert_listing(source, source_id, redirect_url, fields) -> listing_id:
#   1. URL cross-source dedup (RETAINED from today): if listing_exists_by_url(redirect_url) → reuse that id
#   2. INSERT ... ON CONFLICT (source, source_id) DO NOTHING RETURNING id
#   3. if NO row returned (conflict path — listing already existed): SELECT id WHERE (source, source_id)
#      ── the SELECT is MANDATORY (N1): ON CONFLICT DO NOTHING returns NO row on conflict, so the
#         RETURNING id is null exactly in the pre-exists case the fan-out fix targets.
# score_role(listing_id, role): INSERT INTO matches (...) ON CONFLICT (listing_id, role_id) DO NOTHING
#      ── idempotent: a concurrent or re-run write cannot double-insert a (listing, role) Match.

for each active SOURCE/PLUGIN:
  if plugin.supports_role_query:                       # A-capable
    for each active ROLE (from run-start snapshot):
      fetch role-scoped query (role.search_what + filters)
      per listing:
        hours → geo/residency → id = upsert_listing(...)
        if Match(id, role.id) absent:
            prefilter(role) → scrape JD (once, cached) → score(role) → score_role(id, role)
        # else: already scored for this (listing, role) → skip
  else:                                                # B-only global list
    fetch global list
    per listing:
      hours → geo/residency → id = upsert_listing(...) → scrape JD (once)
      for each active ROLE (snapshot) whose prefilter passes AND Match(id, role.id) absent:
          score(role) → score_role(id, role)
```

Key change: **dedup is per `(listing, role)` Match, not per listing row.** A role added to a pre-existing
corpus now scores against existing listings (the bug v1 shipped). Scrape still happens once per listing.
Per-source short-circuit semantics (401/403 drops a provider; transient skips the listing) preserved.

### 4.2 Plugin capability flag

Add `supports_role_query: bool` (default `False` → B-mode) as a class attribute on `JobSource`
(`job_sources/base.py`), documented in `docs/PLUGIN_DEVELOPMENT.md` + `plugins/sources/_template/`.

### 4.3 Per-role prefilter (cost gate) + B-mode cost ceiling (decision 4)

A listing is LLM-scored for a role only if it passes that role's `prefilter.title_include/exclude`
(cheap title-substring gate; zero API cost for non-matches). **Accepted worst case:** a listing passing
**K of N** roles' prefilters incurs **K scoring calls** (scrape is shared; scoring is not). At ~500
listings/run × overlapping roles this is K× today's cost — **accepted for single-user scale (decision 4)**.
Mitigation: ingest logs a **per-run scoring-call + estimated-cost budget line**; no hard cap in Cycle 1.
A-capable sources also multiply *fetch* calls by role count → plugins must respect per-source rate limits
across the role loop (note in `PLUGIN_DEVELOPMENT.md`).

### 4.4 Geo + residency filter

- Distance origin = `Profile.current_location`; radius = `JobPreferences.radius_km`; pass if within radius
  of **any** willing `JobPreferences.locations[]`.
- **Remote** postings bypass the distance filter but are checked against `Profile.residency`
  (`authorized_regions`/`needs_sponsorship`); residency-incompatible remote postings filtered.
- `geocode_fallback` carries over (`pass` default).

### 4.5 Effective scoring inputs per Role

| Input | Resolution |
|---|---|
| skills | `primary_skills.filter(s.id ∈ role.applicable_skills)` |
| seniority | `role.overrides.seniority ?? profile.defaults.seniority` |
| preferred_industries | `role.overrides.preferred_industries ?? profile.defaults.preferred_industries` |
| anti_preferences | `profile.anti_preferences ++ role.anti_preferences` (concatenate) |
| scoring_notes | `profile.scoring_notes ++ role.scoring_notes` (concatenate) |
| target_salary / threshold | per-role |

Response schema unchanged (`score, matched_skills, missing_skills, concerns, verdict`); written to a
`Match` row.

**Effective-skills resolution contract:** load the role's `applicable_skills` (JSONB `int[]`) + the
candidate's `primary_skills`, filter in Python by id-membership (single-user scale; simplest). If done
in SQL, bind `WHERE id = ANY(%s::int[])`. `applicable_skills` is a JSONB array → **no DB-level FK** to
`skills.id`; a hard skill purge (rare) must application-level scrub `applicable_skills` arrays — a
purge-time cleanup, never a hot-path concern.

### 4.6 Feed read authority during Cycle 1 (decision 2B) — was a v1 CONCERN

`matches` is authoritative the moment it exists. Cycle 1 changes `get_feed()`/`get_snippet_feed()` to
read from `matches` instead of `listings.score`. **It selects the best-fit Match ROW per listing, not a
`MAX(score)` scalar (N2)** — a scalar max can't tell the card which role's `matched_skills`/`verdict`/
`model_used` to render (`templates/_card.html` shows them), and isn't legal SQL beside non-aggregated
columns. Use Postgres `DISTINCT ON`:

```sql
SELECT DISTINCT ON (m.listing_id) l.*, m.role_id, m.score, m.matched_skills, m.missing_skills,
                                   m.concerns, m.verdict, m.model_used
FROM listings l
JOIN matches  m ON m.listing_id = l.id
JOIN roles    r ON r.id = m.role_id AND r.active AND NOT r.archived   -- exclude paused/archived
WHERE l.lifecycle = 'scored'
  AND <get_feed: l.description_source='full' | get_snippet_feed: l.description_source='snippet'>
  -- + existing feed filters (remote_only, search, job_type, sort, min_score/threshold)
ORDER BY m.listing_id, m.score DESC;   -- best-fit role wins per listing
```

- **`get_snippet_feed()`** uses the same join with `description_source='snippet'` (N1 from the v2
  review — its join shape is now explicit, not left to the implementer).
- **All-archived / no-active-match listings** have no row from this join → **excluded from the feed**
  (the `JOIN` + `r.active` predicate drops them; this is the intended behavior, stated explicitly).
- The best-fit row is exactly what the Cycle-2 "best-fit" badge uses; the multi-role "also matches"
  pills + Combined view layer on top in Cycle 2 — forward-compatible, not throwaway.
- Legacy `listings.score`/`verdict`/… are **not** read after this change (inert per §3.2). No dual-write,
  no split-brain window.

### 4.7 Snippets (store-then-score) + control surface

- `lifecycle='discovered'` = stored, not yet scored (no Matches). Promotion: scrape-if-needed → score →
  write Matches → `lifecycle='scored'`. **Inline-score remains the default** (O2 RESOLVED); a posting
  becomes `discovered` only when scoring is deferred/failed. Failed-scoring carries a `score_error TEXT`
  on the listing so the Cycle-2 UI can distinguish "awaiting score" from "score failed."
- **Role active toggle** — run processes only `active=true AND archived=false` roles (snapshot at start, §2.7).
- **`--role "<slug|name>"`** — narrow a run to one role (DB lookup).
- **`--rescore`** — rebuild `matches` across active roles; per-Role "re-score just this role" action for
  the Cycle-2 UI when a single role is edited.

### 4.8 Cycle 1 acceptance criteria

- [ ] A-capable + B-only sources both produce correct per-role `Match` rows.
- [ ] **Role-added-to-existing-corpus test:** score Role A, then add Role B and re-run → Role B gets
      Matches for pre-existing listings (guards the dedup fix; do NOT test only the single-fresh-run path).
- [ ] A listing matching two roles → two `Match` rows, one `listings` row.
- [ ] Per-role prefilter gates LLM calls (prefilter-fail → 0 scoring calls); per-run cost budget logged.
- [ ] Remote-residency filter behaves (incompatible remote dropped; compatible passes outside radius).
- [ ] `lifecycle` discovered→scored promotion; `lifecycle` × `description_source` coexist on one row correctly.
- [ ] **Mid-run role edit/delete test:** archiving a role mid-run does not crash Match writes; staleness warning logged.
- [ ] Feed selects the **best-fit Match row** (`DISTINCT ON`); the feed card renders that role's
      `matched_skills`/`missing_skills`/`verdict`/`model_used`; listings whose only matches are
      paused/archived roles are **excluded** (N2).
- [ ] **Dedup-on-conflict test (N1):** `upsert_listing` returns a valid `id` on the *conflict* path
      (pre-existing listing), not null; a re-run does not double-insert Matches (`ON CONFLICT DO NOTHING`).
- [ ] **Hard-purge safety test (N3):** a purge is refused while a `python ingest.py` run holds the
      advisory lock / has an `ingest_runs.status='running'` row (not just web-spawned ingest).
- [ ] **Full CI gate green** (mirror CI, not a subset): `ruff check`, `pytest` (test DB), plus any
      `black --check` / `mypy` the workflow runs. State expected test count at plan time.

---

## 5. Cross-cycle decisions log

| # | Decision | Rationale |
|---|---|---|
| D1 | `scoring_notes` = Profile baseline + per-Role additions | general baseline + role specifics |
| D2 | `anti_preferences` = Profile baseline + per-Role additions | some universal, some role-specific |
| D3 | Salary = `base_salary` anchor + per-Role `target_salary` + `salary_mode` (floor\|display) | richer than a flat floor |
| D4 | `residency` on Profile + remote-residency filter | gap in handoff docs |
| D5 | All config in PostgreSQL | UI CRUD, FK integrity, role lifecycle |
| D6 | Plugin-major loop; roles inner fan-out | preserves proven structure |
| D7 | Per-source `supports_role_query`; B-mode default | not all sources allow server-side filtering |
| D8 | Many-to-many via `matches` join | preserves cross-role signal |
| D9 | Scrape once per listing | cost |
| D10 | Per-application resume = link only; Application deferred | lightest weight |
| **D11** | Lifecycle axis named `discovered`\|`scored`, separate from existing `description_source` | avoids the vocabulary collision (v1 blocker) |
| **D12** | Dedup is match-aware (`(listing,role)`), not row-existence | makes the many-to-many fan-out actually work (v1 blocker) |
| **D13** | Migration is one atomic transaction gated by `schema_version` | interrupted runs roll back, not half-lock (v1 blocker) |
| **D14** | Cycle-1 feed reads `MAX(matches.score)` roll-up (2B) | `matches` authoritative immediately; no split-brain |
| **D15** | Roles soft-delete only (`archived`); hard-purge is ingest-quiescent admin action (3A) | no mid-run FK crash; matches history preserved |
| **D16** | PK = serial int; `slug` is a separate UNIQUE display key; FKs target the int | resolves the v1 PK/slug contradiction |
| **D17** | B-mode worst case = K scoring calls for K matched roles; accepted + budget-logged (no hard cap) | single-user scale |
| **D18** | Dedup: `RETURNING` is null on conflict → mandatory `SELECT id`; Match insert `ON CONFLICT DO NOTHING`; URL dedup retained | N1 (v2-review) |
| **D19** | Cycle-1 feed selects best-fit Match **row** via `DISTINCT ON … ORDER BY score DESC` (not a scalar); all-archived listings excluded | N2 (v2-review) |
| **D20** | Hard-purge gated by a cross-process DB signal (advisory lock / `ingest_runs.status`), not in-process `_ingest_running()` | N3 (v2-review) |
| **D21** | Migration: `lifecycle` added nullable; backfill + `SET NOT NULL` + consistency assertion all INSIDE the txn; `autocommit` restored in `finally` | migration safety (v2-review) |

## 6. Open items

- **O4** — Copy OpenDesign reference docs into `docs/design/` for durable citations (§1). Pending user OK.
  *(O1, O2, O3, O5 resolved in v2 — see §3.2/§4.7/§3.3.)*

## 7. References

- Schema/migration: `db.py:332-405` (listings + migrations), `:297-308` (autocommit pool + commit seam),
  `:363` (`description_source`), `:421-435` (`ingest_runs`), `:880-933` (`get_snippet_feed`).
- Ingest: `ingest.py:41` (`import db`), `:1196` (`init_db`), `:1383-1394` (boolean dedup being replaced),
  `:394-407` (contract-type prefilter), `:639` (`insert_listing`).
- Contract-type values: `config/config.example.json:25-26`.
- Feed render coupling (Cycle 2): `web/feed.py:77-94` / `:136-153`; history `glitchwerks/job-matcher-pr#223`/`#224`;
  open `glitchwerks/job-matcher#580`/`#581`/`#582`.
- Review of v1: PR #752 (`project-reviewer` + `inquisitor`, 2026-05-29).
- OpenDesign handoff (captured 2026-05-29): `ui-layout.md` (UI authority) + `data-model.md`/`api-surface.md`/
  `design-principles.md`/`roles-editor-rebuild.md` (bridging).
- Tracking: epic #751, milestone #12, cycles #747–#750.
