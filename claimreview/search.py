import re

from rapidfuzz import fuzz, process

from . import claim_scanner, settings_store


def _soundex(word):
    """Classic American Soundex phonetic code (letter + 3 digits).

    Used only as a fallback signal - OCR mangles letters ('rn' <-> 'm',
    '0'/'O', etc.) in ways that hurt string similarity but often leave the
    word sounding the same, which Soundex is robust to.
    """
    word = re.sub(r"[^A-Za-z]", "", word).upper()
    if not word:
        return ""

    codes = {
        "B": "1", "F": "1", "P": "1", "V": "1",
        "C": "2", "G": "2", "J": "2", "K": "2", "Q": "2", "S": "2", "X": "2", "Z": "2",
        "D": "3", "T": "3",
        "L": "4",
        "M": "5", "N": "5",
        "R": "6",
    }

    first_letter = word[0]
    digits = []
    prev_code = codes.get(first_letter, "")
    for ch in word[1:]:
        code = codes.get(ch, "")
        if code and code != prev_code:
            digits.append(code)
        prev_code = code

    return (first_letter + "".join(digits) + "000")[:4]


def _soundex_tokens(phrase):
    return [_soundex(tok) for tok in re.findall(r"[A-Za-z']+", phrase)]


def _soundex_overlap_ratio(tokens_a, tokens_b):
    if not tokens_a or not tokens_b:
        return 0.0
    set_a, set_b = set(tokens_a), set(tokens_b)
    return len(set_a & set_b) / max(len(set_a), len(set_b))


def _normalize_for_fuzzy(text):
    """Collapse any run of non-word characters (punctuation, slashes,
    extra whitespace) to a single space before fuzzy comparison, so a
    punctuation-only difference doesn't depress the score - e.g. 'ABC DEF'
    vs 'ABC DEF/ C' scores 82 with token_set_ratio unnormalized but 100
    once both are normalized to 'ABC DEF' / 'ABC DEF C'."""
    return re.sub(r"\W+", " ", text or "").strip()


def values_match(a, b, fuzzy_threshold=None, soundex_threshold=None):
    """Layered equality check between two whole field values (e.g. an
    extracted name vs a CSV reference name) - same exact/fuzzy/soundex
    fallback chain as search_kvs, but comparing two full values against
    each other (OCR noise tolerant) rather than a query against a longer
    text. Returns (matched, method_or_None, score).
    """
    if fuzzy_threshold is None or soundex_threshold is None:
        settings = settings_store.get_settings()
        fuzzy_threshold = fuzzy_threshold if fuzzy_threshold is not None else settings["FUZZY_MATCH_THRESHOLD"]
        soundex_threshold = soundex_threshold if soundex_threshold is not None else settings["SOUNDEX_TOKEN_OVERLAP_THRESHOLD"]

    a_norm, b_norm = _normalize_for_fuzzy((a or "").lower()), _normalize_for_fuzzy((b or "").lower())
    if not a_norm or not b_norm:
        return False, None, 0.0
    if a_norm == b_norm:
        return True, "exact", 100.0

    score = fuzz.token_set_ratio(a_norm, b_norm)
    if score >= fuzzy_threshold:
        return True, "fuzzy", score

    ratio = _soundex_overlap_ratio(_soundex_tokens(a_norm), _soundex_tokens(b_norm))
    if ratio >= soundex_threshold:
        return True, "soundex", round(ratio * 100, 1)

    return False, None, 0.0


def page_kv_dict(page):
    """{key: [values]} for a single cached page's forms + query answers +
    table cells - the same extraction documents_from_docs() does per page,
    factored out so a single page can be searched on its own (e.g. the
    'gps photo' keyword check in classify.py)."""
    kvs = {}
    for form in page.get("forms", []):
        kvs.setdefault(form["key"], []).append(form["value"])
        kvs.setdefault(form["value"], []).append(form["key"])
    for alias, ans in page.get("queries", {}).items():
        answer = (ans.get("answer") or "").strip()
        if answer:
            kvs.setdefault(alias, []).append(answer)
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
                key = header_cell or f"Table column {col_idx + 1}"
                kvs.setdefault(key, []).append(cell)
    return kvs


