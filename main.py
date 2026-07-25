"""FastAPI app for the health-plan intake front door.

Serves a single mobile-first page that walks the member through the three
stages defined in ``schema.py``, one question at a time, saving partial state
as they go.

Run with::

    uvicorn main:app --reload
"""

import base64
import io
import json
import logging
import mimetypes
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import anthropic
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

import schema

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
SESSION_DIR = os.path.join(BASE_DIR, "sessions")
SUBMISSIONS_DIR = os.path.join(BASE_DIR, "submissions")

logger = logging.getLogger("intake.extract")

# The user asked for this exact model -- do not "upgrade" it silently.
EXTRACT_MODEL = "claude-sonnet-4-6"
EXTRACT_TIMEOUT_SECONDS = 45.0
EXTRACT_MAX_TOKENS = 4096
# Anthropic's base64 request cap is 32MB; stay well under it since base64
# inflates the payload by ~4/3.
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_FILES_PER_EXTRACT = 3

app = FastAPI(title="Health Plan Intake", version=schema.SCHEMA_VERSION)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Sessions live in memory and are written through to disk so a reload during
# development doesn't lose a half-finished intake. Swap for a real datastore
# before this handles anyone's actual health information.
_sessions = {}  # type: Dict[str, Dict[str, Any]]
_lock = threading.Lock()


def _session_path(session_id):
    # type: (str) -> str
    return os.path.join(SESSION_DIR, "%s.json" % (session_id,))


def _valid_session_id(session_id):
    # type: (str) -> bool
    """Guard the path join -- ids come from the URL."""
    try:
        uuid.UUID(session_id)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _load_session(session_id):
    # type: (str) -> Optional[Dict[str, Any]]
    with _lock:
        if session_id in _sessions:
            return _sessions[session_id]

    path = _session_path(session_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as handle:
            record = json.load(handle)
    except (ValueError, IOError):
        return None

    with _lock:
        _sessions.setdefault(session_id, record)
        return _sessions[session_id]


def _persist(session_id, record):
    # type: (str, Dict[str, Any]) -> None
    if not os.path.isdir(SESSION_DIR):
        os.makedirs(SESSION_DIR)
    tmp = _session_path(session_id) + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(record, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, _session_path(session_id))


def _merge_field(existing, incoming):
    # type: (Dict[str, Any], Dict[str, Any]) -> Dict[str, Any]
    """Fold an incoming partial field onto the field we already hold.

    The client sends only the keys it means to change, so the label,
    why_we_ask, and other schema metadata survive untouched.
    """
    merged = dict(existing)
    for key in ("value", "source", "confidence", "confirmed", "skipped"):
        if key in incoming:
            merged[key] = incoming[key]

    if "source" in incoming and incoming["source"] not in schema.SOURCES:
        raise HTTPException(
            status_code=422,
            detail="unknown source: %r" % (incoming["source"],),
        )
    confidence = merged.get("confidence")
    if isinstance(confidence, (int, float)) and not 0.0 <= confidence <= 1.0:
        raise HTTPException(
            status_code=422,
            detail="confidence must be in [0, 1], got %r" % (confidence,),
        )
    return merged


def _submission_path(session_id):
    # type: (str) -> str
    return os.path.join(SUBMISSIONS_DIR, "%s.json" % (session_id,))


def _persist_submission(session_id, payload):
    # type: (str, Dict[str, Any]) -> None
    if not os.path.isdir(SUBMISSIONS_DIR):
        os.makedirs(SUBMISSIONS_DIR)
    tmp = _submission_path(session_id) + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, _submission_path(session_id))


PDF_SOURCE_LABELS = {
    "document": "FROM YOUR DOCUMENT",
    "member": "YOU TOLD US",
    "assumed": "ESTIMATED",
    "empty": "NOT ANSWERED",
}


def _pdf_money(n):
    # type: (Any) -> str
    try:
        return "{:,.0f}".format(float(n))
    except (TypeError, ValueError):
        return str(n)


