"""Configurable validation rules that cross-check a claim's documents
against the uploaded claims-management CSV (csv_data.py). Every rule
evaluates fresh against whatever's currently processed/uploaded - no
stored results, no reprocessing needed when a rule or the CSV changes.

Each evaluator returns {status: 'pass'|'fail'|'no_data', message, ...details}.
'no_data' means the rule doesn't (yet) apply to this claim - no CSV row,
no processed documents, an unparseable date - and is never counted as a
violation, only 'fail' is.
"""
import json
import re

from dateutil import parser as date_parser

import os

from flask import current_app

from . import (claim_extract, claim_scanner, claim_summary, classify, csv_data,
               search, settings_store)
from .db import get_db

RULE_TYPES = ("field_present", "field_consistency", "length_of_stay", "documents_present")
# "document"/"photo"/"gps photo" are mutually exclusive (a page's single
# content_type); "has face" is an independent, co-occurring tag (a page can
# be e.g. both "photo" and "has face") - see _page_scope_tags below.
DOC_SCOPE_TAGS = ("document", "photo", "gps photo", "has face")
DEFAULT_DOC_SCOPE_TAGS = ["document", "photo"]


# --------------------------------------------------------------- CRUD

def _normalize_config(rule_type, config):
    """Backfills config keys added after some rules already existed, at
    read time (rather than migrating stored rows), so older rules keep
    behaving exactly as they did before the key existed."""
    if rule_type == "field_present" and "compare_with_csv" not in config:
        # Rules created before the keyword/regex mode was added always
        # searched a CSV field's value - without this they'd silently
        # become empty-keyword rules.
        config["compare_with_csv"] = bool((config.get("csv_field") or "").strip())
    if "doc_scope_tags" not in config:
        # Rules created before document-scope existed effectively considered
        # every page - document+photo (not gps photo) is the closest match
        # to that, and is also the requested default for new rules.
        config["doc_scope_tags"] = list(DEFAULT_DOC_SCOPE_TAGS)
    if "page_scope" not in config:
        # Rules created before "every page" vs "any page" existed only ever
        # checked "found somewhere in the document" - that's any_page.
        config["page_scope"] = "any_page"
    if rule_type == "documents_present":
        config.setdefault("check_declared", True)
        config.setdefault("required_types", [])
        config.setdefault("check_investigation_attachments", True)
        # Off by default: plenty of investigations legitimately have no
        # attachment, so this one is opt-in rather than noise on every claim.
        config.setdefault("flag_investigations_without_attachment", False)
    return config


def list_rules():
    db = get_db()
    rows = db.execute("SELECT * FROM rules ORDER BY id").fetchall()
    return [dict(r, config=_normalize_config(r["rule_type"], json.loads(r["config_json"]))) for r in rows]


def get_rule(rule_id):
    db = get_db()
    row = db.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
    return dict(row, config=_normalize_config(row["rule_type"], json.loads(row["config_json"]))) if row else None


def create_rule(name, rule_type, config):
    if rule_type not in RULE_TYPES:
        raise ValueError(f"Unknown rule_type: {rule_type}")
    db = get_db()
    db.execute("INSERT INTO rules (name, rule_type, config_json, enabled) VALUES (?, ?, ?, 1)",
               (name, rule_type, json.dumps(config)))
    db.commit()


def update_rule(rule_id, name, rule_type, config):
    if rule_type not in RULE_TYPES:
        raise ValueError(f"Unknown rule_type: {rule_type}")
    db = get_db()
    db.execute("UPDATE rules SET name=?, rule_type=?, config_json=? WHERE id=?",
               (name, rule_type, json.dumps(config), rule_id))
    db.commit()


def delete_rule(rule_id):
    db = get_db()
    db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    db.commit()


def set_rule_enabled(rule_id, enabled):
    db = get_db()
    db.execute("UPDATE rules SET enabled=? WHERE id=?", (1 if enabled else 0, rule_id))
    db.commit()


# --------------------------------------------------------------- helpers

