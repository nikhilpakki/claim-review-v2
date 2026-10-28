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


# A section with a heading and no body under it - the heading still has to be
# findable, and a key with no values is never iterated, so the heading stands
# in as its own value.
SECTION_KEY_FALLBACK = "Section text"


def _section_kv_pairs(page):
    """(key, value) pairs for one page's LAYOUT sections.

    Sections are where the narrative lives - a discharge summary is prose under
    a heading, so FORMS produces no key/value pair for it and TABLES sees
    nothing. Searching only forms/queries/tables therefore missed every word of
    it, which is what this repairs.

    The heading becomes the key, so searching for a heading finds the section;
    each line of the body becomes its own value, so a hit can be shown as a
    readable snippet instead of a whole page of prose (and so the fuzzy stage
    scores a query against one line rather than against thousands of
    characters, where token_set_ratio stops discriminating).
    """
    for section in page.get("sections", []):
        title = (section.get("title") or "").strip()
        key = title or SECTION_KEY_FALLBACK
        lines = [line.strip() for line in (section.get("text") or "").splitlines() if line.strip()]
        for line in lines or ([title] if title else []):
            yield key, line


def page_locations(page, file_name):
    """{(file, key, value): location} for one page, keyed as search_* expects.

    page_kv_dict() alone answers "was it found"; this answers "where", which is
    what turns a rule result into a link that opens the preview on the right
    page with the field boxed. Only forms carry geometry from Textract, so
    queries, table cells and section lines get a page-level target with no
    highlight - the same compromise documents_from_docs() makes.
    """
    base = {"page_number": page["page_number"], "image_rel": page.get("image_rel"),
            "width": page.get("width"), "height": page.get("height")}
    locations = {}
    for form in page.get("forms", []):
        locations[(file_name, form["key"], form["value"])] = {
            **base, "key_bbox": form.get("key_bbox"), "value_bbox": form.get("value_bbox")}
        locations.setdefault((file_name, form["value"], form["key"]), {
            **base, "key_bbox": form.get("value_bbox"), "value_bbox": form.get("key_bbox")})
    for alias, ans in (page.get("queries") or {}).items():
        answer = (ans.get("answer") or "").strip()
        if answer:
            locations.setdefault((file_name, alias, answer),
                                 {**base, "key_bbox": None, "value_bbox": None})
    for key, value in _section_kv_pairs(page):
        locations.setdefault((file_name, key, value),
                             {**base, "key_bbox": None, "value_bbox": None})
    return locations


def page_kv_dict(page):
    """{key: [values]} for a single cached page's forms + query answers +
    table cells + LAYOUT section text - the same extraction
    documents_from_docs() does per page, factored out so a single page can be
    searched on its own (e.g. the 'gps photo' keyword check in classify.py)."""
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
    for key, value in _section_kv_pairs(page):
        kvs.setdefault(key, []).append(value)
    return kvs


