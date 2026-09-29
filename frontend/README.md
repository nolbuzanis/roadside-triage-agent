# Towbie Frontend

Public demo (home route `/`) and read-only dispatcher UI (`/admin`) for Towbie. Built with Vite, React, TypeScript, and `@supabase/supabase-js`.

The dashboard authenticates with the single dispatcher Supabase Auth account and reads `assistance_requests` directly from Supabase using the public anon/publishable key. It never contains backend secrets — never put `SUPABASE_SERVICE_ROLE_KEY` in this app.

## Setup

```bash
npm install
cp .env.example .env.local
```

Fill in `frontend/.env.local`:

| Variable | Description |
|----------|-------------|
| `VITE_SUPABASE_URL` | Supabase project URL (Settings → API → Project URL) |
| `VITE_SUPABASE_ANON_KEY` | Supabase anon/publishable key (Settings → API → anon/publishable) |
| `VITE_FEEDBACK_URL` | Destination for the post-call "Send feedback" link (optional — falls back to a placeholder anchor when unset) |

The dispatcher account must already exist (see the root README, "Create the Dispatcher Account (One-Time)").

## Run

```bash
npm run dev
```

Open [http://localhost:3000/admin](http://localhost:3000/admin) and sign in with the dispatcher account. The home route (`/`) is the public demo.

## Build

```bash
npm run build
```

Type-checks with `tsc -b` and produces a static bundle in `dist/`.

## Deploy

Production deployments run automatically from `main` via `.github/workflows/deploy-frontend.yml`: the workflow builds `dist/` with the GitHub repository variables `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` (plus `VITE_API_BASE_URL` and the optional `VITE_FEEDBACK_URL`), then deploys to Firebase Hosting's live channel. The production URL is `https://<firebase-project-id>.web.app`.

One-time setup (Firebase project, deploy service account, repository variables/secret) is documented in the root README under "Production Deployment (Firebase Hosting)".

## Scope

This first version is intentionally minimal and read-only:

- password entry via the single Supabase Auth dispatcher account
- **Active Calls** (`intake_status = 'in_progress'`) and **Past Requests** (terminal statuses), newest first
- per-request started time, caller phone, location, vehicle, issue, and intake status
- missing active fields shown as `Collecting…`, missing terminal fields as `Not collected`
- escalated requests visually distinct
- live INSERT/UPDATE updates via Supabase Realtime (deduplicated by row `id`, with a connection indicator in the header); the subscription is removed on sign-out/unmount

No editing, search, analytics, or maps.
