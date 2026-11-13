"""The Case Validation summary a reviewer hands over, as a Word document.

What leaves this tool at the end of a review is not a spreadsheet of verdicts -
that is claim_export.py - but a document somebody reads: a cover sheet saying
what was looked at and how it came out, then one page per case carrying the
claim's own particulars beside the reviewer's finding, then the pages they
marked while deciding, with what they wrote about each.

Format: a .doc that is really MHTML - the multipart/related web archive Word
itself writes when you Save As "Single File Web Page". It is chosen over a
plain HTML .doc because the evidence screenshots have to travel *inside* the
file, and Word does not render base64 data: URIs; and over a real .docx
because that would mean a new dependency to produce a file nobody asked to be
a .docx. Word opens this, renders the images, and saves it on as .docx
if the reader wants.

Nothing here recomputes anything the claim page does not already compute: the
particulars come from claim_summary and claim_amounts, so a row in the
document and the same row on the claim's own page cannot disagree.
"""
import base64
import html
import io
import os
import re
from datetime import datetime

from PIL import Image

from . import (cache_store, claim_amounts, claim_extract, claim_scanner,
               claim_summary, csv_data, root_state)
from .review_list import OUTCOME_LABELS

# Evidence images are the whole weight of this file. A page renders to roughly
# 150-400 KB at these settings, so the caps are what keep a reviewer who ticks
# every box from building a document that no mail server will carry and that
# this process has to hold in memory while it does.
IMAGE_MAX_EDGE = 1100          # px, longest side
IMAGE_QUALITY = 72
MAX_IMAGES = 150
MAX_IMAGE_BYTES = 40 * 1024 * 1024

# The document width an image is scaled to on the page. 6.2in inside 2cm
# margins on A4 leaves it full-width without pushing a column off the sheet.
IMAGE_DISPLAY_WIDTH = "6.2in"

MIME_TYPE = "application/msword"

# Columns the claims dataset might carry these under. The warehouse tables,
# an uploaded PMJAY export and a hand-made CSV all name them differently, and
# none of the three is worth privileging - the first one present wins and a
# claim with none shows the row blank rather than omitting it.
DISTRICT_COLUMNS = ("district_name", "district", "hospital_district",
                    "hosp_district", "provider_district")
STATE_COLUMNS = ("state_name", "state", "hospital_state", "hosp_state",
                 "provider_state")
HOSPITAL_TYPE_COLUMNS = ("hospital_type", "hosp_type", "provider_type",
                         "hospital_type_desc")
CASE_TYPE_COLUMNS = ("case_type", "claim_type", "treatment_type",
                     "admission_type")
CASE_ID_COLUMNS = ("case_id", "claim_id", "claim_number")

# Single letters as the warehouse records them; see fetch/queries.HOSPITAL_TYPES.
HOSPITAL_TYPE_LABELS = {"p": "Private", "g": "Government",
                        "private": "Private", "government": "Government"}
GENDER_LABELS = {"m": "Male", "f": "Female", "male": "Male",
                 "female": "Female", "o": "Other", "other": "Other"}


# ----------------------------------------------------------------- formatting


def _text(value):
    if value is None:
        return ""
    value = str(value).strip()
    return "" if value.lower() in ("none", "nan", "null") else value


def _e(value):
    """Escaped for the document body, with blanks kept as empty cells."""
    return html.escape(_text(value))


def _pick_column(row, names):
    """The first of `names` this row has a value for, matched case-insensitively.

    Case-insensitively because the same export arrives as `District_Name` from
    one place and `district_name` from another, and a blank District row on
    every case because of a capital letter is not a failure anyone would think
    to go looking for.
    """
    if not row:
        return ""
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for name in names:
        value = _text(lowered.get(name))
        if value:
            return value
    return ""


def _date(value):
    """A claim date as dd/mm/yyyy, or the raw text when it will not parse."""
    raw = _text(value)
    if not raw:
        return ""
    parsed = claim_summary.parse_date_text(raw)
    return parsed.strftime("%d/%m/%Y") if parsed else raw


def _indian_digits(whole):
    """36800 -> 36,800 and 1234567 -> 12,34,567.

    Lakh/crore grouping, not thousands: these are Indian health-scheme claims
    and the document is read by people who would misread 1,234,567.
    """
    digits = str(whole)
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def _rupees(value):
    if value in (None, ""):
        return ""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return _text(value)
    negative = amount < 0
    amount = abs(amount)
    whole = int(amount)
    paise = round(amount - whole, 2)
    rendered = _indian_digits(whole)
    if paise:
        rendered += f"{paise:.2f}"[1:]
    return ("-" if negative else "") + "\u20b9" + rendered