def _procedure_codes_match(claim_id, config):
    """Whether `rule` (via its config's procedure_codes list) applies to this
    claim. ["All"] (the default, case-insensitive) always matches; otherwise
    the claim's CSV 'procedure_code' column must be one of the configured
    codes. Returns (applies: bool, reason_if_not: str|None)."""
    codes = config.get("procedure_codes") or ["All"]
    if any(str(c).strip().lower() == "all" for c in codes):
        return True, None

    row = csv_data.get_claim_row(claim_id)
    claim_code = (row or {}).get("procedure_code")
    claim_code = claim_code.strip() if isinstance(claim_code, str) else (str(claim_code).strip() if claim_code else "")
    if not claim_code:
        return False, "no 'procedure_code' value in the CSV for this claim"

    normalized = {str(c).strip().lower() for c in codes}
    if claim_code.lower() in normalized:
        return True, None
    return False, f"claim's procedure code '{claim_code}' isn't in this rule's configured list"


def _parse_date(value):
    """Shared with the claim-summary panel so a date cannot be read one way in
    a rule and another way in the table beside it - see
    claim_summary.parse_date_text for why the format has to be detected."""
    return claim_summary.parse_date_text(value)


def _page_scope_tags(page, settings):
    """A page's full tag set for doc_scope_tags matching: its content_type
    ('document'/'photo'/'gps photo' - mutually exclusive) plus 'has face'
    if applicable (independent - a photo can also have a face). Returns
    (tags, unclassified) - unclassified means the page predates quality
    metrics entirely and can't be excluded with confidence either way."""
    content_type = classify.classify_page_quality(page, settings).get("content_type")
    tags = {content_type} if content_type else set()
    if classify.classify_faces(page, settings).get("has_face"):
        tags.add("has face")
    return tags, content_type is None


def _scope_page_filter(config, settings, content_type_cache):
    """Builds a page_filter(page) -> bool for documents_from_docs()/
    _collect_query_values() from a rule's doc_scope_tags config, so a rule
    scoped to (say) just 'document'+'photo' pages doesn't see text
    extracted from gps-photo or face pages. A page is included only if
    *every* tag it carries (see _page_scope_tags) is in the configured set
    - so a page that's otherwise a plain 'photo' but also has a detected
    face is excluded unless 'has face' is explicitly added to the scope,
    same as a 'gps photo' page is excluded unless that's added. Returns
    None (no filtering) when every tag is selected, since that's equivalent
    to not filtering and is the common case.

    `content_type_cache` is a plain dict the caller owns for the lifetime
    of one evaluate_rules() call, keyed by id(page) - classify.py's
    classification (which itself does a small keyword search per photo
    page for the gps-photo check, and a face-detection-candidate filter)
    would otherwise be recomputed once per rule that has scope filtering
    active, for every page in the claim.
    """
    tags = set(config.get("doc_scope_tags") or DEFAULT_DOC_SCOPE_TAGS)
    if tags >= set(DOC_SCOPE_TAGS):
        return None

    def _match(page):
        key = id(page)
        if key not in content_type_cache:
            content_type_cache[key] = _page_scope_tags(page, settings)
        page_tags, unclassified = content_type_cache[key]
        return unclassified or page_tags <= tags

    return _match


def _collect_query_values(docs, alias, page_filter=None):
    """Every non-empty Textract QUERY answer for `alias` (a config.DEFAULT_QUERIES
    alias, e.g. "Gender") across every processed document/page in the claim
    that also passes `page_filter` (see _scope_page_filter), if given.
    `docs` is an already-scanned claim_scanner.scan_claim_cached() list -
    no rescanning/rehashing here."""
    values = []
    for doc in docs:
        cached = doc.get("cached_result")
        if not cached:
            continue
        for page in cached["pages"]:
            if page_filter is not None and not page_filter(page):
                continue
            ans = page.get("queries", {}).get(alias)
            if ans and ans.get("answer", "").strip():
                values.append({"file": doc["rel_path"], "page_number": page["page_number"],
                                "answer": ans["answer"].strip(), "confidence": ans["confidence"]})
    return values


