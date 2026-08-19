"""Derives a per-claim summary panel: age/admission-date/discharge-date as
they appear in the uploaded CSV, cross-referenced with the same three
fields independently parsed straight from the claim's own OCR output -
shown on the claim detail page so a reviewer can eyeball CSV-vs-document
agreement (and the resulting length of stay) without digging through every
file.

Extraction priority, per how this was asked for: TABLES first, then form
KEY/VALUE pairs, then Textract QUERY answers - in that order, the first
tier that turns up *any* candidates wins (later tiers are never consulted
once an earlier one has something). Within the winning tier, every
document/page can independently mention the same field (often with OCR
noise), so the most frequently occurring *parsed* value is used - e.g.
three pages agreeing on 12-Mar-2026 outvotes one stray OCR misread -
ties broken by first occurrence.

Dates are matched by label (a table header or form key that reads like
"Date of Admission", "DOA", "Discharge Date", etc.) - never by scanning
bare values for anything date-shaped, that would be far too noisy. Age is
matched by label the same way, but *also* falls back (only if no label
match exists at all in a tier) to scanning that tier's raw values for a
bare "<number> yrs/years" pattern, since real claim data often has age
buried in a messy combined field (e.g. "52 Yrs/M") under a key that OCR
mangled beyond recognizing as "age" - the "yrs/years" unit is what keeps
that fallback from matching arbitrary numbers.
"""
import re
from collections import Counter

from dateutil import parser as date_parser

from . import search

AGE_LABEL_PATTERN = re.compile(r"\bage\b", re.IGNORECASE)
AGE_VALUE_PATTERN = re.compile(r"\b(\d{1,3})\s*(?:yrs?|years?|y)\b", re.IGNORECASE)
AGE_VALUE_BARE_PATTERN = re.compile(r"\b(\d{1,3})\s*(?:yrs?|years?)\b", re.IGNORECASE)

ADMISSION_LABEL_PATTERN = re.compile(
    r"\b(?:date\s*of\s*admission|admission\s*date|adm(?:it|ission)?\.?\s*d(?:ate|t)|doa)\b", re.IGNORECASE)
DISCHARGE_LABEL_PATTERN = re.compile(
    r"\b(?:date\s*of\s*discharge|discharge\s*date|dis(?:ch(?:g|arge)?)?\.?\s*d(?:ate|t))\b", re.IGNORECASE)

# CSV columns this reads from, matching the defaults rules_engine.py's
# length_of_stay rule type already uses for the same claims-data export.
CSV_FIELDS = {
    "name": "patient_name",
    "hospital_name": "hospital_name",
    "age": "age",
    "admission_date": "admission_dt",
    "discharge_date": "discharge_dt",
}


def _parse_age(text):
    if not text:
        return None
    match = AGE_VALUE_PATTERN.search(text)
    if not match:
        return None
    age = int(match.group(1))
    return age if 0 < age <= 120 else None


def _parse_bare_age(text):
    if not text:
        return None
    match = AGE_VALUE_BARE_PATTERN.search(text)
    if not match:
        return None
    age = int(match.group(1))
    return age if 0 < age <= 120 else None


# A leading YYYY-MM-DD is unambiguous and must not be read day-first.
ISO_DATE_PREFIX = re.compile(r"^\s*\d{4}-\d{2}-\d{2}")


def parse_date_text(text):
    """Parse a date from claim data, as a datetime, or None.

    Claim documents write dates day-first (28/06/2026), so that is the default.
    But the claims dataset can also arrive straight from the warehouse, where
    dates are rendered ISO (2026-07-01) - and dateutil applies dayfirst to
    those too, turning 1 July into 7 January. That silently produced wrong
    discharge dates, negative lengths of stay, and false rule violations on
    every fetched claim, so the format is detected before parsing rather than
    assumed.
    """
    if not text or not str(text).strip():
        return None
    text = str(text).strip()
    dayfirst = not ISO_DATE_PREFIX.match(text)
    try:
        return date_parser.parse(text, dayfirst=dayfirst, fuzzy=True)
    except (ValueError, OverflowError):
        return None


