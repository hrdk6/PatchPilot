---
name: PatchPilot
description: The Review Thread. Every agent run reads as a change under review.
colors:
  paper: "#f6f6f2"
  panel: "#ffffff"
  strip: "#f0efea"
  rule: "#e0ded6"
  rule-strong: "#c9c6bc"
  ink: "#1a1d21"
  ink-2: "#464c55"
  ink-3: "#676d77"
  accent: "#4b44c4"
  accent-strong: "#3a33a8"
  accent-soft: "#ecebfb"
  on-accent: "#ffffff"
  pass: "#1d7a36"
  pass-soft: "#e2f1e6"
  on-verdict: "#ffffff"
  fail: "#b42318"
  fail-soft: "#fbe9e7"
  warn: "#8f5a00"
  warn-soft: "#faefd9"
  live: "#35557f"
  live-soft: "#e8eef6"
  add: "#0b7066"
  add-bg: "#e3f4f0"
  add-gutter: "#cfeae3"
  del: "#a3253a"
  del-bg: "#fbeaec"
  del-gutter: "#f3d3d8"
  hunk: "#4a5a70"
  hunk-bg: "#eaeef3"
  sig-blue: "#2a78d6"
  sig-orange: "#eb6834"
  sig-aqua: "#1baf7a"
  sig-yellow: "#eda100"
  sig-magenta: "#e87ba4"
  sig-neutral: "#898781"
  paper-dark: "#121315"
  panel-dark: "#1a1c1f"
  strip-dark: "#212428"
  rule-dark: "#2c3036"
  rule-strong-dark: "#3d424a"
  ink-dark: "#eceded"
  ink-2-dark: "#b9bec5"
  ink-3-dark: "#8f959e"
  accent-dark: "#9a94f2"
  accent-strong-dark: "#b5b1f6"
  accent-soft-dark: "#28264a"
  on-accent-dark: "#16151f"
  pass-dark: "#56c474"
  pass-soft-dark: "#173322"
  on-verdict-dark: "#111315"
  fail-dark: "#f0766e"
  fail-soft-dark: "#3a1d1b"
  warn-dark: "#e0a640"
  warn-soft-dark: "#3a2c12"
  live-dark: "#92b3e4"
  live-soft-dark: "#1d2a3d"
  add-dark: "#62d0c0"
  add-bg-dark: "#12302c"
  add-gutter-dark: "#173d38"
  del-dark: "#f08b9b"
  del-bg-dark: "#371a20"
  del-gutter-dark: "#45212a"
  hunk-dark: "#a3b5cc"
  hunk-bg-dark: "#1f2733"
  sig-blue-dark: "#3987e5"
  sig-orange-dark: "#d95926"
  sig-aqua-dark: "#199e70"
  sig-yellow-dark: "#c98500"
  sig-magenta-dark: "#d55181"
  sig-neutral-dark: "#898781"
typography:
  display:
    fontFamily: "Geist Variable, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "28px"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-0.02em"
  headline:
    fontFamily: "Geist Variable, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "22px"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Geist Variable, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "14px"
    fontWeight: 650
    lineHeight: 1.5
  body:
    fontFamily: "Geist Variable, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  body-small:
    fontFamily: "Geist Variable, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Geist Variable, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: "18px"
  code:
    fontFamily: "JetBrains Mono Variable, ui-monospace, SF Mono, Menlo, Consolas, monospace"
    fontSize: "12.5px"
    fontWeight: 400
    lineHeight: 1.65
  mono-data:
    fontFamily: "JetBrains Mono Variable, ui-monospace, SF Mono, Menlo, Consolas, monospace"
    fontSize: "11.5px"
    fontWeight: 400
    lineHeight: 1.5
  stage-name:
    fontFamily: "JetBrains Mono Variable, ui-monospace, SF Mono, Menlo, Consolas, monospace"
    fontSize: "11px"
    fontWeight: 600
    lineHeight: 1.3
rounded:
  hairline: "1px"
  key: "2px"
  label: "3px"
  base: "4px"
  round: "50%"