def _in_scope_pages_by_doc(docs, page_filter):
    """{rel_path: {page_number, ...}} of every processed document's in-scope
    pages (those passing `page_filter`, or every page if page_filter is
    None) - a document with zero in-scope pages is omitted entirely."""
    result = {}
    for doc in docs:
        cached = doc.get("cached_result")
        if not cached:
            continue
        pages = {p["page_number"] for p in cached["pages"] if page_filter is None or page_filter(p)}
        if pages:
            result[doc["rel_path"]] = pages
    return result


def _doc_page_hits(doc, page_filter, run_search):
    """[(page_number, matched: bool, hit_or_None), ...] for one already-
    scanned doc dict's in-scope pages, using `run_search` (a closure built
    by the caller, e.g. keyword or CSV-value matching) against that page's
    own extracted text alone - never text from a different page."""
    cached = doc.get("cached_result")
    if not cached:
        return []
    results = []
    for page in cached["pages"]:
        if page_filter is not None and not page_filter(page):
            continue
        kvs = search.page_kv_dict(page)
        result = run_search({doc["rel_path"]: kvs}) if kvs else {"best": None}
        results.append((page["page_number"], result["best"] is not None, result["best"]))
    return results


def _doc_hit(page_hits, page_scope):
    """Whether a document counts as a match, given its per-page hit list
    and page_scope ('any_page': at least one in-scope page must match;
    'all_pages': every in-scope page must match). None (not True/False) if
    the document has no in-scope pages at all - it's excluded, not failing."""
    if not page_hits:
        return None
    matched_flags = [matched for _, matched, _ in page_hits]
    return all(matched_flags) if page_scope == "all_pages" else any(matched_flags)


# --------------------------------------------------------------- evaluators

def _evaluate_field_present(claim_id, docs, config, settings, ctx):
    content_type_cache = ctx["content_types"]
    """Two modes, picked by the "compare_with_csv" checkbox:
      - CSV field (compare_with_csv=True): dynamic per-claim lookup - the
        value to search for is read from `csv_field` on this claim's CSV
        row, then fuzzy/soundex-matched (search_kvs), same as before this
        rule type supported anything else.
      - Keyword (default): a fixed `keyword` typed by whoever built the
        rule, matched literally or as a regex (`regex`), with optional
        `case_insensitive` - no fuzzy fallback, since regex/case control
        implies the operator wants exact matching, not typo tolerance.
    `scope` (any_doc/all_docs) x `page_scope` (any_page/all_pages) together
    control how "found" is defined: any_page only requires the value
    somewhere in a document (across however many pages it has - a multi-
    page PDF's pages are pooled together like before this option existed);
    all_pages requires it on literally every one of that document's
    in-scope pages. doc_scope_tags (document/photo/gps photo) restricts
    which pages are even considered in the first place.
    """
    scope = config.get("scope", "any_doc")
    page_scope = config.get("page_scope", "any_page")
    page_filter = _scope_page_filter(config, settings, content_type_cache)

    if config.get("compare_with_csv"):
        csv_field = (config.get("csv_field") or "").strip()
        if not csv_field:
            return {"status": "no_data", "message": "No CSV field configured"}
        row = csv_data.get_claim_row(claim_id)
        value = (row or {}).get(csv_field, "")
        value = value.strip() if isinstance(value, str) else str(value or "").strip()
        if not row or not value:
            return {"status": "no_data", "message": f"No CSV value for '{csv_field}' on this claim"}
        label = f"'{value}'"

        def run_search(subset):
            return search.search_kvs(subset, value)
    else:
        keyword = (config.get("keyword") or "").strip()
        if not keyword:
            return {"status": "no_data", "message": "No keyword configured"}
        regex = bool(config.get("regex"))
        case_insensitive = config.get("case_insensitive", True)
        label = f"pattern '{keyword}'" if regex else f"'{keyword}'"

        def run_search(subset):
            try:
                return search.search_keyword(subset, keyword, regex=regex, case_insensitive=case_insensitive)
            except search.InvalidKeywordPattern as exc:
                raise ValueError(f"Invalid regex pattern: {exc}") from exc

    doc_page_hits = {}
    any_cached = False
    for doc in docs:
        if not doc.get("cached_result"):
            continue
        any_cached = True
        hits = _doc_page_hits(doc, page_filter, run_search)
        if hits:
            doc_page_hits[doc["rel_path"]] = hits

    if not doc_page_hits:
        message = "Claim hasn't been processed yet" if not any_cached else "No in-scope pages found for this claim"
        return {"status": "no_data", "message": message}

    doc_hit = {name: _doc_hit(hits, page_scope) for name, hits in doc_page_hits.items()}
    page_scope_label = "every page" if page_scope == "all_pages" else "any page"

    if scope == "all_docs":
        missing = sorted(name for name, hit in doc_hit.items() if not hit)
        passed = not missing
        issues = [] if passed else [{
            "summary": f"Missing from {len(missing)} of {len(doc_hit)} document(s) ({page_scope_label} required)",
            "items": missing,
        }]
        return {
            "status": "pass" if passed else "fail",
            "message": (f"{label} found ({page_scope_label}) in all {len(doc_hit)} document(s)" if passed
                        else f"{label} missing from {len(missing)} of {len(doc_hit)} document(s)"),
            "issues": issues,
            "missing_documents": missing,
        }

    matched_doc = next((name for name, hit in doc_hit.items() if hit), None)
    if matched_doc:
        first_hit = next(hit for _, matched, hit in doc_page_hits[matched_doc] if matched)
        return {"status": "pass",
                "message": f"{label} found ({page_scope_label}, {first_hit['method']} match in {matched_doc})",
                "issues": [], "match": first_hit}
    return {
        "status": "fail",
        "message": f"{label} not found ({page_scope_label}) in any document",
        "issues": [{"summary": f"{label} not found ({page_scope_label}) in any document", "items": []}],
        "match": None,
    }