def _parse_date(text):
    parsed = parse_date_text(text)
    return parsed.date() if parsed else None


def _iter_pages(docs, page_filter=None):
    for doc in docs:
        cached = doc.get("cached_result")
        if not cached:
            continue
        for page in cached["pages"]:
            if page_filter is not None and not page_filter(page):
                continue
            yield doc, page


def _collect_table_candidates(docs, page_filter=None):
    """(age, admission, discharge) candidate lists, each [(value, file,
    page_number), ...], from table header/cell pairs. Age also gets a bare
    "<n> yrs" fallback scan over every cell if no header-labeled age cell
    was found anywhere."""
    age, admission, discharge, age_bare = [], [], [], []
    for doc, page in _iter_pages(docs, page_filter):
        for table in page.get("tables", []):
            if len(table) < 2:
                continue
            header = table[0]
            for row in table[1:]:
                for col_idx, cell in enumerate(row):
                    cell = (cell or "").strip()
                    if not cell:
                        continue
                    header_cell = header[col_idx].strip() if col_idx < len(header) else ""
                    loc = (doc["rel_path"], page["page_number"])
                    if header_cell and AGE_LABEL_PATTERN.search(header_cell):
                        parsed = _parse_age(cell)
                        if parsed is not None:
                            age.append((parsed, *loc))
                    if header_cell and ADMISSION_LABEL_PATTERN.search(header_cell):
                        parsed = _parse_date(cell)
                        if parsed is not None:
                            admission.append((parsed, *loc))
                    if header_cell and DISCHARGE_LABEL_PATTERN.search(header_cell):
                        parsed = _parse_date(cell)
                        if parsed is not None:
                            discharge.append((parsed, *loc))
                    bare = _parse_bare_age(cell)
                    if bare is not None:
                        age_bare.append((bare, *loc))
    if not age:
        age = age_bare
    return age, admission, discharge


def _collect_form_candidates(docs, page_filter=None):
    """Same shape as _collect_table_candidates, but from Textract FORMS
    key/value pairs."""
    age, admission, discharge, age_bare = [], [], [], []
    for doc, page in _iter_pages(docs, page_filter):
        for form in page.get("forms", []):
            key = (form.get("key") or "").strip()
            value = (form.get("value") or "").strip()
            if not value:
                continue
            loc = (doc["rel_path"], page["page_number"])
            if key and AGE_LABEL_PATTERN.search(key):
                parsed = _parse_age(value)
                if parsed is not None:
                    age.append((parsed, *loc))
            if key and ADMISSION_LABEL_PATTERN.search(key):
                parsed = _parse_date(value)
                if parsed is not None:
                    admission.append((parsed, *loc))
            if key and DISCHARGE_LABEL_PATTERN.search(key):
                parsed = _parse_date(value)
                if parsed is not None:
                    discharge.append((parsed, *loc))
            bare = _parse_bare_age(value)
            if bare is not None:
                age_bare.append((bare, *loc))
    if not age:
        age = age_bare
    return age, admission, discharge


def _collect_query_candidates(docs, page_filter=None):
    """Same shape again, from Textract QUERY answers (config.DEFAULT_QUERIES
    aliases "Age"/"Date of Admission"/"Date of Discharge" - claims processed
    before "Age" was added won't have that alias at all, same backfill
    limitation as every other Textract QUERY in this app)."""
    age, admission, discharge = [], [], []
    for doc, page in _iter_pages(docs, page_filter):
        queries = page.get("queries", {})
        loc = (doc["rel_path"], page["page_number"])
        age_ans = (queries.get("Age") or {}).get("answer", "").strip()
        if age_ans:
            parsed = _parse_age(age_ans) or _parse_bare_age(age_ans)
            if parsed is None and age_ans.isdigit():
                parsed = int(age_ans) if 0 < int(age_ans) <= 120 else None
            if parsed is not None:
                age.append((parsed, *loc))
        adm_ans = (queries.get("Date of Admission") or {}).get("answer", "").strip()
        if adm_ans:
            parsed = _parse_date(adm_ans)
            if parsed is not None:
                admission.append((parsed, *loc))
        dis_ans = (queries.get("Date of Discharge") or {}).get("answer", "").strip()
        if dis_ans:
            parsed = _parse_date(dis_ans)
            if parsed is not None:
                discharge.append((parsed, *loc))
    return age, admission, discharge