def _pdf_field_value(field, value, skipped):
    # type: (Dict[str, Any], Any, bool) -> str
    """Mirror the frontend's displayValue() formatting, for the PDF."""
    if skipped and value is None:
        return "Skipped"
    if value is None or value == "":
        return "Not answered"
    if isinstance(value, bool):
        return "Yes" if value else "No"

    item_schema = field.get("item_schema")
    if isinstance(value, list):
        if not value:
            return "None"
        if item_schema:
            lines = []
            for row in value:
                parts = [row.get(k) for k in ("drug", "dosage", "frequency", "payment_method", "pharmacy") if row.get(k)]
                lines.append(" · ".join(parts) if parts else "")
            return "<br/>".join(l for l in lines if l) or "None"
        return "<br/>".join(str(v) for v in value)
    if isinstance(value, dict):
        lines = []
        for key, spec in (item_schema or {}).items():
            sub = value.get(key)
            if sub is not None:
                lines.append("%s $%s" % (spec.get("label", key), _pdf_money(sub)))
        return "<br/>".join(lines) if lines else "Not answered"

    value_type = field.get("value_type")
    if value_type == "currency":
        return "$%s" % _pdf_money(value)
    if value_type == "percent":
        return "%s%%" % value
    return str(value)


def _build_pdf(record):
    # type: (Dict[str, Any]) -> bytes
    """Render the confirmation screen's data as a one-page PDF.

    Grouped the same way as the confirmation screen (record["review_groups"],
    the same list schema.py hands the frontend), so the download matches what
    the member actually reviewed.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        leftMargin=0.55 * inch, rightMargin=0.55 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "PlanTitle", parent=styles["Title"], fontSize=17, leading=20,
        textColor=colors.HexColor("#1A2420"),
    )
    meta_style = ParagraphStyle(
        "Meta", parent=styles["Normal"], fontSize=7.5, spaceAfter=10,
        textColor=colors.HexColor("#5B7268"),
    )
    group_style = ParagraphStyle(
        "GroupTitle", parent=styles["Heading2"], fontSize=9,
        spaceBefore=7, spaceAfter=2, textColor=colors.HexColor("#0F6B5C"),
    )
    # Field labels are the same question text shown on screen, and they're
    # long -- a wide label column keeps most of them to one line, which is
    # what actually keeps 23 fields on a single page (row height is driven
    # by label wrapping, not by value length; even "Not answered" rows are
    # as tall as filled ones).
    label_style = ParagraphStyle(
        "Label", parent=styles["Normal"], fontSize=7, leading=8.5,
        textColor=colors.HexColor("#5B7268"),
    )
    value_style = ParagraphStyle(
        "Value", parent=styles["Normal"], fontSize=8, leading=9.5,
        textColor=colors.HexColor("#1A2420"),
    )
    tag_style = ParagraphStyle(
        "Tag", parent=styles["Normal"], fontSize=6.5, leading=8,
        textColor=colors.HexColor("#0F6B5C"),
    )

    generated = time.strftime("%B %d, %Y", time.localtime(record.get("updated_at") or time.time()))
    story = [
        Paragraph("Your plan summary", title_style),
        Paragraph(
            "Generated %s &middot; Session %s" % (generated, record.get("session_id") or ""),
            meta_style,
        ),
    ]

    fields_by_name = {}  # type: Dict[str, Dict[str, Any]]
    for stage in record["stages"]:
        fields_by_name.update(stage["fields"])

    for group in record.get("review_groups") or []:
        names = [n for n in group["fields"] if n in fields_by_name]
        if not names:
            continue
        story.append(Paragraph(group["title"].upper(), group_style))

        table_data = []
        for name in names:
            field = fields_by_name[name]
            value_text = _pdf_field_value(field, field.get("value"), field.get("skipped"))
            tag_text = PDF_SOURCE_LABELS.get(field.get("source"), "NOT ANSWERED")
            table_data.append([
                Paragraph(field.get("label", name), label_style),
                Paragraph(value_text, value_style),
                Paragraph(tag_text, tag_style),
            ])

        table = Table(table_data, colWidths=[2.5 * inch, 3.6 * inch, 1.1 * inch])
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ("LINEBELOW", (0, 0), (-1, -2), 0.5, colors.HexColor("#DCE5DE")),
        ]))
        story.append(table)

    doc.build(story)
    return buf.getvalue()


@app.get("/")
def index():
    """Serve the single page."""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/schema")
def get_schema():
    """The blank schema the page renders its questions from."""
    return JSONResponse(json.loads(schema.export_json()))


@app.post("/api/save")
def save(payload=Body(...)):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    """Accept a partial state update.

    Body::

        {
          "session_id": "<uuid or omitted to start a new session>",
          "fields": {"zip": {"value": "94110", "source": "member", ...}},
          "progress": {"stage": "basics", "field": "zip"}   # optional
        }

    Only the fields present in the body are touched -- everything else in the
    session is left alone, so the client can save after every single answer.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="body must be an object")

    fields = payload.get("fields") or {}
    if not isinstance(fields, dict):
        raise HTTPException(status_code=422, detail="`fields` must be an object")

    session_id = payload.get("session_id")
    record = None
    if session_id:
        if not _valid_session_id(session_id):
            raise HTTPException(status_code=422, detail="malformed session_id")
        record = _load_session(session_id)

    if record is None:
        session_id = session_id if _valid_session_id(session_id or "") else str(uuid.uuid4())
        record = json.loads(schema.export_json())
        record["session_id"] = session_id
        record["created_at"] = time.time()

    known = {}  # type: Dict[str, Dict[str, Any]]
    for stage in record["stages"]:
        known.update(stage["fields"])

    unknown = sorted(set(fields) - set(known))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail="unknown field(s): %s" % ", ".join(unknown),
        )

    with _lock:
        for stage in record["stages"]:
            for name, incoming in fields.items():
                if name not in stage["fields"]:
                    continue
                if not isinstance(incoming, dict):
                    raise HTTPException(
                        status_code=422,
                        detail="field %r must be an object" % (name,),
                    )
                stage["fields"][name] = _merge_field(
                    stage["fields"][name], incoming
                )

        if isinstance(payload.get("progress"), dict):
            record["progress"] = payload["progress"]
        record["updated_at"] = time.time()
        _sessions[session_id] = record
        _persist(session_id, record)

    return {
        "session_id": session_id,
        "saved": sorted(fields),
        "updated_at": record["updated_at"],
    }


