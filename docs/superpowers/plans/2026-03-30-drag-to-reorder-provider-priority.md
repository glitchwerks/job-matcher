# Drag-to-Reorder LLM Provider Priority — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a drag-to-reorder Fallback Order section to the LLM Providers tab so users can set provider fallback priority by dragging rows; order is persisted immediately to `providers.json` via a new `/api/providers/reorder` endpoint.

**Architecture:** SortableJS (vendored) drives client-side drag; an `onEnd` callback POSTs the new order to `/api/providers/reorder` as JSON; the endpoint validates keys, calls `save_providers({"provider_order": order})`, and returns an HTTP 200. On failure the JS reverts the DOM. A new partial template `_provider_order.html` renders the list and is `{% include %}`d in `settings.html`; the endpoint also renders it for its 200 response.

**Tech Stack:** Flask, SortableJS 1.15.x, HTMX 1.9 (already present), Jinja2, pytest, existing `save_providers()` / `_load_providers_safe()` from `credentials.py` / `app.py`.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `static/js/sortable.min.js` | Create | Vendored SortableJS — no CDN dependency |
| `templates/_provider_order.html` | Create | Fragment: drag list `<ul>` — used by `{% include %}` and the endpoint response |
| `templates/settings.html` | Modify | Add Fallback Order section + SortableJS `<script>` tag + init script + CSS |
| `app.py` | Modify | Add `POST /api/providers/reorder` route |
| `tests/test_reorder.py` | Create | TDD tests for the reorder endpoint |

---

## Task 1 — Vendor SortableJS

**Files:**
- Create: `static/js/sortable.min.js`

- [ ] **Step 1: Download SortableJS minified build**

```powershell
Invoke-WebRequest -Uri "https://cdn.jsdelivr.net/npm/sortablejs@1.15.3/Sortable.min.js" -OutFile "static/js/sortable.min.js"
```

Expected: file created at `static/js/sortable.min.js`, ~50 KB.

- [ ] **Step 2: Verify it loaded correctly**

```powershell
(Get-Item "static/js/sortable.min.js").Length
```

Expected: output is a number greater than 40000.

- [ ] **Step 3: Commit**

```powershell
git add static/js/sortable.min.js
git commit -m "chore: vendor SortableJS 1.15.3 to static/js (closes no issue — prep for #150)"
```

---

## Task 2 — TDD: Write failing tests for `/api/providers/reorder`

**Files:**
- Create: `tests/test_reorder.py`

- [ ] **Step 1: Create the test file**

```python
# tests/test_reorder.py
"""
TDD tests for POST /api/providers/reorder.

Covered cases:
- Valid order returns 200 and writes provider_order to providers.json
- Response body contains provider display names in submitted order
- Unknown provider key in order returns 400
- Missing 'order' key in JSON body returns 400
- Non-JSON body returns 400
- Empty order list returns 200 (valid — falls back to registry order at runtime)
- Subset of providers (not all listed) returns 200
- Write failure (OSError) returns 500
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from app import app as flask_app


@pytest.fixture()
def tmp_providers_path(tmp_path, monkeypatch):
    """Point _PROVIDERS_PATH at a temp file for full isolation."""
    path = str(tmp_path / "providers.json")
    monkeypatch.setattr(app_module, "_PROVIDERS_PATH", path)
    return path


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


class TestReorderEndpoint:
    def test_valid_order_returns_200(self, client, tmp_providers_path):
        from providers import _PROVIDER_CLASS_MAP
        valid_order = list(_PROVIDER_CLASS_MAP.keys())
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": valid_order}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_valid_order_writes_provider_order_to_file(self, client, tmp_providers_path):
        from providers import _PROVIDER_CLASS_MAP
        keys = list(_PROVIDER_CLASS_MAP.keys())
        # Reverse order to make the write detectable
        new_order = list(reversed(keys))
        client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": new_order}),
            content_type="application/json",
        )
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["provider_order"] == new_order

    def test_response_contains_provider_names_in_order(self, client, tmp_providers_path):
        from providers import _PROVIDER_CLASS_MAP
        keys = list(_PROVIDER_CLASS_MAP.keys())
        new_order = list(reversed(keys))
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": new_order}),
            content_type="application/json",
        )
        html = resp.data.decode()
        # Confirm order by checking position of each provider name in the HTML
        positions = []
        for key in new_order:
            cls = _PROVIDER_CLASS_MAP[key]
            name = cls.settings_schema()["display_name"]
            positions.append(html.index(name))
        assert positions == sorted(positions), "Provider names not in submitted order in response HTML"

    def test_unknown_provider_key_returns_400(self, client, tmp_providers_path):
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": ["anthropic", "not_a_real_provider"]}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_unknown_provider_does_not_write_to_file(self, client, tmp_providers_path):
        client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": ["unknown_llm"]}),
            content_type="application/json",
        )
        assert not os.path.exists(tmp_providers_path)

    def test_missing_order_key_returns_400(self, client, tmp_providers_path):
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"wrong_key": []}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_non_json_body_returns_400(self, client, tmp_providers_path):
        resp = client.post(
            "/api/providers/reorder",
            data="not json at all",
            content_type="text/plain",
        )
        assert resp.status_code == 400

    def test_empty_order_returns_200(self, client, tmp_providers_path):
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": []}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_empty_order_writes_empty_list_to_file(self, client, tmp_providers_path):
        client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": []}),
            content_type="application/json",
        )
        with open(tmp_providers_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["provider_order"] == []

    def test_subset_order_returns_200(self, client, tmp_providers_path):
        from providers import _PROVIDER_CLASS_MAP
        keys = list(_PROVIDER_CLASS_MAP.keys())
        # Submit only the first provider
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": [keys[0]]}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_write_failure_returns_500(self, client, tmp_providers_path, monkeypatch):
        from providers import _PROVIDER_CLASS_MAP
        valid_order = list(_PROVIDER_CLASS_MAP.keys())

        def _failing_save(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(app_module, "save_providers", _failing_save)
        resp = client.post(
            "/api/providers/reorder",
            data=json.dumps({"order": valid_order}),
            content_type="application/json",
        )
        assert resp.status_code == 500
```

