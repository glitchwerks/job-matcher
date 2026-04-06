# Job Matcher — UI Style Guide

> This document extracts and formalizes the design language already present in `static/style.css`. It is the reference for anyone adding new UI components. Do not introduce patterns that contradict what is documented here — update this file first if a new pattern is genuinely needed.

---

## 1. Design Philosophy

**Industrial terminal-ledger.** The UI reads like a terse status board — near-black ground, amber accent for interactive and highlighted elements, monospaced type for all metadata and labels. Score tiers are rendered as rack status indicators (green / amber / red). No decorative chrome earns its place without a functional reason.

The aesthetic is intentionally spare: one max-width column, no sidebar, no gradients, no shadows except on hover-lifted cards.

---

## 2. Design Tokens

All values live in the `:root` block of `static/style.css`. **Never hard-code hex values** — always reference a token.

### Backgrounds

| Token | Value | Use |
|---|---|---|
| `--bg-base` | `#0f1117` | Page background |
| `--bg-surface` | `#171b23` | Cards, panels, form sections |
| `--bg-raised` | `#1e2330` | Inputs, raised chips, neutral badges |
| `--bg-hover` | `#252b38` | Hover background on interactive rows |

### Borders

| Token | Value | Use |
|---|---|---|
| `--border-subtle` | `#2a2f3d` | Dividers, card borders at rest |
| `--border-mid` | `#373e50` | Standard control borders |
| `--border-strong` | `#525c72` | Focused controls, active states |

### Text

| Token | Value | Use |
|---|---|---|
| `--text-primary` | `#eceef5` | Main content, card titles, focused inputs |
| `--text-secondary` | `#b0bace` | Supporting content, table cells, secondary labels |
| `--text-muted` | `#7a8599` | Placeholders, timestamps, tertiary metadata |
| `--text-accent` | `#f5a623` | Interactive highlights, active tabs, amber values |

### Score Tier Colors

Each tier has a `bg` / `text` / `border` triplet. Use the full triplet together.

| Tier | Variable Prefix | Color | When to use |
|---|---|---|---|
| High | `--score-high-*` | Green | Score ≥ 8, configured, success, matched, remote |
| Mid | `--score-mid-*` | Golden | Score 5–7, warnings, setup banners |
| Low | `--score-low-*` | Red | Score < 5, errors, missing skills |
| Null | `--score-null-*` | Neutral grey | Unscored, not-set, unknown |

### Skill Chip Colors

| Variant | Variable Prefix |
|---|---|
| Matched skill | `--chip-match-*` (green) |
| Missing skill | `--chip-miss-*` (red) |

### Card Left-Border Accents

Applied to `.card-details` via `data-tier` attribute.

| Token | Color | Tier |
|---|---|---|
| `--accent-high` | `#4df590` | high |
| `--accent-mid` | `#ffd24d` | mid |
| `--accent-low` | `#ff6b6b` | low |
| `--accent-null` | `#373e50` | null |

### Buttons

| Token | Value |
|---|---|
| `--btn-bg` | `#1e2330` |
| `--btn-hover` | `#262e3f` |
| `--btn-border` | `#373e50` |

### Amber-accented button states

Used by `.btn-view`, `.btn-ingest`, and `.ingest-status-running` — amber-tinted interactive controls.

| Token | Value | Use |
|---|---|---|
| `--text-accent-bright` | `#f5b040` | Hover color on amber-accented buttons; slightly brighter than `--text-accent` |
| `--btn-amber-border` | `#3a2e10` | Border at rest for amber-accented buttons |
| `--btn-amber-hover-bg` | `#221c08` | Hover background for amber-accented buttons |
| `--btn-amber-hover-border` | `#5a4820` | Hover border for amber-accented buttons |

### Active / filled input state

Applied to `.filter-select` and `.filter-input` when they have a non-default value, and to `.btn-bookmark.bookmarked`.

| Token | Value | Use |
|---|---|---|
| `--input-active-bg` | `#1e1500` | Background tint on active/filled inputs and bookmarked button |
| `--input-active-border` | `#5a3a00` | Border on active/filled inputs and bookmarked button |

### Applied button hover

Used by `.btn-apply.applied:hover` — a brighter green to signal confirmed action.