def _days(value):
    if value in (None, ""):
        return ""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return _text(value)
    return f"{count} day" + ("" if count == 1 else "s")


def _years(value):
    raw = _text(value)
    if not raw:
        return ""
    return raw if re.search(r"[A-Za-z]", raw) else f"{raw} years"


def _when(created_at):
    """A stored timestamp as something readable, unchanged if it will not parse."""
    raw = _text(created_at)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime(
            "%d %b %Y %H:%M")
    except ValueError:
        return raw


# ------------------------------------------------------------- claim facts


def _claim_docs(claim_id):
    """The claim's scanned documents, or [] when it is not in this folder.

    A decision outlives the bundle it was made against - the folder gets
    changed, the bundles get deleted after a fetch - and an export of last
    month's work must still produce the claim's particulars from the database
    mirror. Only the OCR columns and the evidence images need the files.
    """
    try:
        claim_path = root_state.get_claim_path(claim_id)
    except (ValueError, FileNotFoundError, RuntimeError):
        return None
    try:
        return claim_scanner.scan_claim_cached(claim_path)
    except OSError:
        return None


def _procedures(extracted):
    """[(code, name), ...] distinct, in the order the claim lists them.

    The first is the main procedure and the rest are additional, which is the
    claim's own ordering rather than a judgement made here.
    """
    seen, out = set(), []
    for row in (extracted or {}).get("claim_bundle_treatments") or []:
        code = _text(row.get("procedure_code"))
        name = _text(row.get("procedure_name"))
        if not code and not name:
            continue
        key = (code.upper(), name.upper())
        if key in seen:
            continue
        seen.add(key)
        out.append((code, name))
    return out


def _procedure_text(pairs):
    return ", ".join(
        f"{code}: {name}" if code and name else (code or name) for code, name in pairs)


def _latest_total(amounts):
    """The claim's money as it finally stood.

    A claim is restated at preauthorisation, at enhancement and again at claim
    time, each its own set of rows; claim_amounts already orders them, so the
    last one with figures is the one that stands.
    """
    for entry in reversed(amounts.get("totals") or []):
        if entry.get("claimed_amount") is not None or entry.get("approved_amount") is not None:
            return entry
    return {}


def _summary_value(summary, key, source):
    for row in summary.get("rows") or []:
        if row["key"] == key:
            return row.get(source)
    return None


def _preferred(summary, key):
    """The claim's own value for a field, falling back outwards.

    The bundle is what the payer and provider systems actually recorded, the
    claims dataset is a re-export of it, and OCR is a reading of a scan. In
    that order.
    """
    for source in ("bundle", "csv", "ocr"):
        value = _text(_summary_value(summary, key, source))
        if value:
            return value
    return ""


