# DESIGN.md — UI Design System (Light)

A calm, content-first interface: neutral surfaces, one restrained accent, hairline
borders, compact but readable controls. It should feel like a well-made productivity
tool — clear, fast, and trustworthy — not a marketing page.

---

## 1. Principles

1. **Content first.** Interface chrome stays neutral; color carries meaning only (selection, status, focus).
2. **One accent color.** Used for focus, selection, links and progress. Primary buttons are near-black.
3. **Hairlines over boxes.** Separate with 1px borders and whitespace, not heavy fills or shadows.
4. **Consistency.** The same control looks and behaves the same everywhere, in the same place.
5. **Progressive disclosure.** Primary actions visible; secondary actions in menus, hover states or popovers.
6. **Reversible actions.** Prefer immediate action + **Undo** over confirmation dialogs. Confirm only irreversible, high-impact actions.
7. **Keyboard-friendly.** Frequent actions have shortcuts; a command palette reaches everything.
8. **Direction-agnostic.** Every component works in LTR and RTL.

**Avoid:** decorative gradients, glassmorphism everywhere, neon or purple-heavy themes, emoji in UI,
oversized rounded cards, shadows on every element, centered body text, decorative icons,
vague or hype copy.

---

## 2. Color

```css
:root {
  /* surfaces */
  --bg:          #ffffff;   /* app background */
  --bg-subtle:   #f7f7f8;   /* sidebars, panel headers, table headers */
  --bg-muted:    #f1f1f3;   /* inputs, segmented tracks, chips */
  --surface:     #ffffff;   /* cards, popovers, drawers, modals */

  /* borders */
  --border:        #e8e8eb; /* hairlines, dividers */
  --border-strong: #d8d8dd; /* control outlines */

  /* text */
  --text:   #18181b;        /* primary */
  --text-2: #56565f;        /* secondary, default icons */
  --text-3: #8a8a93;        /* meta, placeholders, disabled */

  /* accent */
  --accent:      #2563eb;
  --accent-text: #1d4ed8;
  --accent-soft: rgba(37, 99, 235, .09);

  /* status */
  --success: #16a34a;
  --warning: #d97706;  --warning-bg: #fff7e6;  --warning-border: #f5dfae;  --warning-text: #7a4a00;
  --danger:  #dc2626;
  --neutral-status: #8a94a6;
  --highlight: #fff3b0;     /* <mark>, search matches */

  /* interaction overlays (work on any surface) */
  --hover:  rgba(24, 24, 27, .045);
  --active: rgba(24, 24, 27, .075);
  --ring:   0 0 0 3px rgba(37, 99, 235, .22);
}
```

**Rules**
- Selected navigation item: `--active` background + medium weight. Not an accent fill.
- Toggled/selected controls: `--accent-soft` background + `--accent-text`.
- Status mapping: in progress/info = accent · success = `--success` · pending/idle = `--neutral-status` · error = `--danger`.
- Categories/tags: use a fixed hue list rendered as small dots, `hsl(h 60% 52%)`.
  Suggested hues: `4, 26, 45, 140, 186, 215, 262, 320` plus grey (saturation 0).
  Show tags as **dot + neutral text**, never as colored text or saturated blocks.

---

## 3. Typography

```css
--font-sans:  -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, "Noto Sans", sans-serif;
--font-serif: Charter, Georgia, "Noto Serif", serif;          /* optional, long-form reading */
--font-mono:  ui-monospace, "SF Mono", Menlo, monospace;
```
Add script-specific fallbacks (e.g. Arabic, CJK) to each stack when needed.

| Role | Size / line-height | Weight | Notes |
|---|---|---|---|
| Page title | 22px / 1.2 | 650 | letter-spacing −0.02em |
| Section title | 13–15px | 600 | sentence case |
| Group label | 11.5px | 600 | `--text-3`, sentence case (no ALL CAPS) |
| Body / UI | 13px / 1.45 | 400 | default |
| Controls | 12.5px | 500 | buttons, tabs, selects |
| Meta | 12px | 400 | `--text-3` |
| Long-form text | 15–16px / 1.7–1.9 | 400 | editors, articles |

- Numbers in tables, counters, dates and durations: `font-variant-numeric: tabular-nums`.
- Single-line truncation with ellipsis; multi-line clamp for titles (max 2 lines).
- Sentence case everywhere.

---

## 4. Spacing, radius, elevation

**Spacing scale (px):** 2 · 4 · 6 · 8 · 10 · 12 · 16 · 20 · 24 · 32 · 48
- Page padding: 32px desktop, 16px mobile.
- Stack spacing: 4–8px within a group, 16–24px between groups, 32px+ between sections.