def _resolve(tiers):
    """tiers: [(tier_name, [(value, file, page_number), ...]), ...] in
    priority order. The first tier with any candidates wins; within it, the
    most frequently occurring value wins (ties broken by first occurrence
    order). Returns {value, source, file, page_number} or None."""
    for tier_name, candidates in tiers:
        if not candidates:
            continue
        counts = Counter(c[0] for c in candidates)
        best_value = counts.most_common(1)[0][0]
        for value, file_name, page_number in candidates:
            if value == best_value:
                return {"value": best_value, "source": tier_name, "file": file_name, "page_number": page_number}
    return None


def extract_ocr_fields(docs, page_filter=None):
    """{age, admission_date, discharge_date, length_of_stay_days} parsed
    fresh from `docs`' cached OCR output (an already-scanned
    claim_scanner.scan_claim_cached() list) - tables > forms > queries,
    each field resolved independently. length_of_stay_days is None unless
    both dates resolved."""
    table_age, table_adm, table_dis = _collect_table_candidates(docs, page_filter)
    form_age, form_adm, form_dis = _collect_form_candidates(docs, page_filter)
    query_age, query_adm, query_dis = _collect_query_candidates(docs, page_filter)

    age = _resolve([("table", table_age), ("form", form_age), ("query", query_age)])
    admission = _resolve([("table", table_adm), ("form", form_adm), ("query", query_adm)])
    discharge = _resolve([("table", table_dis), ("form", form_dis), ("query", query_dis)])

    los_days = None
    if admission and discharge:
        los_days = (discharge["value"] - admission["value"]).days

    return {"age": age, "admission_date": admission, "discharge_date": discharge,
            "length_of_stay_days": los_days}


def _age_from_dob(dob_text, as_of):
    """Age in whole years at `as_of` (admission, when known - an age is only
    meaningful against a date). The bundle carries a date of birth rather than
    an age, so this is what makes it comparable with the other two sources."""
    dob = _parse_date(dob_text) if dob_text else None
    if not dob or not as_of:
        return None
    years = as_of.year - dob.year - ((as_of.month, as_of.day) < (dob.month, dob.day))
    return years if 0 < years <= 120 else None


def _bundle_fields(bundle_row):
    """The claim's own declared values, from the extracted bundle summary.

    This is the authoritative source of the three: it is what the payer and
    provider systems actually recorded, parsed out of the claim JSON rather
    than read off a scan or re-exported into a spreadsheet. Where OCR
    disagrees with it, the document is the thing to look at.
    """
    if not bundle_row:
        return None

    def text(key):
        value = bundle_row.get(key)
        value = value.strip() if isinstance(value, str) else value
        return value or None

    admission = _parse_date(text("admission_date")) if text("admission_date") else None
    discharge = _parse_date(text("discharge_date")) if text("discharge_date") else None
    return {
        "name": text("patient_name"),
        "hospital_name": text("provider_name"),
        "age": _age_from_dob(text("patient_dob"), admission),
        "patient_dob": text("patient_dob"),
        "admission_date": admission.isoformat() if admission else text("admission_date"),
        "discharge_date": discharge.isoformat() if discharge else text("discharge_date"),
        "length_of_stay_days": (discharge - admission).days if admission and discharge else None,
        "diagnosis": text("diagnosis_name"),
        "claim_status": text("claim_status"),
    }