| Token | Value | Use |
|---|---|---|
| `--btn-apply-hover-color` | `#2acc65` | Hover text color for applied state |
| `--btn-apply-hover-bg` | `#0a2215` | Hover background for applied state |
| `--btn-apply-hover-border` | `#1a5c36` | Hover border for applied state |

### Dismiss button hover

Used by `.btn-dismiss:hover` — dark red tint.

| Token | Value | Use |
|---|---|---|
| `--btn-dismiss-hover-bg` | `#1a0808` | Hover background for dismiss button |
| `--btn-dismiss-hover-border` | `#3a1010` | Hover border for dismiss button |

### Misc

| Token | Value | Use |
|---|---|---|
| `--color-white` | `#fff` | Pure white; used for the toggle-switch knob |

### Typography

| Token | Stack |
|---|---|
| `--font-body` | Georgia, Times New Roman, serif |
| `--font-mono` | Menlo, Consolas, Cascadia Code, Courier New, monospace |
| `--font-ui` | -apple-system, Segoe UI, system-ui, sans-serif |

### Layout & Radius

| Token | Value |
|---|---|
| `--max-width` | `860px` |
| `--radius-sm` | `4px` |
| `--radius-md` | `6px` |
| `--radius-lg` | `10px` |

---

## 3. Typography

| Context | Font | Size | Weight | Transform / Spacing |
|---|---|---|---|---|
| Page / section headings (`.page-heading`) | `--font-mono` | 0.72rem | normal | uppercase, 0.08em |
| Settings section titles | `--font-ui` | 0.85rem | 600 | uppercase, 0.04em |
| Card / provider titles | `--font-ui` | 0.93–0.95rem | 600 | — |
| Body / verdict text | `--font-body` | 0.84–0.88rem | normal | — |
| Field labels (`.settings-label`) | `--font-mono` | 0.68rem | normal | uppercase, 0.08em |
| Metadata / badges / chips | `--font-mono` | 0.65–0.76rem | normal | 0.02–0.10em |
| Stat values (`.stat-value`) | `--font-mono` | 1.55rem | normal | 0.02em, `--text-accent` |
| Logo (`.site-logo`) | `--font-mono` | 0.85rem | normal | uppercase, 0.12em, `--text-muted` |
| Nav tabs (`.nav-tab`) | `--font-mono` | 0.75rem | normal | uppercase, 0.08em |

**Rule:** Use `--font-mono` for all metadata, labels, badges, and code-adjacent content. Use `--font-ui` for headings and names. Use `--font-body` for prose (verdicts, descriptions).

---

## 4. Color Usage Matrix

| Semantic Context | Background | Text | Border |
|---|---|---|---|
| Success / configured / matched / remote | `--score-high-bg` | `--score-high-text` | `--score-high-border` |
| Warning / mid-score / setup banner | `--score-mid-bg` | `--score-mid-text` | `--score-mid-border` |
| Error / low-score / missing | `--score-low-bg` | `--score-low-text` | `--score-low-border` |
| Neutral / unscored / not-set | `--bg-raised` | `--text-muted` | `--border-subtle` |
| On-site / inactive badge | `--bg-raised` | `--text-muted` | `--border-mid` |
| Interactive highlight / accent | — | `--text-accent` | `--text-accent` (as border) |

---

## 5. Component Reference

### Layout

| Class | Element | Notes |
|---|---|---|
| `.page-wrap` | `<div>` | Max-width 860px, centered, padding 0 20px 60px |
| `.card-list` | `<div>` | Flex column, gap 16px |

### Demo Banner

A full-width sticky bar rendered immediately after `<body>` and before `.page-wrap`. Only visible when the app is running with `--demo`.

| Class | Element | Notes |
|---|---|---|
| `.demo-banner` | `<div>` | `position: sticky; top: 0; z-index: 100`; bg `--bg-raised`; text `--text-accent`; border-bottom `--btn-amber-border`; `--font-mono` 0.75rem uppercase, letter-spaced; centered |

### Environment Status Bar

A full-width bar rendered immediately after `<body>` (before the demo banner when both are present) via `{% include '_status_bar.html' %}`. **Only visible when `APP_ENV != 'local'`** — absent during local development entirely (the Jinja `{% if %}` block renders nothing).

The bar displays the active environment name and the short git SHA (`APP_VERSION`), giving a persistent at-a-glance reminder of which stack is being viewed.