**Radius**
| Token | Value | Use |
|---|---|---|
| `--r-xs` | 4px | thumbnails, badges |
| `--r-sm` | 6px | small buttons, menu items, nav items |
| `--r-md` | 8px | buttons, inputs, segmented controls |
| `--r-lg` | 10px | cards, popovers, toasts |
| `--r-xl` | 12px | modals, large panels |
| `--r-pill` | 999px | chips, tags, pills |

**Elevation**
```css
--shadow-sm:    0 1px 2px rgba(16,16,20,.06);
--shadow-md:    0 1px 1px rgba(16,16,20,.06), 0 2px 6px rgba(16,16,20,.08);
--shadow-hover: 0 2px 3px rgba(16,16,20,.07), 0 8px 20px rgba(16,16,20,.13);
--shadow-pop:   0 0 0 1px rgba(16,16,20,.05), 0 4px 8px rgba(16,16,20,.05), 0 16px 36px rgba(16,16,20,.14);
```
- Flat by default. Floating layers (menus, popovers, modals, toasts, drawers) use `--shadow-pop`.
- A card uses a border **or** a soft shadow, not both at full strength.

---

## 5. Layout

- **App shell:** `sidebar (220–240px) | main (1fr)`; sidebar and main scroll independently.
- **Top bar:** 52px, sticky, slightly translucent (`color-mix(in srgb, var(--bg) 90%, transparent)` + `backdrop-filter: blur(12px)`), bottom hairline.
  Left: search. Right: global actions (command palette, refresh, account).
- **Page header:** title + one-line summary on the start side; filters, sort and view switches on the end side; wraps on narrow screens.
- **Detail panel:** right-side drawer, ~400px, over the content; full width on mobile.
- **Reading/editing column:** max width 680–740px, centered.
- **Breakpoints:** ≥1180 full · 900–1180 condensed tables · <900 sidebar becomes an off-canvas drawer with a scrim · <560 single-column grids.

---

## 6. Components

### Buttons
| Variant | Spec |
|---|---|
| Primary | 36px (sm 28px), radius 8, background `--text`, text `--bg`, weight 500. Max one per view. |
| Secondary | 30px (sm 26px), radius 7, 1px `--border-strong`, `--surface`; hover `--bg-subtle`. |
| Ghost | Transparent, `--text-2`; hover `--hover`. |
| Icon | 30×30 (sm 26×26), transparent, `--text-2`; toggled = `--accent-soft` + `--accent-text`. |
| Link | No chrome, `--accent-text`, underline on hover. |
| Destructive | `--danger` text/icon, mostly inside menus; filled red only for final confirmations. |

Disabled: 45% opacity, default cursor.

### Segmented control
Track `--bg-muted`, radius 8, padding 2. Segments 26px; selected = `--surface` with a subtle shadow.
Use for 2–4 mutually exclusive options (views, filters, modes). Icon-only segments are 30px wide.

### Inputs
- **Search:** 34px, `--bg-muted`, no border; on focus becomes `--surface` with accent border + `--ring`. Leading icon, clear button, keyboard hint (`/`).
- **Text input / select:** 30–32px, 1px `--border-strong`, radius 7; custom select chevron (`appearance: none`).
- **Textarea:** radius 8, focus ring, vertical resize; auto-grow in writing surfaces.
- **Checkbox / radio / range:** native controls with `accent-color: var(--accent)`.
- Labels above fields; help text below in 12px `--text-3`; errors in `--danger` with a specific message.

### Chips, tags, badges
- **Filter chip (toggle):** 26px pill, 1px border; selected = `--accent-soft`, borderless.
- **Tag:** 18–20px pill, `--bg-muted`, 11px text, 6px colored dot before the label.
- **Badge (count/status):** small 10.5–12px, tabular numbers; on images use dark translucent `rgba(20,20,24,.74)` with white text.
- **Status indicator:** 7–8px dot + text label (never color alone).

### Navigation (sidebar)
- Items 30px, radius 6: 16px icon + label + right-aligned count (`--text-3`, tabular).
- Selected: `--active` + weight 550.
- Row actions: `…` button appears on hover, replacing the count.
- Groups separated by ~20px with a small group label and an optional `+` action.
- Footer: secondary utilities + sync/status line (dot + short text).

### Cards
- **Content card:** 1px `--border`, radius 10, padding 14–16; hover darker border + `--shadow-sm`.
- **Media card:** image with fixed aspect ratio, radius 4–6, `--shadow-md`, 1px inset hairline so white images don't bleed into the background; 2-line title + meta line below.
- Hover: 2px lift + `--shadow-hover`. Secondary actions (`…`) appear on hover, always visible on touch devices.

