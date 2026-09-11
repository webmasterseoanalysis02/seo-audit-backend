# GoogleSEOAudit backend

A small FastAPI service that does the things the browser bookmarklet
structurally cannot: full redirect-chain tracing across any domain, and
(once you add your own keys) real backlinks and real traffic.

## What's actually working right now

- **`GET /api/redirects?url=`** — fully working, no API key needed. Traces
  every hop of a redirect chain with the real status code and Location
  header at each step, for any URL on any domain.
- **`GET /api/redirects/variants?domain=`** — runs the above against all 4
  http/https/www combinations in one call. This is the exact "check all 4
  versions" feature the bookmarklet has to hand off to an external tool —
  done properly here, with real data for every hop, not just the one
  matching the current origin.

## What's stubbed and needs your own credentials

- **`GET /api/backlinks?domain=`** — wired for DataForSEO's backlinks API.
  Sign up at dataforseo.com (real free trial credit), then set
  `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD` as environment variables on
  your server — never in the code, never committed to git.
- **`GET /api/traffic?site_url=`** — not implemented yet. Real organic
  traffic only exists officially and for free through Google Search
  Console's API, and only for sites you or your client have verified
  ownership of — it can't report on a domain you don't own. See the
  Google docs linked in `main.py` for the OAuth setup; it's a bit more
  involved (a Google Cloud project + OAuth consent screen, or a service
  account) so I left it as a clearly marked stub rather than guessing at
  your setup.

## Running it locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Then open `http://127.0.0.1:8000/docs` — FastAPI generates interactive,
try-it-yourself API docs for free. Test the redirect tracer immediately
with no setup:

```
http://127.0.0.1:8000/api/redirects?url=https://example.com
```

## Deploying it for free

Both of these have a free tier that's enough to start:

- **Railway** (railway.app) — connect this folder as a GitHub repo, it
  detects `requirements.txt` and `main.py` automatically.
- **Render** (render.com) — "New Web Service", same idea. Start command:
  `uvicorn main:app --host 0.0.0.0 --port $PORT`

Either way, add `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` as environment
variables in that platform's dashboard once you're ready to enable
backlinks — not in the code.

## Wiring it into the bookmarklet

Once deployed, you'd get a real URL like
`https://your-app.up.railway.app`. The Redirects tab's "Check all 4
versions" button can then call:

```
https://your-app.up.railway.app/api/redirects/variants?domain=<the domain>
```

instead of running its own client-side fetch — and get real status codes
and destinations for all 4 variants instead of 3 honest "couldn't check
from here" messages. Same idea for backlinks once that endpoint is live:
the Traffic & Backlinks tab could call your `/api/backlinks` endpoint
directly, with your DataForSEO credentials never leaving your server.

Say the word once you've got this deployed and I'll wire the bookmarklet
up to call it.

## Before this is a "real" tool

Two things worth knowing going in:

1. **Cost.** The redirect tracer is free forever — it's just your server
   making HTTP requests. Backlinks are not: DataForSEO and every other
   backlink data provider charge per lookup once your free trial credit
   runs out. Budget for that if you want this to run for clients at
   volume.
2. **CORS in production.** `main.py` currently allows every origin
   (`allow_origins=["*"]`) so it's easy to test from anywhere. Before
   handing this to clients, tighten that to just the origin(s) that
   should be allowed to call it.
