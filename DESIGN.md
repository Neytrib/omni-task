# Design

## Source of truth
- Status: Active. Last refreshed: 2026-09-30.
- Surfaces: private Telegram-linked web board, account entry, task creation/details.
- Evidence: creator's S6 request and post-S9 correction: the prior board felt boring, and drag/drop lacked visible animation. Refresh presentation and motion while preserving authentication, task operations and live data. No external visual reference was supplied.

## Brand
- Personality: crisp, tactile, focused. The creator requested a clean productivity interface with a stronger visual identity and responsive motion.
- Trust: clearly private access, visible save/error states, complete content, honest connection status.
- Avoid: marketing, decorative charts, fabricated data, extra product features, glowing gradients, unnecessary navigation.

## Product goals
- Capture a task, understand its status, move work forward, inspect the complete original message, and delete deliberately.
- Non-goals: teams, due dates, priorities, editing/summarizing content, analytics, custom ordering.
- Success: actual API tasks are all reachable; keyboard users can perform every mutation; errors preserve work.

## Personas and jobs
- Audience from creator's product: an individual managing their private tasks captured in Telegram or directly in the dashboard.
- Context: desktop review and quick phone access from Telegram; the same private account across interfaces.
- Job: see what is pending, in progress, or completed without losing original text/transcripts.

## Information architecture
- One board with three columns. No sidebar or fake secondary sections.
- Entry screen explains Telegram /profile; /login token handling remains unchanged and immediate.
- Header: Omni Task, theme, logout. Workspace: board title, new task, connection/refresh controls.
- Card hierarchy: title, bounded preview, source/date, explicit status selector. Details show all content and timestamps.

## Design principles
- The tasks are the interface; use space and type to distinguish controls from content.
- Make state changes visible and reversible on failure.
- Use a compact workbench layout: the board comes into view quickly, each status has a clear lane, and cards visibly respond to movement. The prior oversized serif heading and diffuse spacing were rejected by the creator.
- Preserve established authentication and API behavior; no visual shortcut bypasses ownership or CSRF.

## Visual language
- Light: ivory canvas, soft gray-green lanes and warm near-white cards; deep graphite text and muted citron primary actions. Status marks use slate, ochre and forest.
- Dark: cool graphite canvas, separate slate lane/card surfaces, pale ink and citron controls. No glowing backgrounds or gradients.
- Typography: locally bundled DM Sans throughout the working interface, with Instrument Serif reserved for small empty-lane numerals. Compact sans-serif heading and readable task titles; no remote font requests.
- Rhythm: 70px utility header, compact heading, narrow toolbar, three aligned lanes; content-driven card heights and consistent card spacing.
- Shapes: a small three-bar brand mark, 15px lane boundaries, 10px card corners, thin surface borders and a stronger lift shadow during dragging.
- Motion: 320ms board-level position transitions carry cards between columns, shift adjacent cards and animate optimistic rollbacks. Pointer-position capture makes a dropped card land in its destination instead of snapping back. Brief dialog/toast entrances and button press/hover feedback; new cards receive a fading highlight. Honor reduced motion in both CSS and JavaScript.
- Icons: small inline SVG controls and source markers with text/accessible names. No illustrations are needed.

## Components
- Preserve bootstrapAuth's once-only fragment exchange and server-session design.
- Preserve board/card/column, connection indicator, theme toggle, task form, task details/confirmation and inline alerts. Drag targets add a visible destination hint; status selectors and whole-card pointer activation remain available.
- CSS tokens are owned by frontend/src/style.css; React components reuse native buttons/selects/dialog semantics.

## Accessibility
- Target WCAG 2.2 AA contrast and keyboard behavior; manual audit is evidence, not certification.
- Visible focus, minimum comfortable targets, labeled inputs/selects, whole-card pointer dragging with an optional keyboard drag handle; status controls do not start drags and ordinary title clicks still open details.
- Native focus-managed dialogs; safe initial Cancel on deletion; restore focus after close/deletion.
- Announce mutations/errors without making the entire board an aria-live region.
- Safe plain text rendering; whitespace-preserving full content; no color-only status distinction.

## Responsive behavior
- Wide: three equal minmax(0,1fr) columns. Narrow: vertically stacked columns with all controls retained.
- Fluid gutters and headings; wrap long words and metadata; no page-width overflow at phone size or zoom.
- Mobile changes status through native select; drag is an enhancement.

## Interaction states
- Loading: skeleton cards with one accessible loading status.
- Empty: useful guidance and create action; each empty column has concise state-specific copy.
- Errors: actionable inline message/retry, no false saved state; rollback rejected optimistic moves.
- Success: quiet saved/new-task indication that dismisses after four seconds; each new notification restarts its lifetime, including repeated identical text. Errors remain until handled or dismissed.
- Offline: last known tasks remain visible, stale warning, no queued writes; drafts preserved.
- S7 connection labels distinguish Live, Connecting, Reconnecting and Offline. Live requires a healthy subscription and a current authoritative snapshot; a disconnected subscriber must not appear Live. Remote deletion closes open details and announces the removal.

## Content voice
- Plain and concise. Use Pending, In Progress, Completed, New task, Open task, Delete task, Log out.
- No implementation jargon in ordinary task flows. Explain refreshing when another interface changed data.

## Implementation constraints
- Existing React 19/TypeScript/Vite; real same-origin API/cookies and CSRF; no hardcoded task data.
- Complete paginated snapshots with bounded restart on stale revision. Idempotent create retry and optimistic mutation fencing.
- Fonts bundled locally; no credentials in localStorage (theme only). No paid API tests or new deployment.
- Tests: DOM/component/state, type check, production build. Browser: Arc only, desktop/mobile/zoom/theme checks where supported.

## Open questions
- None block this stage. Exact palette/type treatment are implementation decisions; no new product scope is inferred.