def claim_facts(claim_id, user_id=None):
    """Everything the Case Summary table prints for one claim."""
    extracted = claim_extract.get(claim_id) or {}
    bundle_rows = extracted.get("claim_bundle_summary") or []
    bundle_row = bundle_rows[0] if bundle_rows else None
    csv_row = csv_data.get_claim_row(claim_id, user_id) or {}
    docs = _claim_docs(claim_id)
    summary = claim_summary.build_claim_summary(
        claim_id, docs or [], csv_row, bundle_row)
    amounts = claim_amounts.build_amounts_panel(extracted)
    totals = _latest_total(amounts)
    procedures = _procedures(extracted)

    gender = _text((bundle_row or {}).get("patient_gender")) or _pick_column(
        csv_row, ("gender", "patient_gender", "sex"))
    hospital_type = _pick_column(csv_row, HOSPITAL_TYPE_COLUMNS)
    case_type = (_pick_column(csv_row, CASE_TYPE_COLUMNS)
                 or _text((bundle_row or {}).get("admission_type")))
    if not case_type:
        for row in extracted.get("claim_bundle_treatments") or []:
            case_type = _text(row.get("treatment_type_description")) or _text(
                row.get("treatment_type"))
            if case_type:
                break

    district = _pick_column(csv_row, DISTRICT_COLUMNS)
    state = _pick_column(csv_row, STATE_COLUMNS)

    return {
        "claim_id": claim_id,
        "available": docs is not None,
        "case_id": (_text((bundle_row or {}).get("case_id"))
                    or _pick_column(csv_row, CASE_ID_COLUMNS) or claim_id),
        "case_type": case_type,
        "patient_name": _preferred(summary, "name"),
        "age": _years(_preferred(summary, "age")),
        "gender": GENDER_LABELS.get(gender.strip().lower(), gender),
        "hospital_name": _preferred(summary, "hospital_name"),
        "district_state": ", ".join(p for p in (district, state) if p),
        "hospital_type": HOSPITAL_TYPE_LABELS.get(
            hospital_type.strip().lower(), hospital_type),
        "main_procedure": _procedure_text(procedures[:1]),
        "additional_procedures": _procedure_text(procedures[1:]),
        # Starred in the document: the claims record against the documents the
        # hospital uploaded. Two rows rather than one because where they
        # differ is the whole point of the comparison.
        "tms_admission": _date(_summary_value(summary, "admission_date", "csv")),
        "tms_discharge": _date(_summary_value(summary, "discharge_date", "csv")),
        "tms_los": _days(_summary_value(summary, "length_of_stay_days", "csv")),
        "doc_admission": _date(_summary_value(summary, "admission_date", "ocr")),
        "doc_discharge": _date(_summary_value(summary, "discharge_date", "ocr")),
        "doc_los": _days(_summary_value(summary, "length_of_stay_days", "ocr")),
        "amount_claimed": _rupees(
            totals.get("claimed_amount")
            if totals.get("claimed_amount") is not None
            else (bundle_row or {}).get("claimed_amount")),
        "amount_approved": _rupees(
            totals.get("approved_amount")
            if totals.get("approved_amount") is not None
            else (bundle_row or {}).get("approved_amount")),
    }


# -------------------------------------------------------------- page images


def page_image(claim_id, document_path, page_number):
    """The rendered page a mark points at, scaled for the document, or None.

    None covers every ordinary reason a page cannot be produced - the folder
    has moved on, the bundle was deleted after its fetch, the document was
    never processed - and the document then prints the description with a line
    saying the image is unavailable, which is more use than an export that
    fails outright over one missing file.
    """
    try:
        claim_path = root_state.get_claim_path(claim_id)
        abs_path = claim_scanner.resolve_doc(claim_path, document_path)
        cached = cache_store.load_cached_result(cache_store.hash_file(abs_path))
    except (ValueError, FileNotFoundError, OSError, RuntimeError):
        return None
    if not cached:
        return None

    page = next((p for p in cached.get("pages") or []
                 if p.get("page_number") == page_number), None)
    if not page or not page.get("image_rel"):
        return None
    try:
        source = cache_store.page_image_path(page["image_rel"])
        if not os.path.exists(source):
            return None
        with Image.open(source) as image:
            image = image.convert("RGB")
            image.thumbnail((IMAGE_MAX_EDGE, IMAGE_MAX_EDGE), Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=IMAGE_QUALITY)
            return buffer.getvalue()
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------- the doc


STYLE = """
@page { size: 21.0cm 29.7cm; margin: 2.0cm 1.8cm 2.0cm 1.8cm; }
body { font-family: Calibri, Arial, sans-serif; font-size: 11.0pt; color: #000000; }
h1 { font-size: 24.0pt; font-weight: normal; color: #2F5496; margin: 0 0 6pt 0; }
h2 { font-size: 12.0pt; margin: 0 0 2pt 0; }
p { margin: 0 0 6pt 0; }
hr { border: none; border-top: 1px solid #A6A6A6; margin: 0 0 14pt 0; }
table.grid { border-collapse: collapse; width: 100%; margin: 0 0 8pt 0; }
table.grid td, table.grid th { border: 1px solid #000000; padding: 4pt 6pt;
  font-size: 10.0pt; vertical-align: top; }
table.grid th { background: #F2F2F2; text-align: left; }
td.label { font-weight: bold; width: 22%; }
.counts { margin: 0 0 2pt 0; font-size: 12.0pt; }
.footnote { font-size: 9.0pt; font-style: italic; color: #404040; }
.evidence-caption { font-size: 10.5pt; font-weight: bold; margin: 10pt 0 4pt 0; }
.evidence-missing { font-size: 10.0pt; font-style: italic; color: #808080; }
.outcome { font-weight: bold; }
"""

PAGE_BREAK = ('<br clear="all" '
              'style="mso-special-character:line-break;page-break-before:always">')