def _agree(values):
    """Compare the non-empty values of one field across sources.

    Returns 'agree' / 'differ' / None (fewer than two sources have a value).
    Text is compared with the same tolerant matcher the search and rules use,
    so "CITY HOSPITAL PVT LTD" and "City Hospital Pvt. Ltd." are not reported
    as a discrepancy; numbers and dates are compared exactly, since a
    one-digit difference there is precisely what a reviewer wants to see.
    """
    present = [v for v in values if v not in (None, "")]
    if len(present) < 2:
        return None
    first = present[0]
    for other in present[1:]:
        if isinstance(first, str) and isinstance(other, str) and not _looks_numeric(first, other):
            matched, _method, _score = search.values_match(first, other)
            if not matched:
                return "differ"
        elif str(first).strip() != str(other).strip():
            return "differ"
    return "agree"


def _looks_numeric(*values):
    return all(str(v).strip().replace("-", "").replace(".", "").isdigit() for v in values)


# How many occurrences of a looked-up name are carried to the UI popup. A
# common hospital name can appear on every page of every document; the count
# shown is the true total, this only caps the list.
MAX_OCCURRENCES = 60


def lookup_in_documents(docs, query, fuzzy_threshold=None, soundex_threshold=None):
    """Find a value we already know (from the claims data) in the OCR output.

    The other fields are read *out* of the documents by label - "Date of
    Admission:" and friends. Names do not work that way: the label is written a
    dozen different ways, OCR mangles it, and on many claim documents the
    patient's name has no label at all. So instead of asking "what does this
    document say the patient name is?", this asks the answerable question:
    "does the name from the claims data appear in these documents?"

    Matching is restricted to values, never field labels - a name is content,
    and scoring it against labels only manufactures hits. The layered
    exact -> fuzzy -> soundex chain is what absorbs OCR noise, and because a
    stage only runs when the stricter one found nothing, a document with a
    clean exact hit never has fuzzy near-misses mixed into its results.

    Returns None when nothing matched, else {value, method, score, count,
    occurrences: [{file, page, key, value, method, score}]}.
    """
    query = (query or "").strip()
    if not query or not docs:
        return None

    documents, locations = search.documents_from_docs(docs)
    if not documents:
        return None

    result = search.search_kvs(
        documents, query, locations=locations,
        fuzzy_threshold=fuzzy_threshold, soundex_threshold=soundex_threshold,
        matched_on={"value"},
    )
    best = result["best"]
    if best is None:
        return None

    occurrences = [{
        "file": hit["file"],
        "page": hit.get("page_number"),
        "key": hit["key"],
        "value": hit["value"],
        "method": hit["method"],
        "score": round(hit["score"], 1),
    } for hit in result["matches"][:MAX_OCCURRENCES]]

    return {
        "value": query,
        "method": best["method"],
        "score": round(best["score"], 1),
        "count": len(result["matches"]),
        "truncated": len(result["matches"]) > len(occurrences),
        "occurrences": occurrences,
    }


COMPARISON_FIELDS = [
    ("name", "Patient name"),
    ("hospital_name", "Hospital"),
    ("age", "Age"),
    ("admission_date", "Admission date"),
    ("discharge_date", "Discharge date"),
    ("length_of_stay_days", "Length of stay (days)"),
]


