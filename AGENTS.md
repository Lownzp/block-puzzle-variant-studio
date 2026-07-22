# Project UI Conventions

## Icons

- Use Lucide for all interface icons. The browser bundle is vendored at `vendor/lucide.min.js`.
- Do not draw icons manually with CSS borders, pseudo-elements, text glyphs, emoji, inline SVG paths, or canvas.
- Reuse an existing Lucide icon before considering any other icon source.
- Keep icon-only controls square and visually aligned. The default compact control is 30 x 30 px with an 18 px icon and a 2 px stroke.
- Every icon-only button must provide both an `aria-label` and a `title` that describe the action.
- Use color to reinforce meaning, not as the only signal. Destructive controls may use the danger color but must retain the standard button shape and hover/focus treatment.
- When dynamic HTML containing `data-lucide` elements is rendered, call `refreshIcons()` after insertion.

## UI Changes

- Follow the existing restrained, work-focused visual language.
- Prefer familiar Lucide symbols over text inside compact utility controls.
- Verify alignment, hover state, accessible name, and dynamic rerendering in the browser after changing controls.
- Do not introduce another icon library unless Lucide lacks the required concept and the exception is documented here.

## Frontend Component Policy

- Do not hand-roll foundational frontend components when a mature, actively maintained, industry-recognized library already solves the problem.
- Use the approved stack by responsibility:
  - Bootstrap for general layout primitives, forms, buttons, dialogs, dropdowns, tabs, alerts, and common interaction states.
  - Video.js for video playback, media controls, seeking, keyboard behavior, and player lifecycle.
  - vis-timeline for draggable/resizable time ranges, timeline zooming, selection, and time-axis rendering.
  - Cropper.js for dragging and resizing the board region on calibration screenshots.
  - Lucide for icons.
- Project-specific code may compose and style these components, but must not reimplement their core behavior with ad hoc DOM dragging, custom range math, CSS-drawn controls, or bespoke accessibility handling.
- Before adding another frontend dependency, verify that the approved stack cannot provide the capability. Document the reason, repository, license, pinned version, and fallback behavior.
- Vendor browser dependencies under `vendor/` with pinned versions and license files. The local application must not require a CDN or live internet connection at runtime.
- For specialist components, prefer projects with active maintenance, clear licensing, accessible behavior, published releases, and substantial real-world usage. Record the choice in the implementation plan.
- Every component integration must be tested through its public API. Do not modify vendored library source code.

## Git And GitHub Workflow

- The canonical remote is `origin` at `https://github.com/Lownzp/block-puzzle-variant-studio.git`; the main branch is `main`.
- Before committing, run `git status --short --branch` and review the staged file list. Do not commit generated videos, benchmark output, task folders, local datasets, logs, caches, Unity build output, or other bulky runtime artifacts.
- Keep `.gitignore` updated when new generated folders or local backup files appear. Do not track `*.bundle`, `benchmark*/`, `视频重建任务/`, `变体视频/`, `数据集/`, `测试素材/`, `校准/`, `改造客户端/`, or `客户端改造分析/`.
- Before pushing code changes, run the relevant verification commands. At minimum for Python/frontend bridge work, run `python -m py_compile variant_bridge.py timeline_analyzer.py recording_finalizer.py reanalyze_truth_set.py` and `python -m unittest -q`.
- Use concise imperative commit messages, for example `Add debug fps analyzer` or `Fix replay aspect crop`.
- This machine's Git may not automatically use the system proxy. If GitHub HTTPS fails with TLS reset, configure the repository proxy with:
  - `git config http.proxy http://127.0.0.1:7897`
  - `git config https.proxy http://127.0.0.1:7897`
- After pushing, verify the remote branch with `git ls-remote origin refs/heads/main` or confirm `git status --short --branch` shows `main...origin/main` with no ahead commits.
