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

The local July 2026 browser gate passes 22 tests with two expected skips across Chromium,
Firefox, and WebKit. Twenty-one dashboard tests cover strict CSP and lazy GPS/Leaflet
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
INP, CLS, long tasks, and normal-UI frame rate. The latest local run passed with a 735 ms usable
shell, three critical requests, 724 ms LCP, 0.050 CLS, no task over 50 ms, and approximately
120 fps on the development host. These synthetic numbers are not reference-Pi evidence.

Manual Edge and assistive-technology testing, 200% zoom checks,
reduced-motion and forced-colors review, contrast confirmation on the release candidate,
interrupted service-worker update/rollback drills, and reference-Pi Lighthouse/frame-rate
testing remain release requirements.