def documents_from_docs(docs, page_filter=None):
    """{rel_path: {key: [values]}} built from an already-scanned doc list
    (claim_scanner.scan_claim_cached - each doc already carries its
    cached_result, no re-hashing/re-reading here). Pulls from all three
    Textract result types - form KV pairs, QUERY answers, and TABLE cells -
    so search/rules see everything, not just forms. Also returns per-hit
    location metadata (page_number, key_bbox, value_bbox, image_rel) keyed
    by (rel_path, key, value) so search results can carry a jump-to target;
    queries and table cells have no bbox from Textract today, so they get a
    page-level jump target with no highlight box.

    `page_filter(page) -> bool`, if given, restricts which pages contribute
    (e.g. a rule scoped to only 'document'-tagged pages) - a document with
    zero pages passing the filter is dropped entirely rather than appearing
    as an empty, always-"missing" entry.
    """
    documents = {}
    locations = {}
    for doc in docs:
        cached = doc.get("cached_result")
        if not cached:
            continue
        kvs = {}
        doc_has_scoped_page = page_filter is None
        for page in cached["pages"]:
            if page_filter is not None:
                if not page_filter(page):
                    continue
                doc_has_scoped_page = True
            loc_base = {
                "page_number": page["page_number"],
                "image_rel": page["image_rel"],
                "width": page["width"],
                "height": page["height"],
            }
            for form in page["forms"]:
                kvs.setdefault(form["key"], []).append(form["value"])
                locations[(doc["rel_path"], form["key"], form["value"])] = {
                    **loc_base,
                    "key_bbox": form["key_bbox"],
                    "value_bbox": form["value_bbox"],
                }
            for alias, ans in page.get("queries", {}).items():
                answer = (ans.get("answer") or "").strip()
                if not answer:
                    continue
                kvs.setdefault(alias, []).append(answer)
                locations.setdefault((doc["rel_path"], alias, answer), {
                    **loc_base, "key_bbox": None, "value_bbox": None,
                })
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
                        key = header_cell or f"Table column {col_idx + 1}"
                        kvs.setdefault(key, []).append(cell)
                        locations.setdefault((doc["rel_path"], key, cell), {
                            **loc_base, "key_bbox": None, "value_bbox": None,
                        })
        if page_filter is not None and not doc_has_scoped_page:
            continue
        documents[doc["rel_path"]] = kvs
    return documents, locations


def load_claim_documents(claim_path):
    """documents_from_docs(), scanning claim_path fresh. For callers that
    don't already have a scan_claim_cached() result on hand (e.g. the
    interactive per-claim search box) - prefer documents_from_docs() when a
    scan is already available to avoid a redundant rescan/rehash."""
    return documents_from_docs(claim_scanner.scan_claim_cached(claim_path))


class InvalidKeywordPattern(ValueError):
    pass


def search_keyword(documents, keyword, regex=False, case_insensitive=True, locations=None):
    """Literal-substring or regex lookup of `keyword` against every
    extracted key AND value (`documents` is {file_name: {key: [values]}}) -
    unlike search_kvs, no fuzzy/soundex fallback: this is for an operator
    who wants exact control (a specific phrase, an exact code, a regex
    pattern), not typo tolerance. Returns {'best': hit_or_None, 'matches': [hit, ...]}.
    Raises InvalidKeywordPattern if `regex` is set and `keyword` doesn't compile.
    """
    locations = locations or {}
    empty = {"best": None, "matches": []}
    if not keyword or not documents:
        return empty

    flags = re.IGNORECASE if case_insensitive else 0
    try:
        pattern = re.compile(keyword if regex else re.escape(keyword), flags)
    except re.error as exc:
        raise InvalidKeywordPattern(str(exc)) from exc

    def _hit(rec):
        loc = locations.get((rec["file"], rec["key"], rec["value"]), {})
        return {"file": rec["file"], "key": rec["key"], "value": rec["value"],
                "matched_on": rec["matched_on"], "method": "regex" if regex else "keyword",
                "score": 100.0, **loc}

    hits = []
    for file_name, kvs in documents.items():
        for key, values in kvs.items():
            for value in values:
                if key and pattern.search(key):
                    hits.append(_hit({"file": file_name, "key": key, "value": value, "matched_on": "key"}))
                if value and pattern.search(value):
                    hits.append(_hit({"file": file_name, "key": key, "value": value, "matched_on": "value"}))

    if not hits:
        return empty
    return {"best": hits[0], "matches": hits}