- [ ] **Step 2: Run tests to confirm they all fail (endpoint doesn't exist yet)**

```powershell
pytest tests/test_reorder.py -v 2>&1 | Select-Object -First 40
```

Expected: most tests fail with `404` (route not found) or `AssertionError`.

---

## Task 3 — Implement `/api/providers/reorder` in `app.py`

**Files:**
- Modify: `app.py` (add route after the `validate_keys` route, around line 580)

- [ ] **Step 1: Find the right insertion point**

```powershell
Select-String -Path "app.py" -Pattern "def validate_keys|def settings" | Select-Object LineNumber, Line
```

- [ ] **Step 2: Add the route to `app.py`**

Insert the following after the `validate_keys` function (locate the line where it ends and insert below it):

```python
@app.route("/api/providers/reorder", methods=["POST"])
def api_providers_reorder():
    """Persist a new LLM provider fallback order.

    Expects JSON body: ``{"order": ["anthropic", "gemini", "openai"]}``

    * All entries must be known keys in ``_PROVIDER_CLASS_MAP``; unknown keys → 400.
    * ``order`` may be a subset of the registry (omitted providers are appended at
      runtime by ``build_provider_chain()``).
    * Writes only ``provider_order`` at the top level of ``providers.json``.
    * Returns the rendered ``_provider_order.html`` fragment on success (200).
    * Returns a plain-text error message on failure (400/500).
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return "Invalid request body — expected JSON object.", 400

    order = body.get("order")
    if not isinstance(order, list):
        return "Missing or invalid 'order' field — expected a JSON array.", 400

    unknown = [k for k in order if k not in _PROVIDER_CLASS_MAP]
    if unknown:
        return f"Unknown provider key(s): {', '.join(unknown)}", 400

    try:
        save_providers({"provider_order": order}, providers_path=_PROVIDERS_PATH)
    except OSError:
        return "Could not save order — check file permissions.", 500

    # Re-build llm_schemas in the new order for the response fragment.
    providers_data = _load_providers_safe()
    llm_section: dict = providers_data.get("llm") or {}
    seen: set[str] = set()
    llm_schemas: list[tuple[str, dict, bool]] = []
    for key in order:
        if key in _PROVIDER_CLASS_MAP and key not in seen:
            cls = _PROVIDER_CLASS_MAP[key]
            schema = cls.settings_schema()
            cfg = llm_section.get(key) or {}
            has_values = bool(cfg.get("api_key", "").strip())
            llm_schemas.append((key, schema, has_values))
            seen.add(key)
    for key in _PROVIDER_CLASS_MAP:
        if key not in seen:
            cls = _PROVIDER_CLASS_MAP[key]
            schema = cls.settings_schema()
            cfg = llm_section.get(key) or {}
            has_values = bool(cfg.get("api_key", "").strip())
            llm_schemas.append((key, schema, has_values))
            seen.add(key)

    return render_template("_provider_order.html", llm_schemas=llm_schemas)
```

Also ensure `save_providers` is imported at the top of `app.py` — it should already be there (added in #156). Verify:

```powershell
Select-String -Path "app.py" -Pattern "from credentials import|save_providers"
```

- [ ] **Step 3: Run the tests — they should still fail (template missing)**

```powershell
pytest tests/test_reorder.py -v 2>&1 | Select-Object -First 30
```

Expected: failures change from 404 to `TemplateNotFound` for `_provider_order.html`.

---

## Task 4 — Create `_provider_order.html` fragment template

**Files:**
- Create: `templates/_provider_order.html`

- [ ] **Step 1: Create the template**

```html
{# _provider_order.html — drag-to-reorder list fragment.
   Context variables:
     llm_schemas  list of (provider_key, schema_dict, has_values_bool)
                  in the desired display order.
   This partial is both {% include %}'d in settings.html and returned
   as an HTTP response fragment from POST /api/providers/reorder.
#}
<ul id="provider-order-list" class="provider-order-list">
  {% for provider_key, schema, has_values in llm_schemas %}
  <li
    data-id="{{ provider_key }}"
    class="order-item{% if not has_values %} order-item--unconfigured{% endif %}">
    <span class="drag-handle" aria-hidden="true">&#8286;</span>
    <span class="order-position">{{ loop.index }}</span>
    <span class="order-provider-name">{{ schema.display_name }}</span>
    {% if has_values %}
      <span class="key-status configured">&#9679; configured</span>
    {% else %}
      <span class="key-status not-set">&#9675; not set</span>
    {% endif %}
  </li>
  {% endfor %}
</ul>
```

- [ ] **Step 2: Run the reorder tests — most should now pass**

```powershell
pytest tests/test_reorder.py -v
```

Expected: all 11 tests pass.

- [ ] **Step 3: Run the full test suite to check for regressions**

```powershell
pytest --tb=short -q
```

Expected: all existing tests still pass, 0 failures.

- [ ] **Step 4: Commit**

```powershell
git add app.py templates/_provider_order.html tests/test_reorder.py
git commit -m "feat: add POST /api/providers/reorder endpoint with TDD (refs #150)"
```

---

## Task 5 — Wire Fallback Order section into `settings.html`

**Files:**
- Modify: `templates/settings.html`

This task adds:
1. CSS for the drag list (inside the existing `<style>` block)
2. A "Fallback Order" section above the provider cards in the LLM tab
3. SortableJS `<script>` tag
4. SortableJS initialization script

- [ ] **Step 1: Add CSS to the `<style>` block in `settings.html`**

Locate the closing `</style>` tag (around line 42) and insert before it:

```css
    /* ── Provider drag-to-reorder list ──────────────────────────── */
    .order-section {
      margin-bottom: 2rem;
    }
    .order-section-heading {
      font-size: 0.85rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-secondary);
      margin: 0 0 0.4rem;
    }
    .order-section-hint {
      font-size: 0.8rem;
      color: var(--text-secondary);
      margin: 0 0 0.75rem;
    }
    .provider-order-list {
      list-style: none;
      margin: 0;
      padding: 0;
    }
    .order-item {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      padding: 0.5rem 0.75rem;
      margin-bottom: 0.3rem;
      background: var(--card-bg, #1e1e2e);
      border: 1px solid var(--border-mid);
      border-radius: 6px;
      cursor: grab;
      user-select: none;
    }
    .order-item:active { cursor: grabbing; }
    .order-item--unconfigured { opacity: 0.55; }
    .drag-handle {
      color: var(--text-secondary);
      font-size: 1.1rem;
      cursor: grab;
      flex-shrink: 0;
    }
    .order-position {
      font-size: 0.75rem;
      font-weight: 700;
      color: var(--text-accent);
      min-width: 1.2rem;
      text-align: right;
      flex-shrink: 0;
    }
    .order-provider-name {
      flex: 1;
      font-size: 0.9rem;
      color: var(--text-primary);
    }
    .order-error {
      margin-top: 0.5rem;
      font-size: 0.85rem;
      color: var(--danger, #f38ba8);
    }
    .sortable-ghost { opacity: 0.3; }
    .sortable-drag  { opacity: 0.9; box-shadow: 0 4px 12px rgba(0,0,0,0.4); }
```

- [ ] **Step 2: Add the Fallback Order section inside the LLM Providers tab pane**

Locate the line `<form class="settings-form" method="post" action="/settings">` inside `pane-llm` (around line 103) and insert the following **before** it:

```html
    {# ── Fallback Order ──────────────────────────────────────────── #}
    <div class="order-section">
      <p class="order-section-heading">Fallback Order</p>
      <p class="order-section-hint">Drag to reorder — provider #1 is tried first. Unconfigured providers are skipped at runtime.</p>
      <div id="provider-order-container">
        {% include "_provider_order.html" %}
      </div>
      <p id="order-error" class="order-error" style="display:none"></p>
    </div>

```

- [ ] **Step 3: Add the SortableJS script tag and init script before `</body>`**

Locate the closing `</body>` tag and insert before it (after the existing tab-switching script):

```html
{# ── SortableJS drag-to-reorder ──────────────────────────────── #}
<script src="/static/js/sortable.min.js"></script>
<script>
(function () {
  var container  = document.getElementById('provider-order-container');
  var errorEl    = document.getElementById('order-error');
  if (!container) return;

  var listEl     = container.querySelector('#provider-order-list');
  var savedOrder = null;

  function updatePositionBadges() {
    var items = listEl.querySelectorAll('.order-item');
    items.forEach(function (item, i) {
      var badge = item.querySelector('.order-position');
      if (badge) badge.textContent = i + 1;
    });
  }

  var sortable = Sortable.create(listEl, {
    animation: 150,
    handle: '.drag-handle',
    ghostClass: 'sortable-ghost',
    dragClass:  'sortable-drag',

    onStart: function () {
      /* Capture order before the drag so we can revert on failure. */
      savedOrder = sortable.toArray();
    },

    onEnd: function () {
      var newOrder = sortable.toArray();
      updatePositionBadges();          /* optimistic update */

      fetch('/api/providers/reorder', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ order: newOrder }),
      })
      .then(function (resp) {
        if (resp.ok) {
          /* Replace container with server-confirmed fragment. */
          resp.text().then(function (html) {
            container.innerHTML = html;
            listEl = container.querySelector('#provider-order-list');
            sortable.destroy();
            sortable = Sortable.create(listEl, sortable.options);
          });
          if (errorEl) errorEl.style.display = 'none';
        } else {
          /* Revert DOM to pre-drag order. */
          if (savedOrder) sortable.sort(savedOrder);
          updatePositionBadges();
          if (errorEl) {
            errorEl.textContent = 'Could not save order — check file permissions.';
            errorEl.style.display = '';
          }
        }
      })
      .catch(function () {
        if (savedOrder) sortable.sort(savedOrder);
        updatePositionBadges();
        if (errorEl) {
          errorEl.textContent = 'Could not save order — network error.';
          errorEl.style.display = '';
        }
      });
    },
  });
}());
</script>
```

- [ ] **Step 4: Run the full test suite**

```powershell
pytest --tb=short -q
```

Expected: all tests pass, 0 failures.

- [ ] **Step 5: Manual smoke test**

Start the app (`python app.py`) and open the **Settings** page.

Verify:
- [ ] The LLM Providers tab shows a "Fallback Order" section above the provider cards
- [ ] Each provider row shows a number (1, 2, 3…), display name, and configured/not-set badge
- [ ] Dragging a row reorders it; position numbers update immediately
- [ ] After a drag, `providers.json` on disk reflects the new `provider_order`
- [ ] Refreshing the page shows the saved order
- [ ] Unconfigured providers (empty api_key) appear dimmed but are still draggable

- [ ] **Step 6: Commit**

```powershell
git add templates/settings.html templates/_provider_order.html
git commit -m "feat: drag-to-reorder LLM fallback order in Settings UI (refs #150)"
```

---

## Task 6 — Final commit and push

- [ ] **Step 1: Run full test suite one last time**

```powershell
pytest --tb=short -q
```

Expected: all tests pass, 0 failures.

- [ ] **Step 2: Push branch and open PR**

```powershell
git checkout -b feat/drag-to-reorder-150
git push -u origin feat/drag-to-reorder-150
```

Then open a PR targeting `main`, referencing `closes #150` in the body.

---

## Self-Review Notes

**Spec coverage check:**
- ✅ Vendor SortableJS → Task 1
- ✅ `/api/providers/reorder` endpoint with validation + error handling → Task 3
- ✅ Writes only `provider_order` at top level of `providers.json` → Task 3 (`save_providers({"provider_order": order})`)
- ✅ HTMX fragment response on success → Task 4 (`_provider_order.html`)
- ✅ DOM rollback on non-200 → Task 5 (`onEnd` catch + `sortable.sort(savedOrder)`)
- ✅ Error message: "Could not save order — check file permissions." → Task 5 (matches spec exactly)
- ✅ Unconfigured providers dimmed + repositionable → Task 4 (`order-item--unconfigured` class) + Task 5 (all providers included in list)
- ✅ Order persisted immediately on drop, no Save button for ordering → Task 5 (fetch in `onEnd`, no form submit)
- ✅ Explicit numbered position badges (user confirmed) → Task 4 (`.order-position` with `loop.index`)

**Placeholder scan:** No TBDs, TODOs, or vague steps found.

**Type consistency:** `llm_schemas` is `list[tuple[str, dict, bool]]` throughout — matches existing `app.py` usage in `GET /settings` and the new endpoint.
