---
name: PatchPilot
description: Follow the magenta line. Every agent run reads as a flight along its planned route.
colors:
  sky-a: "#dfe7fd"
  sky-b: "#f8e2ef"
  sky-c: "#fdebdc"
  paper: "#f4f6fa"
  panel: "#ffffff"
  strip: "#f1f3f8"
  rule: "#e2e6ef"
  rule-strong: "#c8cedd"
  ink: "#16213e"
  ink-2: "#44506c"
  ink-3: "#636d88"
  accent: "#b3267a"
  accent-strong: "#901c62"
  accent-soft: "#fbe7f2"
  on-accent: "#ffffff"
  route: "#c92f86"
  on-verdict: "#ffffff"
  pass: "#177a3f"
  pass-soft: "#e1f3e8"
  fail: "#c0262d"
  fail-soft: "#fdebec"
  warn: "#975a00"
  warn-soft: "#fdf1dc"
  live: "#2a5bd7"
  live-soft: "#e6eefe"
  magnitude: "#5b6b95"
  console: "#131b31"
  console-ink: "#d5dcef"
  add: "#0b7066"
  add-bg: "#e3f4f0"
  del: "#a3253a"
  del-bg: "#fbeaec"
  hunk: "#4a5a7a"
  hunk-bg: "#eaeff8"
  sig-blue: "#2a78d6"
  sig-orange: "#eb6834"
  sig-aqua: "#1baf7a"
  sig-yellow: "#eda100"
  sig-violet: "#8b5cf6"
  sig-neutral: "#898781"
  sky-a-dark: "#1b2754"
  sky-b-dark: "#34183c"
  sky-c-dark: "#172040"
  paper-dark: "#0d1220"
  panel-dark: "#141b2d"
  strip-dark: "#1a2237"
  rule-dark: "#242f4c"
  rule-strong-dark: "#36436a"
  ink-dark: "#e7ebf6"
  ink-2-dark: "#b3bcd3"
  ink-3-dark: "#8a94af"
  accent-dark: "#f27ab9"
  accent-strong-dark: "#f89dcc"
  accent-soft-dark: "#3a1a33"
  on-accent-dark: "#1a0e17"
  route-dark: "#f07ab8"
  on-verdict-dark: "#0d1220"
  pass-dark: "#4fcb7e"
  fail-dark: "#f2787a"
  warn-dark: "#e7a94a"
  live-dark: "#86a8ff"
  magnitude-dark: "#8c9bc6"
  console-dark: "#090d18"
typography:
  display:
    fontFamily: "Instrument Serif, Iowan Old Style, Georgia, serif"
    fontSize: "46px"
    fontWeight: 400
    lineHeight: 1.04
  headline:
    fontFamily: "Instrument Serif, Iowan Old Style, Georgia, serif"
    fontSize: "44px"
    fontWeight: 400
    lineHeight: 1.05
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Instrument Sans Variable, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 650
  body:
    fontFamily: "Instrument Sans Variable, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Instrument Sans Variable, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: "18px"
  code:
    fontFamily: "JetBrains Mono Variable, ui-monospace, SF Mono, Menlo, Consolas, monospace"
    fontSize: "12.5px"
    fontWeight: 400
    lineHeight: 1.7
  mono-data:
    fontFamily: "JetBrains Mono Variable, ui-monospace, SF Mono, Menlo, Consolas, monospace"
    fontSize: "11.5px"
    fontWeight: 400
rounded:
  panel: "14px"
  control: "9px"
  small: "6px"
  pill: "999px"
  round: "50%"
spacing:
  xs: "8px"
  sm: "12px"
  md: "18px"
  lg: "22px"
  xl: "28px"
  gutter: "32px"
  gutter-narrow: "16px"
---

# Design System: PatchPilot

## Overview

**Creative North Star: "Follow the magenta line"**