def _cover(filename, title, rows, tally):
    out = ['<h1>Case Reviews</h1>', '<hr>']
    out.append(f'<p><b>File name -</b> {_e(title or filename)}</p>')
    # Worded as the case pages word it - a case the cover counts as Suspicious
    # must not open on a page headed Validation Outcome: Flagged.
    for status in ("approved", "flagged", "rejected"):
        count = tally["by_status"].get(status, 0)
        out.append(f'<p class="counts">{count} {OUTCOME_LABELS[status]}</p>')
    out.append('<table class="grid"><tr><th>Case ID</th><th>Verdict</th>'
               '<th>Reviewed by</th><th>Reviewed on</th></tr>')
    for row in rows:
        out.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                _e(row["facts"]["case_id"]),
                _e(OUTCOME_LABELS.get(row["review"]["status"],
                                      row["review"]["status"])),
                _e(row["review"].get("reviewer_name") or row["review"].get("reviewer")),
                _e(_when(row["review"]["created_at"]))))
    out.append("</table>")
    return "".join(out)


def _case_table(facts, notes):
    f = facts
    # Six columns so the three-pair rows (name/age/gender) line up with the
    # two-pair ones; the long rows span what they need.
    return f"""<table class="grid">
<tr><td class="label">Case ID</td><td>{_e(f['case_id'])}</td>
    <td class="label">Case Type</td><td colspan="3">{_e(f['case_type'])}</td></tr>
<tr><td class="label">Patient name</td><td>{_e(f['patient_name'])}</td>
    <td class="label">Age</td><td>{_e(f['age'])}</td>
    <td class="label">Gender</td><td>{_e(f['gender'])}</td></tr>
<tr><td class="label">Hospital name</td><td>{_e(f['hospital_name'])}</td>
    <td class="label">District and State</td><td>{_e(f['district_state'])}</td>
    <td class="label">Hospital Type</td><td>{_e(f['hospital_type'])}</td></tr>
<tr><td class="label">Main Procedure code(s) and name(s)</td>
    <td colspan="5">{_e(f['main_procedure'])}</td></tr>
<tr><td class="label">Additional Procedure Code(s) and names</td>
    <td colspan="5">{_e(f['additional_procedures'])}</td></tr>
<tr><td class="label">Date of admission*</td><td>{_e(f['tms_admission'])}</td>
    <td class="label">Date of Discharge*</td><td>{_e(f['tms_discharge'])}</td>
    <td class="label">LOS</td><td>{_e(f['tms_los'])}</td></tr>
<tr><td class="label">Date of admission**</td><td>{_e(f['doc_admission'])}</td>
    <td class="label">Date of Discharge**</td><td>{_e(f['doc_discharge'])}</td>
    <td class="label">LOS</td><td>{_e(f['doc_los'])}</td></tr>
<tr><td class="label">Amount claimed</td><td>{_e(f['amount_claimed'])}</td>
    <td class="label">Amount Approved</td><td colspan="3">{_e(f['amount_approved'])}</td></tr>
<tr><td class="label">Reason if main procedure not justified</td>
    <td colspan="5">{_e(notes)}</td></tr>
<tr><td class="label">Reason if Additional procedure(s) not justified</td>
    <td colspan="5">{_e(notes)}</td></tr>
</table>
<p class="footnote">* TMS records; ** uploaded hospital documents.</p>"""


def _case(index, row, images, budget):
    """One case's section. Appends its images to `images` as it goes."""
    review, facts = row["review"], row["facts"]
    outcome = OUTCOME_LABELS.get(review["status"], review["status"])
    notes = _text(review.get("notes"))
    out = [f'<h2>Case Summary {index}</h2>',
           f'<p class="outcome">Validation Outcome: {_e(outcome)}</p>',
           _case_table(facts, notes)]

    evidence = row.get("evidence") or []
    if not evidence:
        return "".join(out)

    out.append('<p class="evidence-caption">Evidence screenshots</p>')
    for number, item in enumerate(evidence, start=1):
        name = os.path.basename(item["document_path"])
        description = _text(item.get("note")) or f"{name}, page {item['page_number']}"
        out.append(f'<p class="evidence-caption">{number}. {_e(description)}</p>')

        data = None
        if budget["images"] < MAX_IMAGES and budget["bytes"] < MAX_IMAGE_BYTES:
            data = page_image(review["claim_id"], item["document_path"],
                              item["page_number"])
        if data is None:
            out.append('<p class="evidence-missing">'
                       f'[{_e(name)}, page {item["page_number"]} &mdash; image not '
                       'available: the claim is not in the current folder, or its '
                       'documents have not been processed.]</p>')
            continue
        budget["images"] += 1
        budget["bytes"] += len(data)
        filename = f"image{budget['images']:03d}.jpg"
        images.append((filename, data))
        out.append(f'<p><img src="{filename}" width="100%" '
                   f'style="width:{IMAGE_DISPLAY_WIDTH}"></p>')
    return "".join(out)