def _build_search_records(documents):
    records = []
    for file_name, kvs in documents.items():
        for key, values in kvs.items():
            for value in values:
                records.append({"file": file_name, "key": key, "value": value,
                                 "matched_on": "key", "text": _normalize_for_fuzzy(key.lower())})
                records.append({"file": file_name, "key": key, "value": value,
                                 "matched_on": "value", "text": _normalize_for_fuzzy(value.lower())})
    return records


def search_kvs(documents, query, locations=None,
                fuzzy_threshold=None, soundex_threshold=None,
                matched_on=None):
    """Layered lookup of `query` against every extracted key AND value across
    every processed document in the claim (`documents` is
    {file_name: {key: [values]}}):

      1. Exact / substring / regex match  - authoritative
      2. Fuzzy match (rapidfuzz token_set_ratio)  - typos, OCR noise, and
         (since both sides are normalized first) punctuation differences
      3. Soundex phonetic overlap  - letter-level OCR garbling

    Both the query and every candidate key/value are run through
    _normalize_for_fuzzy (punctuation/symbol runs collapsed to a single
    space) before any of the three stages, so e.g. 'ABC DEF' matches
    'ABC DEF/ C' as if it were 'ABC DEF C'.

    Stages 2/3 only run if the previous stage found nothing. Returns
    {'best': hit_or_None, 'matches': [hit, ...]}, each hit extended with
    page_number/image_rel/key_bbox/value_bbox from `locations` when available.

    `matched_on` restricts which side of each pair is a candidate: {'value'}
    when looking for a known value in the documents (a patient's name is
    content, and matching it against field *labels* only invents hits),
    {'key'} when resolving a field label, or None for both. Narrowing this is
    the cheapest way to cut false positives, because it removes whole classes
    of candidate before any fuzzy scoring happens.
    """
    if fuzzy_threshold is None or soundex_threshold is None:
        settings = settings_store.get_settings()
        fuzzy_threshold = fuzzy_threshold if fuzzy_threshold is not None else settings["FUZZY_MATCH_THRESHOLD"]
        soundex_threshold = soundex_threshold if soundex_threshold is not None else settings["SOUNDEX_TOKEN_OVERLAP_THRESHOLD"]
    locations = locations or {}

    query = _normalize_for_fuzzy(query.strip().lower())
    empty = {"best": None, "matches": []}
    if not query or not documents:
        return empty

    records = _build_search_records(documents)
    if matched_on is not None:
        records = [rec for rec in records if rec["matched_on"] in matched_on]
    if not records:
        return empty

    def _hit(rec, method, score):
        loc = locations.get((rec["file"], rec["key"], rec["value"]), {})
        return {"file": rec["file"], "key": rec["key"], "value": rec["value"],
                "matched_on": rec["matched_on"], "method": method, "score": score, **loc}

    def _result(hits):
        matches = sorted(hits, key=lambda h: h["score"], reverse=True)
        return {"best": matches[0], "matches": matches}

    pattern = re.compile(re.escape(query))
    exact_hits = [_hit(rec, "exact", 100.0) for rec in records if rec["text"] and pattern.search(rec["text"])]
    if exact_hits:
        return _result(exact_hits)

    texts = [rec["text"] for rec in records]
    fuzzy = process.extract(query, texts, scorer=fuzz.token_set_ratio,
                             score_cutoff=fuzzy_threshold, limit=None)
    if fuzzy:
        return _result([_hit(records[idx], "fuzzy", score) for _, score, idx in fuzzy])

    query_tokens = _soundex_tokens(query)
    soundex_hits = []
    for rec in records:
        ratio = _soundex_overlap_ratio(query_tokens, _soundex_tokens(rec["text"]))
        if ratio >= soundex_threshold:
            soundex_hits.append(_hit(rec, "soundex", round(ratio * 100, 1)))
    if soundex_hits:
        return _result(soundex_hits)

    return empty