def _cluster_values(occurrences):
    """Group occurrences whose answers fuzzy/soundex-match each other (via
    search.values_match) rather than requiring exact string equality, so
    OCR noise between documents (e.g. 'Abha sha' vs 'AAbh Shaah') doesn't
    read as an inconsistency. Returns a list of {"occs": [occ, ...]}."""
    clusters = []
    for occ in occurrences:
        for cluster in clusters:
            matched, _method, _score = search.values_match(occ["answer"], cluster["occs"][0]["answer"])
            if matched:
                cluster["occs"].append(occ)
                break
        else:
            clusters.append({"occs": [occ]})
    return clusters


def _evaluate_field_consistency(claim_id, docs, config, settings, ctx):
    content_type_cache = ctx["content_types"]
    concept = (config.get("concept") or "").strip()
    if not concept:
        return {"status": "no_data", "message": "No concept configured"}

    page_filter = _scope_page_filter(config, settings, content_type_cache)
    occurrences = _collect_query_values(docs, concept, page_filter=page_filter)
    if config.get("skip_empty"):
        # Drop occurrences with no real word content (blank/garbage OCR
        # noise) so they're neither counted as a value nor compared to CSV.
        occurrences = [occ for occ in occurrences if re.search(r"\w", occ["answer"])]
    if not occurrences:
        return {"status": "no_data", "message": f"'{concept}' wasn't extracted from any document"}

    csv_field = config.get("csv_field")
    csv_value = None
    if config.get("compare_with_csv") and csv_field:
        row = csv_data.get_claim_row(claim_id)
        raw_csv_value = (row or {}).get(csv_field)
        raw_csv_value = raw_csv_value.strip() if isinstance(raw_csv_value, str) else (str(raw_csv_value).strip() if raw_csv_value else "")
        csv_value = raw_csv_value or None

    issues = []
    if csv_value:
        # A CSV reference value is available - consistency is judged by
        # whether every extracted occurrence matches it (fuzzy/soundex
        # tolerant), not by whether documents match each other.
        mismatches = []
        for occ in occurrences:
            matched, _method, _score = search.values_match(occ["answer"], csv_value)
            if not matched:
                mismatches.append(occ)
        distinct_values = sorted({occ["answer"] for occ in occurrences})
        if mismatches:
            issues.append({
                "summary": f"Doesn't match CSV {csv_field}='{csv_value}'",
                "items": [f"'{occ['answer']}' — {occ['file']}" for occ in mismatches],
            })
    else:
        clusters = _cluster_values(occurrences)
        distinct_values = [c["occs"][0]["answer"] for c in clusters]
        if len(clusters) > 1:
            issues.append({
                "summary": "Varies across documents",
                "items": [f"'{c['occs'][0]['answer']}' — {c['occs'][0]['file']}" for c in clusters],
            })

    if config.get("scope", "any_doc") == "all_docs":
        # Same any_page/all_pages x any_doc/all_docs scope field_present
        # uses, reused here: require the concept to have been extracted
        # from every in-scope document (any_page: on at least one of its
        # pages; all_pages: on every one of its pages) - not just checking
        # consistency among whatever was found.
        page_scope = config.get("page_scope", "any_page")
        considered_pages = _in_scope_pages_by_doc(docs, page_filter)
        occurred_pages = {}
        for occ in occurrences:
            occurred_pages.setdefault(occ["file"], set()).add(occ["page_number"])
        missing = sorted(
            file_name for file_name, pages in considered_pages.items()
            if not (pages <= occurred_pages.get(file_name, set()) if page_scope == "all_pages"
                    else occurred_pages.get(file_name))
        )
        if missing:
            page_scope_label = "every page" if page_scope == "all_pages" else "at least one page"
            issues.append({
                "summary": f"Missing from {len(missing)} of {len(considered_pages)} document(s) "
                           f"({page_scope_label} required)",
                "items": missing,
            })

    return {
        "status": "pass" if not issues else "fail",
        "message": ("; ".join(iss["summary"] for iss in issues) if issues
                    else f"{concept} is consistent across {len(occurrences)} occurrence(s)"),
        "issues": issues,
        "distinct_values": distinct_values,
        "occurrences": occurrences, "csv_value": csv_value,
    }


