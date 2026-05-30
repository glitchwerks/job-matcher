# job-matcher 2.0 — Cycle 0 (Foundation) + Cycle 1 (Roles ingest/scoring) — design spec

> **Status:** model LOCKED 2026-05-29. Awaiting user review of this spec before plan-writing.
> **Tracking:** Epic glitchwerks/job-matcher#751 · milestone #12 · Cycle 0 #747 · Cycle 1 #748.
> **Scope of this spec:** Cycles 0 and 1 only (the foundation + the ingest/scoring overhaul).
> Cycle 2 (API + UI, #749) and Cycle 3 (Resumes, #750) get their own specs.

---

## 1. Background & intent

job-matcher today is a single-profile job *scorer*: one `config/profile.json` (the candidate) plus
one `config/config.json` search query drive an ingest pipeline that scores listings against a single
profile and stores them in PostgreSQL. There is **no Role concept** and **no Resume concept** —
"applied" is just a boolean column on `listings` (`db.py:359`).

2.0 introduces two new core domain concepts:

1. **Roles** — a single Profile targets **N roles** (e.g. Software Engineer, Data Engineer), each its
   own search + scoring lens. A listing may match multiple roles (**many-to-many**).
2. **Resumes** — a managed entity, tailorable to a job (Cycle 3 — out of scope here).

This spec covers the **foundation** (the data model moved into PostgreSQL + migration) and the
**ingest/scoring overhaul** that makes the pipeline multi-role aware.

### Authority

- **The model in this spec is authoritative** — it is the product of the brainstorming session
  (2026-05-29) and supersedes the OpenDesign handoff docs where they differ.
- OpenDesign `ui-layout.md` is authoritative for **UI/IA** (Cycle 2).
- OpenDesign `data-model.md`, `api-surface.md`, `design-principles.md`, `roles-editor-rebuild.md`
  are **bridging references**, captured 2026-05-29 at
  `C:\Users\chris\AppData\Roaming\Open Design\namespaces\release-stable-win\data\projects\0115656f-3dfa-45ce-a2d7-e6857d2f2f6a\docs\`.

> **Recommended follow-up (flagged, not done):** copy the four OpenDesign reference docs into the
> repo (`docs/design/`) so this spec can cite them durably. External absolute paths are not durable
> citations per the project's "Cite Sources" discipline. Pending user approval.

---

## 2. The three-category entity model (NORMATIVE)

The model divides into three top-level categories the user navigates, plus the join + posting
entities. `?` = optional/nullable. Types indicative; **verify exact types against the repo at build**.

### 2.1 Profile (singleton) — "who the candidate is"

```
Profile {
  // identity
  name:             str
  email:            str
  country:          str
  current_location: { label: str, lat?: num, lng?: num }   // home/base = distance ORIGIN
  current_role:     str            // what they do today (≠ target roles)
  residency:        {                                  // ← OUR model (gap in handoff docs)
    authorized_regions: str[]      // e.g. ["US"] — where the candidate may legally work
    needs_sponsorship:  bool
  }
  // identity facts
  education:        Education[]
  primary_skills:   Skill[]        // shared bucket {id,name,years} — see 2.2
  // scoring baselines (apply to ALL roles; roles ADD to these — OUR model, decision #1/#2)
  anti_preferences: str[]          // baseline list
  scoring_notes:    str[]          // baseline list
  // compensation anchor (NOT a floor) — seeds role.target_salary + drives feed delta
  base_salary:      { amount: num, currency: str, period: "year"|"hour" }
  // Tier-2 shared defaults a role may override
  defaults:         { seniority: str, preferred_industries: str[] }
}

Education { degree_type: str, degree_field: str, school: str, graduation_year: int }
```

### 2.2 Skill — member of `Profile.primary_skills`

```
Skill {
  id:    str     // STABLE id — roles reference skills by id (text key too fragile)
  name:  str     // (was legacy `description`)
  years: int     // (was legacy `years_active`)
}
// legacy `active` boolean is DROPPED. No per-role weighting anywhere — emphasis via scoring_notes.
```

### 2.3 Role (N per profile) — "what the candidate is aiming for"

```
Role {
  id:     str
  name:   str
  color:  str            // identity color; UI shell recolors on switch (Cycle 2)
  active: bool           // false = paused (stops ingesting; not deleted)

  // Tier 1 — target-defined (no shared base)
  search_what:   str               // the core query — this *is* the role
  prefilter: { title_include: str[], title_exclude: str[] }
  threshold:     num               // 0–10 min score to surface
  scoring_notes: str[]             // per-role ADDITIONS (appended to Profile baseline)

  // per-role attributes
  anti_preferences:  str[]         // per-role ADDITIONS (appended to Profile baseline)
  target_salary:     num           // seeded from profile.base_salary, then editable
  applicable_skills: str[]         // BINARY refs into profile.primary_skills[].id
  default_resume_id: str?          // Cycle 3 — linked default resume

  // Tier 2 — shared default w/ override (SPARSE: present key = override; absent = inherit)
  overrides: { seniority?: str, preferred_industries?: str[] }
}
```

`location` / `radius` / `work_arrangement` / `job_types` are deliberately **absent** — they are
global (Job Preferences), never per-role.

### 2.4 Job Preferences (singleton, global) — "where & how the candidate will work"

```
JobPreferences {
  locations:        Location[]                       // willing-to-work places
  radius_km:        num                              // SINGLE global radius applied to every location
  work_arrangement: ("onsite"|"hybrid"|"remote")[]   // multi-select HARD PULL FILTER
  job_types:        ("contract"|"contract_to_hire"|"full_time"|"part_time"|"internship")[]  // HARD PULL FILTER
  max_days_old:     int?                             // global freshness gate

  // salary handling — user-selectable mode (OUR model, decision #3)
  salary_mode:      "floor" | "display"
  floor_amount:     num?            // required when salary_mode == "floor"
}

Location { label: str, lat?: num, lng?: num }   // NO per-location radius
```

- `work_arrangement` / `job_types` / `max_days_old` are **hard pull filters** — exclude postings at
  ingestion, before LLM scoring.
- **`salary_mode == "floor"`** → hard-filter out postings below `floor_amount` at ingestion.
- **`salary_mode == "display"`** → no salary filter; the feed shows delta-vs-`base_salary`
  (Cycle 2 UI): neutral text below base (`−10% vs base`), green above (`+20% vs base`).
- **No extracted salary** is a **red flag, never an auto-drop** (Cycle 2 may offer an opt-in hide filter).

### 2.5 Listing + Match (the posting and its per-role scores)

```
Listing {                          // the existing `listings` table, extended
  ...all existing columns (title, company, location, salary_*, description, source, source_id, …)
  state:             "snippet" | "scored"   // snippet = discovered, not yet LLM-scored (NEW)
  applied_resume_id: str?                    // Cycle 3 link-only
  // per-listing scoring columns (score, verdict, matched_skills, …) are SUPERSEDED by Match rows
}

Match {                            // NEW join — one row per (listing, role) scored pair
  listing_id:     fk → listings.id
  role_id:        fk → roles.id
  score:          num              // 0–10 (tiers: hi 8+, mid 5–7, lo <5)
  matched_skills: str[]            // serialized JSON (same pattern db.py uses today)
  missing_skills: str[]
  concerns:       str[]
  verdict:        str
  model_used:     str              // "provider/model"
  tokens_input:   int
  tokens_output:  int
  overridden_via: str[]            // which Tier-2 overrides applied (Combined-view marker, Cycle 2)
  PRIMARY KEY (listing_id, role_id)
}
```

### 2.6 Entity-relationship summary

```
Profile (1) ──owns──> Skill (*)           Profile (1) ──owns──> Role (*)
Role (*) ──applicable_skills──> Skill (*)  (binary id refs, many-to-many)
Listing (1) ──< Match >── (1) Role         (many-to-many scoring; Match is the join)
JobPreferences (1) ──hard-filters──> Listing ingestion
Profile.base_salary ──derived %──> Listing.salary  (feed delta, Cycle 2)
Resume / Application — DEFERRED to Cycle 3 (applied_resume_id is link-only)
```

---

## 3. Cycle 0 — Foundation: schema + migration

### 3.1 Current state (captured)

- `listings` table: `db.py:332-366` (CREATE TABLE) — scoring lives in columns on this table today
  (`score, matched_skills, missing_skills, concerns, verdict, model_used, tokens_*`, `db.py:348-358`).
- Idempotent migration pattern: `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` loop, `db.py:368-382`.
- `ingest_runs` table: `db.py:421-435`. Geocache: `db.py:410-416`.
- Candidate fields today: `config/profile.example.json` (`primary_skills` with `description/years_active/active`,
  `education`, `seniority`, `anti_preferences`, `preferred_industries`, `location{center,radius_km,…}`,
  `scoring_notes`). Search/prefilter: `config/config.json` (`search.what/where/distance/salary_min/max_days_old`,
  `scoring.threshold`, `prefilter.title_include/title_exclude/require_contract_*`).

### 3.2 New tables (added to `db.init_db()`)

All created with `CREATE TABLE IF NOT EXISTS`; column additions via the existing `ADD COLUMN IF NOT
EXISTS` loop, so `init_db()` stays idempotent and safe to re-run (consistent with `db.py:368-382`).

| Table | Shape | Notes |
|---|---|---|
| `profile` | singleton (enforce single row, e.g. `id=1`) | Profile (2.1); JSON columns for `education`, `anti_preferences`, `scoring_notes`, `defaults`, `residency`, `base_salary` following the existing JSON-in-TEXT pattern |
| `skills` | rows `{id, name, years}` | stable `id` (e.g. slug or serial-backed text key) |
| `job_preferences` | singleton | JobPreferences (2.4); `locations` as JSON; arrays as JSON |
| `roles` | collection | Role (2.3); `prefilter`, `scoring_notes`, `anti_preferences`, `applicable_skills`, `overrides` as JSON columns |
| `matches` | join, PK `(listing_id, role_id)` | Match (2.5); FKs to `listings.id` and `roles.id`; indexes on `role_id` and `listing_id` |

`listings` additions: `state TEXT NOT NULL DEFAULT 'scored'`, `applied_resume_id TEXT NULL`.

> **Decision — keep vs drop legacy scoring columns on `listings`.** Recommendation: **retain** the
> legacy columns through Cycle 1 (do not drop), backfill `matches` from them during migration, and
> have new writes target `matches`. Dropping is a separate cleanup once all reads move to `matches`
> (Cycle 2). Rationale: avoids a destructive, hard-to-reverse migration mid-transition; the columns
> are cheap to carry. Flag for reviewer.

### 3.3 Migration (one-time, idempotent)

1. **Seed Profile** from `config/profile.json`:
   - `primary_skills[].description → name`, `years_active → years`, assign stable `id`; drop `active`.
   - `education`, `anti_preferences` (→ baseline), `scoring_notes` (→ baseline) copied across.
   - `seniority`, `preferred_industries` → `defaults`.
   - `location.center` → `current_location.label`; `location.radius_km` → JobPreferences `radius_km`.
   - `base_salary`, `residency`, `current_role` are **new** — seed empty/defaults; user fills via UI (Cycle 2).
2. **Seed JobPreferences** from `config.json`: `search.distance`/`location.radius_km` → `radius_km`;
   `max_days_old` → global; `prefilter.require_contract_*` → `job_types`/`work_arrangement` (best-effort
   map, flag ambiguous); `search.salary_min` → `salary_mode="floor"`, `floor_amount=salary_min`
   (preserves today's filtering behavior as the migration default).
3. **Seed one Role** from `config.json` `search`: `search_what = search.what`, `prefilter` from
   `prefilter.title_include/exclude`, `threshold = scoring.threshold`, `active = true`, a default color,
   `applicable_skills = all skill ids` (so behavior is unchanged), empty per-role additions/overrides.
4. **Backfill `matches`** from existing `listings` scoring columns against the single migrated Role:
   one `Match` per already-scored listing (`seen=1`); set `listings.state='scored'`. Listings with
   `seen=0` (score failed) → `state` stays `'scored'` with no match, or `'snippet'` — **decision: set
   `'snippet'`** so the retry path (Cycle 1) re-scores them.

Migration runs inside `db.init_db()` guarded by an existence check (skip if `profile` row present), so
re-runs are no-ops — same defensive pattern as the JSearch reclassify migration (`db.py:388-405`).

### 3.4 Cycle 0 acceptance criteria

- [ ] `db.init_db()` creates all new tables; re-running is a no-op (idempotency test).
- [ ] Migration populates Profile + JobPreferences + one Role from existing flat files.
- [ ] Existing `listings` preserved; `matches` backfilled for `seen=1` rows; `seen=0` → `state='snippet'`.
- [ ] Tests: schema creation, migration idempotency, JSON round-trip for the new JSON columns,
      `conftest.py` test-DB guard respected (scoped DELETE by `source_id` prefix — no TRUNCATE).

---

## 4. Cycle 1 — Roles ingest/scoring overhaul

### 4.1 The pipeline stays plugin-major

The current pipeline iterates **per source/plugin** (pull → filter → scrape → score → insert), and
`ingest.py` is already DB-aware (`import db` at `ingest.py:41`; `db.init_db()` `:1196`;
`db.listing_exists()` `:1383`; geocache `:553-575`; `db.insert_listing()` `:639`). 2.0 keeps that
outer loop **unchanged** — Roles are an *inner* fan-out, not a phase-major restructuring.

```
for each active SOURCE/PLUGIN:                       # OUTER LOOP — UNCHANGED
  if plugin.supports_role_query:                     # A-capable: rich server-side filtering
      for each active ROLE:
          fetch role-scoped query (role.search_what + filters)
          per listing:
              hours filter → geo/residency filter → dedup (1 row per source/source_id)
              → prefilter(role) → scrape JD (ONCE per listing) → score(role) → write Match
  else:                                              # B-only: global list, no useful server filter
      fetch global list
      per listing:
          hours filter → geo/residency filter → dedup → scrape JD (ONCE per listing)
          for each active ROLE whose prefilter(role) passes:
              score(role) → write Match
```

Invariants:
- **Scoring is always per-Role**, written to `matches` (never to `listings` columns for new writes).
- **Scrape happens once per listing**, shared across roles (cost) — even in B-mode where multiple
  roles score the same listing.
- The existing per-source short-circuit semantics are preserved (auth 401/403 drops a provider for
  the run; transient failure skips the current listing) — see `credentials.score_listing_with_fallback`.

### 4.2 Plugin capability flag

Add `supports_role_query: bool` to the source-plugin contract (default `false` → B-mode, the safe
fallback). A-capable plugins implement role-scoped fetch using `role.search_what` + the role's gates.
Document in `docs/PLUGIN_DEVELOPMENT.md` and the `plugins/sources/_template/`.

### 4.3 Per-role prefilter is the cost gate

B-only sources scoring every listing against every active role multiplies LLM cost by role count.
Mitigation (baked in): a listing is LLM-scored for a role **only if it passes that role's
`prefilter.title_include/exclude`**. The prefilter is title-substring matching (cheap, no API call) —
the same kind of gate that exists today, now evaluated per-role. A cost test asserts that a listing
failing a role's prefilter incurs **zero** scoring calls for that role.

### 4.4 Geo + residency filter

- Distance origin = `Profile.current_location`; radius = `JobPreferences.radius_km`; applied to
  `JobPreferences.locations[]` (a listing passes if within radius of **any** willing location).
- A listing whose `work_arrangement` is **remote** **bypasses the distance filter** but is checked
  against `Profile.residency` (`authorized_regions` / `needs_sponsorship`). Residency-incompatible
  remote postings are filtered.
- `geocode_fallback` behavior carries over from today (`pass` default).

### 4.5 Effective scoring inputs per Role

The LLM scoring prompt for a `(listing, role)` pair is assembled from:

| Input | Source | Resolution |
|---|---|---|
| skills | `Profile.primary_skills` filtered by `role.applicable_skills` (binary) | `effective_skills = primary_skills.filter(s ∈ role.applicable_skills)` |
| seniority | `role.overrides.seniority ?? profile.defaults.seniority` | sparse override |
| preferred_industries | `role.overrides.preferred_industries ?? profile.defaults.preferred_industries` | sparse override |
| anti_preferences | `profile.anti_preferences (baseline) + role.anti_preferences (additions)` | **concatenate** (OUR model) |
| scoring_notes | `profile.scoring_notes (baseline) + role.scoring_notes (additions)` | **concatenate** (OUR model) |
| target_salary | `role.target_salary` | per-role |
| threshold | `role.threshold` | min score to surface |

The scoring response schema is unchanged (`score, matched_skills, missing_skills, concerns, verdict`);
results write to a `Match` row with `model_used` + token counts (as today, but per-role).

### 4.6 Snippets (store-then-score)

A discovered posting that has **not** been LLM-scored persists with `state='snippet'` (raw posting
data, no `Match` rows). Scoring promotes it to `state='scored'` and writes its `Match` rows. This
enables the Cycle 2 "awaiting scrape" feed state and an on-demand scrape/score action. In Cycle 1 the
backend support is: write `state='snippet'` when a posting is stored pre-scoring, and a function to
promote (scrape if needed → score → write matches → `state='scored'`).

> **Decision — snippet creation policy.** Recommendation: a posting becomes a `snippet` when it
> passes coarse filters + at least one role's prefilter but scoring is deferred/failed; the normal
> path still scores inline and writes `state='scored'` directly. The "always store as snippet first,
> score in a second pass" variant is a larger pipeline change — **defer** unless the reviewer wants it.
> Flag for reviewer.

### 4.7 CLI / control surface

- **Role `active` toggle** — a run processes only `active=true` roles (read from DB at run start).
- **`--role "<name>"`** — narrow a run to a single role (looked up by name/id in the DB). Works
  because ingest is already DB-aware (`ingest.py:41`).
- **`--rescore`** — rebuild `matches` across all active roles (extends today's `--rescore`). A
  per-Role "re-score just this role's listings" action is also exposed (used by the Cycle 2 UI when a
  single role is edited).

### 4.8 Cycle 1 acceptance criteria

- [ ] A-capable and B-only sources both produce correct per-role `Match` rows.
- [ ] A listing matching two roles → two `Match` rows, one `listings` row.
- [ ] Per-role prefilter gates LLM calls (cost test: prefilter-fail → 0 scoring calls for that role).
- [ ] Remote-residency filter: a remote posting incompatible with `residency` is filtered; a
      compatible one passes despite being outside the distance radius.
- [ ] Snippet → scored promotion path covered by a test.
- [ ] `--role` and `--rescore` (all-active + single-role) behave per 4.7.
- [ ] **Full CI gate green** (mirror CI, not a subset): `ruff check`, `pytest` against the test DB,
      plus any `black --check` / `mypy` the workflow runs. State expected test count at plan time.

---

## 5. Cross-cycle decisions log (2026-05-29)

| # | Decision | Rationale |
|---|---|---|
| D1 | `scoring_notes` = Profile baseline + per-Role additions | existing notes are general; superset is more flexible (our session, authoritative) |
| D2 | `anti_preferences` = Profile baseline + per-Role additions | some universal, some role-specific |
| D3 | Salary = `base_salary` anchor (Profile) + per-Role `target_salary` + user-selectable `salary_mode` (floor \| display) | richer than a flat floor; preserves filtering as an option |
| D4 | `residency`/work-auth on Profile + remote-residency filter | gap in handoff docs; needed for correct remote filtering |
| D5 | All config in PostgreSQL | enables UI CRUD, FK integrity for `matches`, role lifecycle; departs from legacy flat-file philosophy deliberately |
| D6 | Ingest stays plugin-major; roles are inner fan-out | preserves proven structure + per-source short-circuit semantics |
| D7 | Per-source `supports_role_query` flag; B-mode default | not all sources allow server-side filtering |
| D8 | Many-to-many via `matches` join | preserves cross-role signal the LLM is paid to produce |
| D9 | Scrape once per listing | cost |
| D10 | Per-application resume = link only (`applied_resume_id`); Application entity deferred | lightest weight that satisfies the requirement |

## 6. Open items for the reviewer

- **O1** — Keep vs drop legacy `listings` scoring columns during transition (§3.2). Lean: keep through Cycle 1.
- **O2** — Snippet creation policy: inline-score-default vs always-snippet-first (§4.6). Lean: inline default.
- **O3** — `seen=0` listings → `state='snippet'` on migration (§3.3 step 4). Confirm.
- **O4** — Copy OpenDesign reference docs into `docs/design/` for durable citations (§1). Needs user OK.
- **O5** — Exact `job_types`/`work_arrangement` mapping from legacy `require_contract_*` (§3.3 step 2)
  is best-effort; confirm the enum mapping at build.

## 7. References

- Current schema & migration pattern: `db.py:332-405` (listings + migrations), `db.py:421-435` (ingest_runs).
- Ingest DB-awareness: `ingest.py:41` (`import db`), `:1196` (`init_db`), `:1383-1390` (dedup), `:639` (`insert_listing`).
- Current candidate fields: `config/profile.example.json`; search/prefilter: `config/config.json` (CLAUDE.md § Config & profile).
- OpenDesign handoff (captured 2026-05-29): `ui-layout.md` (UI authority), `data-model.md`, `api-surface.md`,
  `design-principles.md`, `roles-editor-rebuild.md` (bridging references).
- Tracking: Epic #751, milestone #12, Cycle 0 #747, Cycle 1 #748, Cycle 2 #749, Cycle 3 #750.
