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
    allow_methods=["GET", "POST"],
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
# 4. PDF CLIENT REPORT — fully working, no API key required
# =========================================================================
# Takes the issues the bookmarklet already found (Issue/Warning/Opportunity,
# exactly the vocabulary it already uses internally) and renders a clean,
# client-ready PDF with the same severity colors as the tool itself.

from io import BytesIO
from datetime import datetime, timezone

from fastapi.responses import Response
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle


class IssueItem(BaseModel):
    name: str
    type: str  # "Issue" | "Warning" | "Opportunity"
    priority: str  # "High" | "Medium" | "Low"
    count: int = 1


class FindingItem(BaseModel):
    name: str
    type: str  # "Issue" | "Warning" | "Opportunity" | "Good"
    detail: str = ""


class ReportRequest(BaseModel):
    url: str
    domain: str
    summary: dict[str, str] = {}
    issues: list[IssueItem] = []
    schema_findings: list[FindingItem] = []
    ai_findings: list[FindingItem] = []
    tech_stack: list[str] = []


_SEVERITY_COLOR = {
    "Issue": colors.HexColor("#B23A2E"),
    "Warning": colors.HexColor("#B8720A"),
    "Opportunity": colors.HexColor("#1F6F54"),
    "Good": colors.HexColor("#1F6F54"),
}
_SEVERITY_BG = {
    "Issue": colors.HexColor("#FBEAE7"),
    "Warning": colors.HexColor("#FCEFDC"),
    "Opportunity": colors.HexColor("#E7F5EE"),
    "Good": colors.HexColor("#E7F5EE"),
}
_SEVERITY_ORDER = {"Issue": 0, "Warning": 1, "Opportunity": 2, "Good": 3}