def documents_from_docs(docs, page_filter=None):
    """{rel_path: {key: [values]}} built from an already-scanned doc list
    (claim_scanner.scan_claim_cached - each doc already carries its
    cached_result, no re-hashing/re-reading here). Pulls from all four
    Textract result types - form KV pairs, QUERY answers, TABLE cells, and
    LAYOUT section headings and text - so search/rules see everything, not
    just forms. Also returns per-hit location metadata (page_number, key_bbox,
    value_bbox, image_rel) keyed by (rel_path, key, value) so search results
    can carry a jump-to target; queries, table cells and sections have no bbox
    from Textract today, so they get a page-level jump target with no
    highlight box.

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
            for key, value in _section_kv_pairs(page):
                kvs.setdefault(key, []).append(value)
                locations.setdefault((doc["rel_path"], key, value), {
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

    hits = [_hit({"file": file_name, "key": key, "value": value, "matched_on": side})
            for file_name, key, value, side in _pattern_hits(documents, pattern)]

    if not hits:
        return empty
    return {"best": hits[0], "matches": hits}


# A regex asks for an exact shape, which OCR does not reliably produce: a
# pattern like (operative|inter\-operative) misses "lnter-operative", or a
# stray space inside the word, while a reviewer reading the page can plainly
# see the word is there. So the plain words inside the pattern are also put
# through the tolerant search below, and the caller counts either as found.
#
# This lives here, rather than with the rules, because two different things
# ask what a pattern means: a rule's regex keyword and a document tag's
# pattern. If they disagreed, the same pattern would find a page when typed
# into a rule and not find it when typed into a tag.
#
# Only the literal words are taken. Everything that is regex syntax - classes,
# quantifiers, anchors, alternation, groups - separates one word from the next
# rather than being part of one.
_REGEX_SYNTAX = re.compile(
    r"\[(?:\\.|[^\]])*\]"                # a whole character class: [A-Z] is not a word
    r"|\\[dswDSWbBAZnrtfv0-9]"           # shorthand classes, backreferences
    r"|\\(?P<escaped>[^dswDSWbBAZnrtfv0-9])"  # an escaped literal: keep the character
    r"|[|()\[\]{}^$*+?.]"                # the rest of the syntax
)
_TERM_SEPARATOR = "\x00"

# Below this many characters a derived term is not worth searching for
# tolerantly. Measured against the fuzzy threshold of 80: "bpm" matches "bp" at
# 80 and "lscs" matches "lsc" at 85.7, so very short words report hits that are
# not the word that was asked for. The regex still looks for them exactly.
MIN_SEARCH_TERM = 3


def literal_terms(pattern):
    r"""The plain words inside a regex, for the tolerant search to look up.

    (operative|inter\-operative)  -> ['operative', 'inter-operative']
    \d+\s*bpm                     -> ['bpm']
    baby|birth|\d+\.\d+\s*(kg|g)  -> ['baby', 'birth']   ('kg', 'g' are too short)

    Returns [] for a pattern that is all syntax, which simply means the caller
    behaves exactly as it did before there was a tolerant stage at all.
    """
    def _replace(match):
        # An escaped punctuation character belongs to the word it sits in: the
        # \- in inter\-operative is a hyphen, not a separator.
        escaped = match.group("escaped")
        if escaped and not escaped.isspace():
            return escaped
        return _TERM_SEPARATOR

    flattened = _REGEX_SYNTAX.sub(_replace, str(pattern or ""))
    terms, seen = [], set()
    for chunk in flattened.split(_TERM_SEPARATOR):
        term = chunk.strip()
        if len(term) < MIN_SEARCH_TERM or not any(c.isalnum() for c in term):
            continue
        if term.lower() in seen:
            continue
        seen.add(term.lower())
        terms.append(term)
    return terms


def _pattern_hits(documents, pattern):
    """(file, key, value, matched_on) for every extracted field `pattern` is
    found in.

    The unit of matching is one field, not the page's text joined together:
    ^ and $ then mean the start and end of a value, which is what someone
    writing ^(final bill|invoice) intends, and a pattern cannot accidentally
    span two unrelated fields that happen to sit next to each other.
    """
    for file_name, kvs in documents.items():
        for key, values in kvs.items():
            for value in values:
                if key and pattern.search(key):
                    yield file_name, key, value, "key"
                if value and pattern.search(value):
                    yield file_name, key, value, "value"


def matches_pattern(documents, pattern):
    """Whether an already-compiled `pattern` is found anywhere in `documents`.

    The same matching as search_keyword(regex=True), short-circuited and
    without building hits: a document tag only needs to know whether the page
    carries it, and asks once per tag per page.
    """
    for _ in _pattern_hits(documents, pattern):
        return True
    return False


def search_terms(documents, terms, locations=None, fuzzy_threshold=None):
    """Find any of `terms` inside the extracted text, tolerating OCR noise.

    This exists because search_kvs answers a different question. It scores the
    query against a *whole* field value, which is right when comparing a name
    against a name, and wrong when looking for one word inside a sentence:
    measured, "operative" against "inter-0perative findings" scores 48.5 that
    way and 88.9 as a partial match. A rule asking whether a word appears needs
    the second reading.

    partial_ratio finds the best-matching run inside the value, so a garbled
    letter costs a little and surrounding words cost nothing. Measured against
    the threshold of 80, with the term "operative":

        inter-0perative findings    88.9   found, a zero for an o
        oprative note attached      94.1   found, a dropped letter
        conservative management     77.8   not found
        routine discharge summary   50.0   not found

    It does match "cooperative" and "postoperative", which contain the word
    outright - but so does the regex the terms were taken from, so this agrees
    with the pattern rather than widening it.

    Returns the same {'best', 'matches'} shape as the other searches.
    """
    if fuzzy_threshold is None:
        fuzzy_threshold = settings_store.get_settings()["FUZZY_MATCH_THRESHOLD"]
    locations = locations or {}
    cleaned = [t for t in ((term or "").strip() for term in terms or []) if t]
    empty = {"best": None, "matches": []}
    if not cleaned or not documents:
        return empty

    # Normalized once, not once per field compared against: a page runs to a
    # few hundred fields, and a document tag asks this of every page it does
    # not match outright. score_cutoff lets rapidfuzz abandon a comparison that
    # cannot reach the threshold instead of scoring it fully.
    #
    # min_chars is what keeps partial_ratio honest. It scores the best window
    # of the longer string against the shorter one, so when the field is
    # shorter than the term the roles reverse and a *fragment of the term*
    # scores 100: "course in hospital" against a field reading "Hospital" is
    # 100.0, and so is "final bill" against "bill". Requiring the field to be
    # at least as long as the threshold says the term may be corrupted keeps
    # the question "is this word in this text" from quietly becoming "is this
    # text a piece of this word" - while still allowing the dropped letters
    # this stage exists for ("oprative note attached" for "operative", 94.1).
    prepared = []
    for term in cleaned:
        needle = _normalize_for_fuzzy(term.lower())
        prepared.append((term, needle, len(needle) * fuzzy_threshold / 100.0))
    hits = []
    for file_name, kvs in documents.items():
        for key, values in kvs.items():
            for value in values:
                for side, text in (("key", key), ("value", value)):
                    normalized = _normalize_for_fuzzy((text or "").lower())
                    if not normalized:
                        continue
                    for term, needle, min_chars in prepared:
                        if len(normalized) < min_chars:
                            continue
                        score = fuzz.partial_ratio(needle, normalized,
                                                   score_cutoff=fuzzy_threshold)
                        if not score:
                            continue
                        loc = locations.get((file_name, key, value), {})
                        hits.append({"file": file_name, "key": key, "value": value,
                                     "matched_on": side, "method": "fuzzy term",
                                     "term": term, "score": float(score), **loc})
                        break
    if not hits:
        return empty
    matches = sorted(hits, key=lambda h: h["score"], reverse=True)
    return {"best": matches[0], "matches": matches}


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