**Variants:**

| Class | Environment | Tokens | Notes |
|---|---|---|---|
| `.env-status-bar--dev` | `APP_ENV=dev` | `--score-mid-bg` / `--score-mid-text` / `--score-mid-border` | Amber — signals a non-production deployment |
| `.env-status-bar--prod` | `APP_ENV=prod` | `--bg-surface` / `--text-muted` / `--border-subtle` | Neutral — production is the default, no alarm color needed |

**Child elements:**

| Class | Notes |
|---|---|
| `.env-status-bar__env` | The environment label (`dev` or `prod`), uppercase via CSS |
| `.env-status-bar__sep` | Middle-dot separator (`·`); `--text-muted` regardless of variant |
| `.env-status-bar__sha` | The `APP_VERSION` value (short SHA injected by the deploy workflow); uppercase via CSS |

**Base styles (shared by both variants):** `display: flex; align-items: center; justify-content: center; gap: 0.5rem; padding: 0.25rem 1rem; font-family: var(--font-mono); font-size: 0.7rem; letter-spacing: 0.04em; text-transform: uppercase`.

**When `APP_ENV` is injected:** `app.py` sets `app.jinja_env.globals['APP_ENV']` from the `APP_ENV` environment variable, defaulting to `'local'`. The compose files set `APP_ENV=dev` / `APP_ENV=prod` respectively. Local runs without Docker receive no env var and therefore see no bar.

### Navigation

| Class | Element | Notes |
|---|---|---|
| `.site-header` | `<header>` | Flex row space-between; border-bottom `--border-subtle` |
| `.site-logo` | `<span>` | `--font-mono`, uppercase; accent `<span>` inside uses `--text-accent` |
| `.site-nav` | `<nav>` | Flex gap 2px |
| `.nav-tab` | `<a>` | `--font-mono`, uppercase; add `.active` for current page |

#### Feed sub-navigation

A secondary tab bar that renders below the `<header>` only on feed pages (`/` and `/snippets`). It groups the two feed views under the single top-level **feed** nav tab.

| Class | Element | Notes |
|---|---|---|
| `.feed-sub-nav` | `<nav>` | Flex row, gap 2px, border-bottom `--border-subtle`; placed after `</header>` and before page content |
| `.feed-sub-tab` | `<a>` | `--font-mono` 0.68rem uppercase; lighter than `.nav-tab` (smaller font, less padding) to signal subordinate tier; add `.active` for current sub-page |

**Active state rules:**
- Top-level `feed` nav-tab gets `.active` when `view` is `'feed'` OR `'snippets'`
- `.feed-sub-tab` for `listings` (href `/`) gets `.active` when `view == 'feed'`
- `.feed-sub-tab` for `snippets` (href `/snippets`) gets `.active` when `view == 'snippets'`
- Sub-nav is absent on all non-feed pages (bookmarks, applied, stats, profile, settings)

### Cards (collapsible)

Cards use `<details>`/`<summary>` for native expand/collapse. Tier is set via `data-tier` on the outer element.

| Class | Element | Notes |
|---|---|---|
| `.card-details` | `<details>` | Add `data-tier="high\|mid\|low\|null"` to set left-border accent |
| `.card-summary` | `<summary>` | Collapsed row — flex, hover lifts background |
| `.summary-main` | `<span>` | Flex column inside summary |
| `.summary-title` | `<span>` | `--font-ui` 0.93rem weight 600; truncates with ellipsis |
| `.summary-meta` | `<span>` | `--font-mono` 0.72rem; use `.sep` spans between segments |
| `.card-body` | `<article>` | Expanded content; padding 0 22px 16px |
| `.card-divider` | `<div>` | 1px `--border-subtle` horizontal rule inside card body |

### Badges & Pills

All badges share the pill shape: `border-radius: 20px`, `--font-mono`, padding `2–3px 7–10px`. Always use the full bg/text/border triplet from the color matrix.