def _document_html(filename, title, rows, tally):
    images, budget = [], {"images": 0, "bytes": 0}
    body = [_cover(filename, title, rows, tally)]
    for index, row in enumerate(rows, start=1):
        body.append(PAGE_BREAK)
        body.append(_case(index, row, images, budget))

    html_text = f"""<html xmlns:o="urn:schemas-microsoft-com:office:office"
xmlns:w="urn:schemas-microsoft-com:office:word"
xmlns="http://www.w3.org/TR/REC-html40">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<meta name="ProgId" content="Word.Document">
<title>Case Validation Summary</title>
<!--[if gte mso 9]><xml><w:WordDocument><w:View>Print</w:View>
<w:Zoom>100</w:Zoom></w:WordDocument></xml><![endif]-->
<style>{STYLE}</style>
</head>
<body>
{''.join(body)}
</body>
</html>"""
    return html_text, images, budget


# MHTML, the same container Word writes for "Single File Web Page". The parts
# are addressed by Content-Location and the HTML refers to them relatively, so
# `image001.jpg` in the body resolves to the part declared at
# <BASE_LOCATION>image001.jpg - which is why the base has to be an absolute
# URL even though no such path exists anywhere.
BOUNDARY = "----=_NextPart_claim_review_case_validation"
BASE_LOCATION = "file:///C:/CaseValidation/"


def _b64(data):
    encoded = base64.b64encode(data).decode("ascii")
    return "\r\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76)) + "\r\n"


def _mhtml(html_text, images):
    out = [
        "MIME-Version: 1.0\r\n",
        f'Content-Type: multipart/related; boundary="{BOUNDARY}"; type="text/html"\r\n',
        "\r\n",
        "This is a Web archive file. Open it with Microsoft Word.\r\n",
        f"\r\n--{BOUNDARY}\r\n",
        f"Content-Location: {BASE_LOCATION}casevalidation.htm\r\n",
        "Content-Transfer-Encoding: base64\r\n",
        'Content-Type: text/html; charset="utf-8"\r\n\r\n',
        _b64(html_text.encode("utf-8")),
    ]
    for name, data in images:
        out += [
            f"\r\n--{BOUNDARY}\r\n",
            f"Content-Location: {BASE_LOCATION}{name}\r\n",
            "Content-Transfer-Encoding: base64\r\n",
            "Content-Type: image/jpeg\r\n\r\n",
            _b64(data),
        ]
    out.append(f"\r\n--{BOUNDARY}--\r\n")
    return "".join(out).encode("ascii")


def timestamp(now=None):
    return (now or datetime.now()).strftime("%Y%m%d_%H%M%S")


def build(reviews, evidence_by_review, user_id=None, title=None, now=None):
    """(filename, bytes, report) for a selection of decisions.

    `reviews` are review_list rows, already in the order the cases should be
    numbered in. `report` says what was left out, so the page can tell the
    reviewer rather than letting them find a truncated document later.
    """
    filename = f"Case_Validation_{timestamp(now)}.doc"

    facts_cache = {}
    rows = []
    for review in reviews:
        claim_id = review["claim_id"]
        if claim_id not in facts_cache:
            facts_cache[claim_id] = claim_facts(claim_id, user_id)
        rows.append({"review": review, "facts": facts_cache[claim_id],
                     "evidence": evidence_by_review.get(review["id"], [])})

    tally = {"by_status": {}}
    for review in reviews:
        tally["by_status"][review["status"]] = tally["by_status"].get(
            review["status"], 0) + 1

    html_text, images, budget = _document_html(filename, title, rows, tally)
    wanted = sum(len(row["evidence"]) for row in rows)
    return filename, _mhtml(html_text, images), {
        "cases": len(rows),
        "images": budget["images"],
        "evidence": wanted,
        "images_omitted": wanted - budget["images"],
        "claims_missing": sorted({row["facts"]["claim_id"] for row in rows
                                  if not row["facts"]["available"]}),
    }
