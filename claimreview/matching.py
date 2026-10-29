"""How a typed pattern is looked for in a claim's extracted text.

One engine behind both places an administrator types something to find: a
rule's keyword and a document tag's pattern. They were close before and not
identical - a tag was always a regex and always got the tolerant stage, while
a plain rule keyword got neither - so the same words typed into the two
screens could disagree, with nothing on either screen to say why.

The controls are the same on both, and mean the same thing:

  case-insensitive  on by default; OCR capitalisation is not dependable
  regex             the pattern is a regular expression, not fixed text
  exact             look for the plain phrases inside the pattern, literally
  fuzzy             and/or look for those phrases tolerantly, by the chosen
                    methods, at the chosen threshold

Stages run strongest first and stop at the first that finds anything, so a
hit always carries the best evidence there was for it, and the panel behind a
tag badge can say which stage put it there.
"""
import re

from . import search

# Offered in the order they are tried, which is also strongest first.
FUZZY_METHODS = [
    ("whole", "Whole field",
     "Scores the phrase against a whole field, the way the Search tab does. "
     "Finds a garbled rendering of a short field; will not find a phrase "
     "buried in a longer sentence."),
    ("partial", "Inside a field",
     "Scores the phrase against the stretch of a field that best matches it, "
     "so a phrase is found inside a longer sentence. Looser than whole field, "
     "and the reason a tag finds real OCR'd pages."),
    ("soundex", "Sounds like",
     "Compares the phrase phonetically, so letter-level garbling that changes "
     "the spelling but not the sound is still found. Loosest of the three."),
]
FUZZY_KEYS = [key for key, _label, _help in FUZZY_METHODS]

# Says "this submission came from the matching controls", so an unticked box
# can be told apart from a caller that never had the controls in front of it.
MARKER = "match_controls"

# What a new rule and a new tag start with. A tag is a pattern by nature - it
# is written once, centrally, to recognise a kind of page - while a rule's
# keyword is far more often a phrase somebody wants found as they typed it,
# so only the regex box differs between them.
#
# Both tolerant methods are on, in that order, and the second is the reason
# this product finds anything on a real scan. Whole-field scoring is the
# stricter of the two and runs first, but it only recognises a field that is
# *mostly* the phrase: measured, "declaration" against a field reading
# "DECLARATlON CERTIFICATE" is 58.8 whole and 90.9 inside. Defaulting to
# whole alone would quietly stop every tag finding the garbled headings it
# exists to find, so the fallback is on until somebody turns it off.
RULE_DEFAULTS = {"case_insensitive": True, "regex": False, "exact": True,
                 "fuzzy": ["whole", "partial"], "fuzzy_threshold": None}
TAG_DEFAULTS = {**RULE_DEFAULTS, "regex": True}


def normalize(config, defaults=None):
    """A stored or submitted config, filled in and bounded.

    Missing keys take the defaults, which is what carries a rule or a tag
    written before these controls existed onto them.
    """
    config = config or {}
    base = dict(defaults or RULE_DEFAULTS)
    fuzzy = config.get("fuzzy", base["fuzzy"])
    if isinstance(fuzzy, str):
        fuzzy = [part for part in re.split(r"[,\s]+", fuzzy) if part]
    threshold = config.get("fuzzy_threshold", base["fuzzy_threshold"])
    try:
        threshold = max(1, min(100, int(threshold))) if threshold else None
    except (TypeError, ValueError):
        threshold = None
    return {
        "case_insensitive": bool(config.get("case_insensitive", base["case_insensitive"])),
        "regex": bool(config.get("regex", base["regex"])),
        "exact": bool(config.get("exact", base["exact"])),
        # Ordered by FUZZY_KEYS rather than by how they arrived, so the stages
        # always run strongest first however the form was filled in.
        "fuzzy": [key for key in FUZZY_KEYS if key in set(fuzzy or [])],
        "fuzzy_threshold": threshold,
    }


def from_form(form, prefix="", defaults=None):
    """The config a submitted form describes.

    An unticked checkbox is absent from the submission rather than false, so
    on a form that was actually filled in, a missing box is an answer and the
    defaults must not paper over it - somebody who turned everything off is
    told so rather than quietly given the defaults back.

    Which is why the rendered block carries a marker field. Without it the
    submission did not come from these controls at all - a caller passing a
    plain dict, or a form saved before they existed - and the defaults are
    the right reading. See the macro in partials/match_controls.html.
    """
    if not form.get(f"{prefix}{MARKER}"):
        return normalize({}, defaults)

    def ticked(name):
        return form.get(f"{prefix}{name}") in ("on", "1", "true", True)
    return normalize({
        "case_insensitive": ticked("case_insensitive"),
        "regex": ticked("regex"),
        "exact": ticked("exact"),
        "fuzzy": [key for key in FUZZY_KEYS if ticked(f"fuzzy_{key}")],
        "fuzzy_threshold": (form.get(f"{prefix}fuzzy_threshold") or "").strip() or None,
    }, defaults)