def _evaluate_length_of_stay(claim_id, docs, config, settings, ctx):
    content_type_cache = ctx["content_types"]
    row = csv_data.get_claim_row(claim_id)
    if not row:
        return {"status": "no_data", "message": "No CSV row for this claim"}

    adm_field = config.get("csv_admission_field") or "admission_dt"
    dis_field = config.get("csv_discharge_field") or "discharge_dt"
    adm = _parse_date(row.get(adm_field))
    dis = _parse_date(row.get(dis_field))
    if not adm or not dis:
        return {"status": "no_data", "message": f"CSV '{adm_field}'/'{dis_field}' missing or unparseable"}

    los_days = (dis.date() - adm.date()).days
    issues = []
    if los_days < 0:
        issues.append({"summary": f"Discharge ({dis.date()}) is before admission ({adm.date()})", "items": []})
    max_days = config.get("max_los_days")
    if max_days and los_days > max_days:
        issues.append({"summary": f"Length of stay ({los_days}d) exceeds configured max ({max_days}d)", "items": []})

    extracted = {}
    if config.get("compare_with_extracted", True):
        page_filter = _scope_page_filter(config, settings, content_type_cache)
        for alias, csv_dt, label in (("Date of Admission", adm, "admission"),
                                      ("Date of Discharge", dis, "discharge")):
            occs = _collect_query_values(docs, alias, page_filter=page_filter)
            if not occs:
                extracted[alias] = {"status": "no_data"}
                continue
            match = None
            for occ in occs:
                d = _parse_date(occ["answer"])
                if d:
                    match = (d, occ)
                    if d.date() == csv_dt.date():
                        break
            if not match:
                extracted[alias] = {"status": "no_data", "message": "extracted date unparseable"}
                continue
            matches = match[0].date() == csv_dt.date()
            extracted[alias] = {"status": "pass" if matches else "fail",
                                 "csv_date": csv_dt.date().isoformat(),
                                 "extracted_date": match[0].date().isoformat(),
                                 "source": match[1]["file"]}
            if not matches:
                issues.append({
                    "summary": f"Extracted {label} date differs from CSV ({csv_dt.date()})",
                    "items": [f"{match[0].date()} — {match[1]['file']}"],
                })

    return {
        "status": "pass" if not issues else "fail",
        "message": ("; ".join(iss["summary"] for iss in issues) if issues
                    else f"Length of stay: {los_days} day(s), dates check out"),
        "issues": issues,
        "los_days": los_days, "admission_date": adm.date().isoformat(),
        "discharge_date": dis.date().isoformat(), "extracted": extracted,
    }


