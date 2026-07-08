# OpenEvo Desktop-Only Shell Design

Tracked by #65.

## Goal

Make the assets packaged for `openevo desktop serve` render as a standalone
OpenEvo Desktop product shell rather than the shared Polar dashboard shell.

The shared dashboard remains available for Polar platform development. This
slice only changes the build mode used for packaged OpenEvo Desktop assets.

## Current State

`web/src/App.tsx` always renders the shared dashboard frame:

- top navigation with Polar Dashboard branding;
- links to `/openevo`, `/`, `/tasks`;
- routes for dashboard, task detail, session detail, and compare views;
- footer copy that describes the shared dashboard.

PR #64 made `openevo desktop serve` serve that bundle safely from one FastAPI
process. It also added SPA fallbacks for the visible shared-dashboard routes so
the current bundle does not break. That is acceptable as a compatibility layer,
but it is not the desired packaged product shape.

## Approach

Add an explicit OpenEvo Desktop build mode to the existing Vite app.

The React entrypoint stays the same, but the app shell branches on a single
build-time flag:

```text
VITE_OPENEVO_DESKTOP_ONLY=true
```

When the flag is false or absent, the existing shared dashboard behavior stays
unchanged. When the flag is true, the app renders an OpenEvo-only shell:

- no shared Polar dashboard navigation;
- no visible links to dashboard/task/session/compare pages;
- the `/openevo` route renders the existing `OpenEvoDesktop` component;
- `/` renders the same OpenEvo Desktop experience for client-side fallback;
- unknown browser paths also land on the OpenEvo Desktop experience rather than
  the shared dashboard not-found card.

This keeps the sidecar API contract unchanged. The sidecar still owns
`/openevo-api/*`, and static route fallback remains server-side.

## Build Commands

Keep the existing command:

```bash
cd web && npm run build
```

This remains the shared dashboard build for platform development.

Add:

```bash
cd web && npm run build:openevo
```

This runs Vite with `--mode openevo-desktop`, loading
`web/.env.openevo-desktop`, which sets `VITE_OPENEVO_DESKTOP_ONLY=true`. The
OpenEvo-only build is written to `web/dist`. Release packaging then copies
`web/dist/` to:

```text
src/openevo/desktop/web/
```

`openevo desktop serve` packages and serves this OpenEvo-only asset set.

## Server Routes

The Python Desktop server should not need a different API contract. It can keep
the shared-dashboard SPA fallbacks added in #64 because those routes are harmless
compatibility paths:

- `/tasks`, `/tasks/*`
- `/sessions`, `/sessions/*`
- `/compare`

The OpenEvo-only packaged build should not present links to those routes. Unknown
`/openevo-api/*` paths must continue returning API 404s.

## Tests

Web tests cover:

- the shared app shell still renders dashboard navigation by default;
- the OpenEvo-only shell renders OpenEvo Desktop content without shared dashboard
  navigation;
- the OpenEvo-only shell renders OpenEvo Desktop at `/`;
- the `build:openevo` script uses `vite build --mode openevo-desktop`;
- `.env.openevo-desktop` sets `VITE_OPENEVO_DESKTOP_ONLY=true`;
- the packaged `src/openevo/desktop/web/index.html` is generated from the
  OpenEvo-only build.

Python package tests from #64 continue to cover static asset packaging, route
serving, and incomplete asset failure.

## Non-Goals

This slice does not add:

- Electron, Tauri, native menus, tray, code signing, or installers;
- a new design system or full visual redesign;
- changes to `/openevo-api/*`;
- changes to remote SSH, bootstrap, run supervision, Codex transcript capture,
  model serving, or evolution artifacts;
- removal of the shared Polar dashboard development routes.

## Self-Review

- The design has one narrow behavior change: the packaged build mode renders an
  OpenEvo-only app shell.
- The shared dashboard remains available, so existing platform workflows are not
  removed.
- The server route behavior from #64 remains compatible and does not intercept
  sidecar API paths.
- The release asset copy flow is explicit and testable.