def build_claim_summary(claim_id, docs, csv_row, bundle_row=None):
    """The full comparison panel for one claim, across up to three sources:
    the claim bundle's own extracted values, the claim's OCR output, and the
    claims dataset. `csv_row` is csv_data.get_claim_row(claim_id); `bundle_row`
    the mirrored claim_bundle_summary row (claim_extract.py), None when the
    claim was not fetched through this app; `docs` an already-scanned
    claim_scanner.scan_claim_cached() list."""
    csv_row = csv_row or {}

    def _csv_field(key):
        value = csv_row.get(CSV_FIELDS[key])
        value = value.strip() if isinstance(value, str) else value
        return value or None

    csv_admission = _parse_date(_csv_field("admission_date")) if _csv_field("admission_date") else None
    csv_discharge = _parse_date(_csv_field("discharge_date")) if _csv_field("discharge_date") else None
    csv_los_days = (csv_discharge - csv_admission).days if csv_admission and csv_discharge else None

    ocr = extract_ocr_fields(docs)
    bundle = _bundle_fields(bundle_row)

    csv_values_seed = {"name": _csv_field("name"), "hospital_name": _csv_field("hospital_name")}
    csv_values = {
        "name": _csv_field("name"),
        "hospital_name": _csv_field("hospital_name"),
        "age": _csv_field("age"),
        "admission_date": csv_admission.isoformat() if csv_admission else _csv_field("admission_date"),
        "discharge_date": csv_discharge.isoformat() if csv_discharge else _csv_field("discharge_date"),
        "length_of_stay_days": csv_los_days,
    }
    # Names are found by looking the known value *up* in the documents rather
    # than by reading a labelled field, because on real claim documents the
    # name rarely sits behind a recognizable label. The claims data is the
    # reference; the bundle's own value is the fallback when no claims row
    # exists, so the lookup still works for an unmatched claim.
    lookups = {}
    for key in ("name", "hospital_name"):
        term = csv_values_seed.get(key) or (bundle.get(key) if bundle else None)
        found = lookup_in_documents(docs, term)
        if found:
            lookups[key] = found

    ocr_values = {
        # OCR currently only parses these three fields out of the documents by
        # label; name/hospital come from the reverse lookup above. A field no
        # source can speak to is shown blank rather than omitted, so the table
        # has one row per field either way.
        "name": lookups["name"]["value"] if "name" in lookups else None,
        "hospital_name": lookups["hospital_name"]["value"] if "hospital_name" in lookups else None,
        "age": ocr["age"]["value"] if ocr["age"] else None,
        "admission_date": ocr["admission_date"]["value"].isoformat() if ocr["admission_date"] else None,
        "discharge_date": ocr["discharge_date"]["value"].isoformat() if ocr["discharge_date"] else None,
        "length_of_stay_days": ocr["length_of_stay_days"],
    }
    ocr_sources = {
        "age": ocr["age"]["source"] if ocr["age"] else None,
        "admission_date": ocr["admission_date"]["source"] if ocr["admission_date"] else None,
        "discharge_date": ocr["discharge_date"]["source"] if ocr["discharge_date"] else None,
    }

    rows = []
    for key, label in COMPARISON_FIELDS:
        bundle_value = bundle.get(key) if bundle else None
        lookup = lookups.get(key)
        rows.append({
            "key": key,
            "label": label,
            "bundle": bundle_value,
            "ocr": ocr_values.get(key),
            "ocr_source": ocr_sources.get(key),
            # Set only for the looked-up fields: how the value was found in
            # the documents, and every place it turned up. The UI links the
            # value to this list so the reviewer can judge the hits for
            # themselves rather than trusting a bare tick.
            "ocr_lookup": None if not lookup else {
                "method": lookup["method"], "score": lookup["score"],
                "count": lookup["count"], "truncated": lookup["truncated"],
            },
            "ocr_occurrences": lookup["occurrences"] if lookup else [],
            "csv": csv_values.get(key),
            "agreement": _agree([bundle_value, ocr_values.get(key), csv_values.get(key)]),
        })

    return {
        "rows": rows,
        "has_bundle": bundle is not None,
        "bundle": bundle,
        "csv": {
            "name": _csv_field("name"),
            "hospital_name": _csv_field("hospital_name"),
            "age": _csv_field("age"),
            "admission_date": csv_admission.isoformat() if csv_admission else _csv_field("admission_date"),
            "discharge_date": csv_discharge.isoformat() if csv_discharge else _csv_field("discharge_date"),
            "length_of_stay_days": csv_los_days,
        },
        "ocr": {
            "age": ocr["age"]["value"] if ocr["age"] else None,
            "age_source": ocr["age"]["source"] if ocr["age"] else None,
            "admission_date": ocr["admission_date"]["value"].isoformat() if ocr["admission_date"] else None,
            "admission_date_source": ocr["admission_date"]["source"] if ocr["admission_date"] else None,
            "discharge_date": ocr["discharge_date"]["value"].isoformat() if ocr["discharge_date"] else None,
            "discharge_date_source": ocr["discharge_date"]["source"] if ocr["discharge_date"] else None,
            "length_of_stay_days": ocr["length_of_stay_days"],
        },
    }