spacing:
  xxs: "4px"
  xs: "8px"
  sm: "12px"
  md: "16px"
  lg: "20px"
  xl: "24px"
  gutter: "32px"
  gutter-narrow: "16px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    typography: "{typography.label}"
    rounded: "{rounded.base}"
    padding: "0 12px"
    height: "32px"
  button-primary-hover:
    backgroundColor: "{colors.accent-strong}"
    textColor: "{colors.on-accent}"
  button-secondary:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.base}"
    padding: "0 12px"
    height: "32px"
  button-secondary-hover:
    backgroundColor: "{colors.strip}"
    textColor: "{colors.ink}"
  input:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    typography: "{typography.body-small}"
    rounded: "{rounded.base}"
    padding: "6px 10px"
    height: "32px"
  panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.base}"
    padding: "16px"
  panel-head:
    backgroundColor: "{colors.strip}"
    textColor: "{colors.ink}"
    typography: "{typography.title}"
    padding: "8px 16px"
    height: "40px"
  label-pass:
    backgroundColor: "{colors.pass-soft}"
    textColor: "{colors.pass}"
    typography: "{typography.label}"
    rounded: "{rounded.label}"
    padding: "1px 7px 1px 5px"
  label-fail:
    backgroundColor: "{colors.fail-soft}"
    textColor: "{colors.fail}"
    typography: "{typography.label}"
    rounded: "{rounded.label}"
    padding: "1px 7px 1px 5px"
  label-warn:
    backgroundColor: "{colors.warn-soft}"
    textColor: "{colors.warn}"
    typography: "{typography.label}"
    rounded: "{rounded.label}"
    padding: "1px 7px 1px 5px"
  label-live:
    backgroundColor: "{colors.live-soft}"
    textColor: "{colors.live}"
    typography: "{typography.label}"
    rounded: "{rounded.label}"
    padding: "1px 7px 1px 5px"
  label-neutral:
    backgroundColor: "{colors.strip}"
    textColor: "{colors.ink-2}"
    typography: "{typography.label}"
    rounded: "{rounded.label}"
    padding: "1px 7px 1px 5px"
  tag:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink-2}"
    typography: "{typography.mono-data}"
    rounded: "{rounded.label}"
    padding: "0 6px"
  verdict-score-pass:
    backgroundColor: "{colors.pass}"
    textColor: "{colors.on-verdict}"
    rounded: "{rounded.label}"
    size: "52px"
  verdict-score-fail:
    backgroundColor: "{colors.fail}"
    textColor: "{colors.on-verdict}"
    rounded: "{rounded.label}"
    size: "52px"
  verdict-score-pending:
    backgroundColor: "{colors.strip}"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.label}"
    size: "52px"
  diff-line-add:
    backgroundColor: "{colors.add-bg}"
    textColor: "{colors.ink}"
    typography: "{typography.code}"
  diff-line-del:
    backgroundColor: "{colors.del-bg}"
    textColor: "{colors.ink}"
    typography: "{typography.code}"
  diff-hunk:
    backgroundColor: "{colors.hunk-bg}"
    textColor: "{colors.hunk}"
    typography: "{typography.code}"
    padding: "3px 12px"
  signal-toggle-pressed:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.ink}"
    typography: "{typography.mono-data}"
    rounded: "{rounded.base}"
    height: "24px"
---

# Design System: PatchPilot

## Overview

**Creative North Star: "The Review Thread"**

A run is a change under review. The dashboard borrows the grammar of code-review tools (a change header with the issue as its subject line, a scored verdict, a review log of state transitions, patchsets, diff gutters, checks) because the evidence it shows is exactly that material. It deliberately refuses the category default of a stat-card grid over tables: the hero surface reads top to bottom as subject, verdict, stage rail, then change info beside the review log, then the proposed diff with its patchsets and checks.

Light is the primary theme. The use scene is a reviewer on a laptop in daylight, skimming for under a minute, so the world is warm review paper with white panels ruled by hairlines and near-black ink. A full dark token set mirrors every role and is applied through `prefers-color-scheme: dark`; there is no in-app theme toggle. Density is that of a working tool: 14px body, 13px controls and tables, compact 40px header strips, and every number in tabular figures so durations and counts align on their right edge.

