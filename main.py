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
import re
import traceback
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


@app.middleware("http")
async def _allow_private_network(request, call_next):
    # Lets a bookmarklet running on a public https site reach a server on localhost
    # (Chrome's Private Network Access / Local Network Access preflight).
    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


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
    bare = domain.replace("http://", "").replace("https://", "").strip("/")
    bare = re.sub(r"^www\.", "", bare, flags=re.IGNORECASE)
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
    logo_url: str = ""
    summary: dict[str, str] = {}
    seo_score: int = 0
    seo_rating: str = ""
    ai_score: int = 0
    issues: list[IssueItem] = []
    schema_findings: list[FindingItem] = []
    ai_findings: list[FindingItem] = []
    performance_findings: list[FindingItem] = []
    tech_stack: list[str] = []
    tech_stack_icons: list[dict] = []
    keywords_top: list[dict] = []
    keywords_seed: str = ""
    social_og: dict[str, str] = {}
    social_twitter: dict[str, str] = {}
    tags_found: list[dict] = []
    redirect_summary: dict = {}


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


from reportlab.graphics.shapes import Drawing, Circle, String, Wedge
from reportlab.graphics import renderPDF
from reportlab.platypus import Flowable, Image, Spacer
from reportlab.lib.utils import ImageReader
from xml.sax.saxutils import escape as _esc


def _score_color(score: int):
    if score >= 85:
        return colors.HexColor("#1F8A5B")
    if score >= 60:
        return colors.HexColor("#D98A0B")
    return colors.HexColor("#C4382B")


class ScoreDonut(Flowable):
    """A circular score gauge: colored arc showing score/100, number in the middle."""

    def __init__(self, score: int, label: str, size: int = 34 * mm):
        Flowable.__init__(self)
        self.score = max(0, min(100, int(score)))
        self.label = label
        self.size = size

    def wrap(self, availWidth, availHeight):
        return (self.size, self.size + 7 * mm)

    def draw(self):
        c = self.canv
        r = self.size / 2.0
        cx, cy = r, (self.size / 2.0) + 7 * mm
        ring = 3.6 * mm
        color = _score_color(self.score)

        # track
        c.setStrokeColor(colors.HexColor("#E8EBE8"))
        c.setLineWidth(ring)
        c.circle(cx, cy, r - ring / 2, stroke=1, fill=0)

        # progress arc (starts at top, clockwise)
        if self.score > 0:
            c.setStrokeColor(color)
            c.setLineWidth(ring)
            c.setLineCap(1)
            extent = -360.0 * self.score / 100.0
            p = c.beginPath()
            p.arc(cx - (r - ring / 2), cy - (r - ring / 2),
                  cx + (r - ring / 2), cy + (r - ring / 2),
                  90, extent)
            c.drawPath(p, stroke=1, fill=0)

        # score number
        c.setFillColor(color)
        c.setFont("Helvetica-Bold", 19)
        c.drawCentredString(cx, cy - 4, str(self.score))

        # "/100"
        c.setFillColor(colors.HexColor("#8A938C"))
        c.setFont("Helvetica", 7)
        c.drawCentredString(cx, cy - 13, "/100")

        # label under the donut
        c.setFillColor(colors.HexColor("#5B6660"))
        c.setFont("Helvetica-Bold", 7.5)
        c.drawCentredString(cx, 1.5 * mm, self.label.upper())


