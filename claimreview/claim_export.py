"""The claims-list CSV export.

One row per fully processed claim, carrying four things side by side:

- the claim's own columns, exactly as they came out of the warehouse;
- what is physically in the bundle (unique documents, total pages);
- every rule's verdict, as PASS / FAIL / NA;
- what OCR read out of the documents, and where it disagrees with the claim.

The point is the last pair. A rule verdict says a check failed; the OCR columns
next to the warehouse columns say *what* differs, which is what somebody
triaging a few hundred rows in a spreadsheet actually sorts and filters on.

Nothing here recomputes anything the application does not already compute for
the claim page - it reuses claim_summary and rules_engine, so a row in the CSV
and the page for that claim cannot disagree.
"""
import csv
import io

from . import (claim_extract, claim_scanner, claim_summary, csv_data,
               processing, rules_engine)

# The OCR fields, in the order they appear in the CSV. These are
# claim_summary.COMPARISON_FIELDS, named here so the column headings are a
# deliberate choice rather than whatever the UI happens to label them.
OCR_COLUMNS = [
    ("name", "patient_name"),
    ("hospital_name", "hospital_name"),
    ("age", "age"),
    ("admission_date", "admission_date"),
    ("discharge_date", "discharge_date"),
    ("length_of_stay_days", "length_of_stay_days"),
]
OCR_PREFIX = "OCR_"

# Only claims whose documents are all processed. A partially processed claim
# would export OCR columns that are blank because the work has not been done
# rather than because nothing was found, which is a different statement and not
# one a spreadsheet can tell apart.
EXPORTABLE_STATUSES = {"completed"}

CLAIM_ID_COLUMN = "registration_id"
BUNDLE_COLUMNS = ["unique_documents", "total_pages"]


def _rule_column(rule, used):
    """A stable, unique column name for a rule.

    Two rules can share a name - nothing stops it - and a CSV with two
    identical headings silently loses one of them.
    """
    base = "RULE_" + " ".join(str(rule["name"]).split())
    if base not in used:
        used.add(base)
        return base
    unique = f"{base} (#{rule['id']})"
    used.add(unique)
    return unique


def _mismatch(ocr_value, claim_value):
    """TRUE / FALSE / N/A for one OCR field against the claim's own value.

    N/A covers both directions of "there is nothing to compare": OCR did not
    find the field, or the claim has no value for it. TRUE means both sides
    spoke and disagreed, which is the only case worth a reviewer's attention.

    Comparison is claim_summary's, so "CITY HOSPITAL PVT LTD" and "City
    Hospital Pvt. Ltd." are not reported as a discrepancy while a one-digit
    difference in a date or an age is.
    """
    if ocr_value in (None, "") or claim_value in (None, ""):
        return "N/A"
    return "TRUE" if claim_summary._agree([ocr_value, claim_value]) == "differ" else "FALSE"


def _bundle_counts(docs):
    """(unique documents, total pages) for a claim.

    Unique by content hash: the same bytes filed under two names is one
    document, and counting it twice would overstate every bundle that carries
    a duplicate - which, in this data, most of them do.
    """
    seen, pages = set(), 0
    for doc in docs:
        file_hash = doc.get("file_hash")
        if file_hash in seen:
            continue
        seen.add(file_hash)
        cached = doc.get("cached_result") or {}
        pages += cached.get("num_pages") or len(cached.get("pages") or [])
    return len(seen), pages