def _build_report_pdf(req: ReportRequest) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=22 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleX", parent=styles["Title"], alignment=TA_LEFT, fontSize=20, spaceAfter=2)
    sub_style = ParagraphStyle("SubX", parent=styles["Normal"], textColor=colors.HexColor("#5B6660"), fontSize=10, spaceAfter=16)
    h2_style = ParagraphStyle("H2X", parent=styles["Heading2"], fontSize=13, spaceBefore=16, spaceAfter=8)
    cell_name = ParagraphStyle("CellName", parent=styles["Normal"], fontSize=10, textColor=colors.HexColor("#151A16"), leading=13)
    cell_meta = ParagraphStyle("CellMeta", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#5B6660"), leading=11)

    story = [
        Paragraph("SEO Audit Report", title_style),
        Paragraph(f"{req.url}<br/>Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')}", sub_style),
    ]

    counts = {"Issue": 0, "Warning": 0, "Opportunity": 0}
    for it in req.issues:
        counts[it.type] = counts.get(it.type, 0) + 1

    badge_style = ParagraphStyle("badge", parent=styles["Normal"], alignment=1, textColor=colors.white)
    badge_data = [[
        Paragraph(f"<b>{counts.get('Issue', 0)} Issues</b>", badge_style),
        Paragraph(f"<b>{counts.get('Warning', 0)} Warnings</b>", badge_style),
        Paragraph(f"<b>{counts.get('Opportunity', 0)} Opportunities</b>", badge_style),
    ]]
    badge_table = Table(badge_data, colWidths=[55 * mm, 55 * mm, 55 * mm], rowHeights=[9 * mm])
    badge_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), _SEVERITY_COLOR["Issue"]),
        ("BACKGROUND", (1, 0), (1, 0), _SEVERITY_COLOR["Warning"]),
        ("BACKGROUND", (2, 0), (2, 0), _SEVERITY_COLOR["Opportunity"]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(badge_table)

    if req.summary:
        story.append(Paragraph("Page summary", h2_style))
        sum_rows = [[Paragraph(f"<b>{k}</b>", cell_meta), Paragraph(v or "&mdash;", cell_name)] for k, v in req.summary.items()]
        sum_table = Table(sum_rows, colWidths=[35 * mm, 130 * mm])
        sum_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#DCE1DC")),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(sum_table)

    if req.issues:
        story.append(Paragraph("Findings", h2_style))
        issues_sorted = sorted(req.issues, key=lambda x: _SEVERITY_ORDER.get(x.type, 3))
        find_rows = []
        for it in issues_sorted:
            color = _SEVERITY_COLOR.get(it.type, colors.grey)
            combined = Paragraph(
                f"<b>{it.name}</b><br/><font size=8 color='#5B6660'>{it.priority} priority &middot; {it.count} occurrence(s)</font>",
                cell_name,
            )
            type_p = Paragraph(
                f"<font color='{color.hexval()}'><b>{it.type.upper()}</b></font>",
                ParagraphStyle("typeX", parent=styles["Normal"], alignment=2, fontSize=9),
            )
            find_rows.append([combined, type_p])

        find_table = Table(find_rows, colWidths=[130 * mm, 35 * mm])
        style_cmds = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#DCE1DC")),
        ]
        for idx, it in enumerate(issues_sorted):
            bg = _SEVERITY_BG.get(it.type)
            if bg:
                style_cmds.append(("BACKGROUND", (0, idx), (-1, idx), bg))
        find_table.setStyle(TableStyle(style_cmds))
        story.append(find_table)

    if req.schema_findings:
        story.append(Paragraph("Schema.org markup", h2_style))
        rows = []
        for it in sorted(req.schema_findings, key=lambda x: _SEVERITY_ORDER.get(x.type, 4)):
            color = _SEVERITY_COLOR.get(it.type, colors.grey)
            combined = Paragraph(f"<b>{it.name}</b><br/><font size=8 color='#5B6660'>{it.detail}</font>", cell_name)
            type_p = Paragraph(f"<font color='{color.hexval()}'><b>{it.type.upper()}</b></font>", ParagraphStyle("typeS", parent=styles["Normal"], alignment=2, fontSize=9))
            rows.append([combined, type_p])
        tbl = Table(rows, colWidths=[130 * mm, 35 * mm])
        style_cmds = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#DCE1DC")),
        ]
        for idx, it in enumerate(sorted(req.schema_findings, key=lambda x: _SEVERITY_ORDER.get(x.type, 4))):
            bg = _SEVERITY_BG.get(it.type)
            if bg:
                style_cmds.append(("BACKGROUND", (0, idx), (-1, idx), bg))
        tbl.setStyle(TableStyle(style_cmds))
        story.append(tbl)

    if req.ai_findings:
        story.append(Paragraph("AI crawler & answer-engine readiness", h2_style))
        sorted_ai = sorted(req.ai_findings, key=lambda x: _SEVERITY_ORDER.get(x.type, 4))
        rows = []
        for it in sorted_ai:
            color = _SEVERITY_COLOR.get(it.type, colors.grey)
            combined = Paragraph(f"<b>{it.name}</b><br/><font size=8 color='#5B6660'>{it.detail}</font>", cell_name)
            type_p = Paragraph(f"<font color='{color.hexval()}'><b>{it.type.upper()}</b></font>", ParagraphStyle("typeA", parent=styles["Normal"], alignment=2, fontSize=9))
            rows.append([combined, type_p])
        tbl2 = Table(rows, colWidths=[130 * mm, 35 * mm])
        style_cmds2 = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#DCE1DC")),
        ]
        for idx, it in enumerate(sorted_ai):
            bg = _SEVERITY_BG.get(it.type)
            if bg:
                style_cmds2.append(("BACKGROUND", (0, idx), (-1, idx), bg))
        tbl2.setStyle(TableStyle(style_cmds2))
        story.append(tbl2)

    if req.tech_stack:
        story.append(Paragraph("Detected technologies", h2_style))
        tech_text = " &middot; ".join(req.tech_stack)
        story.append(Paragraph(tech_text, cell_name))

    doc.build(story)
    buf.seek(0)
    return buf.read()


@app.post("/api/report/pdf")
def generate_pdf_report(req: ReportRequest):
    pdf_bytes = _build_report_pdf(req)
    filename = f"seo-audit-{req.domain}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