def _stat_bar(counts: dict, styles) -> Table:
    """Colored count bar: Issues / Warnings / Opportunities."""
    cell = ParagraphStyle("statcell", parent=styles["Normal"], alignment=1, textColor=colors.white, leading=11)
    data = [[
        Paragraph(f"<font size=15><b>{counts.get('Issue', 0)}</b></font><br/><font size=7>ISSUES</font>", cell),
        Paragraph(f"<font size=15><b>{counts.get('Warning', 0)}</b></font><br/><font size=7>WARNINGS</font>", cell),
        Paragraph(f"<font size=15><b>{counts.get('Opportunity', 0)}</b></font><br/><font size=7>OPPORTUNITIES</font>", cell),
    ]]
    tbl = Table(data, colWidths=[54 * mm, 54 * mm, 54 * mm], rowHeights=[15 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#C4382B")),
        ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#D98A0B")),
        ("BACKGROUND", (2, 0), (2, 0), colors.HexColor("#1F8A5B")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return tbl


def _pill(text: str, color) -> Table:
    """A small rounded-looking colored badge."""
    style = ParagraphStyle(f"pill{text}", fontName="Helvetica-Bold", fontSize=6.8,
                           textColor=colors.white, alignment=1, leading=8)
    tbl = Table([[Paragraph(text.upper(), style)]], colWidths=[26 * mm], rowHeights=[5.6 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), color),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return tbl


def ke_(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\u2026"


def _fetch_image_bytes(url: str, timeout: float = 4.0) -> Optional[bytes]:
    """Best-effort image download. Never raises — a missing logo must not break the report."""
    if not url:
        return None
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception:
        pass
    return None


def _image_flowable(img_bytes: Optional[bytes], max_w, max_h):
    """Scale an image to fit within max_w x max_h, preserving aspect ratio."""
    if not img_bytes:
        return None
    try:
        iw, ih = ImageReader(BytesIO(img_bytes)).getSize()
        scale = min(max_w / iw, max_h / ih)
        return Image(BytesIO(img_bytes), width=iw * scale, height=ih * scale)
    except Exception:
        return None


def _header_block(title_flowables: list, logo_flowable, total_width=162 * mm, logo_width=28 * mm) -> Table:
    """Title block on the left, website logo (if available) pinned top-right."""
    right = logo_flowable if logo_flowable is not None else Spacer(1, 1)
    tbl = Table([[title_flowables, right]], colWidths=[total_width - logo_width, logo_width])
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return tbl


class HBar(Flowable):
    """A rounded percentage bar, used for site-wide pattern visualization."""

    def __init__(self, pct: float, color, width=78 * mm, height=4.6 * mm):
        Flowable.__init__(self)
        self.pct = max(0, min(100, pct))
        self.barcolor = color
        self.width = width
        self.height = height

    def wrap(self, availWidth, availHeight):
        return (self.width, self.height)

    def draw(self):
        c = self.canv
        c.setFillColor(colors.HexColor("#E8EBE8"))
        c.roundRect(0, 0, self.width, self.height, self.height / 2.0, fill=1, stroke=0)
        if self.pct > 0:
            fill_w = max(self.width * self.pct / 100.0, self.height)
            c.setFillColor(self.barcolor)
            c.roundRect(0, 0, fill_w, self.height, self.height / 2.0, fill=1, stroke=0)


class CWVGauge(Flowable):
    """A zone-shaded gauge (green/amber/red) with a marker at the actual value —
    same visual language as Google PageSpeed Insights' own Core Web Vitals gauges."""

    def __init__(self, value, good_max, poor_min, scale_max, width=62 * mm, height=4.6 * mm):
        Flowable.__init__(self)
        self.value = value
        self.good_max = good_max
        self.poor_min = poor_min
        self.scale_max = scale_max
        self.width = width
        self.height = height

    def wrap(self, availWidth, availHeight):
        return (self.width, self.height + 3 * mm)

    def draw(self):
        c = self.canv
        y = 3 * mm
        good_w = self.width * (self.good_max / self.scale_max)
        poor_start_w = self.width * (self.poor_min / self.scale_max)
        c.setFillColor(colors.HexColor("#1F8A5B"))
        c.roundRect(0, y, good_w, self.height, 1.1 * mm, fill=1, stroke=0)
        c.setFillColor(colors.HexColor("#D98A0B"))
        c.rect(good_w, y, max(poor_start_w - good_w, 0), self.height, fill=1, stroke=0)
        c.setFillColor(colors.HexColor("#C4382B"))
        c.roundRect(poor_start_w, y, max(self.width - poor_start_w, 0), self.height, 1.1 * mm, fill=1, stroke=0)
        pos = min(self.width, self.width * (min(self.value, self.scale_max) / self.scale_max))
        c.setFillColor(colors.HexColor("#151A16"))
        c.circle(pos, y + self.height / 2.0, 1.7 * mm, fill=1, stroke=0)


# Metric name -> (good_max, poor_min, scale_max) for the CWV gauge zones,
# matching the same thresholds the bookmarklet itself uses to classify good/needs/poor.
_CWV_SCALES = {
    "Largest Contentful Paint (LCP)": (2.5, 4.0, 8.0),
    "Cumulative Layout Shift (CLS)": (0.1, 0.25, 0.5),
    "Time to First Byte (TTFB)": (800, 1800, 3000),
    "Full Page Load": (2.5, 4.0, 8.0),
}


def _cwv_row(name: str, detail: str, styles, cell_name):
    """Builds a gauge row for a Core Web Vitals finding, parsing the numeric value
    back out of its detail string. Falls back to a plain finding row if the metric
    isn't recognized or the value can't be parsed."""
    scales = _CWV_SCALES.get(name)
    match = re.search(r"[\d.]+", detail) if scales else None
    if not (scales and match):
        return None
    value = float(match.group(0))
    good_max, poor_min, scale_max = scales
    unit = "ms" if "TTFB" in name else ("s" if "LCP" in name or "Load" in name else "")
    name_p = Paragraph(f"<b>{name}</b>", cell_name)
    value_p = Paragraph(f"<font size=9>{value}{unit}</font>", cell_name)
    row = Table([[name_p, CWVGauge(value, good_max, poor_min, scale_max), value_p]],
                colWidths=[52 * mm, 66 * mm, 16 * mm])
    row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#E8EBE8")),
    ]))
    return row


def _section_divider(text: str, styles) -> Table:
    style = ParagraphStyle("divider", parent=styles["Normal"], textColor=colors.white, fontSize=12, alignment=0)
    tbl = Table([[Paragraph(f"<b>{text}</b>", style)]], colWidths=[162 * mm], rowHeights=[10 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#151A16")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return tbl


def _finding_table(items, styles, cell_name) -> Table:
    """items: objects with .name, .type, and either (.priority, .count) or .detail"""
    items_sorted = sorted(items, key=lambda x: _SEVERITY_ORDER.get(x.type, 4))
    rows = []
    for it in items_sorted:
        color = _SEVERITY_COLOR.get(it.type, colors.grey)
        sub = getattr(it, "detail", None)
        if sub is None:
            sub = f"{it.priority} priority &middot; {it.count} occurrence(s)"
        else:
            sub = _esc(sub)
        combined = Paragraph(f"<b>{_esc(it.name)}</b><br/><font size=8 color='#5B6660'>{sub}</font>", cell_name)
        rows.append([combined, _pill(it.type, color)])
    tbl = Table(rows, colWidths=[134 * mm, 28 * mm])
    style_cmds = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#E8EBE8")),
        ("LEFTPADDING", (0, 0), (0, -1), 9),
        ("RIGHTPADDING", (1, 0), (1, -1), 4),
    ]
    for idx, it in enumerate(items_sorted):
        bar = _SEVERITY_COLOR.get(it.type)
        if bar:
            style_cmds.append(("LINEBEFORE", (0, idx), (0, idx), 2.5, bar))
    tbl.setStyle(TableStyle(style_cmds))
    return tbl


def _build_report_pdf(req: ReportRequest) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=18 * mm, bottomMargin=16 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleX", parent=styles["Title"], alignment=TA_LEFT, fontSize=22, spaceAfter=1, textColor=colors.HexColor("#151A16"))
    sub_style = ParagraphStyle("SubX", parent=styles["Normal"], textColor=colors.HexColor("#5B6660"), fontSize=9.5, spaceAfter=4)
    h2_style = ParagraphStyle("H2X", parent=styles["Heading2"], fontSize=12, spaceBefore=14, spaceAfter=6, keepWithNext=1, textColor=colors.HexColor("#151A16"))
    cell_name = ParagraphStyle("CellName", parent=styles["Normal"], fontSize=9.5, textColor=colors.HexColor("#151A16"), leading=12.5)
    cell_meta = ParagraphStyle("CellMeta", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#5B6660"), leading=11)

    logo_flow = _image_flowable(_fetch_image_bytes(req.logo_url), 26 * mm, 26 * mm)
    title_block = [
        Paragraph("SEO Audit Report", title_style),
        Paragraph(f"{_esc(req.url)}", sub_style),
        Paragraph(f"Generated {datetime.now(timezone.utc).strftime('%d %B %Y')}", sub_style),
    ]
    story = [
        _header_block(title_block, logo_flow),
        Spacer(1, 6 * mm),
    ]

    all_findings = list(req.issues) + list(req.schema_findings) + list(req.ai_findings) + list(req.performance_findings)
    counts = {"Issue": 0, "Warning": 0, "Opportunity": 0}
    for it in all_findings:
        if it.type in counts:
            counts[it.type] += 1

    # Scores row: two donuts + rating text
    rating_style = ParagraphStyle("rating", parent=styles["Normal"], fontSize=12, leading=16, textColor=colors.HexColor("#151A16"))
    rating_text = req.seo_rating or ""
    rating_para = Paragraph(
        f"<b>{rating_text}</b><br/><font size=9 color='#5B6660'>"
        f"{counts.get('Issue', 0)} issue(s), {counts.get('Warning', 0)} warning(s) and "
        f"{counts.get('Opportunity', 0)} opportunity(ies) found across this page.</font>",
        rating_style,
    )
    score_row = Table(
        [[ScoreDonut(req.seo_score, "SEO Score"), ScoreDonut(req.ai_score, "AI Readiness"), rating_para]],
        colWidths=[40 * mm, 40 * mm, 82 * mm],
    )
    score_row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (1, 0), "TOP"),
        ("VALIGN", (2, 0), (2, 0), "MIDDLE"),
        ("ALIGN", (0, 0), (1, 0), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(score_row)
    story.append(Spacer(1, 4 * mm))
    story.append(_stat_bar(counts, styles))

    # ---- SEO REPORT section ----
    story.append(Spacer(1, 7 * mm))
    story.append(_section_divider("SEO REPORT", styles))

    if req.summary:
        story.append(Paragraph("Page summary", h2_style))
        sum_rows = [[Paragraph(f"<b>{k}</b>", cell_meta), Paragraph(_esc(v) if v else "&mdash;", cell_name)] for k, v in req.summary.items()]
        sum_table = Table(sum_rows, colWidths=[32 * mm, 130 * mm])
        sum_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#E8EBE8")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F7F8F7")),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(sum_table)

    if req.issues:
        story.append(Paragraph("On-page findings", h2_style))
        story.append(_finding_table(req.issues, styles, cell_name))

    if req.keywords_top:
        story.append(Paragraph("Top content keyword phrases", h2_style))
        if req.keywords_seed:
            story.append(Paragraph(f"<font size=8.5 color='#5B6660'>Suggested seed keyword: </font><b>{_esc(req.keywords_seed)}</b>", cell_name))
            story.append(Spacer(1, 3 * mm))
        kw_rows = []
        for kw in req.keywords_top[:10]:
            score = kw.get("score")
            count = kw.get("count")
            meta = f"{count} occurrence(s)" if count is not None else ""
            kw_rows.append([
                Paragraph(f"<b>{_esc(str(kw.get('phrase', '')))}</b><br/><font size=8 color='#5B6660'>{meta}</font>", cell_name),
                HBar(score if score is not None else 0, _score_color(score or 0), width=40 * mm),
                Paragraph(f"<font size=9>{score}/100</font>" if score is not None else "", cell_name),
            ])
        kw_tbl = Table(kw_rows, colWidths=[84 * mm, 44 * mm, 34 * mm])
        kw_tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#E8EBE8")),
            ("LEFTPADDING", (0, 0), (0, -1), 9),
        ]))
        story.append(kw_tbl)

    if req.schema_findings:
        story.append(Paragraph("Schema.org markup", h2_style))
        story.append(_finding_table(req.schema_findings, styles, cell_name))

    if req.social_og or req.social_twitter:
        story.append(Paragraph("Social tags (Open Graph / Twitter)", h2_style))
        soc_rows = []
        for k, v in list((req.social_og or {}).items()) + list((req.social_twitter or {}).items()):
            soc_rows.append([Paragraph(f"<b>{_esc(str(k))}</b>", cell_meta), Paragraph(_esc(ke_(str(v), 90)), cell_name)])
        soc_tbl = Table(soc_rows, colWidths=[42 * mm, 120 * mm])
        soc_tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#E8EBE8")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F7F8F7")),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(soc_tbl)

    if req.tags_found:
        story.append(Paragraph("Google tags found", h2_style))
        tag_rows = [[Paragraph(f"<b>{_esc(str(tg.get('label', '')))}</b>", cell_name),
                     Paragraph(f"<font face='Courier' size=8.5>{_esc(ke_(str(tg.get('code', '')), 70))}</font>", cell_name)]
                    for tg in req.tags_found]
        tag_tbl = Table(tag_rows, colWidths=[56 * mm, 106 * mm])
        tag_tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#E8EBE8")),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (0, -1), 9),
        ]))
        story.append(tag_tbl)

    if req.performance_findings:
        story.append(Paragraph("Performance (Core Web Vitals)", h2_style))
        gauge_rows, plain_findings = [], []
        for it in req.performance_findings:
            row = _cwv_row(it.name, it.detail, styles, cell_name)
            (gauge_rows if row else plain_findings).append(row or it)
        for row in gauge_rows:
            story.append(row)
        if plain_findings:
            story.append(_finding_table(plain_findings, styles, cell_name))

    if req.redirect_summary:
        story.append(Paragraph("Redirects (www / https variants)", h2_style))
        rd_rows = []
        for label in ("HTTP", "HTTPS", "HTTP + WWW", "HTTPS + WWW"):
            info = req.redirect_summary.get(label)
            if info:
                rd_rows.append([Paragraph(f"<b>{label}</b>", cell_meta),
                                Paragraph(f"<font size=8.5>{_esc(ke_(str(info.get('status_text', 'Not checked')), 110))}</font>", cell_name)])
        if rd_rows:
            rd_tbl = Table(rd_rows, colWidths=[32 * mm, 130 * mm])
            rd_tbl.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#E8EBE8")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F7F8F7")),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ]))
            story.append(rd_tbl)

    # ---- AI-SEO (GEO / AEO) REPORT section ----
    if req.ai_findings:
        story.append(Spacer(1, 7 * mm))
        story.append(_section_divider("AI-SEO REPORT  (GEO / AEO)", styles))
        story.append(Paragraph("AI crawler & answer-engine readiness", h2_style))
        story.append(_finding_table(req.ai_findings, styles, cell_name))

    if req.tech_stack:
        story.append(Paragraph("Detected technologies", h2_style))
        chip_style = ParagraphStyle("chip", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=colors.HexColor("#151A16"))
        icon_by_name = {ic.get("name"): ic.get("icon_url") for ic in (req.tech_stack_icons or [])}
        icons_used = [0]

        def _chip_cell(name):
            url = icon_by_name.get(name)
            if url and icons_used[0] < 12:
                icons_used[0] += 1
                icon = _image_flowable(_fetch_image_bytes(url, timeout=3.0), 4 * mm, 4 * mm)
                if icon:
                    inner = Table([[icon, Paragraph(_esc(name), chip_style)]], colWidths=[6 * mm, 46 * mm])
                    inner.setStyle(TableStyle([
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                    ]))
                    return inner
            return Paragraph(_esc(name), chip_style)

        rows, row = [], []
        for name in req.tech_stack:
            row.append(_chip_cell(name))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            while len(row) < 3:
                row.append(Paragraph("", chip_style))
            rows.append(row)
        chip_tbl = Table(rows, colWidths=[54 * mm, 54 * mm, 54 * mm])
        chip_tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F7F8F7")),
            ("GRID", (0, 0), (-1, -1), 2, colors.white),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.append(chip_tbl)

    doc.build(story)
    buf.seek(0)
    return buf.read()