@app.get("/api/session/{session_id}")
def get_session(session_id):
    # type: (str) -> Dict[str, Any]
    """Return the saved state for a session."""
    if not _valid_session_id(session_id):
        raise HTTPException(status_code=404, detail="no such session")
    record = _load_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such session")
    return record


@app.get("/api/pdf/{session_id}")
def get_pdf(session_id):
    # type: (str) -> Response
    """The member-facing download: a one-page PDF built from the saved session.

    Replaces the old client-side JSON export -- the JSON itself is now
    backend-only (see /api/submit), never handed to the member directly.
    """
    if not _valid_session_id(session_id):
        raise HTTPException(status_code=404, detail="no such session")
    record = _load_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such session")
    pdf_bytes = _build_pdf(record)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="emme-plan-summary.pdf"'},
    )


@app.post("/api/submit")
def submit(payload=Body(...)):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    """Persist the full schema JSON for Emme's cost engine to pick up.

    The member never sees this file -- it's the backend record of what they
    confirmed, written to submissions/{session_id}.json.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="body must be an object")
    session_id = payload.get("session_id")
    if not session_id or not _valid_session_id(session_id):
        raise HTTPException(status_code=422, detail="a valid session_id is required")

    _persist_submission(session_id, payload)
    return {"status": "ok", "session_id": session_id}


@app.get("/api/submissions/{session_id}")
def get_submission(session_id):
    # type: (str) -> Dict[str, Any]
    """Let Emme's cost engine retrieve a submitted plan summary."""
    if not _valid_session_id(session_id):
        raise HTTPException(status_code=404, detail="no such submission")
    path = _submission_path(session_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="no such submission")
    with open(path, "r") as handle:
        return json.load(handle)


def _extractable_fields():
    # type: () -> List[Dict[str, Any]]
    """Flatten the schema into the field list the extraction prompt describes.

    Every field is offered -- a card mostly yields carrier/plan_type/metal_tier,
    an EOB mostly yields the cost-sharing numbers, but which document the
    member uploaded isn't known ahead of time, so the model decides what's
    actually present.
    """
    out = []
    blank = schema.build_schema()
    for record in schema.iter_fields(blank):
        field = record["field"]
        entry = {
            "name": record["name"],
            "label": field["label"],
            "value_type": field["value_type"],
        }
        if field.get("choices"):
            entry["choices"] = field["choices"]
        if field.get("item_schema"):
            entry["item_schema"] = {
                key: spec["label"] for key, spec in field["item_schema"].items()
            }
        out.append(entry)
    return out