Colour is governed by law rather than taste. Each chromatic family owns exactly one meaning (verdict, action, retrieval signal, diff direction) and never lends itself to another. Everything else is ink on paper. Motion is almost absent: one authored entrance, the stage-rail bars growing in, and otherwise only state feedback. Reduced motion is respected by removing every animation and transition.

**Key Characteristics:**
- Warm paper, white panels, 1px hairline rules, grey header strips on every panel; flat, no drop shadows.
- Squared 4px corners on every container and control; 3px on labels and score squares.
- Geist Variable for all language, JetBrains Mono Variable for machine identifiers and code; both self-hosted so screenshots render identically on every OS.
- Verdict green only for a passing validation; violet only for what can be acted on.
- Retrieval signals keyed from a validated categorical palette, confined to the retrieval trace, with the label always beside the key.
- Diff additions in teal and removals in rose, never borrowing pass or fail.
- One authored motion moment (stage-rail bars grow in); one signature interaction (signal focus in the retrieval legend).

## Colors

A neutral warm-paper field carrying four strictly separated colour families, each with one job.

### Primary
- **Review Violet** (`accent`; dark `accent-dark`): the only accent, and only on things that can be acted on: links, the primary button (New run, Download diff), the focus ring, the text caret, a pressed signal-legend toggle, and linked badges. Hover deepens to **Pressed Violet** (`accent-strong`). **Violet Wash** (`accent-soft`) backs a pressed toggle, a hovered linked badge, and text selection. Text on violet uses `on-accent`.

### Secondary
- **Verdict Green** (`pass`, `pass-soft`): a passing validation, and nothing else. It fills the Verified +1 score square, the `fixed` status label, the "validation passed" patchset label, the final review-log marker of a fixed run, and pass-rate bars at or above 75% (the fraction of tasks whose validation passed). Text and icons on a solid verdict fill (green or red score squares, the passed review-log marker) use **On Verdict** (`on-verdict`: white in light, near-black in dark so the lighter dark-theme greens and reds keep their contrast).
- **Verdict Red** (`fail`, `fail-soft`): a run or patchset that did not pass (Verified −1, `tests-failed`, `budget-exhausted`, `patch-invalid`), failed checks, error banners, the stopped stage with its dashed stop line, and a pass-rate bar at zero.
- **Caution Amber** (`warn`, `warn-soft`): honesty caveats and infrastructure trouble: the unisolated-sandbox banner and label, "review before merging", sandbox/setup/timeout failures, the worker-down label, and mid-range pass-rate bars.
- **Slate Live** (`live`, `live-soft`): a run in progress: the running label (its icon pulses), the current stage on the rail, stage visit counts, info banners.

### Tertiary
- **Addition Teal** (`add`, `add-bg`, `add-gutter`): added diff lines, their gutter, the `+` sign, and the `+n` diff stat.
- **Removal Rose** (`del`, `del-bg`, `del-gutter`): removed diff lines, their gutter, the `−` sign, and the `−n` diff stat.
- **Hunk Slate** (`hunk`, `hunk-bg`): the `@@` hunk strips.
- **Retrieval Signal Keys** (`sig-blue`, `sig-orange`, `sig-aqua`, `sig-yellow`, `sig-magenta`, `sig-neutral`): drawn from the validated categorical palette, one key per retrieval signal: semantic-similarity blue, named-in-failure-output orange, lexical-overlap yellow, test-referencing-symbol magenta. The two graph signals share aqua, told apart as solid (import-graph-neighbor) versus hatched (call-graph-neighbor); the two rare signals share the neutral key, solid (path-named-in-issue) versus hatched (repository-convention-file). Each key is a 10px square with a 1px ink ring at 35% so it holds its edge on both themes.