@app.post("/api/report/pdf")
def generate_pdf_report(req: ReportRequest):
    try:
        pdf_bytes = _build_report_pdf(req)
    except Exception:
        traceback.print_exc()
        try:
            safe = req.model_copy(update={"logo_url": "", "tech_stack_icons": [], "keywords_top": [], "social_og": {}, "social_twitter": {}, "tags_found": [], "redirect_summary": {}})
            pdf_bytes = _build_report_pdf(safe)
        except Exception as exc:
            traceback.print_exc()
            raise HTTPException(500, f"Report generation failed: {exc}")
    filename = f"seo-audit-{req.domain}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class SitePageFinding(BaseModel):
    name: str
    type: str
    count: int | None = None


class SitePageResult(BaseModel):
    url: str
    findings: list[SitePageFinding] = []


class SiteReportRequest(BaseModel):
    domain: str
    logo_url: str = ""
    pages: list[SitePageResult] = []


def _build_site_pdf(req: SiteReportRequest) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=18 * mm, bottomMargin=16 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleX2", parent=styles["Title"], alignment=TA_LEFT, fontSize=22, spaceAfter=1, textColor=colors.HexColor("#151A16"))
    sub_style = ParagraphStyle("SubX2", parent=styles["Normal"], textColor=colors.HexColor("#5B6660"), fontSize=9.5, spaceAfter=4)
    h2_style = ParagraphStyle("H2X2", parent=styles["Heading2"], fontSize=12, spaceBefore=14, spaceAfter=6, keepWithNext=1, textColor=colors.HexColor("#151A16"))
    cell_name = ParagraphStyle("CellName2", parent=styles["Normal"], fontSize=9.5, textColor=colors.HexColor("#151A16"), leading=12.5)

    logo_flow = _image_flowable(_fetch_image_bytes(req.logo_url), 26 * mm, 26 * mm)
    title_block = [
        Paragraph("Full Site SEO Audit", title_style),
        Paragraph(f"{req.domain}", sub_style),
        Paragraph(f"Generated {datetime.now(timezone.utc).strftime('%d %B %Y')} &middot; {len(req.pages)} page(s) crawled", sub_style),
    ]
    story = [
        _header_block(title_block, logo_flow),
        Spacer(1, 6 * mm),
    ]

    counts = {"Issue": 0, "Warning": 0, "Opportunity": 0}
    by_check: dict[str, dict] = {}
    for pg in req.pages:
        for f in pg.findings:
            if f.type in counts:
                counts[f.type] += 1
            by_check.setdefault(f.name, {"count": 0, "type": f.type})
            by_check[f.name]["count"] += 1

    clean_pages = sum(1 for p in req.pages if not p.findings)
    site_score = round(100 * clean_pages / len(req.pages)) if req.pages else 0

    rating_style = ParagraphStyle("rating2", parent=styles["Normal"], fontSize=12, leading=16, textColor=colors.HexColor("#151A16"))
    rating_para = Paragraph(
        f"<b>{clean_pages} of {len(req.pages)} pages clean</b><br/>"
        f"<font size=9 color='#5B6660'>{counts.get('Issue', 0)} issue(s), {counts.get('Warning', 0)} warning(s) and "
        f"{counts.get('Opportunity', 0)} opportunity(ies) found across the crawled pages.</font>",
        rating_style,
    )
    score_row = Table(
        [[ScoreDonut(site_score, "Pages Clean"), rating_para]],
        colWidths=[44 * mm, 118 * mm],
    )
    score_row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (0, 0), "TOP"),
        ("VALIGN", (1, 0), (1, 0), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(score_row)
    story.append(Spacer(1, 4 * mm))
    story.append(_stat_bar(counts, styles))
    story.append(Spacer(1, 7 * mm))
    story.append(_section_divider("SITE-WIDE PATTERNS", styles))

    if by_check:
        rows_sorted = sorted(by_check.items(), key=lambda kv: (_SEVERITY_ORDER.get(kv[1]["type"], 4), -kv[1]["count"]))
        rows = []
        for name, info in rows_sorted:
            color = _SEVERITY_COLOR.get(info["type"], colors.grey)
            pages_pct = round(100 * info["count"] / len(req.pages)) if req.pages else 0
            name_p = Paragraph(f"<b>{name}</b>", cell_name)
            count_p = Paragraph(f"<font size=8.5>{info['count']}/{len(req.pages)}</font>", cell_name)
            rows.append([name_p, HBar(pages_pct, color), count_p])
        tbl = Table(rows, colWidths=[68 * mm, 78 * mm, 16 * mm])
        style_cmds = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#E8EBE8")),
            ("LEFTPADDING", (0, 0), (0, -1), 9),
        ]
        for idx, (name, info) in enumerate(rows_sorted):
            bar = _SEVERITY_COLOR.get(info["type"])
            if bar:
                style_cmds.append(("LINEBEFORE", (0, idx), (0, idx), 2.5, bar))
        tbl.setStyle(TableStyle(style_cmds))
        story.append(tbl)

    if req.pages:
        story.append(Spacer(1, 7 * mm))
        story.append(_section_divider("PER-PAGE BREAKDOWN  (WORST FIRST)", styles))
        pages_sorted = sorted(req.pages, key=lambda p: -len(p.findings))
        rows = []
        for pg in pages_sorted:
            issue_count = len(pg.findings)
            color = colors.HexColor("#C4382B") if issue_count >= 3 else (
                colors.HexColor("#D98A0B") if issue_count else colors.HexColor("#1F8A5B"))
            names = ", ".join(f.name for f in pg.findings) if pg.findings else "No issues found"
            combined = Paragraph(
                f"<font size=8.5>{pg.url}</font><br/><font size=7.5 color='#5B6660'>{names}</font>",
                cell_name,
            )
            count_p = Paragraph(
                f"<font color='{color.hexval()}'><b>{issue_count}</b></font>",
                ParagraphStyle("pgX", parent=styles["Normal"], alignment=2, fontSize=11),
            )
            rows.append([combined, count_p])
        tbl2 = Table(rows, colWidths=[146 * mm, 16 * mm])
        style_cmds2 = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#E8EBE8")),
            ("LEFTPADDING", (0, 0), (0, -1), 9),
        ]
        for idx, pg in enumerate(pages_sorted):
            cnt = len(pg.findings)
            bar = colors.HexColor("#C4382B") if cnt >= 3 else (
                colors.HexColor("#D98A0B") if cnt else colors.HexColor("#1F8A5B"))
            style_cmds2.append(("LINEBEFORE", (0, idx), (0, idx), 2.5, bar))
        tbl2.setStyle(TableStyle(style_cmds2))
        story.append(tbl2)

    doc.build(story)
    buf.seek(0)
    return buf.read()


@app.post("/api/report/site-pdf")
def generate_site_pdf_report(req: SiteReportRequest):
    try:
        pdf_bytes = _build_site_pdf(req)
    except Exception:
        traceback.print_exc()
        try:
            pdf_bytes = _build_site_pdf(req.model_copy(update={"logo_url": ""}))
        except Exception as exc:
            traceback.print_exc()
            raise HTTPException(500, f"Site report generation failed: {exc}")
    filename = f"site-audit-{req.domain}.pdf"
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