def _extraction_prompt():
    # type: () -> str
    fields = _extractable_fields()
    lines = [
        "You are reading a health insurance card, Summary of Benefits and "
        "Coverage (SBC), or Explanation of Benefits (EOB) for a member "
        "filling out an intake form. Extract every one of the following "
        "fields you can actually find evidence for in the document(s). "
        "Do not guess or infer a value that isn't shown -- leave it out "
        "entirely if it's not present.",
        "",
        "Fields (name: label [type] [choices/sub-fields]):",
    ]
    for f in fields:
        bits = "%s: %s [%s]" % (f["name"], f["label"], f["value_type"])
        if f.get("choices"):
            bits += " choices=%s" % (f["choices"],)
        if f.get("item_schema"):
            bits += " sub_fields=%s" % (f["item_schema"],)
        lines.append("- %s" % bits)
    lines += [
        "",
        "Respond with ONLY a single valid JSON object, no markdown code "
        "fences, no commentary before or after it. Shape:",
        "{",
        '  "<field_name>": {',
        '    "value": <the extracted value, matching the field\'s type>,',
        '    "confidence": <number 0-1, your belief this value is correct>,',
        '    "snippet": "<the exact text from the document this came from>"',
        "  },",
        "  ...",
        "}",
        "Omit any field you found no evidence for -- do not include it with a "
        "null value. For \"choices\" fields, the value must be exactly one of "
        "the listed choices. For currency/percent/integer fields, the value "
        "is a bare number (no \"$\" or \"%\"). For the copays field, value is "
        "an object keyed by the sub-field names shown above. For the "
        "prescriptions field, value is a list of objects with drug/dosage/"
        "frequency/pharmacy keys.",
    ]
    return "\n".join(lines)


def _guess_media_type(filename, fallback):
    # type: (Optional[str], Optional[str]) -> Optional[str]
    if fallback and fallback != "application/octet-stream":
        return fallback
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed


def _content_block_for_upload(media_type, data_b64):
    # type: (str, str) -> Optional[Dict[str, Any]]
    if media_type == "application/pdf":
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data_b64,
            },
        }
    if media_type in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data_b64,
            },
        }
    return None


def _coerce_value(field, raw_value):
    # type: (Dict[str, Any], Any) -> Any
    """Reject a model-returned value that doesn't fit the field's own shape.

    Extracted values feed straight into the intake state, so a field the
    model got wrong in *type* (not just in fact) should be dropped rather
    than stored -- better an empty field the member fills in than a plan
    summary state that silently contains the wrong shape of data.
    """
    value_type = field["value_type"]
    if value_type in ("currency", "percent", "integer"):
        if isinstance(raw_value, bool):
            return None
        if isinstance(raw_value, (int, float)):
            return raw_value
        if isinstance(raw_value, str):
            try:
                return float(raw_value.replace("$", "").replace("%", "").replace(",", ""))
            except ValueError:
                return None
        return None
    if value_type == "boolean":
        return raw_value if isinstance(raw_value, bool) else None
    if field.get("choices"):
        return raw_value if raw_value in field["choices"] else None
    if value_type == "object" and field.get("item_schema"):
        if not isinstance(raw_value, dict):
            return None
        return {k: v for k, v in raw_value.items() if k in field["item_schema"]}
    if value_type == "list" and field.get("item_schema"):
        if not isinstance(raw_value, list):
            return None
        rows = []
        for row in raw_value:
            if isinstance(row, dict):
                rows.append({k: v for k, v in row.items() if k in field["item_schema"]})
        return rows
    if value_type == "list":
        return raw_value if isinstance(raw_value, list) else None
    return raw_value if isinstance(raw_value, str) else None


def _parse_extraction_response(text):
    # type: (str) -> Dict[str, Any]
    """Best-effort JSON parse. The prompt says "no fences" but models slip."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


def _pypdf_extract_text(pdf_bytes):
    # type: (bytes) -> str
    """Local, offline text extraction -- no network, no API key required."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        return ""
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(pages)


_MONEY_PATTERN = r"\$?\s?([\d]{1,3}(?:,\d{3})*(?:\.\d{2})?)"

DEDUCTIBLE_INDIVIDUAL_LABELS = (
    r"in.?network\s+deductible\s+applied\s+to\s+date",
    r"individual\s+deductible",
    r"deductible\s+applied",
)

OOP_MET_YTD_LABELS = (
    r"in.?network\s+out.?of.?pocket\s+maximum\s+applied\s+to\s+date",
    r"out.?of.?pocket\s+maximum\s+applied",
    r"oop\s+met",
)