def _normalize_label(value):
    return " ".join(str(value or "").strip().lower().split())


def _declared_name_index(rows, key="file_name"):
    """{lowercased file name: [row, ...]} for rows that name a file.

    One extracted row can name several files: documents are collapsed by
    content hash, so a file declared twice under different names arrives as
    "download6.jpg | download6S.jpg". Each name is checked on its own -
    treating the joined string as one filename would report every merged row
    as missing.
    """
    index = {}
    for row in rows:
        for name in str(row.get(key) or "").split("|"):
            name = name.strip()
            if name:
                entry = index.setdefault(name.lower(), {"display": name, "rows": []})
                entry["rows"].append(row)
    return index


def _evaluate_documents_present(claim_id, docs, config, settings, ctx):
    """Compare what the claim bundle *says* it contains against what is
    actually on disk.

    This is the one check that does not depend on OCR: the payer/provider JSON
    declares its documents (claim_bundle_documents) and its investigation
    attachments, and the review folder either has those files or it does not.
    A referenced-but-absent document is invisible to every text-based rule,
    because there is no page to read.

    Needs the local extraction mirror (claim_extract.py), so it only applies to
    claims fetched through this app - otherwise there is nothing declaring what
    *should* be there, and the rule reports no_data rather than guessing.
    """
    data = claim_extract.get(claim_id)
    if not data:
        return {"status": "no_data",
                "message": "No extracted bundle metadata for this claim - it was not fetched through this app"}

    declared = data.get("claim_bundle_documents") or []
    investigations = data.get("claim_bundle_investigations") or []
    on_disk = ctx["file_names"]
    supported = {e.lower() for e in current_app.config["SUPPORTED_EXTENSIONS"]}

    def check_names(rows, name_key):
        """(missing, unverifiable, checked) for rows naming a file. Files whose
        extension the review side does not scan (e.g. .png) are reported as
        unverifiable rather than missing - absent from `docs` only means the
        scanner ignores that type, not that the file is gone."""
        missing, unverifiable, checked = [], [], 0
        for name, entry in _declared_name_index(rows, name_key).items():
            extension = os.path.splitext(name)[1].lower()
            first = entry["rows"][0]
            label = (first.get("document_type")
                     or first.get("investigation_name") or "").strip()
            # Report the individual file name, not the row's whole
            # pipe-joined set - the reviewer needs to know which file is gone.
            display = entry["display"] + (f" — {label}" if label else "")
            if extension not in supported:
                unverifiable.append(display)
                continue
            checked += 1
            if name not in on_disk:
                missing.append(display)
        return missing, unverifiable, checked

    issues = []
    notes = []
    declared_checked = 0

    if config.get("check_declared", True):
        missing, unverifiable, declared_checked = check_names(declared, "file_name")
        if missing:
            issues.append({
                "summary": f"{len(missing)} declared document(s) are not in the claim folder",
                "items": sorted(missing),
            })
        if unverifiable:
            notes.append(f"{len(unverifiable)} declared file(s) of a type this app does not scan")

    required = [t for t in (config.get("required_types") or []) if str(t).strip()]
    if required:
        declared_labels = [
            _normalize_label(row.get("document_type")) + " " + _normalize_label(row.get("document_desc"))
            for row in declared
        ]
        absent = [
            wanted for wanted in required
            if not any(_normalize_label(wanted) in label for label in declared_labels)
        ]
        if absent:
            issues.append({
                "summary": f"{len(absent)} required document type(s) are not declared on this claim",
                "items": sorted(absent),
            })

    if config.get("check_investigation_attachments", True):
        with_attachment = [row for row in investigations if str(row.get("attachment_name") or "").strip()]
        missing, _unverifiable, _checked = check_names(with_attachment, "attachment_name")
        if missing:
            issues.append({
                "summary": f"{len(missing)} investigation attachment(s) are not in the claim folder",
                "items": sorted(missing),
            })
        without = len(investigations) - len(with_attachment)
        if without and config.get("flag_investigations_without_attachment"):
            issues.append({
                "summary": f"{without} investigation item(s) have no attachment at all",
                "items": sorted({
                    str(row.get("investigation_name") or row.get("investigation_code") or "(unnamed)")
                    for row in investigations
                    if not str(row.get("attachment_name") or "").strip()
                }),
            })

    if issues:
        message = "; ".join(issue["summary"] for issue in issues)
    else:
        message = f"All {declared_checked} declared document(s) are present"
        if notes:
            message += " (" + "; ".join(notes) + ")"

    return {
        "status": "fail" if issues else "pass",
        "message": message,
        "issues": issues,
        "declared_documents": len(declared),
        "documents_checked": declared_checked,
    }