### Lists and tables
- Row min-height 48–60px, bottom hairline, hover `--hover`, keyboard focus inset ring.
- Header row 36px, 11.5px/600 `--text-3`; sortable headers show a direction arrow.
- Numeric columns right-aligned with tabular numbers.
- Collapse to primary column + actions on small screens.

### Popovers and menus
- Width 210–280px, radius 10, padding 6, `--shadow-pop`.
- Section labels 11.5px/600 `--text-3`; items 30px with 16px icons; full-bleed hairline separators.
- Shortcut hints as right-aligned `<kbd>`.
- Open below the trigger, flip above when needed, keep 8px from viewport edges.
- Close on outside click and `Esc`; return focus to the trigger.

### Toasts
- Bottom-center, dark `#1c1c1f`, white text, radius 10.
- At most one action (e.g. **Undo**) + a close button. Auto-dismiss ~6s (longer when the action matters).
- One toast at a time; a new toast replaces the old one.

### Modals and command palette
- Scrim `rgba(10,10,12,.28)`; panel positioned ~12vh from the top, max width 600px, radius 12.
- Command palette (`⌘K`): large 15px input, results with icon + title + meta, ↑/↓/↵ navigation, recent items when the query is empty.
- Dialogs: title, one sentence, primary + secondary buttons aligned to the end.

### Drawers
- Right side, ~400px, full height, hairline + soft shadow on the inner edge.
- Header 56px: title + meta, icon actions, close.
- Body in sections with small labels; destructive actions last.

### Empty states
- Centered, max 380px wide: 15px/600 title, one explanatory sentence, one primary action.
- Explain **how** to get started, not just "Nothing here".

### Loading
- Spinner (20px, 2px border) only for short waits; skeletons for layouts that take longer.
- Show progress text for long jobs ("Processing 40 of 120…").

### Banners
- Inline, dismissible: `--warning-bg` / `--warning-border` / `--warning-text`, radius 8.
- Use for persistent conditions (offline, permissions, degraded mode), not for confirmations.

### Progress
- Bar: 4px track `--bg-muted`, fill `--accent`, radius 2.
- Thin 3px variant for inline or on-image progress.

---

## 7. Iconography

- Outline icons on a 24px grid, **stroke 1.6**, round caps and joins, `fill: none; stroke: currentColor`.
- Sizes: 18px default · 16px in navigation and menus · 14–15px inside buttons · 12px inline.
- Deliver as an inline SVG sprite (`<symbol>` + `<use href="#i-name">`) or a single icon set; never mix styles.
- Pair icons with labels unless the meaning is universal (close, search, back, more, add).

---

## 8. Motion

- 120ms: hover, opacity. 150–200ms: lifts, drawers, sidebars.
- Easing: `ease` / `ease-out`. No bounce, no parallax, no looping decoration.
- Animate `opacity` and `transform` only.
- Honor `prefers-reduced-motion: reduce`.

---

## 9. Internationalization (LTR / RTL)

- Set `dir` on the document; use `dir="auto"` on user-generated content.
- Use **logical CSS properties** (`margin-inline-start`, `padding-inline-end`, `inset-inline-start`, `text-align: start`).
- Mirror directional icons (back/forward, chevrons) in RTL; don't mirror logos, media controls or checkmarks.
- Inline tools and handles sit on the **start side of the text direction** and never cover content.
- Mixed-direction snippets inside a block: `unicode-bidi: isolate` (or `<bdi>`).
- Keyboard "next/previous" follows reading direction.
- Localize placeholders, dates and number formats.

---

## 10. Accessibility

- Visible focus on all interactive elements: `outline: 2px solid var(--accent); outline-offset: 2px` or `--ring`.
- Contrast: body text ≥ 4.5:1; `--text-3` only for non-essential meta.
- Target size ≥ 26px (≥ 30px for primary actions); ≥ 44px on touch-first screens.
- Icon-only buttons: `aria-label` + `title` (include the shortcut).
- Never use color alone for meaning — add text, an icon or a pattern.
- Support full keyboard use; trap focus inside modals; `Esc` closes the top-most layer.

---

## 11. Content & microcopy

- Sentence case; start actions with a verb ("Create project", "Export report").
- Be specific about outcomes: "Saved", "3 items moved to Archive · Undo".
- Errors: what happened + how to fix it; no blame, no raw codes in headlines.
- Counts include units: "12 items · 3.4 MB".
- Tooltips name the action and its shortcut: "Search (/)".
- Keep labels short; put explanations in help text, not in buttons.