def build_columns(user_id=None, rules=None):
    """(column names, rule-id -> column) for the export.

    Worked out before any claim is read, so the file can be streamed: the claim
    columns come from the loaded dataset's own column list and the rule columns
    from the rule set, neither of which varies per claim.
    """
    rules = rules_engine.list_rules(user_id) if rules is None else rules
    claim_columns = [c for c in (csv_data.get_available_fields(user_id) or [])
                     if c != CLAIM_ID_COLUMN]

    used, rule_columns = set(), {}
    for rule in rules:
        rule_columns[rule["id"]] = _rule_column(rule, used)

    columns = [CLAIM_ID_COLUMN, *claim_columns, *BUNDLE_COLUMNS]
    columns += [OCR_PREFIX + name for _key, name in OCR_COLUMNS]
    columns += [f"{OCR_PREFIX}{name}_mismatch" for _key, name in OCR_COLUMNS]
    columns += [rule_columns[rule["id"]] for rule in rules]
    return columns, rule_columns, claim_columns


def claim_row(claim, docs, settings, user_id, rules, rule_columns, claim_columns):
    """One claim's row, as {column: value}."""
    claim_id = claim["claim_id"]
    source_row = csv_data.get_claim_row(claim_id, user_id) or {}
    row = {CLAIM_ID_COLUMN: claim_id}
    # The warehouse's own columns, untouched - this is the raw claim, and a
    # reviewer cross-checking against the mart needs it to match exactly.
    for column in claim_columns:
        row[column] = source_row.get(column)

    unique_documents, total_pages = _bundle_counts(docs)
    row["unique_documents"] = unique_documents
    row["total_pages"] = total_pages

    # The bundle's own extracted values, when the claim was fetched through this
    # app - the same row the claim page compares against, so the CSV and the
    # page agree about what the bundle says.
    bundle_rows = claim_extract.get_dataset(claim_id, "claim_bundle_summary") or []
    summary = claim_summary.build_claim_summary(
        claim_id, docs, source_row, bundle_rows[0] if bundle_rows else None)
    by_key = {r["key"]: r for r in summary["rows"]}
    for key, name in OCR_COLUMNS:
        entry = by_key.get(key) or {}
        row[OCR_PREFIX + name] = entry.get("ocr")
        row[f"{OCR_PREFIX}{name}_mismatch"] = _mismatch(entry.get("ocr"), entry.get("csv"))

    results = rules_engine.evaluate_rules(claim_id, claim["path"], docs=docs,
                                          settings=settings, user_id=user_id, rules=rules)
    verdicts = {r["rule_id"]: r for r in results}
    for rule in rules:
        result = verdicts.get(rule["id"]) or {}
        status = result.get("status")
        # NA covers both "this rule does not apply to this claim" and "it could
        # not be evaluated" - from a spreadsheet's point of view neither is a
        # verdict, and the rule's own page explains which.
        row[rule_columns[rule["id"]]] = {"pass": "PASS", "fail": "FAIL"}.get(status, "NA")
    return row


def iter_rows(root, settings, user_id=None, latest_runs=None):
    """Yield (claim_id, row) for every fully processed claim under `root`.

    A generator so the response can stream: a folder of a few thousand claims
    means re-reading every cached document, and a reviewer should see the file
    start arriving rather than watch a blank tab decide whether it has hung.
    """
    from .db import get_latest_runs
    latest_runs = get_latest_runs() if latest_runs is None else latest_runs
    rules = rules_engine.list_rules(user_id)
    columns, rule_columns, claim_columns = build_columns(user_id, rules)

    for claim in claim_scanner.list_claims(root):
        claim_id = claim["claim_id"]
        status = processing.get_claim_status(claim_id, latest_runs.get(claim_id))
        if status["status"] not in EXPORTABLE_STATUSES:
            continue
        docs = claim_scanner.scan_claim_cached(claim["path"])
        yield claim_id, claim_row(claim, docs, settings, user_id, rules,
                                  rule_columns, claim_columns)


def stream_csv(root, settings, user_id=None):
    """The whole CSV, a chunk at a time, header first."""
    columns, _rule_columns, _claim_columns = build_columns(user_id)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")

    def take():
        value = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return value

    writer.writeheader()
    yield take()
    for _claim_id, row in iter_rows(root, settings, user_id):
        writer.writerow(row)
        yield take()
