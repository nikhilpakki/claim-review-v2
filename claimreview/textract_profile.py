"""Which Textract features and queries a document was analyzed with.

The Textract cache is keyed on document *content* alone, which is what makes
identical bytes cost one analysis no matter how many claims reference them. The
consequence is that changing the requested features or queries does not
invalidate anything: enable SIGNATURES today and every already-processed
document stays as it was, silently reporting no signatures - which a reviewer
would read as "these documents are clean".

So each cached result records the profile it was produced under, and a document
is "stale" only when the *current* profile asks for something the cached result
cannot contain. That direction matters:

- Documents processed before this existed carry no profile. They were analyzed
  with the full five-feature set (LEGACY_FEATURE_TYPES), so they are a superset
  of any narrower profile and are never stale on account of features.
- Narrowing the profile (dropping SIGNATURES, say) makes nothing stale either -
  the data is already there, and it costs nothing to keep showing it.
- Widening it does, and only then, because that is the only case where
  re-running Textract would produce something new.

Nothing is reprocessed automatically: re-analysis is a real AWS charge per
page, so staleness is surfaced and the reviewer decides.
"""
from . import textract_client

# Textract accepts at most 15 queries per analyze_document call.
MAX_QUERIES = 15

# What the response parser extracts, independently of which features were
# requested. Bump this when parsing starts producing something new from the
# same Textract call, because the feature list alone cannot express that:
#
#   0 - forms, tables, signatures, queries. LAYOUT was requested and its blocks
#       were discarded, so those documents have no sections despite listing
#       LAYOUT as a feature.
#   1 - LAYOUT blocks grouped into titled sections (the discharge summary).
PARSER_VERSION = 1

# Extracted only when LAYOUT is requested, so there is nothing to add without it.
PARSER_FEATURE_REQUIREMENTS = {1: "LAYOUT"}

# Settings keys that switch an optional feature on.
FEATURE_SETTINGS = {
    "TABLES": "ENABLE_TEXTRACT_TABLES",
    "SIGNATURES": "ENABLE_TEXTRACT_SIGNATURES",
    "QUERIES": "ENABLE_TEXTRACT_QUERIES",
}

# Always requested; not switchable. FORMS is the key/value backbone and LAYOUT
# is the only route to narrative text, so turning either off would disable most
# of the app rather than tune it.
ALWAYS_ON = ["FORMS", "LAYOUT"]


class QueryConfigError(ValueError):
    """A problem with the custom-queries text, safe to show the user."""


def parse_queries(text):
    """Parse the Advanced-settings queries box into Textract's query list.

    One per line, `Alias: question text`. The alias is what answers are keyed
    by throughout the app (claim_summary reads "Date of Admission" and friends),
    so renaming an alias detaches it from whatever reads it - which is why the
    defaults are offered as the starting point rather than an empty box.
    """
    queries = []
    seen = set()
    for line_number, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        alias, separator, question = line.partition(":")
        if not separator or not alias.strip() or not question.strip():
            raise QueryConfigError(
                f"Line {line_number} is not 'Alias: question text' - got {line!r}"
            )
        alias = alias.strip()
        if alias in seen:
            raise QueryConfigError(f"Duplicate query alias {alias!r} on line {line_number}")
        seen.add(alias)
        queries.append({"Text": question.strip(), "Alias": alias})

    if len(queries) > MAX_QUERIES:
        raise QueryConfigError(
            f"Textract allows at most {MAX_QUERIES} queries per page; got {len(queries)}"
        )
    return queries


def format_queries(queries):
    """Render a query list back into the editable one-per-line form."""
    return "\n".join(f"{q['Alias']}: {q['Text']}" for q in queries or [])


def active_features(settings):
    """The feature list to request, given current settings."""
    features = list(ALWAYS_ON)
    for feature in ("TABLES", "SIGNATURES", "QUERIES"):
        if settings.get(FEATURE_SETTINGS[feature]):
            features.append(feature)
    # Keep the canonical order so a profile comparison is not order-sensitive.
    return [f for f in textract_client.ALL_FEATURE_TYPES if f in features]


def active_queries(settings, default_queries):
    """The queries to send, or [] when queries are switched off.

    A malformed queries box falls back to the defaults rather than failing the
    run: processing a claim with the standard queries is a far better outcome
    than refusing to process it because of a typo in a setting.
    """
    if not settings.get(FEATURE_SETTINGS["QUERIES"]):
        return []
    raw = (settings.get("TEXTRACT_QUERIES") or "").strip()
    if not raw:
        return list(default_queries or [])
    try:
        return parse_queries(raw)
    except QueryConfigError:
        return list(default_queries or [])


def current_profile(settings, default_queries):
    """{features, queries, parser} describing how a document would be analyzed
    and parsed now."""
    queries = active_queries(settings, default_queries)
    return {
        "features": active_features(settings),
        "queries": sorted(q["Alias"] for q in queries),
        "parser": PARSER_VERSION,
    }


def cached_profile(cached):
    """The profile a cached result was produced under.

    Results predating this bookkeeping are attributed to the legacy five-feature
    set and whatever query aliases they actually carry - which is exactly what
    they contain, so nothing is claimed that is not there.
    """
    stored = (cached or {}).get("analysis_profile")
    if stored:
        return {
            "features": list(stored.get("features") or []),
            "queries": sorted(stored.get("queries") or []),
            # A profile written before parser versioning came from the parser
            # that did extract sections, so absence here means 0.
            "parser": int(stored.get("parser") or 0),
        }
    aliases = set()
    for page in (cached or {}).get("pages", []):
        aliases.update((page.get("queries") or {}).keys())
    return {"features": list(textract_client.LEGACY_FEATURE_TYPES),
            "queries": sorted(aliases), "parser": 0}


def missing_from_cache(cached, profile):
    """What the current profile asks for that this cached result cannot have.

    Returns {features: [...], queries: [...]}; empty lists mean nothing is
    missing and re-analysis would tell the reviewer nothing new.
    """
    stored = cached_profile(cached)
    # Parser steps whose output this result cannot contain: newer than what
    # produced it, and only counted when the feature they read is requested.
    parser_gains = [
        version for version in range(stored.get("parser", 0) + 1, profile.get("parser", 0) + 1)
        if PARSER_FEATURE_REQUIREMENTS.get(version) in profile["features"]
    ]
    return {
        "features": [f for f in profile["features"] if f not in stored["features"]],
        "queries": [q for q in profile["queries"] if q not in stored["queries"]],
        "parser_gains": parser_gains,
    }


def is_stale(cached, profile):
    missing = missing_from_cache(cached, profile)
    return bool(missing["features"] or missing["queries"] or missing.get("parser_gains"))


def describe_missing(missing):
    """One short phrase naming what a reprocess would add."""
    parts = []
    if missing["features"]:
        parts.append(", ".join(missing["features"]))
    if missing["queries"]:
        parts.append(f"{len(missing['queries'])} query answer(s)")
    if missing.get("parser_gains"):
        parts.append("document sections (needed for the discharge summary)")
    return " and ".join(parts)
