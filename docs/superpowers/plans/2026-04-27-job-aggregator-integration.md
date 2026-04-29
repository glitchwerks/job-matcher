# job-aggregator Integration — Scoping Plan

**Tracking:** Issue #345 under Milestone #8 ("Phase 2: job-aggregator integration") in `cbeaulieu-gt/job-matcher-pr`.

**Goal:** Retrofit `job-matcher-pr` to consume the standalone `job-aggregator` package as its source-fetch layer, deleting `plugins/sources/` and the in-tree plugin loader.

**Status:** Reviewed and refined by user 2026-04-27, then revised after **two** adversarial review rounds the same day. Decisions below are resolved; sub-issues filed under Milestone #8 (#346 Phase A, #347 Phase B, #348 Phase C, #349 Phase D, #350 Phase E).

## Decision Log (user-resolved 2026-04-27, revised post-inquisitor v1 + v2)

1. **Architecture:** Library import — but wrapped behind a **`SourceProvider` Protocol** defined in `job-matcher-pr`. The user's framing: *"the core system shouldn't be so aware of job-aggregator that it's tightly coupled — see it as another potential source of jobs to import at every ingest cycle."* `JobAggregatorProvider` is one implementation today; future `MCPSourceProvider`, `OtherAggregatorProvider`, etc. plug in by implementing the same Protocol.
2. **Protocol value types are designed around job-matcher-pr's needs, not upstream's shape.** `SourceInfo`, `SourceClient`, and `PluginField` are defined by what the in-tree pipeline + Settings UI actually consume — notably `SourceInfo.is_enabled` and `SourceInfo.credentials_required` (job-matcher-pr UX concepts not present in upstream `PluginInfo`) and `PluginField.default` (used by `templates/settings.html` lines 397, 398, 516). `JobAggregatorProvider` performs real translation from upstream's `PluginInfo`/`JobSource`/upstream `PluginField` into these locally-defined types. This is what makes the Protocol meaningful decoupling rather than a 1:1 rename. See §1 for details.
3. **`providers.json` migration timing:** the on-disk migration to native shape is **deferred to Phase B**, not Phase A. Phase A reads the legacy shape unchanged and `JobAggregatorProvider.make_clients()` does in-memory translation for arbeitnow only (legacy `job_sources["arbeitnow"]` → upstream's expected credentials shape). Phase B migrates `providers.json` on-disk to native shape **at the moment** all 10 sources are routed through `JobAggregatorProvider`, so no readers of the legacy shape remain. The migration script `scripts/migrate_providers_json.py` is written and committed in Phase A (cost-free to commit), but invoked at Phase B deploy time. This eliminates the contradiction where Phase A would simultaneously delete legacy readers and leave 9 sources still depending on them. See Phase A and Phase B scope sections.
4. **Phase E ordering:** stays conditional — no leapfrog of Phase B. File-URL install of `job_aggregator` is fine for the spike.
5. **Baseline capture cadence:** all phases (A, B, C, D). Capture script committed in Phase A; pre/post baseline JSON files committed in each phase's PR.
6. **Protocol-import isolation is enforced in CI**, not by checkbox. Phase A adds a CI step (grep or `import-linter` contract) that fails the build if any `from job_aggregator` or `import job_aggregator` appears outside `job_sources/aggregator_provider.py`. See Phase A acceptance criteria.
7. **Soak time before deletion (Phase C):** 3 successful nightly runs after Phase B merges, not 1. Phase C is irreversible deletion; one run is not enough confidence. Aligned across §2 and §4.
8. **Rollback procedures are explicitly documented per phase** (§2). Phase A is reversible by `git revert`. Phase B is reversible by reverting both PRs and restoring `providers.json` from the `.bak` file written by the migration script. Phase C is reversible only by `git revert` of the deletion PR — this is the irreversibility that justifies the longer soak.
9. **`is_enabled` storage:** continues to live in `providers.json`. During Phase A the legacy `job_sources[<key>].enabled` field carries it. After Phase B's migration, enablement lives in a job-matcher-pr-specific `enabled` key inside each per-plugin block (`plugins.<key>.enabled: true`), kept alongside upstream's expected credential keys. **Upstream's `make_enabled_sources` does NOT consult an `enabled` key** — verified against `I:/career/job-aggregator/src/job_aggregator/registry.py:160–222`. The bridge **must explicitly filter** `providers["plugins"]` by `enabled` before building the credentials dict it passes to `make_enabled_sources`. This filter step is named in `JobAggregatorProvider.make_clients()` (Phase A scope) with a unit test asserting that `enabled: false` excludes the source from the returned client list, regardless of credential presence.
10. **Phase A `requirements.txt` install source:** uses a **git URL pointing at the upstream's commit SHA** (`job-aggregator @ git+https://github.com/cbeaulieu-gt/job-aggregator@<sha>`), not `file:///I:/career/job-aggregator`. The Windows file URL would not resolve inside the Linux Docker container that the docker-build smoke test exercises, and CI runs Linux. Developers wanting a local-edit workflow on the upstream repo can run `pip install -e I:/career/job-aggregator` after the regular install to override with a working copy.
11. **Phase A feature-flag mechanism:** an environment variable `JOB_AGGREGATOR_SOURCES` containing a comma-separated list of source keys (e.g. `JOB_AGGREGATOR_SOURCES=arbeitnow`). When set, the named sources route through `JobAggregatorProvider`; all others route through `LegacyInTreeProvider`. When unset (Phase B onward), all sources route through `JobAggregatorProvider` and the env-var read is deleted. Verifiable removal: `grep -rn JOB_AGGREGATOR_SOURCES` returns empty after Phase B.
12. **`LegacyInTreeProvider` lifetime:** introduced in Phase A as a thin shim wrapping the existing `job_sources/auto_register.py` loader so the 9 non-arbeitnow sources continue working through the same `SourceProvider` Protocol. Lives in `job_sources/legacy_provider.py`. **Deleted in Phase B** (not Phase C) at the moment all 9 sources transition to `JobAggregatorProvider` — Phase B's "Files touched" lists the deletion explicitly. Phase C deletes `job_sources/auto_register.py` (the wrapped loader); the shim and the loader die in different phases by design.
13. **Bridge boundary unwrapping:** `JobAggregatorProvider` passes `providers["plugins"]` (the inner dict, post-Phase-B; or the in-memory-translated equivalent during Phase A) to `make_enabled_sources(credentials=...)`, **NOT the whole `providers` dict** — verified against `registry.py:201` which calls `credentials.get(key, {})` directly. Easy to get wrong silently; named explicitly in Phase A scope with a unit test.
14. **`SourceClient.pages()` return type — finalized during Phase A spike.** Initial sketch is `Iterator[list[dict]]` to match in-tree shape today, but if implementation reveals that `dict` leaks upstream's per-source key shape into consumers (per inquisitor v2 NEW-CRIT-1), the Protocol gets revised in the same Phase A PR to use a TypedDict job-matcher-pr owns (e.g. `CanonicalListing`). The decision is deferred to the spike on the principle that the right return type becomes obvious when bridge code is written, not when planning text is written.
15. **Auto-migration safety net in `credentials.py` is transition-only.** Phase B adds an auto-migration helper inside `credentials.load_providers()` that detects legacy-shape files at load time and migrates them in-memory, in case the deploy preflight migration is missed. This is a deliberate transition-window concession to the rejected-runtime-translator pattern (Decision Log #2/#3) — **with an explicit removal trigger**: deleted in Phase D as part of the same PR that updates the docs, with a one-line `assert` that `providers.json` is in native shape on disk. The safety net does not live forever.

---

## 1. Architectural Decision

### Recommendation: **Library import wrapped behind a `SourceProvider` Protocol**

Import `job_aggregator` only inside the `JobAggregatorProvider` implementation. The pipeline (`ingest.py`) and settings UI (`services/provider_schemas.py`, `web/settings.py`) talk to the **Protocol**, never to `job_aggregator` directly. This keeps job-aggregator at arms-length from the core system: it's "one provider among potentially several," not "the source of jobs."

Sketch (final shape lands in Phase A):

```python
# job_sources/provider.py
class PluginField(Protocol):
    name: str            # e.g. "app_id"
    label: str           # human-readable, for the Settings template
    type: str            # "text" | "password" — drives input rendering
    required: bool       # drives the "credentials_required" rollup
    help_text: str       # tooltip / placeholder hint
    default: str | None  # consumed by templates/settings.html (lines 397, 398, 516)

class SourceInfo(Protocol):
    key: str                       # canonical source identifier (DB `source` column)
    label: str                     # display name for the Settings page
    fields: tuple[PluginField, ...]
    is_enabled: bool               # job-matcher-pr UX concept (per-source toggle)
    credentials_required: bool     # True iff any field has required=True

class SourceClient(Protocol):
    SOURCE: str
    def pages(self) -> Iterator[list[dict]]: ...   # matches in-tree shape today

class SourceProvider(Protocol):
    def list_sources(self) -> list[SourceInfo]: ...
    def make_clients(
        self, *, providers_data: dict, search: SearchParams
    ) -> list[SourceClient]: ...
    def scrape(self, url: str) -> str: ...
```

The value types above are deliberately **defined by what `job-matcher-pr` consumes** (the ingest pipeline + the Settings template + `credentials.py`), not by what upstream `job_aggregator` happens to expose. `is_enabled` and `credentials_required` are job-matcher-pr UX concepts with no upstream equivalent. `default` exists because the existing settings template reads `field.default` directly. The bridge (`JobAggregatorProvider`) is where real translation happens — upstream `PluginInfo.fields` (which lacks `default` and the enablement concepts) is mapped into these locally-defined types, and the per-listing record translation goes from upstream `JobRecord` into the in-tree DB row shape.

This is what makes the Protocol meaningful decoupling instead of a 1:1 rename of upstream's surface. A second implementation (e.g. an MCP-backed provider) would never re-derive these types from upstream — it would implement them directly.

`JobAggregatorProvider(SourceProvider)` is the only implementation today. `ingest.py`'s pipeline iterates over **a list of providers** (currently length 1) — additional providers added later require zero pipeline changes, only a new implementation of the Protocol and a registration entry.

### Translation is where the abstraction earns its keep

Upstream's `JobRecord` is a proper superset of the in-tree DB shape: it carries `url` (vs in-tree `redirect_url`), `description_source` (no in-tree equivalent), `extra` (upstream-only), `remote_eligible` (upstream-only), and treats missing company as `None` (in-tree uses `""`). The bridge **translates** these into the in-tree DB shape, with unit tests per source. Some upstream improvements — most notably `description_source: "full"` vs no field at all — are deliberately discarded by the bridge in Phase A (adopting them is a separate decision; see §7 Out of Scope). The acceptance criterion is therefore **DB-shape compatibility** (the translated record matches what `db.insert_listing` accepts and what the app reads back), not byte-identity with upstream.

### Why (not the more complex options)

The user owns both repos. The "loose coupling" benefit of the subprocess option is largely cosmetic when one human ships both sides — there is no upstream API churn risk that the maintainer is not personally creating. The ingest pipeline already loops over Python objects (`for client in sources: for page in _safe_pages(client):` at `ingest.py:1336–1338`), and `job_aggregator` exposes the same `pages()` generator shape (`I:/career/job-aggregator/docs/plugin_authoring.md` §`Implementing pages()`). The mapping is line-for-line.

The schema versioning, envelope, `description_source` truth table, and `CredentialsError`/`PluginConflictError`/`SchemaVersionError` exception hierarchy in `job_aggregator` are explicitly designed as a stable public contract (`docs/output_schema.md` §Schema Versioning, §Deprecation Policy). The package treats its Python API and JSONL envelope as **the same contract** — there is no payoff in choosing the JSONL surface over the Python one.

The Settings UI introspection requirement (Risk #1 below) makes the subprocess option strictly worse: the UI needs `PluginInfo.fields` (a Python object with typed attributes), not a CLI subprocess return code. Option B would require shelling out for every settings page render, or duplicating the schema in `job-matcher-pr`.

### Concrete kill criteria — revisit if any of these fire

1. **Two consecutive minor releases of `job_aggregator` ship breaking changes to `pages()` / `normalise()` / `make_enabled_sources()` Python signatures** (the schema-versioned envelope is irrelevant here — only the Python surface). If this happens, the loose-coupling argument becomes real and we revisit Option B.
2. **A second consumer of `job_aggregator` appears that uses the JSONL surface only.** If both consumers exist, the JSONL contract is the lowest common denominator and the case for using the same boundary in `job-matcher-pr` strengthens.
3. **The Flask app's import time grows by >500 ms after integration**, attributable to plugin entry-point discovery. If so, defer plugin loading behind a lazy import or move to subprocess.

If none of these fire within ~6 months of integration, the decision is settled and this section can be archived.

### Hybrid (Option C) — explicitly rejected

Importing the library but only using its JSONL output is the worst of both worlds: pays the import cost, gives up the typed Python surface that the Settings UI needs, and adds an unnecessary serialization round-trip. No reason to choose this.

---

## 2. Phased Work Breakdown

Five phases, each scoped to a single PR. Phase A is a deliberate spike to prove the boundary; Phase B is the bulk migration; Phase C is the demolition; Phase D is documentation; Phase E is conditional (only fires if any earlier phase needs upstream changes).

All work happens in worktrees under `.worktrees/` per the project's git policy. Each PR merges to `main` after CI green.

### Phase A — Spike: define `SourceProvider` Protocol + route arbeitnow (issue #346)

**Branch:** `feat/346-aggregator-spike-arbeitnow`
**Scope:** This phase establishes the *permanent* abstraction layer (the Protocol and its first implementation) and proves the boundary on one source. **`providers.json` on-disk format is NOT migrated in Phase A.** The 9 other sources continue to use the existing in-tree plugin loader, which still reads `providers["job_sources"][...]` at `web/settings.py:250`, `services/provider_schemas.py:393`, `credentials.py:299–304`, and `ingest.py:1075` (`_inject_env_var_credentials`). Migrating the on-disk file in Phase A would break those readers. The migration script is *written* in Phase A but *invoked* in Phase B — see Decision Log #3.

**Why arbeitnow:** No credentials, no rate-limit gotchas, already exists in both repos in directly comparable form (`plugins/sources/arbeitnow/plugin.py` vs `I:/career/job-aggregator/src/job_aggregator/plugins/arbeitnow/plugin.py`). This proves the integration boundary on the simplest possible source so we can iterate on the boundary, not on plugin behavior.

**Files touched:**
- `requirements.txt` — add `job-aggregator @ git+https://github.com/cbeaulieu-gt/job-aggregator@<sha>` per Decision Log #10. **NOT** `file:///I:/career/job-aggregator` — that won't resolve in the Linux Docker build.
- `job_sources/provider.py` (new) — defines the `SourceProvider` Protocol and value types (`SourceInfo`, `SourceClient`, `PluginField`) per §1. **No `job_aggregator` import here.** `SourceClient.pages()` return type may be revised in this same PR per Decision Log #14 if the spike reveals `dict` leakage.
- `job_sources/aggregator_provider.py` (new) — `JobAggregatorProvider` class implementing the Protocol. **The only file in `job-matcher-pr` that imports `job_aggregator`** (apart from `requirements.txt`). Reads legacy-shape `providers.json` (`providers["job_sources"]`), **filters by per-source `enabled` field (Decision Log #9)** before building credentials, translates the per-source dict in-memory to upstream's expected credentials shape, and passes the inner per-plugin dict (NOT the whole providers dict — Decision Log #13) to `make_enabled_sources(credentials=..., search=...)`. **Catches `CredentialsError` per source, logs a warning, and omits that source from the returned client list** — replicating today's silent-omission UX so one bad credential blob does not abort the entire ingest run (see Risk #5). Same per-source isolation applies to `PluginConflictError` and `SchemaVersionError` raised at construction time.
- `job_sources/legacy_provider.py` (new) — `LegacyInTreeProvider` thin shim wrapping `job_sources/auto_register.py` so the 9 non-arbeitnow sources continue to satisfy the `SourceProvider` Protocol during the transition. **Deleted in Phase B** per Decision Log #12.
- `ingest.py` — feature-flagged code path **driven by env var `JOB_AGGREGATOR_SOURCES`** (Decision Log #11): for each comma-separated key in the env var, route through `JobAggregatorProvider`; route everything else through `LegacyInTreeProvider`. Pipeline iterates over a list of `SourceProvider` instances (initially `[JobAggregatorProvider(), LegacyInTreeProvider()]` — Phase B drops the second). When env var is unset, all sources use the legacy provider, so dev workflows that don't set it preserve today's behavior.
- `scripts/migrate_providers_json.py` (new, *committed but not invoked*) — one-shot migration from `{"job_sources": {...}}` → `{"schema_version": "1.0", "plugins": {...}}` with a per-plugin `enabled` field preserved (see Decision Log #9). Idempotent (detects existing `schema_version` and exits 0). Writes a `.bak` of the original file before mutation. Used by Phase B at deploy time.
- `tests/test_aggregator_provider.py` (new) — confirms `JobAggregatorProvider`'s **DB-shape translation** produces records that pass the same `db.insert_listing` validation as today's in-tree path, using a recorded upstream `JobRecord` fixture and asserting field-for-field on the translated dict. Includes a test that triggers `CredentialsError` and confirms the source is omitted, not propagated.
- `tests/test_migrate_providers_json.py` (new) — covers the migration script (idempotency, backup, malformed-input handling, `enabled` preservation).
- `tests/test_source_keys_round_trip.py` (new) — loads a fixture of distinct `source` strings captured from the dev DB (committed at `tests/fixtures/db_source_strings.json`) and asserts each maps to a registered upstream `SOURCE` constant from `job_aggregator`. Fails if any in-tree `source` string has no upstream equivalent. Closes Risk #3 with an automated check.
- `tests/fixtures/db_source_strings.json` (new, committed) — captured once with `psql -c "SELECT DISTINCT source FROM jobs"` against the dev DB.
- `scripts/capture_ingest_baseline.py` (new) — reusable baseline-capture script for all phases. See §4.
- `docs/baselines/2026-04-27-pre-aggregator.json` (new, committed) — baseline captured before any code is changed in Phase A.
- `.github/workflows/*.yml` (or existing CI config) — add an enforcement step (see acceptance criteria below).

**Acceptance criteria:**
- [ ] `job_sources/provider.py` defines the `SourceProvider` Protocol per §1 (with `is_enabled`, `credentials_required`, `default` on the value types) — no `job_aggregator` import in this file.
- [ ] `JobAggregatorProvider` implements the Protocol and is the only file (besides `requirements.txt`) that imports `job_aggregator`.
- [ ] **CI enforcement:** a CI step runs `grep -rn 'from job_aggregator\|import job_aggregator' --include='*.py' . | grep -v 'job_sources/aggregator_provider.py' | grep -v '__pycache__'` and exits non-zero if it finds any matches. Build fails if a stray import slips in. (Equivalent `import-linter` contract or `ruff` custom rule is acceptable; if `import-linter` is chosen, verify current syntax via Context7 before writing the contract.)
- [ ] **Credential isolation:** `JobAggregatorProvider.make_clients()` catches `CredentialsError`, `PluginConflictError`, and `SchemaVersionError` per source, logs a warning, and omits the source. A unit test asserts that one bad blob does not abort other sources.
- [ ] **Enablement filter:** `JobAggregatorProvider.make_clients()` reads the per-source `enabled` field from `providers.json` and skips sources with `enabled: false` BEFORE invoking `make_enabled_sources`. Unit test: a source with valid credentials but `enabled: false` does not appear in the returned client list. Closes inquisitor v2 NEW-HIGH-1.
- [ ] **Bridge boundary:** `JobAggregatorProvider` passes the inner per-plugin dict (e.g. `providers["job_sources"]` translated to the native shape, then unwrapped) to `make_enabled_sources(credentials=...)` — NOT the whole `providers` dict. Unit test asserts the dict shape passed in is what `registry.py:201`'s `credentials.get(key, {})` expects. Closes inquisitor v2 NEW-MED-2.
- [ ] **Feature flag mechanism:** routing is driven by env var `JOB_AGGREGATOR_SOURCES` (comma-separated source keys). Unset → legacy provider for all sources. `JOB_AGGREGATOR_SOURCES=arbeitnow` → `JobAggregatorProvider` for arbeitnow only. Documented in CLAUDE.md commands section in Phase A's PR. Phase B's "removal" criterion is `grep -rn JOB_AGGREGATOR_SOURCES` returning empty.
- [ ] `LegacyInTreeProvider` lives in `job_sources/legacy_provider.py` and satisfies the `SourceProvider` Protocol. Phase B's "Files touched" lists this file's deletion explicitly.
- [ ] `pip install -r requirements.txt` succeeds in a fresh venv with no PyPI access (uses local wheel / `git+file://` install).
- [ ] **Docker-build smoke test:** `docker build -f docker/Dockerfile .` against a worktree-local Dockerfile succeeds, and `docker run --rm <image> python -c "import job_aggregator"` exits 0. Closes Risk #4 / inquisitor finding L1.
- [ ] `scripts/migrate_providers_json.py` is committed, has tests covering idempotency / backup / malformed-input / `enabled`-preservation, and is **NOT invoked** by anything in Phase A's deploy path.
- [ ] `python ingest.py --hours 24` runs to completion with arbeitnow routed through `JobAggregatorProvider` and the other 9 sources still routed through the in-tree loader.
- [ ] **DB-shape compatibility (replaces byte-identity):** for a captured sample of 10 arbeitnow listings, `JobAggregatorProvider`'s translated record passes `db.insert_listing` validation and round-trips to the same DB-row shape as today's in-tree path. Per-source unit tests cover the `JobRecord` → DB-row translation. (Upstream-only fields like `description_source`, `extra`, `remote_eligible` are deliberately discarded — see §7.)
- [ ] **Source-key round-trip test passes:** `tests/test_source_keys_round_trip.py` confirms every distinct `source` string in the dev DB maps to a registered upstream `SOURCE`. Closes Risk #3.
- [ ] Pre-baseline JSON captured before code changes; post-baseline captured after; diff documented in PR description.
- [ ] Existing tests (`pytest`) still pass; new tests all pass.
- [ ] DB rows continue to use `source = "arbeitnow"` exactly.

**Rollback:** Phase A is fully reversible by `git revert` of PR #346. Nothing on disk has changed -- `providers.json` is still in legacy shape, no in-tree files have been deleted. The bridge is added code, not modified code.

**Dependencies:** None. This is the entry phase.

#### Pre-Phase-B verification (issue #352)

Before Phase B work begins, run `scripts/verify_phase_a_pre_b.ps1` from the worktree root to confirm all Phase A deferred acceptance criteria (AC #13, #16, #17) are satisfied against the live dev DB.

**Command:**

```powershell
.\scripts\verify_phase_a_pre_b.ps1 -DatabaseUrl "postgresql://jobmatcher:<password>@localhost:5432/jobmatcher_dev"
# Or if DATABASE_URL is already exported:
.\scripts\verify_phase_a_pre_b.ps1
# Skip the live ingest run (steps 1, 2, 4 only):
.\scripts\verify_phase_a_pre_b.ps1 -SkipSmoke
```

**Expected pass output:**

```
[PASS]  1. Source-string fixture refresh    no drift (10 keys)
[PASS]  2. Pre-aggregator baseline          docs/baselines/2026-04-27-pre-aggregator.json (X KB)
[PASS]  3. Live ingest smoke run            JobAggregatorProvider + LegacyInTreeProvider markers seen; log: phase-a-pre-b-smoke-<ts>.log
[PASS]  4. Post-aggregator baseline         delta: +N rows (pre=X, post=Y)
All non-skipped steps PASSED.
```

**Troubleshooting per failure mode:**

- **Step 1 FAIL -- DB unreachable:** `DATABASE_URL` is wrong, or the dev Docker stack is not running. Start with `docker compose -p job-matcher-pr-dev --env-file .env.dev -f docker-compose.dev.yml up -d` and verify `psql "$env:DATABASE_URL" -c "SELECT 1"` works.
- **Step 2 FAIL -- baseline still stub:** `capture_ingest_baseline.py` exited non-zero. Re-run it manually with `python scripts/capture_ingest_baseline.py --label pre-aggregator` and inspect stderr. Common cause: `DATABASE_URL` not set, or `psycopg2` not installed in the venv.
- **Step 3 FAIL -- JobAggregatorProvider marker missing:** arbeitnow did not route through the aggregator bridge. Confirm `JOB_AGGREGATOR_SOURCES=arbeitnow` was active during the run (the script sets it, but check the log header). Confirm PR #351 is checked out in this worktree.
- **Step 3 FAIL -- Fetching from source marker missing:** no legacy sources were fetched at all. The dev `config/config.json` may have all sources disabled, or credentials are missing for all nine non-arbeitnow sources.
- **Step 4 FAIL -- post baseline invalid:** same as step 2. Check `DATABASE_URL` and psycopg2.

---

### Phase B — Migrate the remaining 9 sources + on-disk `providers.json` migration (issue #347)

**Branch:** `feat/347-aggregator-migrate-remaining`
**Scope:** Route adzuna, jooble, jsearch, usajobs, himalayas, jobicy, remoteok, remotive, the_muse through `job_aggregator`, **and** migrate `providers.json`'s on-disk format to native shape `{"schema_version": "1.0", "plugins": {...}}`. The migration runs only after all 10 sources are wired through `JobAggregatorProvider` and all legacy `providers["job_sources"]` readers have been updated or removed — eliminating the readers and the file shape in the same PR is what makes this safe (and what made splitting it across A and B unsafe).

**Files touched:**
- `job_sources/aggregator_provider.py` — extend `make_clients()` to handle all 10 sources; remove the in-memory legacy-shape translation once `providers.json` is migrated to native shape (the file now matches what upstream expects, modulo the job-matcher-pr `enabled` extension key per Decision Log #9). Bridge boundary unwrapping (Decision Log #13) and enablement filter (Decision Log #9) carry over from Phase A unchanged.
- **Delete `job_sources/legacy_provider.py`** (the `LegacyInTreeProvider` shim from Phase A) per Decision Log #12 — all 9 sources now route through `JobAggregatorProvider`, so the shim has no remaining caller.
- `ingest.py` — remove the feature flag added in Phase A. The pipeline iterates `[JobAggregatorProvider()]` for all 10 sources. Remove `_inject_env_var_credentials`'s legacy `providers["job_sources"]["adzuna"]` write path at line 1075 and replace with the equivalent write into the native shape (`providers["plugins"]["adzuna"]`).
- `credentials.py` — update `load_providers()` (lines 149, 161, 231, 299, 334, 403) to read/write the native `{"schema_version", "plugins"}` shape directly. Remove all `"job_sources"` references. **Auto-migrates legacy-shape files at load time as a transition-only safety net per Decision Log #15** (calls into the same migration helper used by the script). The safety net is **deleted in Phase D** alongside an `assert` that on-disk `providers.json` is in native shape — it does not live forever.
- `services/provider_schemas.py` — update line 163 (`(providers.get("job_sources") or {}).get("adzuna")`) and line 393 (`data.setdefault("job_sources", {})`) to use `plugins` instead.
- `web/settings.py` — update line 250's read of `providers["job_sources"]` to use `plugins`.
- `job_sources/auto_register.py` — update lines 131, 133, 138, 155, 161 to write the native `plugins` shape (or, if Phase C deletes this file entirely, this update is moot — verify at PR time).
- `config/providers.example.json`, `.env.dev.example`, `.env.prod.example` — update to native shape so fresh installs use the new format.
- `scripts/deploy-remote-linux.sh` — add a preflight step that detects legacy-shape `providers.json` on the prod server, runs `scripts/migrate_providers_json.py`, and **aborts the deploy on migration failure**. The script writes a `.bak` before mutation. Closes M4.
- `tests/test_aggregator_provider.py` — extend with parameterized cases for each of the remaining 9 sources using captured fixtures.
- `tests/test_credentials_native_shape.py` (new) — covers the rewritten `credentials.py` against fixtures of both legacy-shape (auto-migrated) and native-shape `providers.json`.

**Pre-merge migration steps (developer workflow):**
1. Locally run `python scripts/migrate_providers_json.py config-dev/providers.json` against the dev stack; verify ingest still works on legacy-shape AND native-shape during the transition (the auto-migration path in `credentials.py` covers this).
2. Commit the migrated `config-dev/providers.json` IF it is checked in (verify — it may be gitignored). If gitignored, document the manual migration step in the PR description.

**Acceptance criteria:**
- [ ] All 10 sources fetch via `job_aggregator` end-to-end on a real ingest run.
- [ ] **Zero references to `providers["job_sources"]` remain** in any `.py` file (CI grep step). Native shape is the only on-disk format.
- [ ] Per-source listing counts and canonical-field samples match a baseline capture taken before this phase (see Verification Strategy below).
- [ ] Adzuna env-var injection (`ADZUNA_APP_ID` / `ADZUNA_APP_KEY` from `_inject_env_var_credentials`) still works — now writing into `providers["plugins"]["adzuna"]`.
- [ ] All existing `pytest` suites pass.
- [ ] Settings UI still renders all sources with correct field schemas (the deeper UI rewrite to consume the Protocol directly happens in Phase C; Phase B only changes the underlying dict key).
- [ ] **Prod-deploy preflight verified:** `scripts/deploy-remote-linux.sh` detects a legacy-shape file, migrates it, leaves a `.bak`, and aborts cleanly if migration fails. Tested against a staging copy before the production deploy.
- [ ] Per-plugin `enabled` field is preserved by the migration script (Decision Log #9). Disabled sources stay disabled after migration.

**Rollback:** revert PR #347 *and* PR #346 together, then restore `providers.json` from the `.bak` file written by the migration script (or by manual re-edit). Both reverts are required because Phase B removes the feature flag and the `LegacyInTreeProvider` shim Phase A relied on. If the rollback window has expired (e.g. a post-rollback ingest run already wrote rows), no data is lost — the on-disk shape change is invertible by the same script reading the `.bak`.

**Dependencies:** Phase A merged.

---

### Phase C — Delete `plugins/sources/` and the legacy plugin loader (issue #348)

**Branch:** `feat/348-aggregator-remove-legacy`
**Scope:** Demolition. Once Phase B is on main and stable for **3 successful nightly runs** (per Decision Log #7), delete the in-tree plugin loader and its plugins. Phase C is the irreversible phase — the longer soak window exists because `git revert` of a deletion PR is the only path back, and a corrupted-data scenario discovered on day 4 would still be recoverable.

**Files touched:**
- Delete: `plugins/sources/*` (all 10 source folders + `_template/`).
- Delete: `job_sources/auto_register.py`, `job_sources/loader.py`, `job_sources/base.py` — or whatever subset the bridge no longer needs. (Audit at PR time.)
- `services/provider_schemas.py` — the `_PROVIDER_CLASS_MAP` references in `_build_llm_schemas` are LLM-only and stay; but the **source** schema introspection used by the Settings UI must be rewritten to call `provider.list_sources()` (via the `SourceProvider` Protocol from Phase A) and read `SourceInfo.fields`. **No direct `job_aggregator` import here** — the rewiring talks to the Protocol, not the concrete implementation. See Risk #1.
- `web/settings.py` (and templates) — adjust to consume the new `PluginInfo`-derived schema shape.
- `ingest.py` — remove `from job_sources.auto_register import ensure_plugins_registered` and the call site at `ingest.py:1253–1254`. Pipeline iterates over `providers: list[SourceProvider]` (still length 1: `[JobAggregatorProvider()]`) — no direct `job_aggregator` import in this file.
- Tests — drop `tests/test_plugin_loader.py` (if it exists) and any tests asserting in-tree plugin behavior; keep bridge / canonical-shape tests.

**Acceptance criteria:**
- [ ] `git ls-tree HEAD -- plugins/sources/` returns empty.
- [ ] `python ingest.py` runs end-to-end with no in-tree plugin code on the import path.
- [ ] Settings UI lists every source with the correct credential fields, sourced via `provider.list_sources()` returning `SourceInfo` (the Protocol value type — *not* an upstream `PluginInfo` directly). The template still reads `field.default`, `field.label`, `field.type`, `field.required`, `field.help_text`, plus the new `is_enabled` and `credentials_required` rollups on `SourceInfo`.
- [ ] No dead imports remain (`ruff`/`pyflakes` clean).
- [ ] CI grep step from Phase A still passes (no `from job_aggregator` outside `aggregator_provider.py`).
- [ ] All `pytest` suites pass.

**Rollback:** revert PR #348. The deletion is invertible by git, but any cleanup that ran on the prod server (e.g. removed `plugins/sources/` from a Docker volume) needs the volume restored from backup. Recommend tagging the pre-Phase-C commit on prod (`git tag prod-pre-phase-c`) before the deploy so rollback is one tag-push.

**Dependencies:** Phase B merged + **3 successful nightly ingest runs** on main with Phase B's code (Decision Log #7).

---

### Phase D — Update CLAUDE.md and README (issue #349)

**Branch:** `feat/349-aggregator-docs`
**Scope:** Pure documentation. Reflect the new architecture in onboarding docs.

**Files touched:**
- `CLAUDE.md` — rewrite the "Architecture" section's bullet describing in-tree plugins; rewrite "Adding New Job Sources" to point at `job-aggregator`'s `docs/plugin_authoring.md` and explain that new sources are now contributed upstream (or via entry-points in a separate package). Remove the line about folders starting with `_` being skipped.
- `README.md` — if it documents source plugins (audit), update accordingly.
- `docs/PLUGIN_DEVELOPMENT.md` — either delete (and add a CLAUDE.md pointer to the upstream guide) or rewrite to a one-page redirect.
- `credentials.py` — **delete the auto-migration safety net** added in Phase B per Decision Log #15. Add a one-line `assert` at load time that `providers["schema_version"] == "1.0"` (i.e. on-disk file is in native shape). Closes the transition window.

**Acceptance criteria:**
- [ ] CLAUDE.md "Adding New Job Sources" section references `job_aggregator` documentation, not `plugins/sources/_template/`.
- [ ] No references to `plugins/sources/` remain in any committed `.md` file.
- [ ] A new contributor reading CLAUDE.md alone can locate the plugin-authoring guide.
- [ ] **CI enforcement:** a CI step runs `grep -rn 'plugins/sources/' --include='*.md' .` and exits non-zero if it finds matches outside the plan files in `docs/superpowers/plans/` (which are historical records). Same enforcement pattern as Phase A's import check (L2).

**Rollback:** revert PR #349. Docs-only — no operational impact.

**Dependencies:** Phase C merged (so the docs describe the actual state of the code).

---

### Phase E (conditional) — Coordinate `job_aggregator` 1.0.0 + PyPI publish (issue #350)

**Branch:** in `cbeaulieu-gt/job-aggregator`, not this repo.
**Scope:** Only fires if any earlier phase exposes a missing or broken behavior in `job_aggregator` that requires an upstream fix and a release bump. If everything works on the current `0.1.0` wheel / git install, skip this phase entirely and PyPI publish can happen on the upstream's own schedule.

**Acceptance criteria (only if triggered):**
- [ ] Upstream issue filed in `cbeaulieu-gt/job-aggregator` describing the gap.
- [ ] Upstream PR merged + tag cut.
- [ ] `requirements.txt` in `job-matcher-pr` updated to pin the new version (file URL, git tag, or PyPI).

**Dependencies:** Triggered ad-hoc by Phase A or B findings.

---

## 3. Risk Register

### Risk #1 — Settings UI plugin-schema introspection

**Current state:** `services/provider_schemas.py` introspects this repo's plugin shape via the `_PROVIDER_CLASS_MAP` (LLM providers — unaffected) and via `job_sources/loader.py` (sources — affected). The settings page reads `source.json` files in each `plugins/sources/<key>/` folder to render credential field inputs. The template (`templates/settings.html` lines 397, 398, 516) reads `field.default` as a placeholder, plus `field.label`, `field.type`, `field.required`, `field.help_text`, and (per the page's per-source toggle) an enablement flag and a "credentials required" rollup.

**Future state:** Phase C rewires `services/provider_schemas.py`'s source-schema path to call `provider.list_sources()` returning `SourceInfo` (the **Protocol value type**, not upstream's `PluginInfo`). The bridge translates upstream `PluginInfo.fields` → the in-tree `PluginField` value type, filling in `default` from upstream defaults where present and from `""` otherwise. `is_enabled` is read from the per-plugin `enabled` field in the migrated `providers.json` (Decision Log #9). `credentials_required` is computed by `JobAggregatorProvider` as `any(f.required for f in fields)`.

**Mitigation:** Per §1, the value types are designed around exactly the fields the template consumes — there is no "translate to fit the template" adapter step at the call site, because the Protocol IS the contract the template renders against. The only translation is inside `JobAggregatorProvider`. A manual smoke test of the Settings page after Phase C is still required to catch any field-level rendering surprises.

**Severity:** Medium. The change is mechanical but touches user-visible UI; needs a manual smoke test of the Settings page after Phase C.

---

### Risk #2 — Credentials format mismatch

**Current `providers.json` shape** (this repo):
```json
{
  "job_sources": {
    "adzuna": {"enabled": true, "app_id": "...", "app_key": "..."}
  }
}
```

**`job_aggregator` expected shape** (from `I:/career/job-aggregator/docs/credentials_format.md`):
```json
{
  "schema_version": "1.0",
  "plugins": {
    "adzuna": {"app_id": "...", "app_key": "..."}
  }
}
```

Differences: top-level key (`job_sources` vs `plugins`), the per-source `enabled` flag (job-matcher-pr concept; preserved as a job-matcher-pr extension key per Decision Log #9), and the `schema_version` envelope.

**Also affected:** `_inject_env_var_credentials()` at `ingest.py:1075` injects `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` into the legacy shape; Phase B updates this to write into `providers["plugins"]["adzuna"]`.

**Also affected:** the dev/prod docker-compose stacks mount `config-dev/` and `config/` respectively. The on-disk `providers.json` IS migrated to native shape — but only in Phase B, when all 10 sources are routed through `JobAggregatorProvider` and all readers of the legacy shape (`web/settings.py:250`, `services/provider_schemas.py:163,393`, `credentials.py:149,161,231,299,334,403`, `ingest.py:1075`, `job_sources/auto_register.py:131,133,138,155,161`) have been rewritten to read the native shape.

**Phase A approach:** in-memory translation inside `JobAggregatorProvider.make_clients()` for arbeitnow only. `providers.json` on disk stays in legacy shape. The 9 in-tree sources continue to read it the way they do today. This eliminates the contradiction the inquisitor flagged.

**Phase B approach:** rewrite all 9 reader sites to use the native shape; run `scripts/migrate_providers_json.py` against `config-dev/providers.json` (locally, pre-PR) and against the prod server (via `scripts/deploy-remote-linux.sh` preflight, post-merge). The migration script is idempotent and writes a `.bak`.

**Mitigation:** the in-memory translator in Phase A is a 10-line helper; the on-disk migration script in Phase B is unit-tested for idempotency, backup, malformed input, and `enabled`-preservation. Both are covered in Phase A's tests (script) and Phase B's tests (rewritten readers).

**Severity:** Medium. Pure data-shape transformation; test coverage straightforward. Risk is now contained to a single PR (Phase B) instead of being split across Phases A and B as previously.

---

### Risk #3 — DB `source` string round-trip

The `jobs` table has rows from prior ingest runs whose `source` column equals strings like `"adzuna"`, `"arbeitnow"`, `"jooble"`, etc. The unique constraint is `(source, source_id)`. If `job_aggregator` emits a different `source` string for any plugin, dedup breaks: every existing listing re-inserts and we double-count or constraint-violate.

**Verification:** From `I:/career/job-aggregator/docs/output_schema.md` §Supported Sources, the `SOURCE` keys are: `adzuna`, `arbeitnow`, `himalayas`, `jobicy`, `jooble`, `jsearch`, `remoteok`, `remotive`, `the_muse`, `usajobs`. Cross-check against the in-tree plugin folder names in `plugins/sources/`: identical except verify `the_muse` (vs e.g. `themuse`) and any other underscore conventions before Phase A.

**Mitigation:**
1. **Automated test (Phase A deliverable):** `tests/test_source_keys_round_trip.py` loads a fixture of distinct `source` strings (captured once from the dev DB into `tests/fixtures/db_source_strings.json`) and asserts each maps to a registered upstream `SOURCE`. The test fails CI if any in-tree `source` string has no upstream equivalent. This replaces the "expectation, not barrier" SQL-audit-by-hand approach.
2. Bridge layer additionally asserts `record["source"] == plugin_class.SOURCE` at runtime and logs a startup error if any rename is detected — defense in depth on top of the test.
3. If a rename is required for any source (test fails locally during Phase A), write a one-line migration: `UPDATE jobs SET source = 'new_key' WHERE source = 'old_key';` in `db.py:init_db()` wrapped in a try/except.

**Severity:** High consequence if missed (silent dedup failure, double cost). Now caught automatically by the round-trip test rather than relying on a manual audit.

---

### Risk #4 — Local-install dependency syntax (Phase A bootstrap)

`pip install git+file:///I:/career/job-aggregator` works on Windows but the slashes and drive letter are pip-version-sensitive; older pip / `uv pip` may need `git+file:///I:/career/job-aggregator@main`. The local wheel install (`pip install I:/career/job-aggregator/dist/job_aggregator-0.1.0-py3-none-any.whl`) is the most portable but requires the wheel to be rebuilt whenever upstream changes.

**Mitigation:** Document both options in `requirements.txt` as a comment; default to `file://` URL with the wheel as fallback. If issues arise, look up current pip syntax via Context7 (`mcp__plugin_context7_context7__query-docs` with `pip` library ID) before debugging blind.

**Severity:** Low. Bootstrap-only; resolved once and forgotten.

---

### Risk #5 — `CredentialsError` raised at construction-time bypasses `_safe_pages()`

The current pipeline at `ingest.py:1086–1135` wraps `client.pages()` in `_safe_pages` so a plugin throwing an unhandled exception during *iteration* aborts only that source, not the whole run. **But `_safe_pages` does not wrap client construction.** `job_aggregator`'s `CredentialsError` is raised at construction time (inside `make_clients()`), so a single bad credential blob would propagate up and abort the entire ingest run — strictly worse than today's behavior, where the in-tree loader emits a config warning and silently omits the source.

**Mitigation:** `JobAggregatorProvider.make_clients()` MUST catch `CredentialsError` per source, log a warning, and omit that source from the returned client list. This replicates today's UX. A unit test in Phase A asserts that one source raising `CredentialsError` does not prevent the other sources from being constructed and returned. Other upstream exceptions raised during iteration (`ScrapeError`, `PluginConflictError`, `SchemaVersionError`) continue to be handled by `_safe_pages`'s broad `Exception` catch.

**Smoke test:** Phase A's smoke test should deliberately misconfigure one source (e.g. blank Adzuna `app_id`) to confirm the run continues with the other 9.

**Severity:** Medium (raised from Low). A single misconfigured credential would have aborted the entire nightly run; the per-source catch in `make_clients()` is the only thing preventing that.

---

### Risk #6 — Hours / max_pages parameter mapping

The current pipeline reads `config["search"]["max_pages"]` and `config["search"]["max_days_old"]`, plus the CLI `--hours` flag that overrides `max_days_old`. `job_aggregator`'s `SearchParams` has `hours` and `max_pages` (per `I:/career/job-aggregator/src/job_aggregator/schema.py:125`), but uses different defaults and arithmetic.

**Mitigation:** In Phase B, write a single `_build_search_params(config, hours)` helper that produces a `SearchParams` instance and is unit-tested for the mapping (`max_days_old=7` → `hours=168`, etc.).

**Severity:** Low. Pure data mapping.

---

## 4. Verification Strategy — proving no regression

The "no regression" claim is testable. Before and after each integration phase, capture two artifacts and diff them.

### Capture script (Phase A deliverable)

A new script `scripts/capture_ingest_baseline.py` that:
1. Runs the ingest pipeline against a fixed `--hours 24` window with a fixed search config.
2. Collects the `(source, count, sample_listing_dict)` triples — sample is the first 3 listings per source.
3. Writes a JSON file named `docs/baselines/2026-04-27-pre-aggregator.json` (and similar timestamped files for after each phase).

### Diff procedure

After each phase, re-run the capture and diff against the prior baseline. Acceptance:
- **Counts:** within ±10% per source (real-world API jitter accounts for some noise; >10% delta in either direction is investigated).
- **Sample fields:** `source`, `source_id`, `title`, `company`, `location`, `salary_min`, `salary_max`, `description` (first 200 chars), `redirect_url`, `created_at` are byte-identical for at least 1 of 3 sample listings per source. (The other two may differ if the upstream API rotated them between captures.)
- **Description length distribution:** mean and median description length per source within ±20% — catches cases where `description_source` resolution changes from `"full"` to `"snippet"` due to a `skip_scrape` mismatch.

### Baseline files are committed artifacts

Per the "Verify Artifact Persistence" rule in `~/.claude/CLAUDE.md`: every `docs/baselines/*.json` file must be `git add`-ed in the same PR that references it. The capture script's output path is determined by the script, not by an out-of-band manual copy.

### Nightly canary (Phase B → Phase C gate)

After Phase B merges, the next **3 scheduled ingest runs** are watched manually: do counts / fetched-vs-scored ratios / cost estimates land in the same range as pre-integration? If yes for all 3, Phase C (deletion) unblocks. (Aligned with Decision Log #7 — single-run soak is insufficient for an irreversible deletion.)

---

## 5. Sub-Issue Table

Filed under Milestone #8 ("Phase 2: job-aggregator integration"), epic #345.

| # | Title | Phase | Depends on | Acceptance criteria summary |
|---|---|---|---|---|
| #346 | Spike: define SourceProvider Protocol + route arbeitnow (no on-disk migration) | A | (none) | Protocol defined with job-matcher-pr-shaped value types (`is_enabled`, `credentials_required`, `default`); `JobAggregatorProvider` is the only file importing `job_aggregator` (CI-enforced); `providers.json` on-disk format unchanged; arbeitnow routed through Protocol via in-memory legacy→native translation; `CredentialsError` per-source isolation tested; source-key round-trip test passes; DB-shape compatibility (not byte-identity) verified for 10 sample listings; docker-build smoke test passes; pre/post baselines committed |
| #347 | Migrate remaining 9 sources + on-disk `providers.json` migration | B | #346 merged | All 10 sources route through `JobAggregatorProvider`; on-disk `providers.json` migrated to native shape with `enabled` extension key preserved; zero `providers["job_sources"]` references in codebase; per-source counts & samples within tolerance vs Phase A baseline; Adzuna env-var injection works against native shape; settings UI still renders; `scripts/deploy-remote-linux.sh` preflight migrates prod's `providers.json` and aborts on failure; pre/post baselines committed |
| #348 | Delete plugins/sources/ and legacy plugin loader; rewire settings UI to Protocol | C | #347 merged + **3** successful nightly runs | `git ls-tree HEAD -- plugins/sources/` empty; `services/provider_schemas.py` source-schema path consumes `provider.list_sources()` returning Protocol `SourceInfo` (no `job_aggregator` import); settings UI lists all sources with correct credential fields and per-source enablement; `ruff` clean; CI grep step still green; tests green; pre/post baselines committed |
| #349 | Update CLAUDE.md and README to reflect SourceProvider architecture | D | #348 merged | "Adding New Job Sources" describes the `SourceProvider` Protocol and points at upstream `docs/plugin_authoring.md`; no references to `plugins/sources/` remain in any committed `.md` file outside `docs/superpowers/plans/` (CI-enforced grep); pre/post baselines committed (lightweight — docs-only diff) |
| #350 | (conditional) Cut job-aggregator 1.0.0 + publish | E | Triggered by A or B finding | Upstream issue filed, PR merged, tag cut, `requirements.txt` updated |

---

## 6. Resolved Questions

Original four questions (2026-04-27, user) plus inquisitor-pass resolutions (2026-04-27, post-review). All recorded for the record.

**Original:**
1. ~~Confirm Option A?~~ → **Library import, but wrapped behind a `SourceProvider` Protocol so the core system stays decoupled from job-aggregator specifically.** See updated §1.
2. ~~`providers.json` shape?~~ → **Migrate to native shape** via one-shot script. (Inquisitor revision: timing moved from Phase A to Phase B — see Decision Log #3.)
3. ~~Phase E leapfrog?~~ → **No.** Stays conditional.
4. ~~Baseline capture cadence?~~ → **All phases** (A, B, C, D). Capture script is a Phase A deliverable.

**Inquisitor pass (2026-04-27):**
5. ~~Are the Protocol value types decoration or real decoupling?~~ → **Real decoupling.** Value types redesigned around what job-matcher-pr's pipeline + Settings UI consume (`is_enabled`, `credentials_required`, `default`), not around upstream's shape. Bridge does real translation. See §1 + Decision Log #2.
6. ~~Does Phase A's partial migration break the 9 in-tree sources?~~ → **Yes — fixed by deferring on-disk migration to Phase B.** See Decision Log #3 + Phase A/B scope.
7. ~~Is the "no `job_aggregator` import outside the bridge" rule enforced?~~ → **Now CI-enforced** via grep step (or `import-linter` contract). See Phase A acceptance criteria.
8. ~~Is "byte-identical canonical fields" achievable?~~ → **No — replaced with DB-shape compatibility.** Upstream is a proper superset; the bridge translates and discards upstream-only fields. See §1 + Phase A criteria.
9. ~~Is the source-key round-trip claim a real barrier?~~ → **Now an automated test** (`tests/test_source_keys_round_trip.py`). See Risk #3.
10. ~~Does `CredentialsError` abort the whole ingest?~~ → **Yes if unhandled — now caught per-source in `make_clients()`.** See Risk #5.
11. ~~Soak time before Phase C deletion?~~ → **3 nightly runs** (was 1). See Decision Log #7 + §4.
12. ~~Rollback procedures?~~ → **Documented per phase.** See per-phase Rollback subsections in §2.
13. ~~Where does `is_enabled` live after migration?~~ → **In `providers.json` as a per-plugin extension key** (`plugins.<key>.enabled`). Upstream's `make_enabled_sources` infers from credential presence and ignores unknown keys; verify at Phase B planning time. See Decision Log #9.
14. ~~Prod-server `providers.json` migration path?~~ → **Wired into `scripts/deploy-remote-linux.sh` preflight in Phase B.** Migration runs on first deploy after Phase B merges; deploy aborts on migration failure. See Phase B scope + Risk #2.
15. ~~Local-install `git+file://` syntax verified?~~ → **Docker-build smoke test added to Phase A acceptance criteria.** See Phase A.
16. ~~Markdown grep rule for `plugins/sources/` enforced?~~ → **CI step in Phase D.** See Phase D acceptance criteria.

**Inquisitor pass v2 (2026-04-27, second adversarial round):**
17. ~~Does `make_enabled_sources` actually consult `enabled`?~~ → **No — bridge must filter explicitly.** `JobAggregatorProvider.make_clients()` reads the per-source `enabled` field and skips disabled sources before invoking `make_enabled_sources`. See Decision Log #9 + Phase A acceptance criteria. Closes NEW-HIGH-1 (the named single most-likely failure mode of v1).
18. ~~Does the `requirements.txt` `file:///` line work in Linux Docker?~~ → **No — replaced with git URL pointing at upstream commit SHA.** See Decision Log #10. Closes NEW-CRIT-2.
19. ~~What's the feature-flag mechanism?~~ → **Env var `JOB_AGGREGATOR_SOURCES`** (comma-separated source keys). See Decision Log #11. Closes NEW-MED-1.
20. ~~Does `LegacyInTreeProvider` survive as dead code?~~ → **No — it's spec'd in `job_sources/legacy_provider.py` and explicitly deleted in Phase B.** See Decision Log #12. Closes NEW-HIGH-3.
21. ~~Does the bridge pass the right dict shape to `make_enabled_sources`?~~ → **Inner per-plugin dict, not the whole `providers` dict.** Documented + unit-tested. See Decision Log #13. Closes NEW-MED-2.
22. ~~Does `SourceClient.pages() -> Iterator[list[dict]]` leak upstream's shape?~~ → **Possibly — return type finalized during the Phase A spike.** Initial sketch is `list[dict]`; revised to a TypedDict job-matcher-pr owns if implementation reveals leakage. Decision Log #14. Acknowledges NEW-CRIT-1 with deferred-to-spike resolution rather than upfront over-spec.
23. ~~Does the auto-migration safety net live forever?~~ → **No — explicit removal in Phase D with on-disk-shape assertion.** Decision Log #15. Closes NEW-HIGH-2.

---

## 7. Out of Scope

Explicitly not in this plan:

- Touching the LLM provider chain (`providers/`, `credentials.py` LLM section, `_PROVIDER_CLASS_MAP`). Those are unrelated.
- Refactoring `_safe_pages`, the geo filter, the prefilter, or any other pipeline stage. The integration is at the source-fetch boundary only.
- Touching the database schema. The `(source, source_id)` constraint and existing rows must round-trip — see Risk #3.
- Scoring, dedup, or persistence logic. None of these change.
- Updating the dev/prod docker compose files for the integration itself (no new env vars, no new mounts). `scripts/deploy-remote-linux.sh` IS updated in Phase B for the migration preflight, but compose files are not touched.
- **Adopting upstream-only `JobRecord` fields** (`description_source`, `extra`, `remote_eligible`, the `url` vs `redirect_url` rename, `company: None` vs `company: ""`). These are deliberately discarded by the bridge. Adopting any of them is a separate decision with its own DB-schema and UI implications, evaluated post-integration if needed.
- **Adopting upstream's preferred typing for value types** (e.g. `dataclass`-based `PluginInfo` directly). The Protocol approach is the contract; if a future phase wants to collapse the Protocol and use upstream's types directly, that is a separate decision and a separate PR.

---

*Generated by Claude Code on behalf of @cbeaulieu-gt for scoping review. No code has been written, no issues filed, no commits made.*