_EVALUATORS = {
    "field_present": _evaluate_field_present,
    "field_consistency": _evaluate_field_consistency,
    "length_of_stay": _evaluate_length_of_stay,
    "documents_present": _evaluate_documents_present,
}


def _dedupe_by_hash(docs):
    """Collapses docs sharing identical content (same file_hash) down to
    one representative each, in scan order - the same grouping the
    Documents tab uses (routes/claims.py _group_duplicate_docs). Without
    this, a rule's "N of M documents" counts every duplicate-content copy
    as if it were a separate document, which doesn't match what the UI
    calls a "document" (its unique-document count) and reads as a much
    bigger problem than it is."""
    seen = {}
    for doc in docs:
        seen.setdefault(doc["file_hash"], doc)
    return list(seen.values())


def evaluate_rules(claim_id, claim_path, docs=None, settings=None):
    """Every enabled rule's live result for this claim. `docs` can be an
    already-scanned claim_scanner.scan_claim_cached() list, when the caller
    (e.g. the claims-list home page, which also needs it for quality
    rollups) already has one on hand - avoids each rule independently
    rescanning/rehashing every file in the claim. Likewise `settings`, when
    the caller already fetched it."""
    if docs is None:
        docs = claim_scanner.scan_claim_cached(claim_path)
    if settings is None:
        settings = settings_store.get_settings()
    # Shared for the lifetime of this call. `file_names` is taken from the
    # *undeduped* scan on purpose: two documents with identical bytes under
    # different names collapse to one entry in the deduped list, and a rule
    # that asks "is this declared file on disk?" would then report the
    # dropped name as missing.
    ctx = {
        "content_types": {},
        "file_names": {doc["file_name"].lower() for doc in docs},
        "claim_path": claim_path,
    }
    docs = _dedupe_by_hash(docs)
    results = []
    for rule in list_rules():
        if not rule["enabled"]:
            continue
        applies, reason = _procedure_codes_match(claim_id, rule["config"])
        if not applies:
            outcome = {"status": "no_data", "message": f"Not applicable to this claim ({reason})"}
        else:
            try:
                outcome = _EVALUATORS[rule["rule_type"]](claim_id, docs, rule["config"], settings, ctx)
            except Exception as exc:  # a bad/edge-case config shouldn't break the whole page
                outcome = {"status": "no_data", "message": f"Rule evaluation error: {exc}"}
        results.append({"rule_id": rule["id"], "name": rule["name"],
                         "rule_type": rule["rule_type"], **outcome})
    return results
