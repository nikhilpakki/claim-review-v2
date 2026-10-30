"""Pulling values out of a claim's extracted text, and judging them.

The other rule types answer "is this here?". This one answers "what does it
say?" - the amount on the bill, the UHID on the card, the number of days - and
optionally turns that answer into a verdict.

Three things make it a separate engine rather than another mode of matching.py:

  - A regex is mandatory. There is no tolerant stage, because a fuzzy match
    has no capture groups: nothing to hand back but the field it landed in.
  - It reads a wider unit of text. matching.py tests each extracted field on
    its own, so a form pair is two unrelated strings and a pattern naming the
    label can never reach the number beside it. Here each pair is *also*
    offered joined, "TOTAL AMOUNT 1,24,500", so one pattern can span both.
  - It collects every match rather than stopping at the first, because the
    interesting case is a claim where two documents disagree.
"""
import re

from . import search

# Which capture group to take when nothing is chosen. 0 is the whole match,
# which is what a pattern with no groups in it can offer.
DEFAULT_GROUPS = [0]
# How many group numbers the form offers. Well past anything a reviewer writes
# by hand, and the options beyond a pattern's own group count are hidden.
MAX_GROUPS = 9
# A runaway pattern (one that can match the empty string, say) must not be
# able to hang a claim page. Findings are deduplicated well below this in
# practice; hitting it means the pattern is wrong, and the result says so.
MAX_FINDINGS = 2000

# (key, label, kind). The label completes the sentence "the extracted
# value ...", so the form reads as one line rather than three controls.
OPERATORS = [
    ("num_lt", "is less than", "number"),
    ("num_eq", "equals", "number"),
    ("num_gt", "is greater than", "number"),
    ("num_ne", "does not equal", "number"),
    ("text_eq", "is exactly", "text"),
    ("text_contains", "contains", "text"),
    ("text_not_contains", "does not contain", "text"),
]
OPERATOR_LABELS = {key: label for key, label, _kind in OPERATORS}
OPERATOR_KINDS = {key: kind for key, _label, kind in OPERATORS}
DEFAULT_OPERATOR = "num_lt"

DEFAULTS = {
    "pattern": "",
    "case_insensitive": True,
    "groups": list(DEFAULT_GROUPS),
    "validate": False,
    "operator": DEFAULT_OPERATOR,
    "compare_value": "",
    # Off by default: a claim the pattern finds nothing on is a claim this
    # rule has nothing to say about, which is NO DATA everywhere else in the
    # rule set. Ticking it makes absence itself the finding.
    "fail_when_missing": False,
}


class InvalidExtraction(ValueError):
    """A configuration that cannot be run - reported on the form, not raised
    at a reviewer reading a claim."""


def normalize(config):
    """A stored config with every key present, so an evaluator never has to
    ask whether a setting predates the feature."""
    config = config or {}
    out = {}
    for key, default in DEFAULTS.items():
        value = config.get(key, default)
        if isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, list):
            value = [int(v) for v in value if str(v).strip().lstrip("-").isdigit()] or list(default)
        elif isinstance(default, str):
            value = str(value or "")
        out[key] = value
    if out["operator"] not in OPERATOR_LABELS:
        out["operator"] = DEFAULT_OPERATOR
    return out


def compile_pattern(pattern, case_insensitive=True):
    try:
        return re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error as exc:
        raise InvalidExtraction(f"Invalid regex pattern: {exc}") from exc


def validate(config):
    """Raises InvalidExtraction describing the first thing wrong, or returns
    the compiled pattern. Every message is written to be shown on the form."""
    config = normalize(config)
    if not config["pattern"].strip():
        raise InvalidExtraction("Give the rule a pattern to extract with.")
    compiled = compile_pattern(config["pattern"], config["case_insensitive"])
    for group in config["groups"]:
        if group < 0 or group > compiled.groups:
            raise InvalidExtraction(
                f"The pattern has {compiled.groups} capture group(s), so group "
                f"{group} does not exist. Group 0 is the whole match.")
    if config["validate"]:
        if not config["compare_value"].strip():
            raise InvalidExtraction("Give the comparison a value to check against.")
        if OPERATOR_KINDS[config["operator"]] == "number" \
                and parse_number(config["compare_value"]) is None:
            raise InvalidExtraction(
                f"'{config['compare_value']}' is not a number, so it cannot be "
                "compared with one. Pick a text comparison instead.")
    return compiled


# ------------------------------------------------------------ what to read

def text_units(page, file_name):
    """[(text, location), ...] - every piece of text on one page a pattern may
    be run against, each with somewhere to jump to.

    Built from the same page_kv_dict() every other search reads, so extraction
    cannot see text the Search tab and the keyword rules cannot. What it adds
    is the joined form: a pair indexed as key -> value also appears as
    "key value", which is the only way a pattern naming a field's label can
    reach the value printed beside it.

    Form pairs are indexed both ways round by page_kv_dict, so the reversed
    join ("1,24,500 TOTAL AMOUNT") is offered too. It is harmless - a pattern
    written label-first simply does not match it - and dropping it would mean
    re-deriving which side was the key, which is exactly the thing the shared
    index exists to avoid.

    Table rows are added here rather than taken from the shared index, which
    pairs each cell with its *column header* - the only pairing a text search
    can use. That leaves "Room rent" and "9,000" in different pairs, so a
    pattern naming the line item could never reach its amount. A bill is read
    along its rows, so each row is offered whole as well.
    """
    kvs = search.page_kv_dict(page)
    locations = search.page_locations(page, file_name)
    base = {"page_number": page.get("page_number"), "image_rel": page.get("image_rel"),
            "key_bbox": None, "value_bbox": None}
    units = {}
    for key, values in kvs.items():
        key = (key or "").strip()
        if not values:
            if key:
                units.setdefault(key, base)
            continue
        for value in values:
            value = (value or "").strip()
            location = locations.get((file_name, key, value), base)
            # Joined first, deliberately. A value reached through both its
            # own unit and the joined one is deduplicated down to whichever
            # came first, and "UHID PB-11-22" says where the value was read
            # from while the bare "PB-11-22" says only that it was read.
            for text in (f"{key} {value}".strip(), key, value):
                if text:
                    units.setdefault(text, location)
    for table in page.get("tables", []):
        # Row 0 is the header; joining it would only produce a line of column
        # names, which is never the value anyone is after.
        for row in table[1:]:
            joined = " ".join((cell or "").strip() for cell in row).strip()
            if joined:
                units.setdefault(joined, base)
    return list(units.items())


