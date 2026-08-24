"""Narrative sections recovered from Textract's LAYOUT blocks.

A discharge summary is prose under a heading. FORMS never sees it - there is no
key/value pair to extract - and a QUERY answer gives at most a sentence. LAYOUT
marks the document's structure, so grouping narrative blocks under the nearest
preceding heading (done per page in textract_client) is the only way to get the
whole passage.

Two things this has to handle that a single page does not show:

- Each page is its own analyze_document call, so a section running across a
  page break arrives as a titled section on one page followed by *untitled*
  sections on the next. Those are stitched back together here.
- A claim holds many documents, and more than one may carry something
  summary-like. The best-matching heading across the whole claim wins, and the
  document and page it came from are reported so the reviewer can go and look.
"""
from rapidfuzz import fuzz

from . import search

# Heading phrasings that mark a discharge summary or its equivalent, as they
# actually appear on Indian claim paperwork.
DISCHARGE_SUMMARY_SECTION_ALIASES = [
    "discharge summary", "clinical summary", "course in hospital",
    "summary of treatment", "discharge notes", "hospital course",
    "summary of hospital stay", "discharge report", "summary of admission",
    "hospital summary", "summary of care", "discharge information",
    "summary of treatment and care", "summary of hospitalization",
    "discharge instructions", "summary of medical care",
    "hospitalization summary", "summary of patient care",
    "discharge documentation", "summary of hospital course",
    "summary of treatment provided", "discharge summary report",
    "summary of hospital stay and treatment",
]

# Below this many characters a "summary" is a heading with nothing under it -
# worth ignoring rather than presenting as the claim's discharge summary.
MIN_SUMMARY_CHARS = 40


def logical_sections(docs):
    """Sections per claim, stitched across page boundaries.

    Yields {title, text, file, page, pages} in document order. `page` is where
    the heading appeared; `pages` counts how many pages the section spans.
    """
    for doc in docs:
        cached = doc.get("cached_result")
        if not cached:
            continue
        current = None
        for page in cached.get("pages", []):
            for section in page.get("sections", []):
                title = (section.get("title") or "").strip()
                text = (section.get("text") or "").strip()
                if title:
                    if current:
                        yield current
                    current = {"title": title, "text": text,
                               "file": doc["rel_path"], "page": page["page_number"],
                               "pages": 1}
                elif current is not None and text:
                    # Untitled narrative after a heading: the same section
                    # continuing, very often onto the next page.
                    current["text"] = (current["text"] + "\n" + text).strip()
                    if page["page_number"] != current["page"]:
                        current["pages"] += 1
        if current:
            yield current


def match_section(docs, aliases, fuzzy_threshold):
    """Best section whose heading matches any alias, or None.

    Exact containment wins outright; otherwise the heading is scored against
    each alias and has to clear `fuzzy_threshold`, because OCR mangles headings
    ("DISCHAKGE SUMMAKY") and a heading is short enough that a bad match is
    easy to make and expensive to trust.
    """
    best = None
    for section in logical_sections(docs):
        heading = search._normalize_for_fuzzy(section["title"].lower())
        if not heading:
            continue
        for alias in aliases:
            if alias in heading:
                score, method = 100.0, "exact"
            else:
                score, method = fuzz.token_set_ratio(alias, heading), "fuzzy"
            if score < fuzzy_threshold:
                continue
            if best is None or score > best["score"]:
                best = {**section, "score": round(float(score), 1),
                        "method": method, "matched_alias": alias}
    return best


def build_discharge_summary(docs, settings):
    """The claim's discharge summary as free text, or None.

    None covers three cases that look the same to a reviewer and are worth
    telling apart in the UI: no document has a matching heading, LAYOUT was not
    requested when these documents were processed, or the matching section is
    an empty heading.
    """
    threshold = settings.get("FUZZY_MATCH_THRESHOLD", 80)
    match = match_section(docs, DISCHARGE_SUMMARY_SECTION_ALIASES, threshold)
    if match is None or len(match["text"]) < MIN_SUMMARY_CHARS:
        return None
    return match


def has_any_sections(docs):
    """Whether any processed document carries LAYOUT sections at all - lets the
    UI say "processed without LAYOUT" instead of "nothing found"."""
    for doc in docs:
        cached = doc.get("cached_result")
        if not cached:
            continue
        for page in cached.get("pages", []):
            if page.get("sections"):
                return True
    return False
