# Dispatcher Dashboard

Read-only dispatcher UI for the roadside assistance triage agent. Built with Vite, React, TypeScript, and `@supabase/supabase-js`.

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

The dispatcher account must already exist (see the root README, "Create the Dispatcher Account (One-Time)").

## Run

```bash
npm run dev
```

Open [http://localhost:3000](http://localhost:3000) and sign in with the dispatcher account.

## Build

```bash
npm run build
```

Type-checks with `tsc -b` and produces a static bundle in `dist/`.

## Scope

This first version is intentionally minimal and read-only:

- password entry via the single Supabase Auth dispatcher account
- **Active Calls** (`intake_status = 'in_progress'`) and **Past Requests** (terminal statuses), newest first
- per-request started time, caller phone, location, vehicle, issue, and intake status
- missing active fields shown as `Collecting…`, missing terminal fields as `Not collected`
- escalated requests visually distinct
- live INSERT/UPDATE updates via Supabase Realtime (deduplicated by row `id`, with a connection indicator in the header); the subscription is removed on sign-out/unmount

No editing, search, analytics, or maps.