On an aeronautical chart the planned route is printed in magenta, and pilots fly by keeping to it. PatchPilot is an autopilot for bug fixes, so a run is drawn as a flight: the state machine's nine stages are waypoints on a route, the verdict is an instrument dial, and the review log is the flight record. The page still reads top to bottom as subject, verdict, route, then change info beside the review log, then the proposed diff, patchsets and retrieval trace.

The world is a chart under a dawn sky. A soft wash of airspace blue, magenta and peach, crossed by a faint chart graticule, sits behind the header and fades out before the content; everything below is cool chart-white paper with white panels. Dark mode is the same chart at night: deep navy with a dusk-plum sky. Both themes follow `prefers-color-scheme`.

Boldness is spent in one place: the route. Everything around it is disciplined: layered panels, one accent, status colours that keep their meaning.

**Key characteristics:**
- Instrument Serif for page titles and the run's subject line; Instrument Sans for every other human word; JetBrains Mono for anything a machine produced. All self-hosted so screenshots render identically everywhere.
- Magenta is the route and the controls you steer with. Status colours never borrow it.
- Panels sit on the paper with a soft navy-tinted shadow and 14px corners; controls use 9px; labels and toggles are pills.
- One orchestrated entrance per page, and motion that answers the reader's actions. All of it switches off under reduced motion.

## Colors

### The route and the controls
- **Magenta Line** (`accent`, `route`): links, the primary button, the focus ring, the caret, pressed signal toggles, and the route itself: flown legs of the stage rail, waypoint rings, transition arrows in the review log, patchset icons, and the rule beside a plan. Hover deepens to `accent-strong`; `accent-soft` backs pressed toggles and selection.

### Status (unchanged meaning)
- **Verdict Green** (`pass`): a passing validation and nothing else: the +1 dial and queue badge, `fixed`, the landed final waypoint, the final review-log marker of a fixed run, pass-rate bars at or above 75%.
- **Verdict Red** (`fail`): did not pass: −1, failed checks, the stopped waypoint, error banners.
- **Caution Amber** (`warn`): honesty caveats and infrastructure trouble: the unisolated-sandbox banner, "review before merging", setup/timeout failures.
- **Airspace Blue** (`live`): a run in the air: the running label, the current waypoint (an aircraft with a beacon pulse), a spinning dial.

### Evidence
- **Magnitude** (`magnitude`): plain quantities: stage durations, retrieval scores, used budget slots, single-series chart bars.
- **Console** (`console`, `console-ink`): sandbox and command output, set as light text on deep chart navy in both themes.
- **Diff teal and rose** (`add*`, `del*`, `hunk*`): additions and removals, each with a 3px edge on its side of the line. Never pass or fail.
- **Retrieval signal keys** (`sig-*`): one key per signal, confined to the retrieval trace. The test-referencing signal is violet so no signal can be mistaken for the route.

### Named rules
**The Magenta Line Rule.** Magenta marks the route and what can be acted on. It never colours status, headings or chart data.

**The Verdict Green Law.** Green appears only where a validation actually passed. A finished job that is not a validation keeps its check mark in ink.

**The Not By Colour Alone Rule.** Every status pairs its colour with an icon and a word; the stopped waypoint carries a cross and the word "stopped".

## Typography

- **Display** (Instrument Serif 400, 46px, 1.04; 34px below 860px): the issue title as the run's subject, capped at 26ch.
- **Headline** (Instrument Serif 400, 44px; 36px below 860px): page titles.
- **Title** (Instrument Sans 650, 14px): panel headers.
- **Body** (Instrument Sans 400, 14px/1.5); page intros at 15px, capped at 68ch.
- **Label** (Instrument Sans 600, 12px): status labels, table headers, hints.
- **Code** (JetBrains Mono 12.5px): diffs, output, commands. **Mono data** (11.5px): ids, paths, model ids, stage and signal names.

**The Mono Is Machine Rule.** Mono is only for what a machine produced or addresses. **The Tabular Numbers Rule.** Durations, counts, scores and costs use tabular figures, right-aligned.