| Class | Variant classes | Use |
|---|---|---|
| `.score-badge` | `.tier-high`, `.tier-mid`, `.tier-low`, `.tier-null` | Score display (large) |
| `.score-badge--sm` | Same tier classes | Score display (compact, in summary row) |
| `.badge-remote` | — | Remote location tag (green tier) |
| `.badge-onsite` | — | On-site location tag (neutral) |
| `.badge-jobtype` | — | Job type tag (neutral) |
| `.badge-source` | — | Job source provider tag (neutral, dashed border to distinguish from job-type) |
| `.badge-snippet` | — | Warning indicator shown on cards in the Snippets tab; uses full mid-tier triplet (`--score-mid-bg` / `--score-mid-text` / `--score-mid-border`) to signal that the score is from a short API description, not a full scraped JD; solid border distinguishes it from `.badge-source` |
| `.badge-new` | — | "New" indicator on cards never opened by the user; amber border/text (`--text-accent`); removed via HTMX OOB swap on expand (CSS `[open]` selector kept as fallback); absent on reload once `opened_at` is persisted |
| `.model-badge` | — | LLM model identifier (muted, 70% opacity) |
| `.key-status` | `.configured`, `.not-set` | Settings credential status |
| `.validation-badge` | `.validation-valid`, `.validation-invalid`, `.validation-warning`, `.validation-muted` | API key validation results |
| `.validation-detail` | — | Muted helper text rendered after a `.validation-badge` for non-`valid`, non-`not_configured` states; `--font-body` 0.8rem, `--text-muted` |
| `.chip` | `.matched`, `.missing` | Skill chips in card body |

### Buttons

All buttons extend `.btn` (base). Add a modifier class for semantic variants.

| Class | Color | Use |
|---|---|---|
| `.btn` | `--text-secondary` | Base; inherits by all buttons |
| `.btn-view` | `--text-accent` | External link to job listing |
| `.btn-bookmark` | `--text-muted` → `--star-filled` | Add `.bookmarked` for filled state |
| `.btn-apply` | `--text-muted` → `--score-high-text` | Add `.applied` for confirmed state |
| `.btn-dismiss` | `--text-muted` → `--dismiss-hover` | Transparent background at rest |
| `.btn-save` | Inherits `.btn` | Settings form save; `align-self: flex-start` |
| `.btn-ingest` | `--text-accent` | Trigger ingest run |
| `.btn-validate` | Inherits `.btn` | API key validation trigger |

### Forms & Settings

| Class | Element | Notes |
|---|---|---|
| `.settings-form` | `<form>` | Flex column gap 16px; max-width `var(--max-width)` |
| `.provider-row` | `<div>` | `--bg-surface` card with `--border-subtle`, `--radius-md`, padding 16px 20px |
| `.provider-header` | `<div>` | Flex row, align-items center, gap 10px |
| `.provider-name` | `<span>` | `--font-ui` 0.95rem weight 600, `--text-primary` |
| `.settings-label` | `<label>` | `--font-mono` 0.68rem uppercase 0.08em, `--text-muted` |
| `.settings-label--mt` | modifier on `.settings-label` | Adds `margin-top: 1rem`; use when a label follows a `.row-list` without a natural gap |
| `.settings-input` | `<input>`, `<textarea>` | `--font-mono` 0.76rem; `--bg-raised` bg; width 100% |
| `.filter-input` | `<input type="text">` | Filter bar text input; width 220px |
| `.filter-select` | `<select>` | Filter bar dropdown; custom SVG arrow |
| `.filter-toggle` | `<label>` | Checkbox label wrapper in filter bar |
| `.provider-home-link` | `<a>` | External link icon (↗) placed next to provider names on the Settings → Job Sources tab; `font-size: 0.8rem`, `var(--text-muted)` at rest, `var(--text-accent)` on hover, `transition: color 0.15s` |
| `.source-description` | `<p>` | Short blurb below `.provider-header` on each source card; `--font-body` 0.82rem, `--text-muted`, `line-height: 1.5`; rendered only when `schema.description` is set |
| `.save-bar` | `<div>` | Sticky unsaved-changes bar; see §5 Save Bar below |
| `.save-bar--visible` | modifier on `.save-bar` | Added by JS when a text/password field is dirty; animates in via `max-height` + `opacity`; removed when all fields are restored to original values |
| `.save-bar-label` | `<span>` | "Unsaved changes" text; `--font-mono` 0.8rem, `--score-mid-text` (amber — warning semantics) |

#### Profile form — repeating row inputs

