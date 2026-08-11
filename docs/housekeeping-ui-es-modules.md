# Housekeeping: split `ui/index.html` into ES modules

**Status:** Not started — deliberately deferred, not forgotten. Low priority,
low risk, purely organizational. No functional/behavioral change intended.

## Why

`ui/index.html` is ~1534 lines: 330 HTML markup, 70 CSS/Tailwind config, 177
lines of embedded example-case data (the two saved cases' full PRD + feedback
documents as string literals), and ~1024 lines of actual JS logic covering
roughly 15 distinct pieces of functionality (SSE streaming, live event
dispatch, turn cards, the evidence modal, the thinking indicator, the
progress bar, the replay engine, rate-limit-aware error handling, the
markdown export, etc.) — all hand-rolled in vanilla JS with manual DOM
construction (`h()` calls), no framework doing state→DOM diffing for you.

That's not bloat — it's the natural size of what the file does given its own
constraint (single dependency-light file, no build step, documented in
`CLAUDE.md`). But it is genuinely harder to navigate than it needs to be:
finding "where does the evidence modal live" means scrolling/searching one
long file instead of opening a named one.

## What NOT to do

This was explicitly discussed and rejected: **do not introduce a bundler or
a frontend framework** (React/Vue/Next.js/etc.) to solve this. Reasoning,
for whoever picks this up later so it doesn't get re-litigated:

- This app is a single page with no routing, no SSR/SSG need (everything
  renders client-side, driven by an SSE stream that only starts after a
  user submits a form) — the actual reasons to reach for something like
  Next.js don't apply here.
- The interesting backend work is Python (LangGraph, the fact-checker, the
  `astream` fix) — a JS framework would mean either keeping two stacks for
  no functional gain, or reimplementing already-validated agent logic in JS
  for zero benefit.
- "Single dependency-light file, no build step" is a deliberate,
  documented constraint for this whole project, not an oversight. A
  bundler/`node_modules`/build step would undo the "clone and run, zero
  frontend tooling" property this project has everywhere, not just the UI.

The fix here is **native ES modules only** — `<script type="module"
src="...">`, which browsers execute directly. Zero build step, zero new
dependencies. Purely: move existing functions into files with
`export`/`import`, unchanged. If this ever grows enough to need real
component structure or SSR, that's a bigger, separate decision — not
something to back into via this cleanup.

## The split

Proposed file breakdown (`ui/js/`), by concern — each pulled from the
current single script with the function bodies unchanged, just relocated
and wired with `export`/`import`:

- `js/cases.js` — the embedded example-case data (`CASES`). Immediately
  moves 177 lines from "code" to "data you skip past."
- `js/dom-helpers.js` — `h()`, `sleep()`
- `js/snippet-format.js` — the truncation-detection/formatting helpers
- `js/evidence.js` — evidence row building + the detail modal
  (`buildEvidenceRow`, `openEvidenceModal`, `closeEvidenceModal`)
- `js/thinking.js` — the trailing thinking indicator
  (`buildThinkingIndicator`, `showThinking`, `hideThinking`)
- `js/progress.js` — the progress bar (`buildProgressBar`, `markProgress`,
  `applyStepState`, `buildProgressStep`)
- `js/rendering.js` — turn cards, round rows, flags strip
  (`buildInProgressCard`, `addRoundRow`, `finalizeTurn`, `addToolCall`,
  `buildFlagRow`, `addFlags`)
- `js/verdict.js` — verdict rendering (`showVerdict`, `fillList`,
  `REC_LABEL`, `REC_STYLE`)
- `js/export.js` — the markdown export (`buildMarkdown`, `downloadMarkdown`,
  `slugify`, `mdEvidenceLine`, `mdEvidenceBlock`, `mdFlagLine`). Good first
  candidate to extract — already proven fully DOM-independent (it was
  tested standalone in Node, outside the browser, while building it).
- `js/setup.js` — case picker, context blocks, form handling
- `js/app.js` — entry point: `state`, `handleEvent`, `startDebate`,
  `replayExample`, event-listener wiring, the init calls at the bottom

Roughly 10-11 focused files instead of one ~1024-line block.

## Two things to actually handle, not just cosmetic

1. **`server/app.py` doesn't currently serve anything under `ui/` except
   `index.html` itself** (`GET /` → `FileResponse("ui/index.html")`; only
   `/examples` is mounted via `StaticFiles`). Splitting into `ui/js/*.js`
   needs a matching static mount, e.g.:
   ```python
   app.mount("/js", StaticFiles(directory="ui/js"), name="ui-js")
   ```
   or mount `/ui` more generally — decide which when actually doing this.
   Without it, every `<script type="module" src="js/...">` 404s.

2. **`CLAUDE.md` currently states `ui/index.html` *is* the single
   dependency-light file.** After this split it becomes the entry point
   pulling in several `ui/js/*.js` files — still zero build step, but no
   longer literally one file. Update that line (and the "Project structure"
   tree in `README.md`, which also currently shows `ui/` as just
   `index.html`) to match, same as every other doc-accuracy pass this
   project has had.

## Caveat worth knowing, not a blocker

ES modules require being served over HTTP — `<script type="module">`
doesn't work opening `index.html` via `file://` (CORS blocks it). Doesn't
matter in practice here, since this app is always served through FastAPI,
never opened as a bare file — just worth knowing it's the real constraint
of this approach if that ever changes.