def _dollar_match_near_label(text, label_patterns):
    # type: (str, Any) -> Optional[Any]
    """Find a dollar amount within ~100 characters after a label.

    EOB PDFs often split a label and its dollar amount across lines, so this
    deliberately allows whitespace and nearby content between them. Returning
    the match keeps the source snippet available for the review screen.
    """
    for pattern in label_patterns:
        match = re.search(
            pattern + r"[\s\S]{0,100}?\$?\s*([\d,]+\.\d{2})",
            text,
            re.IGNORECASE,
        )
        if match:
            return match
    return None

PROVIDER_NAME_RE = re.compile(
    r"provider name:\s*([A-Za-z][A-Za-z.,'\- ]{1,60})",
    re.IGNORECASE,
)

# Checked in order so a compound name ("Blue Cross Blue Shield") wins over
# matching just one half of it. Values are the exact strings from
# schema.CARRIERS so a regex hit renders as a normal picked choice on
# Screen 2, not free text.
CARRIER_PATTERNS = [
    (re.compile(r"blue cross blue shield", re.IGNORECASE), "Blue Cross Blue Shield"),
    (re.compile(r"blue cross", re.IGNORECASE), "Blue Cross Blue Shield"),
    (re.compile(r"blue shield", re.IGNORECASE), "Blue Cross Blue Shield"),
    (re.compile(r"unitedhealthcare", re.IGNORECASE), "UnitedHealthcare"),
    (re.compile(r"\baetna\b", re.IGNORECASE), "Aetna"),
    (re.compile(r"\bcigna\b", re.IGNORECASE), "Cigna"),
    (re.compile(r"\bhumana\b", re.IGNORECASE), "Humana"),
    (re.compile(r"kaiser permanente", re.IGNORECASE), "Kaiser Permanente"),
    (re.compile(r"\bkaiser\b", re.IGNORECASE), "Kaiser Permanente"),
]


def _line_for_match(text, match):
    # type: (str, Any) -> str
    """The single line a regex match landed on, as the source snippet."""
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    if end == -1:
        end = len(text)
    return text[start:end].strip()


def _regex_extract_fields(text):
    # type: (str) -> Dict[str, Dict[str, Any]]
    """Cheap, offline pattern matching over PDF text -- no model, no network.

    Runs before (and independently of) the LLM path, so extraction still
    produces something useful with no API key and no connectivity at all.
    """
    out = {}  # type: Dict[str, Dict[str, Any]]

    m = _dollar_match_near_label(text, DEDUCTIBLE_INDIVIDUAL_LABELS)
    if m:
        out["deductible_individual"] = {
            "value": float(m.group(1).replace(",", "")),
            "confidence": 0.8,
            "source_snippet": _line_for_match(text, m),
        }

    m = _dollar_match_near_label(text, OOP_MET_YTD_LABELS)
    if m:
        out["oop_met_ytd"] = {
            "value": float(m.group(1).replace(",", "")),
            "confidence": 0.8,
            "source_snippet": _line_for_match(text, m),
        }

    m = PROVIDER_NAME_RE.search(text)
    if m:
        out["primary_doctor_name"] = {
            "value": m.group(1).strip(),
            "confidence": 0.8,
            "source_snippet": _line_for_match(text, m),
        }

    for pattern, canonical in CARRIER_PATTERNS:
        m = pattern.search(text)
        if m:
            out["carrier"] = {
                "value": canonical,
                "confidence": 0.8,
                "source_snippet": _line_for_match(text, m),
            }
            break

    return out