Used on the Profile page for fields where the user manages an ordered list of values (skills, anti-preferences, scoring notes, etc.). Each `.row-list` contains one or more `.row-item` divs. JavaScript adds/removes rows without a page reload.

| Class | Element | Notes |
|---|---|---|
| `.row-list` | `<div>` | Flex column, `gap: 6px`, `margin-bottom: 4px`; wraps all `.row-item` children |
| `.row-item` | `<div>` | Flex row, `align-items: center`, `gap: 8px`; contains one `.settings-input` + one `.btn-row-remove` |
| `.btn-row-remove` | `<button type="button">` | Minus (−) button; muted at rest, red tier (`--score-low-text` / `--score-low-border`) on hover; 0.78rem `--font-mono`; `:focus-visible` amber outline |
| `.btn-row-add` | `<button type="button">` | Dashed-border add button; muted at rest, accent amber on hover; must carry `data-list-id` and `data-name` attributes (used by delegated click handler instead of `onclick`) |
| `.field-hint` | `<p>` | Helper text below a label; `--font-mono` 0.68rem, `--text-muted`; `margin-bottom: 0` by default |
| `.field-hint--mb` | modifier on `.field-hint` | Adds `margin-bottom: 0.75rem`; use when a hint precedes a sub-section heading |
| `.form-section-divider` | `<hr>` | 1px `--border-subtle` rule dividing logical sub-sections within a `.provider-row` card; `margin: 1rem 0 0.5rem` |
| `.form-subsection-title` | `<p>` | `--font-ui` 0.78rem weight 600 uppercase; `--text-secondary`; `margin: 0 0 0.75rem` |
| `.form-subsection-title--no-top` | modifier on `.form-subsection-title` | Sets `margin-top: 0`; use immediately after a `.form-section-divider` to avoid double spacing |

**JS pattern** — row buttons use event delegation (single `document.addEventListener('click', ...)` handler in `profile.html`). Never add `onclick` attributes directly; instead set `data-list-id` and `data-name` on `.btn-row-add` buttons. `.btn-row-remove` needs no data attributes — the handler walks up to `.row-item` / `.row-list` via `closest()`.

### Save Bar

A sticky bar that slides up from the bottom of the form when the user edits a credential field. Dismisses on form submit. HTMX checkbox toggles do **not** trigger it — only `<input type="text">` and `<input type="password">` changes do.

```html
<div class="save-bar" id="save-bar-llm" aria-live="polite" aria-hidden="true">
  <span class="save-bar-label">Unsaved changes</span>
  <button type="button" class="btn btn-save">Save</button>
</div>
```

Place this `<div>` **just before** the existing `<button type="submit" class="btn btn-save">Save</button>` inside the form. The original submit button is kept as a non-JS fallback.

Visibility is controlled by toggling `.save-bar--visible` via a delegated `input` listener scoped to `type === 'text' || type === 'password'`. The bar's Save button click handler is attached via `addEventListener` in the dirty-state IIFE — no inline `onclick`. `aria-hidden` is toggled alongside the CSS class (removed when showing, restored when hiding) so `aria-live="polite"` correctly announces the bar to screen readers when it appears.

| Class | Element | Notes |
|---|---|---|
| `.save-bar` | `<div>` | `position: sticky; bottom: 0`; collapses when hidden via `max-height: 0` + `overflow: hidden` (zero padding, zero margin-top, `opacity: 0`, `pointer-events: none`) — takes up no layout space at rest |
| `.save-bar--visible` | modifier | Expands bar (`max-height: 4rem`, full padding, `margin-top: 1rem`) and makes it opaque and interactive; added/removed by JS |
| `.save-bar-label` | `<span>` | `--font-mono`, `--score-mid-text`; amber signals a pending-action state |

### Toggle Switch

Used for binary on/off controls (e.g. source enabled). Pure CSS — no JS required.

```html
{% if toggle_disabled %}
  <span class="toggle-hint">Add credentials to enable</span>
{% endif %}
<label class="source-toggle">
  <input
    type="checkbox"
    name="..."
    aria-label="Enable [Name]"
    {% if is_enabled and not toggle_disabled %}checked{% endif %}
    {% if toggle_disabled %}disabled{% endif %}>
  <span class="source-toggle-label"></span>
  <span class="source-toggle-track">
    <span class="source-toggle-knob"></span>
  </span>
</label>
```