# --------------------------------------------------------- pattern -> words

# The three things a ( can open. Flags configure the pattern and match no
# text; a lookaround asserts what sits beside the match without being part of
# it, so its contents are not text to look for either; everything else is an
# ordinary group, whose contents are.
_INLINE_FLAGS = re.compile(r"\(\?[aiLmsux]+(?:-[imsx]+)?\)")
_LOOKAROUND = re.compile(r"\(\?(?:[=!]|<[=!])")
_GROUP_OPEN = re.compile(r"\(\?(?:P<[^>]*>|P=[^)]*\)|:|>)")


def _skip_class(source, index):
    """The index just past the ] closing the character class opening at
    `index`. A ] immediately after the [ or [^ is a literal one."""
    index += 1
    if index < len(source) and source[index] == "^":
        index += 1
    if index < len(source) and source[index] == "]":
        index += 1
    while index < len(source) and source[index] != "]":
        index += 2 if source[index] == "\\" else 1
    return index + 1


def _skip_group(source, index):
    """The index just past the ) balancing the ( at `index`."""
    depth, end = 0, len(source)
    while index < end:
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            index = _skip_class(source, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return end


def literal_phrase(source):
    r"""One alternative of a pattern, as the plain phrase it looks for.

        discharge\s+notes?   ->  discharge note
        \d+\s*bpm            ->  bpm
        inter\-operative     ->  inter-operative

    Everything that is regex syntax describes *how* to match rather than what
    to match, so it contributes either a gap or nothing:

      \s          whitespace between words, so a single space
      \d \w \b    a class or an assertion - no one literal text
      [A-Z] .     likewise
      ^ $ ( )     position and grouping
      \. \-       an escaped character is that character, not syntax
      x? x*       x is optional, so the phrase without it is the one that is
                  present either way - which is the one worth looking for
      (xy)?       likewise for a whole group: note(s)? is "note"
      x+          x appears at least once, so once is right
    """
    source = str(source or "")
    # Where in `out` each open group started, so a quantifier on its closing
    # bracket can reach back over everything the group contained.
    out, groups, index, end = [], [], 0, len(source)
    while index < end:
        char = source[index]
        # None means "produced nothing and takes no quantifier of its own",
        # which is how a bracket differs from a character that matched.
        piece = None
        if char == "\\" and index + 1 < end:
            escaped = source[index + 1]
            index += 2
            if escaped in "sntrfv":
                piece = " "
            elif escaped.isalnum():
                piece = ""                 # \d \w \b - a class or assertion
            else:
                piece = escaped            # \. \- \( - the character itself
        elif char == "[":
            index = _skip_class(source, index)
            piece = ""
        elif char == "(":
            flags = _INLINE_FLAGS.match(source, index)
            if flags:
                index = flags.end()
                continue
            if _LOOKAROUND.match(source, index):
                # What a lookaround asserts sits beside the match, not in it.
                index = _skip_group(source, index)
                continue
            opened = _GROUP_OPEN.match(source, index)
            index = opened.end() if opened else index + 1
            groups.append(len(out))
            continue
        elif char == ")":
            index += 1
            start = groups.pop() if groups else len(out)
            if index < end and source[index] in "*+?":
                if source[index] in "*?":
                    del out[start:]        # the group is optional - leave it out
                index += 1
                if index < end and source[index] == "?":
                    index += 1
            continue
        elif char == "{":                  # a repeat count, not text
            while index < end and source[index] != "}":
                index += 1
            index += 1
            piece = ""
        else:
            index += 1
            piece = "" if char in "^$.*+?" else char
        # A quantifier belongs to whatever was just read. A gap stays a gap
        # however it is quantified - "discharge\s*notes" is still two words -
        # but an optional character is dropped, because the phrase without it
        # is the one that is there either way.
        if index < end and source[index] in "*+?":
            if source[index] in "*?" and piece != " ":
                piece = ""
            index += 1
            if index < end and source[index] == "?":   # lazy: *? +? ??
                index += 1
        out.append(piece or "")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def derive_terms(pattern, regex=True, min_chars=None):
    r"""The plain phrases a pattern looks for, for the exact and fuzzy stages.

        (discharge summary|discharge\s+notes?)
            -> ['discharge summary', 'discharge note']

    The pipe is the one piece of regex syntax meaning "or another whole thing
    to look for" rather than describing a single match, so it separates one
    phrase from the next instead of being stripped out of one. Fixed text is
    split on it too: a pipe typed into a plain keyword is far more likely to
    mean "either of these" than to be a character someone wants found.

    A pattern that is nothing but syntax yields nothing, which simply means
    the exact and fuzzy stages have no work and only the regex runs.
    """
    if min_chars is None:
        min_chars = search.MIN_SEARCH_TERM
    parts = [literal_phrase(part) if regex else part.strip()
             for part in str(pattern or "").split("|")]
    terms, seen = [], set()
    for term in parts:
        term = term.strip()
        # Counted in letters and digits, not characters: stripping the syntax
        # out of "\d+\.\d+\s*(kg" leaves ". kg", which is long enough to look
        # like a phrase and is really just the two letters that survived.
        if sum(c.isalnum() for c in term) < min_chars:
            continue
        if term.lower() in seen:
            continue
        seen.add(term.lower())
        terms.append(term)
    return terms


# ------------------------------------------------------------------ running

def compile_pattern(pattern, config):
    """The compiled regex for the pattern stage, or None if there isn't one.

    Fixed text is escaped rather than refused, so the pattern stage still
    means "this text, exactly" when the regex box is off.
    """
    flags = re.IGNORECASE if config["case_insensitive"] else 0
    source = pattern if config["regex"] else re.escape(pattern or "")
    try:
        return re.compile(source, flags)
    except re.error as exc:
        raise search.InvalidKeywordPattern(str(exc)) from exc


def validate(pattern, config):
    """Why this pattern and these controls cannot find anything, or None.

    A pattern with every stage switched off is the one combination that is
    silently useless - it would simply never match, with nothing to say so.
    """
    if not (pattern or "").strip():
        return "No keyword or pattern given"
    compile_pattern(pattern, config)
    if config["regex"] or config["exact"] or config["fuzzy"]:
        if not config["regex"] and not derive_terms(pattern, config["regex"]):
            return ("Nothing to look for: the pattern is all regex syntax, so "
                    "there are no plain words to search for. Tick “treat as "
                    "a regex pattern” to match it as written.")
        return None
    return ("Nothing is switched on: tick at least one of regex, exact match, "
            "or a fuzzy method, or this will never match anything.")


def run(documents, pattern, config, locations=None, prepared=None):
    """{'best', 'matches'} - the strongest stage that finds anything.

    Stages stop at the first that produces hits. A tolerant hit is therefore
    only ever reported when the exact text genuinely is not there, which is
    what lets the reason shown against a hit be trusted.
    """
    empty = {"best": None, "matches": []}
    if not documents or not (pattern or "").strip():
        return empty
    locations = locations or {}
    compiled, terms = prepared or (compile_pattern(pattern, config),
                                   derive_terms(pattern, config["regex"]))

    def result(hits):
        if not hits:
            return None
        matches = sorted(hits, key=lambda h: h["score"], reverse=True)
        return {"best": matches[0], "matches": matches}

    if config["regex"]:
        found = result(search.pattern_hits(documents, compiled, locations,
                                           method="regex"))
        if found:
            return found
    if config["exact"] and terms:
        found = result(search.exact_term_hits(
            documents, terms, locations,
            case_insensitive=config["case_insensitive"]))
        if found:
            return found
    # Without the regex box the pattern is fixed text, so it is its own phrase
    # and the fuzzy stages can look for it even with exact matching switched
    # off. With the box on, only the derived phrases are searchable text.
    fuzzy_terms = terms or ([] if config["regex"] else [pattern])
    for method in config["fuzzy"]:
        if not fuzzy_terms:
            break
        found = result(search.fuzzy_term_hits(
            documents, fuzzy_terms, method, locations,
            threshold=config["fuzzy_threshold"]))
        if found:
            return found
    return empty


def describe(config, terms=None):
    """A one-line summary of what these controls do, for a settings list."""
    parts = ["regex" if config["regex"] else "fixed text"]
    if config["exact"]:
        parts.append("exact" + (f" ({', '.join(repr(t) for t in terms)})"
                                if terms else ""))
    if config["fuzzy"]:
        labels = {key: label for key, label, _help in FUZZY_METHODS}
        at = f" at {config['fuzzy_threshold']}%" if config["fuzzy_threshold"] else ""
        parts.append("fuzzy: " + ", ".join(labels[k].lower()
                                           for k in config["fuzzy"]) + at)
    parts.append("case insensitive" if config["case_insensitive"]
                 else "case sensitive")
    return " · ".join(parts)