### Neutral
- **Review Paper** (`paper`): the page field behind everything.
- **Panel White** (`panel`): panels, cards, the top bar, inputs, secondary buttons, the diff body.
- **Header Strip** (`strip`): every panel's header strip, table-row hover, output wells, commit messages, diff gutters, empty bar tracks, the neutral label.
- **Hairline** (`rule`) and **Firm Hairline** (`rule-strong`): 1px dividers and panel borders; the firm rule outlines controls, tags and unfilled budget slots.
- **Ink** (`ink`), **Ink Secondary** (`ink-2`), **Ink Tertiary** (`ink-3`): text in three steps (primary, supporting, metadata). `ink-2` also fills magnitude bars: stage durations, retrieval score bars, used budget slots, and single-series chart bars.

### Named Rules
**The Verdict Green Law.** Green (`pass`) appears only where a validation actually passed. A finished job that is not a validation (`succeeded`, `completed`) keeps its check mark in ink, and a check that exited 0 is drawn in ink on the strip tone, not in green.

**The One Violet Rule.** The violet accent marks only what can be acted on: links, the primary button, focus, the caret, a pressed toggle. It never colours data, headings, status or decoration.

**The Signal Key Rule.** Retrieval-signal colours come from the validated categorical palette and appear only in the retrieval trace (its legend and its per-chunk reasons). Shared hues are told apart by fill, solid versus hatched, and the signal name always travels with its key. Charts elsewhere draw plain magnitude in `ink-2`.

**The Diff Is Not a Verdict Rule.** Diff additions and removals use their own teal and rose, never pass or fail, so a green or red line never reads as a judgement.

**The Not By Colour Alone Rule.** Every status label pairs its tone with an icon (check, cross, alert, clock, minus) and its word; the stopped stage carries a stop icon and the word "stopped" as well as its red.

## Typography

**Display Font:** Geist Variable (with system-ui, -apple-system, Segoe UI, Roboto, sans-serif)
**Body Font:** Geist Variable (same stack)
**Label/Mono Font:** JetBrains Mono Variable (with ui-monospace, SF Mono, Menlo, Consolas, monospace)

**Character:** A neutral, precise grotesque carries every sentence and label at moderate weights (400, 550, 600, 650, 700), and a quiet coding mono marks what a machine produced. Both are self-hosted through @fontsource-variable so demos and README screenshots render the same everywhere.

### Hierarchy
- **Display** (650, 28px, 1.2, −0.02em; 23px below 860px): the issue title as the change's subject line, capped at 48ch.
- **Headline** (650, 22px, 1.25, −0.01em): page titles on list and form pages.
- **Title** (650, 14px): panel header strips; 13px/650 for sub-heads (h3, h4).
- **Body** (400, 14px, 1.5): running text, reasons in the review log; prose blocks capped at 75ch.
- **Body Small** (400, 13px): controls, tables, change meta line.
- **Label** (600, 12px, 18px line): status labels, table headers, verdict label, hints.
- **Code** (mono 400, 12.5px, 1.65 in diffs, 1.55 in output wells): diffs, sandbox output, commands, textareas.
- **Mono Data** (mono 400–600, 11.5px): tags, signal names, trace reasons, queue meta, chart row labels, the sandbox limits line; stage names at 11px/600.

### Named Rules
**The Mono Is Machine Rule.** Mono is reserved for what a machine produced or addresses: code, diffs, run ids, hashes, repo paths, model ids, state names, stop reasons, signal names. Human language is always in Geist.

**The Tabular Numbers Rule.** Durations, counts, scores and costs stay in Geist with tabular figures (`font-variant-numeric: tabular-nums`) and right alignment, so columns of time line up on their last digit.

## Layout

A single centred column (max-width 1360px) under a sticky 52px top bar, with a 32px side gutter and 28px top padding on desktop, 16px gutter below 860px. Panels stack with a 20px gap; grids use a 20px gutter. Panel bodies pad at 16px; header strips at 8px 16px with a 40px minimum height. The working spacing steps are 4, 8, 12, 16, 20, 24 and 32px, with 14px used for stacked form fields and banner spacing.

The run page follows a fixed order: change header (subject left, verdict block and Download diff right) ruled off by a hairline; honesty banners; the stage rail as nine equal columns across the full width; then a 5:7 split of change info (sticky at 64px from the top) beside the review log; then the proposed change, patchsets, and the retrieval trace at full width. The Runs page is a review queue: a verdict score square leads each row, then the subject in 14px/600 with mono meta beneath.