| Class | Element | Notes |
|---|---|---|
| `.source-toggle` | `<label>` | Outer wrapper; `margin-left: auto` pushes it right in a flex row |
| `.source-toggle-label` | `<span>` | Renders "Enabled" or "Disabled" text via `::before` pseudo-element; driven by `input:checked ~` sibling selector |
| `.source-toggle-track` | `<span>` | 36×20px pill; `--border-mid` at rest, `--text-accent` when checked |
| `.source-toggle-knob` | `<span>` | 14×14px circle; slides right via `calc(36px - 14px - 6px)` on checked; `#fff` |
| `.toggle-hint` | `<span>` | Italic helper text (`var(--text-muted)`, 0.75rem) shown alongside a disabled toggle |

**Checked state** is driven by CSS `input:checked ~` sibling selectors (note `~`, not `+` — the label span sits between the input and the track).

**Disabled state** — when a source requires credentials that are not yet filled in, add `disabled` to the `<input>` and render `.toggle-hint`. Always render the input as unchecked when disabled (prevents a confusing "on but locked" visual). CSS rules:

- `input:disabled ~ .source-toggle-track` and `input:disabled ~ .source-toggle-label` — 40% opacity, `cursor: not-allowed`
- `.source-toggle:has(input:disabled)` — `cursor: not-allowed` on the outer label
- Keyless sources (no required credentials) are never disabled.

### Tabs

| Class | Notes |
|---|---|
| `.settings-tabs` | Flex container; border-bottom `--border-mid` |
| `.settings-tab-btn` | Add `.active` for selected tab; uses `--text-accent` + bottom border |
| `.tab-pane` | Hidden by default; add `.active` to show |

Tab switching is handled by a small inline JS block (no library).

### Notices & Alerts

| Class | Color | Notes |
|---|---|---|
| `.save-notice` | Green (`--score-high-*`) | Success; auto-fades after 4s via `notice-fade-out` animation |
| `.save-error` | Red (`--score-low-*`) | Persistent error |
| `.setup-banner` | Amber (`--score-mid-*`) | Setup/configuration prompt; left border `--text-accent` |

### Destructive Action / Confirmation Panel

Used on the Settings page **Danger Zone** to gate irreversible operations (e.g. "Clear Database") behind an explicit typed confirmation. The pattern has three layers:

1. **Trigger button (`.btn-danger`)** — at rest, sitting outside the confirmation panel. Red border/text (`--score-low-*`) against the neutral page background. Clicking it reveals the panel via JS toggle (no page reload).
2. **Confirmation panel (`.clear-db-panel`)** — hidden by default (`display:none`); toggled to `.visible` by the trigger's click handler. Background `--score-low-bg`, border `--score-low-border`, `--radius-md`. Contains:
   - A `<ul class="clear-db-warnings">` of consequence bullets (`--font-body`, `--score-low-text`).
   - A `<p class="clear-db-count">` showing the current row count so the user knows what they are deleting (`--font-mono`, `--score-low-text`).
   - A `<form class="clear-db-form">` containing the label, text input, submit, and cancel.
3. **Confirmation input (`.clear-db-input`) + submit button (`.btn-danger-confirm`)** — the submit starts `disabled`; a vanilla `oninput` handler activates it only when the input value matches the required phrase exactly (e.g. `"DELETE"`). No framework required.

**Cancel button (`.clear-db-cancel`)** — muted text-only button that collapses the panel and resets the input.

**HTMX wiring** — the form posts via `hx-post`, targeting `#clear-db-result` (a `<div>` that sits above the trigger button). The route returns an HTML fragment; on success the fragment contains a `.save-notice` confirming the count deleted and a hidden `#clear-db-panel` div to collapse the panel.

**Color rules** — use the full `--score-low-*` triplet (`--score-low-bg`, `--score-low-text`, `--score-low-border`) consistently across the panel, label, input border, and button. Do not mix in amber or neutral tier tokens inside the danger panel.

**Section wrapper (`.danger-zone`)** — placed after the tabbed settings forms, separated by a `--border-subtle` top border. Contains a `.danger-zone-heading` (`--font-mono`, uppercase, `--score-low-text`).