# ------------------------------------------------------------ the values

def values_in(text, compiled, groups):
    """[(group, value), ...] every selected group of every match in `text`.

    An unparticipating group (the `(b)` of `(a)|(b)` when `a` matched) and a
    group that captured only whitespace are both dropped: they are the pattern
    saying "not this branch", not a value worth reporting.

    A group number the pattern does not have is skipped rather than raised -
    validate() rejects that on the form, but a rule saved before its pattern
    was edited down must still evaluate instead of erroring on every claim.
    """
    found = []
    for match in compiled.finditer(text):
        for group in groups:
            try:
                value = match.group(group)
            except IndexError:
                continue
            value = (value or "").strip()
            if value:
                found.append((group, value))
    return found


def extract(docs, compiled, groups, page_filter=None):
    """Every distinct value the pattern finds across a claim's processed
    documents, in document then page order.

    Deduplicated on (document, page, group, value), case-folded. The same
    amount is reached three ways on one page - as the value alone, as the pair
    joined, and as the reversed join - and reporting it three times would say
    something about the index rather than about the claim. The same value on
    two different pages stays two findings: that is the claim repeating
    itself, which is worth seeing.
    """
    findings, seen, truncated = [], set(), False
    for doc in docs:
        cached = doc.get("cached_result")
        if not cached:
            continue
        for page in cached.get("pages", []):
            if page_filter is not None and not page_filter(page):
                continue
            for text, location in text_units(page, doc["rel_path"]):
                for group, value in values_in(text, compiled, groups):
                    fingerprint = (doc["rel_path"], page.get("page_number"), group,
                                   value.casefold())
                    if fingerprint in seen:
                        continue
                    seen.add(fingerprint)
                    findings.append({
                        "value": value, "group": group, "source": text,
                        "file": doc["rel_path"], "page_number": page.get("page_number"),
                        "key_bbox": location.get("key_bbox"),
                        "value_bbox": location.get("value_bbox"),
                    })
                    if len(findings) >= MAX_FINDINGS:
                        return findings, True
    return findings, truncated


# ------------------------------------------------------------ the verdict

# Indian digit grouping ('1,24,500') is just commas as far as this is
# concerned, so no assumption is made about how many digits a group holds.
_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def parse_number(text):
    """The first number in `text`, or None. Tolerant of what sits around it -
    'Rs. 1,24,500/-' is 124500.0 - because an extracted field rarely contains
    the number and nothing else."""
    match = _NUMBER.search(str(text or ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "").rstrip("."))
    except ValueError:
        return None


def compare(value, operator, target, case_insensitive=True):
    """(satisfied, why_not) for one extracted value.

    `why_not` is None when satisfied and a short phrase otherwise, so a failing
    rule can say 'no number in "Nil"' rather than just listing the value.
    """
    kind = OPERATOR_KINDS.get(operator, "text")
    if kind == "number":
        left, right = parse_number(value), parse_number(target)
        if right is None:
            return False, f"'{target}' is not a number"
        if left is None:
            return False, "no number in it"
        satisfied = {
            "num_lt": left < right,
            "num_eq": left == right,
            "num_gt": left > right,
            "num_ne": left != right,
        }[operator]
        # A number that simply failed the test needs no explaining - it is
        # printed beside the comparison. Only the two cases above do.
        return satisfied, None
    left = str(value or "")
    right = str(target or "")
    if case_insensitive:
        left, right = left.casefold(), right.casefold()
    satisfied = {
        "text_eq": left == right,
        "text_contains": right in left,
        "text_not_contains": right not in left,
    }.get(operator, False)
    return satisfied, None


def _plain(number):
    """A float back as a reviewer would write it: 5.0 is 5, 5.5 stays 5.5."""
    return str(int(number)) if float(number).is_integer() else str(number)


def describe(config):
    """One line for the rules list: what is extracted, and what is then asked
    of it."""
    config = normalize(config)
    groups = config["groups"]
    where = ("whole match" if groups == [0]
             else "group " + ", ".join(str(g) for g in groups))
    summary = f'Extract /{config["pattern"]}/ ({where})'
    if not config["case_insensitive"]:
        summary += ", case-sensitive"
    if config["validate"]:
        summary += (f', valid when it {OPERATOR_LABELS[config["operator"]]} '
                    f'"{config["compare_value"]}"')
    else:
        summary += ", no pass/fail"
    if config["fail_when_missing"]:
        summary += "; fails when nothing is found"
    return summary