Responsive behaviour: at 1100px the stage rail wraps to five columns and the change split collapses to one column (change info stops being sticky); at 860px the top bar wraps, the change header stacks with the verdict stretched full width, and the rail becomes three columns; benchmark charts run four across above 1100px, two by two below, one column under 700px. Wide tables and diffs scroll horizontally inside their panel, never the page.

## Elevation & Depth

The system is flat. Depth is carried by three tones (paper, panel, strip) and 1px hairlines, not by shadows. Nothing lifts on hover; state is shown by a border shifting to `ink-3` and a background shifting to the strip tone.

### Shadow Vocabulary
- **Key ring** (`box-shadow: 0 0 0 1px color-mix(in srgb, var(--ink) 35%, transparent)`): the outline on retrieval-signal key squares, so light keys hold their edge on white and dark keys on charcoal.
- **Current-page ring** (`box-shadow: inset 0 0 0 1px var(--accent-strong)`): the New run button when it is the current page.

### Named Rules
**The Hairline Rule.** Surfaces are separated by 1px rules and tonal steps only. No drop shadows, no blur, no gradients except the loading skeleton.

## Shapes

Squared and tool-like. Every container and control uses a 4px radius: panels, cards, buttons, inputs, banners, output wells, the verdict block, diff files and check lists. Small inline marks go tighter: 3px for status labels, tags, patchset strips and score squares, 2px for signal keys and the focus ring corner, 1px for bars and budget slots. Only two things are round: review-log markers (22px) and check icons (20px). Panel header strips take the panel's top corners; the stopped stage is marked by a 2px dashed red line on its trailing edge. Icons are authored 16px SVGs in a single 1.5px round-capped stroke that inherit `currentColor`.

## Components

### Buttons
Plain and compact; the accent is spent only where the click matters.
- **Shape:** squared (4px), 32px minimum height, 0 12px padding, 13px/600 label, optional 16px icon with a 6px gap.
- **Primary:** violet fill with `on-accent` text; used for New run and Download diff.
- **Secondary:** white panel fill, firm-hairline border, ink text.
- **Hover / Focus:** primary deepens to `accent-strong`; secondary borders shift to `ink-3` over the strip tone, in a 150ms colour transition. Focus is a 2px violet outline offset 2px.
- **Danger:** red text and a 45% red border (Cancel run); hover washes in `fail-soft`. Disabled drops to 50% opacity.

### Chips (status labels and tags)
- **Status label:** 12px/600 text with a leading 14px icon, 3px radius, the tone's soft background, its colour for text, and a 30% tinted border. Tones: pass, fail, warn, live, neutral. Running and queued labels pulse their icon.
- **Tag:** mono 11.5px in `ink-2` on white with a firm-hairline border, for sandbox kind, chunk kind, difficulty.

### Cards / Containers
- **Corner Style:** 4px.
- **Background:** panel white on review paper.
- **Shadow Strategy:** none (see Elevation & Depth).
- **Border:** 1px `rule`.
- **Internal Padding:** 16px, under a strip-toned header strip (8px 16px, 40px min height) that every panel carries. Collapsed patchsets are `details` whose strip is the summary and opens in place.

### Inputs / Fields
- **Style:** white fill, firm-hairline border, 4px radius, 6px 10px padding, 13px text; textareas switch to 12.5px mono. Placeholder in `ink-3`.
- **Focus:** border and 2px outline both turn violet, outline offset 0.
- **Error / Disabled:** disabled at 55% opacity; errors surface as a red banner with its remediation.

### Navigation
A white 52px top bar with a hairline base: the lettermark and "PatchPilot" at 15px/700, then text tabs in `ink-2` at 550 weight. The current tab turns ink with a 2px ink underline (never violet, since it is where you are, not an action). New run sits in the bar as the primary button. System status labels (sandbox isolation, worker, version) sit right in 12px. Below 860px the bar wraps and the status row takes the full width.