| Class | Element | Notes |
|---|---|---|
| `.danger-zone` | `<div>` | Section wrapper; `margin-top: 2.5rem`, `border-top: 1px solid var(--border-subtle)` |
| `.danger-zone-heading` | `<h2>` | `--font-mono` 0.72rem uppercase 0.08em, `--score-low-text`; marks the section as destructive |
| `.btn-danger` | `<button>` | Trigger to reveal the panel; `--bg-raised` bg, `--score-low-text` color, `--score-low-border` border; hover lifts to `--score-low-bg` |
| `.clear-db-panel` | `<div>` | Confirmation panel; hidden until `.visible` added by JS; `--score-low-bg` / `--score-low-border` |
| `.clear-db-warnings` | `<ul>` | Consequence list; `--font-body` 0.84rem, `--score-low-text`; `::before` content "⚠ " |
| `.clear-db-count` | `<p>` | Row count display; `--font-mono` 0.76rem, `--score-low-text` |
| `.clear-db-form` | `<form>` | Flex column, gap 0.5rem, max-width 380px; HTMX `hx-post` |
| `.clear-db-label` | `<label>` | `--font-mono` 0.68rem uppercase 0.08em, `--score-low-text` |
| `.clear-db-input` | `<input type="text">` | `--font-mono` 0.76rem; `--bg-raised` bg; `--score-low-border` border; focus border to `--score-low-text` |
| `.btn-danger-confirm` | `<button type="submit">` | Starts `disabled`; enabled by `oninput` check; `--score-low-bg` / `--score-low-text` / `--score-low-text` border; hover inverts (text on `--score-low-text` bg) |
| `.clear-db-cancel` | `<button type="button">` | Muted text-only; collapses panel and resets input |

### Empty State

| Class | Notes |
|---|---|
| `.empty-state` | Center-aligned, padding 80px 20px |
| `.empty-state-icon` | Large emoji/icon; 2.4rem, 25% opacity |
| `.empty-state-title` | `--font-ui` 1.05rem weight 500, `--text-secondary` |
| `.empty-state-body` | `--font-mono` 0.76rem, `--text-muted`, line-height 1.8 |

### Stats

| Class | Notes |
|---|---|
| `.stats-summary` | Flex wrap, gap 16px — container for stat boxes |
| `.stat-box` | `--bg-surface` card; flex 1, min-width 160px, `--radius-lg` |
| `.stat-value` | `--font-mono` 1.55rem, `--text-accent` |
| `.stat-label` | `--font-mono` 0.70rem uppercase 0.10em, `--text-muted` |
| `.stats-table` | `--font-mono` 0.78rem; border-collapse; cells border-bottom `--border-subtle` |
| `.stats-section-heading` | `--font-mono` 0.68rem uppercase weight 600, `--text-muted` |

### Filter Bar (`_filter_bar.html` partial)

The filter bar is extracted into `templates/_filter_bar.html` so both the Listings page (`/`) and the Snippets page (`/snippets`) share a single implementation. Include it via `{% with %}` to pass context:

```jinja
{% with action="/snippets", search=search, sort=sort, remote_only=remote_only,
        job_type=job_type, job_types=job_types, threshold=threshold, min_score=min_score %}
  {% include "_filter_bar.html" %}
{% endwith %}
```

**Variables required by the partial:**

| Variable | Type | Notes |
|---|---|---|
| `action` | `str` | Form `action` URL — `"/"` for Listings, `"/snippets"` for Snippets |
| `search` | `str \| None` | Current text search value; `None` or empty string renders a blank input |
| `sort` | `str \| None` | Current sort value; `None` = score DESC, `"date_posted"` = newest first |
| `remote_only` | `bool` | Whether the remote-only checkbox is checked |
| `job_type` | `str \| None` | Currently selected job type filter; `None` = all types |
| `job_types` | `list[str]` | Available job types for the dropdown; the dropdown is omitted when empty |
| `threshold` | `float` | Configured score floor; used by the Clear link logic |
| `min_score` | `float \| None` | Current min_score override; `None` = no override (uses threshold) |

**Controls rendered:**