@app.post("/api/extract")
async def extract(request: Request):
    # type: (Request) -> Dict[str, Any]
    """Pull field values out of an uploaded plan document.

    Accepts a multipart upload from Screen 1 (one or more files) or a bare
    JSON body with just a ``session_id`` (nothing to extract from). Two
    extraction paths run, in order:

    1. **Local regex pass** (``_regex_extract_fields``), over PDF text pulled
       with pypdf -- no network, no API key, works entirely offline.
    2. **LLM pass** via Claude, only attempted when ``ANTHROPIC_API_KEY`` is
       set and a file was actually uploaded. Each file goes to the model as a
       ``document`` block (PDF) or an ``image`` block, alongside a prompt
       asking for every schema field it can find.

    Where both passes find the same field, the LLM's value wins -- it reads
    context the regexes can't. Where only the regex pass found something (no
    key, call failed, or the model missed it), that value is kept. The empty
    state only shows if neither pass found anything.

    On *any* failure in the LLM path -- an unreadable file, a timeout, an API
    error, a response that isn't valid JSON -- that path's result is just an
    empty dict; the regex results (if any) still come back normally, and
    nothing about the failure reaches the member.
    """
    session_id = None
    files = []  # type: List[Dict[str, Any]]

    content_type = request.headers.get("content-type", "")
    try:
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            session_id = form.get("session_id")
            for value in form.values():
                filename = getattr(value, "filename", None)
                if not filename:
                    continue
                body = await value.read()
                if not body or len(body) > MAX_UPLOAD_BYTES:
                    continue
                media_type = _guess_media_type(filename, getattr(value, "content_type", None))
                if not media_type:
                    continue
                files.append({
                    "filename": filename,
                    "media_type": media_type,
                    "bytes": body,
                })
                if len(files) >= MAX_FILES_PER_EXTRACT:
                    break
        else:
            payload = await request.json()
            if isinstance(payload, dict):
                session_id = payload.get("session_id")
    except Exception:
        logger.exception("failed to read /api/extract upload")
        files = []

    # Pass 1: local, offline, no key required. Runs against every uploaded
    # PDF regardless of whether the LLM path is even available.
    regex_fields = {}  # type: Dict[str, Any]
    pdf_text = "\n".join(
        _pypdf_extract_text(f["bytes"]) for f in files if f["media_type"] == "application/pdf"
    ).strip()
    if pdf_text:
        try:
            regex_fields = _regex_extract_fields(pdf_text)
        except Exception:
            logger.exception("regex extraction failed for session %r", session_id)
            regex_fields = {}

    # Pass 2: the LLM, only when there's a key to use and something to send it.
    llm_fields = {}  # type: Dict[str, Any]
    content_blocks = []
    for f in files:
        block = _content_block_for_upload(f["media_type"], base64.b64encode(f["bytes"]).decode("ascii"))
        if block is not None:
            content_blocks.append(block)

    if content_blocks and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            client = anthropic.Anthropic().with_options(timeout=EXTRACT_TIMEOUT_SECONDS)
            response = client.messages.create(
                model=EXTRACT_MODEL,
                max_tokens=EXTRACT_MAX_TOKENS,
                messages=[{
                    "role": "user",
                    "content": content_blocks + [{"type": "text", "text": _extraction_prompt()}],
                }],
            )

            if response.stop_reason == "refusal":
                logger.warning("extraction refused for session %r", session_id)
            else:
                text = next((b.text for b in response.content if b.type == "text"), "")
                parsed = _parse_extraction_response(text)
                if not isinstance(parsed, dict):
                    raise ValueError("extraction response was not a JSON object")

                known = {r["name"]: r["field"] for r in schema.iter_fields(schema.build_schema())}
                for name, entry in parsed.items():
                    if name not in known or not isinstance(entry, dict):
                        continue
                    value = _coerce_value(known[name], entry.get("value"))
                    if value is None or value == [] or value == {}:
                        continue
                    confidence = entry.get("confidence")
                    if not isinstance(confidence, (int, float)):
                        confidence = 0.7
                    confidence = max(0.0, min(1.0, float(confidence)))
                    llm_fields[name] = {
                        "value": value,
                        "confidence": confidence,
                        "source_snippet": entry.get("snippet"),
                    }
        except Exception:
            # Bad file, API error, timeout, malformed JSON, missing/invalid
            # key -- all the same outcome: the LLM pass just contributes
            # nothing, and the regex pass's results (if any) still stand.
            logger.exception("LLM extraction failed for session %r", session_id)
            llm_fields = {}

    # LLM values win where both passes found the same field -- it has more
    # context than a regex does. Regex-only fields (no key, call failed, or
    # the model missed it) are kept as-is.
    fields_out = dict(regex_fields)
    fields_out.update(llm_fields)

    low_confidence = sorted(n for n, f in fields_out.items() if f["confidence"] < 0.7)

    if fields_out:
        message = "Found %d detail%s -- give them a quick look below." % (
            len(fields_out), "" if len(fields_out) == 1 else "s",
        )
    else:
        message = (
            "We couldn't read anything from that. Your details are "
            "pre-filled with estimates instead -- correct whatever looks wrong."
        )

    return {
        "status": "ok",
        "schema_version": schema.SCHEMA_VERSION,
        "session_id": session_id,
        "fields": fields_out,
        "low_confidence": low_confidence,
        "message": message,
    }
