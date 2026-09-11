"""
GoogleSEOAudit backend
-----------------------
A small FastAPI service that does the things a browser bookmarklet
structurally cannot: trace a full redirect chain across any domain,
and (once you add your own API keys) fetch real backlink and traffic data.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000

Then open http://127.0.0.1:8000/docs for interactive API docs (free, from FastAPI).
"""

import os
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(
    title="GoogleSEOAudit Backend",
    description="Redirect tracing, backlinks, and traffic data for the GoogleSEOAudit tool.",
    version="0.1.0",
)

# --- CORS ---------------------------------------------------------------
# The bookmarklet runs on whatever site the user is auditing, so its
# origin is different every time. Restrict this in production to just
# the origins you actually trust calling this API from (e.g. your own
# install page's domain), rather than "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this before going live publicly
    allow_methods=["GET"],
    allow_headers=["*"],
)


# =========================================================================
# 1. REDIRECT CHAIN — fully working, no API key required
# =========================================================================
# This is the thing the bookmarklet cannot do: fetch's `redirect: 'manual'`
# always returns an opaque response with no readable status or Location
# header, in every browser, for every origin. A server has no such
# restriction — `requests`/`httpx` can read every hop directly.

class RedirectHop(BaseModel):
    url: str
    status_code: Optional[int] = None
    location: Optional[str] = None
    error: Optional[str] = None


class RedirectChainResponse(BaseModel):
    input_url: str
    hops: list[RedirectHop]
    final_url: str
    final_status: Optional[int] = None
    redirect_count: int


MAX_HOPS = 15


@app.get("/api/redirects", response_model=RedirectChainResponse)
def trace_redirects(url: str = Query(..., description="URL to trace, including scheme")):
    """
    Follows a URL through every redirect hop and reports the real
    status code and Location header at each step — including hops
    that cross domains, protocols, or www/non-www, none of which a
    browser will expose to JavaScript.
    """
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL must start with http:// or https://")

    hops: list[RedirectHop] = []
    current = url

    with httpx.Client(follow_redirects=False, timeout=10.0) as client:
        for _ in range(MAX_HOPS):
            try:
                resp = client.get(current, headers={"User-Agent": "GoogleSEOAudit-Bot/1.0"})
            except httpx.RequestError as exc:
                hops.append(RedirectHop(url=current, error=str(exc)))
                break

            if 300 <= resp.status_code < 400 and "location" in resp.headers:
                location = str(httpx.URL(resp.headers["location"], base=current))
                hops.append(RedirectHop(url=current, status_code=resp.status_code, location=location))
                current = location
                continue

            hops.append(RedirectHop(url=current, status_code=resp.status_code))
            break
        else:
            hops.append(RedirectHop(url=current, error=f"Stopped after {MAX_HOPS} hops (possible loop)"))

    redirect_count = sum(1 for h in hops if h.location)
    final = hops[-1]

    return RedirectChainResponse(
        input_url=url,
        hops=hops,
        final_url=final.url,
        final_status=final.status_code,
        redirect_count=redirect_count,
    )


@app.get("/api/redirects/variants")
def trace_all_variants(domain: str = Query(..., description="Bare domain, e.g. example.com")):
    """
    Runs trace_redirects() against all 4 http/https/www variants —
    the exact check the bookmarklet has to ask an external tool for,
    now done properly with real status codes for every hop.
    """
    bare = domain.replace("http://", "").replace("https://", "").lstrip("www.")
    variants = [
        f"http://{bare}/",
        f"https://{bare}/",
        f"http://www.{bare}/",
        f"https://www.{bare}/",
    ]
    return {v: trace_redirects(v) for v in variants}


# =========================================================================
# 2. BACKLINKS — stubbed, needs your own DataForSEO (or similar) credentials
# =========================================================================
# DataForSEO is the most cost-effective dedicated backlinks API and has a
# real free trial credit. Sign up at dataforseo.com, then set these as
# environment variables — NEVER hardcode them in this file or commit them:
#
#   DATAFORSEO_LOGIN=your_login
#   DATAFORSEO_PASSWORD=your_password
#
# Docs: https://docs.dataforseo.com/v3/backlinks/summary/live/

DATAFORSEO_LOGIN = os.environ.get("DATAFORSEO_LOGIN")
DATAFORSEO_PASSWORD = os.environ.get("DATAFORSEO_PASSWORD")


@app.get("/api/backlinks")
async def get_backlinks(domain: str = Query(..., description="Bare domain, e.g. example.com")):
    if not (DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD):
        raise HTTPException(
            501,
            "Backlinks endpoint is not configured. Set DATAFORSEO_LOGIN and "
            "DATAFORSEO_PASSWORD as environment variables (see comments in main.py) "
            "to enable this endpoint.",
        )

    payload = [{"target": domain, "limit": 100}]
    async with httpx.AsyncClient(auth=(DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD), timeout=20.0) as client:
        resp = await client.post(
            "https://api.dataforseo.com/v3/backlinks/summary/live",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


# =========================================================================
# 3. REAL ORGANIC TRAFFIC — stubbed, uses Google Search Console (official, free)
# =========================================================================
# This is the one genuinely free, genuinely accurate traffic source that
# exists — because it's Google's own data. The catch: it only works for
# properties you (or your client) have verified in Search Console, via
# OAuth or a service account. It cannot report on a competitor's traffic —
# nothing legitimate can.
#
# Setup: https://developers.google.com/webmaster-tools/v1/how-tos/authorizing
# pip install google-api-python-client google-auth

@app.get("/api/traffic")
def get_traffic(site_url: str = Query(..., description="Verified property URL in Search Console")):
    raise HTTPException(
        501,
        "Not implemented yet. Real traffic data requires Google Search Console "
        "OAuth for a property you or your client has verified — see the comment "
        "above this function for the setup docs. This only works for owned/"
        "verified sites, not arbitrary competitor domains.",
    )


@app.get("/")
def root():
    return {
        "service": "GoogleSEOAudit backend",
        "endpoints": ["/api/redirects?url=", "/api/redirects/variants?domain=", "/api/backlinks?domain=", "/api/traffic?site_url="],
        "docs": "/docs",
    }