1. Text input (`.filter-input`) — search by title or company
2. Min-score select (`.filter-select`) — 5+/6+/7+/8+/9+ options; auto-submits on change
3. Sort select (`.filter-select`) — Score / Date posted; auto-submits on change
4. Job-type select (`.filter-select`) — populated from `job_types`; omitted when list is empty; auto-submits on change
5. Remote-only checkbox (`.filter-toggle`) — auto-submits on change
6. Filter button (`.btn.filter-btn`) — explicit submit
7. Clear link (`.filter-clear`) — shown only when any filter is active; href is `action`

The Clear link appears when any of `search`, `min_score`, `remote_only`, `job_type`, or `sort` is truthy. Its `href` is set to `action` so it navigates to the bare page URL, resetting all params.

### Ingest Trigger (feed page)

Rendered via `templates/_ingest_trigger.html`, included in `index.html` inside a `.ingest-trigger-container` div. Has two states:

**Idle state** (`.ingest-trigger-wrap`):

| Class | Notes |
|---|---|
| `.ingest-trigger-wrap` | `--bg-surface` card; `--border-subtle`; `--radius-md`; padding 16px 20px; `margin-bottom: 16px` to separate from `.card-list` below |
| `.ingest-form` | Flex row, `align-items: center`, `flex-wrap: wrap`, gap 12px |
| `.btn-ingest` | Amber-tinted action button; `--text-accent` colour, dark amber border/hover |
| `.ingest-opt` | Muted label for optional controls (checkboxes, selects) alongside the button |
| `.ingest-rescore-hint` | Italic helper text (`--font-body` 0.78rem, `--text-muted`) rendered on a new row below the rescore checkbox via `flex-basis: 100%`; explains that rescore-only mode skips fetching |
| `.ingest-last-run` | `--font-mono` 0.72rem; `--text-secondary`; summary of the most recent run |
| `.ingest-run-time` | `--text-muted`; timestamp inside `.ingest-last-run` |

**Running state** (`.ingest-status-running`):

| Class | Notes |
|---|---|
| `.ingest-status-running` | Inline-flex pill; `--bg-surface`, dark amber border, `--text-accent`; `margin-bottom: 16px` matches idle state gap |
| `.ingest-spinner` | Pulsing dot; pure CSS animation, no images |

Both states carry `margin-bottom: 16px` to maintain consistent spacing above `.card-list` regardless of which state is active.

---

## 6. State Conventions

| State | Rule |
|---|---|
| **Hover** | Slightly lift background (`--bg-hover`), increase border color toward `--border-strong` |
| **Focus (native inputs)** | `border-color: --border-strong`, `color: --text-primary`, no outline |
| **Focus (custom controls)** | `outline: 2px solid var(--text-accent)` with `outline-offset: 2px` via `:focus-visible` |
| **Active / selected** | Use `--text-accent` or the appropriate tier accent color |
| **Disabled / loading** | `opacity: 0.5`, `pointer-events: none` |
| **Filled input** | When a text input has a non-placeholder value, apply amber tint: `background: #1e1500`, `border-color: #5a3a00` |
| **Applied / bookmarked** | Use the "filled" tier color (green for applied, amber star for bookmarked) with matching bg + border |

---

## 7. Rules for New Components

Follow these when writing new CSS or HTML:

1. **Always use CSS custom properties.** Never hard-code a hex value. If no token fits, add one to `:root` and document it here.

2. **Positive states use `--score-high-*`.** Anything that signals "good", "configured", "active", or "matching" uses the green tier triplet. This includes remote badges, validated keys, and matched skills.

3. **Pill / badge shape is fixed.** `border-radius: 20px`, `--font-mono`, `0.65–0.72rem`, `padding: 2–3px 7–10px`. Never deviate from this shape for status/label badges.

4. **All custom interactive controls need `:focus-visible` styling.** Hidden-checkbox toggles, custom selects, drag handles — all must expose a `2px solid var(--text-accent)` outline on keyboard focus.

5. **Font assignment is strict.** Metadata, labels, badges → `--font-mono` uppercase. Names, headings → `--font-ui`. Body prose (verdicts, descriptions) → `--font-body`.

6. **Settings forms follow the standard layout.** `flex-column`, `gap: 16px`, `max-width: 600px`. New settings sections should use `.provider-row` as the card wrapper.

7. **Transitions are short.** Use 100–200ms ease for color/background/border transitions. No bounce, no spring, no delays.

8. **Binary controls use the toggle switch pattern,** not a styled `<input type="checkbox">`. See §5 Toggle Switch.