### Verdict Block
The change's score in review-tool vocabulary: a 52px score square (+1 in verdict green, −1 in red, 0 on the strip tone; amber for infrastructure failures) beside "Verified", the status label and the mono stop reason. The score digit on a solid green or red square uses `on-verdict`. The same score recurs as a 28px square leading every row of the Runs queue.

### Stage Rail
Nine state-machine stages in order across the full width, each a column with its mono stage name, a ×n visit count in slate when revisited, a 6px duration bar scaled to the slowest stage, and the duration right-aligned in tabular figures. The current stage is slate; the stopped stage takes a red wash, a red bar, a stop icon with "stopped", and a dashed red stop line. This is the one authored motion moment: the bars grow in from the left (`scaleX` from 0, 700ms, `cubic-bezier(0.16, 1, 0.3, 1)`).

### Review Log
State transitions as a review thread: `FROM → TO` in mono with the target in ink 650, the reason beneath in body text, and the duration right-aligned. Routine steps carry only a 5px dot; a green filled marker with an `on-verdict` check marks a fixed finish, a red-washed cross a failure, a slate marker the live step. Transitions group under collapsible patchset strips; earlier patchsets start closed.

### Diff
Unified diff in mono 12.5px: two 44px line-number gutters on the strip tone, a 20px sign column, then code. Additions are teal-washed with a deeper teal gutter and a bold teal `+`; removals the same in rose with `−`. Hunk headers sit on slate strips. Each file has a strip header with a file icon, the path, and a mono `+n −n` stat.

### Retrieval Trace (signature interaction: signal focus)
A ranked table of retrieved chunks: rank, path and symbol meta, a score with a thin right-anchored magnitude bar, and the reasons that selected it, each with its signal key and contribution. Above it, the always-visible signal legend is a row of toggle buttons (mono 11.5px, 24px high). Pressing one sets `aria-pressed`, gives it a violet border and wash, and focuses the trace on that signal: non-matching rows dim to 32% opacity and non-matching reasons within matching rows to 45%, over a 180ms opacity transition. Pressing it again clears the focus.

### Bar Charts
Hand-rolled HTML horizontal bars, each backed by a visually hidden table: mono row labels, a 10px bar from a firm-hairline baseline with a 3px rounded end, and a right-aligned tabular value. Plain magnitude is `ink-2`; a second series is `rule-strong` with a key above. Status tones appear only where the bar reports a state (pass rate).

### Banners
Honesty caveats and errors: a 4px-radius block with a masked 16px icon, a 650-weight title in the tone colour, and supporting text in `ink-2` on the tone's soft wash.

## Do's and Don'ts

### Do:
- **Do** keep light as the primary theme and ship every new token in both the light set and the `prefers-color-scheme: dark` set.
- **Do** give every panel a strip-toned header strip (40px min height, 8px 16px padding) above a white body with a 1px `rule` border and 4px corners.
- **Do** use `pass` only where a validation passed, and pair every status colour with an icon and a word.
- **Do** keep the violet accent for links, primary buttons, focus and pressed toggles.
- **Do** confine retrieval-signal keys to the retrieval trace and always print the signal name beside its key.
- **Do** set durations, counts, costs and scores in Geist with tabular figures, right-aligned.
- **Do** back every chart with an accessible table.
- **Do** keep motion to state feedback (150ms colour, 180ms focus dimming) and let the stage-rail grow-in remain the single authored entrance; every animation and transition must switch off under `prefers-reduced-motion: reduce`.

### Don't:
- **Don't** use verdict green for anything that is not a passing validation: not a completed job, not a check that exited 0, not a diff addition, not decoration.
- **Don't** use the violet accent on data, headings, status, the current nav tab, or chart bars.
- **Don't** let the retrieval-signal colours leave the trace; charts elsewhere draw magnitude in `ink-2`.
- **Don't** colour diff additions and removals with `pass` or `fail`.
- **Don't** add drop shadows, lifted hover states or gradient fills; depth is paper, panel, strip and hairlines.
- **Don't** round containers beyond 4px or make pills of labels.
- **Don't** set human language in mono, or machine identifiers in the sans.
- **Don't** fall back to a stat-card grid over tables for a run; it reads as a change under review.
