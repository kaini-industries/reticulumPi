# Dashboard Accessibility

ReticulumPi targets WCAG 2.2 AA on evergreen Chromium, Firefox, WebKit/Safari, and Edge from
320 px through 4K, using keyboard, pointer, and touch input.

## Required component behavior

- Page structure uses landmarks and ordered headings.
- Collapsible sections use buttons with `aria-expanded`, `aria-controls`, and the `hidden`
  state. Enter and Space operate them.
- Every input has a programmatic label; validation uses `aria-invalid` and
  `aria-describedby`.
- Confirmations use native dialogs with initial focus, Escape handling, focus containment,
  and focus restoration.
- Connection, stale-data, completion, and error changes use restrained live regions.
- Tables have captions and scoped headers. Sort buttons expose `aria-sort`.
- Canvas and SVG visualizations have useful accessible names and adjacent textual/table
  equivalents.
- Status is never communicated by color alone.

## Visual and motion requirements

- Normal text contrast is at least 4.5:1; large text and non-text controls are at least 3:1.
- All touch controls are at least 44 by 44 CSS pixels.
- At 200% zoom, content remains usable without loss or two-dimensional page scrolling.
- `prefers-reduced-motion` disables nonessential transitions, pulses, and automatic motion.
- `forced-colors` preserves control boundaries, focus, and state.
- Glow is reserved for live or urgent state; monospace is reserved for technical values.

## Release verification

Playwright and axe run in Chromium, Firefox, and WebKit at phone portrait/landscape, tablet,
desktop, and 4K sizes. Release requires zero critical or serious axe findings plus manual
keyboard, screen-reader smoke, zoom, touch-target, contrast, reduced-motion, and forced-color
checks. Automated results supplement rather than replace manual verification.

The 2026-08-14 pre-tag browser gate passes 26 tests with four expected service-worker skips
across Chromium, Firefox, and WebKit. The dashboard tests cover strict CSP and lazy GPS/Leaflet
loading; zero critical or serious axe findings on the login, dashboard, and spectrum shells,
including an audit view that exposes every available top-level panel; source-level names for
lazy radio, link-test, messaging, bridge, and routing controls; native collapsible keyboard
behavior; restart-dialog focus and Escape restoration; pointer and keyboard spectrum zoom;
DPR-aware waterfall canvases; no horizontal overflow from 320 px through 4K; 44 px phone
targets; a 599 by 320 px phone-landscape viewport; keyboard-focusable named scroll regions;
the two-column tablet breakpoint; and the 1,440 px desktop content bound. The
authoritative Chromium service-worker test also passes; its two non-Chromium matrix entries
are intentionally skipped. The gates are defined in
`tests/browser/dashboard.spec.mjs` and `tests/browser/service-worker.spec.mjs` and run with
`npm run test:dashboard:e2e`.

The separate authoritative Chromium performance lane emulates 1 Mbps downstream and 150 ms
RTT, attaches machine-readable evidence, and gates usable-shell time, critical requests, LCP,
INP, CLS, long tasks, and normal-UI frame rate. The 2026-08-14 run passed with a 741 ms usable
shell, three critical requests, 728 ms LCP, 0.04983 CLS, 8 ms longest observed interaction,
no long task, and approximately 117.49 fps on the development host. These synthetic numbers are
not reference-Pi evidence.

## Independent pre-tag source audit

An independent technical audit on 2026-08-14 scored the unchanged dashboard source **18/20**
with no P0 or P1 finding:

| Dimension | Score | Evidence |
| --- | ---: | --- |
| Accessibility | 4/4 | Expanded-panel WCAG 2.2 AA axe scan returned zero findings at every impact; automated keyboard, reduced-motion, forced-colors, and 200% text-resize/no-overflow probes passed. |
| Performance | 4/4 | The authoritative slow-LAN budget passed with the measurements above. |
| Responsive behavior | 4/4 | Browser coverage spans 320 px through 4K; a 640 px/200% text probe had no page overflow. |
| Theming | 3/4 | Core field-instrument semantics are clear, but raw color use remains fragmented and several optional feature declarations reference undefined tokens. |
| Anti-patterns | 3/4 | Hierarchy and collapsibles control density, but repeated metric-card accents and one absolute loading label retain avoidable visual noise. |

The audit is bound to commit `ec1553fb7954a72f42d495231d9d423b9f51c48c`, dashboard static tree
`d5f58db56a9691a50c531c666c6d9fd2a12f2e13`, browser-test tree
`ed5aa8d3ca840b7bfeed40428903e020b3810d5e`, and main CI run
[`31809150224`](https://github.com/kaini-industries/reticulumPi/actions/runs/31809150224),
attempt 1. The expanded one-shot axe scan returned an empty violations array. This is source-tree
evidence, not exact-tag, signed-candidate, physical-display, or release approval evidence.

The non-blocking findings are tracked for a later dashboard-focused change: consolidate semantic
theme tokens and define the currently unresolved `--muted`, `--ff-mono`, `--bg-secondary`, and
`--text-secondary` properties; anchor loading status within its owning metric instead of the whole
grid; and further reduce repeated ring, corner, and glow accents. They do not weaken the pending
manual candidate gates below.

Manual Edge and assistive-technology testing, 200% zoom checks,
reduced-motion and forced-colors review, contrast confirmation on the release candidate,
interrupted service-worker update/rollback drills, and reference-Pi Lighthouse/frame-rate
testing remain release requirements.