## Layout

A centred 1360px column under a sticky 60px frosted top bar (it scrolls away below 860px, where it wraps). Page padding is 44px 32px on desktop, 26px 16px on phones. Panels stack 22px apart and pad at 18px under a 48px header.

The run page: change header (serif subject and mono meta chips left, verdict dial and Download diff right), honesty banners, the route across the full width, a 5:7 split of change info (sticky) beside the review log, then the proposed change, patchsets, retrieval trace, baseline and artifacts.

Responsive: at 1100px the route wraps to five waypoints a row and the split becomes one column; at 860px the route shows three a row. A leg never runs off the end of a row. Wide tables and diffs scroll inside their panel.

## Elevation & Shapes

Depth comes from three tones (paper, panel, strip), 1px rules, and two soft shadows tinted with the ink navy: `shadow-1` for panels, `shadow-2` for the route and the verdict, the two things a skimming reader should find first. The primary button carries a magenta glow.

Corners follow hierarchy: 14px panels, 9px controls, wells and diffs, 6px tags and meta chips, pills for status labels, nav and signal toggles, circles for waypoints, dials, queue badges and log markers.

## Motion

Each page has one entrance, and every other motion answers something the reader did.

- **Run page, the route:** waypoints pop in and the magenta legs draw one after another (110ms apart, 360ms each), the duration bars grow in behind them, then the verdict dial sweeps to its score (1.1s, starting at 900ms).
- **Runs page:** the queue lands row by row (40ms stagger, 420ms rise).
- **Benchmark detail:** chart bars grow from zero.
- **Live state:** the current waypoint's beacon pulses and a pending dial spins; running labels pulse their icon.
- **Responses:** buttons press to 97%, disclosures unfold their content, chevrons rotate, the signal legend dims unrelated trace rows (180ms), the brand's route dashes march on hover.

Under `prefers-reduced-motion: reduce` every animation and transition is removed, and every element rests in its final state.

## Components

- **Verdict dial:** a 64px ring (track in `rule`, sweep in the tone colour) around a 46px soft-tone disc with the score. +1 green, −1 red, amber for infrastructure failures, a spinning blue arc while running, empty while queued.
- **Route (stage rail):** nine waypoints. Reached stages are magenta rings with a centre dot; unreached ones are dashed grey; the current one is a blue disc with an aircraft; the stopped one a red disc with a cross on a red wash; the final waypoint of a fixed run a green disc with a check. Legs between reached waypoints are solid magenta, planned legs dashed grey. Each stage shows its mono name, a ×n revisit count, a duration bar and the time.
- **Review log:** entries joined by a 2px thread; routine steps are hollow dots, a fixed finish a green check with a halo, failures a red cross. Patchsets group under collapsible strips.
- **Queue badge:** a 32px circle with the score, solid green or red with a soft halo.
- **Buttons:** 34px, 9px corners. Primary is a magenta gradient with a glow; secondary is white with a firm rule; danger is red text and border.
- **Inputs:** 34px, 9px corners; focus turns the border magenta with a 3px soft magenta ring.
- **Banners:** tinted wash, 3px tone edge on the left, masked icon, title in the tone colour.
- **Navigation:** a pill track; the current page is a raised white pill in ink (never magenta: it is where you are, not an action).

## Do's and Don'ts

### Do
- Ship every new token in both the light and dark sets.
- Keep magenta for the route and for controls.
- Pair every status colour with an icon and a word.
- Keep one entrance per page and make it skippable by reduced motion.
- Back every chart with an accessible table.

### Don't
- Don't use green for anything but a passing validation.
- Don't use magenta for status, headings or chart data.
- Don't let retrieval-signal colours leave the trace.
- Don't colour diff lines with pass or fail.
- Don't add entrance animations to individual panels; the page has one moment.
- Don't set human language in mono, or machine identifiers in the sans.
