"""Admin-defined page tags, on top of the four built-in ones.

A tag is a name and a regex. A page carries the tag when the pattern is found
in that page's extracted text, which is what lets a rule be scoped to, say,
only the bills in a claim rather than to every document in it.

Found means exactly what it means for a rule's regex keyword: the same text,
the same per-field matching, and the same tolerant second look at the plain
words inside the pattern when the pattern itself does not match. The two share
search.literal_terms and search._pattern_hits so they cannot drift - a pattern
that finds a page as a rule has to find it as a tag, or an admin would be
writing the same regex twice and getting two answers.

Tags are central and global, like the rules they scope. A tag that meant
different things to different reviewers would make two people's badges disagree
with no way to tell why, so only an administrator can manage them, and everyone
sees the same set.

How a custom tag behaves in a rule's Document scope is deliberately *not* the
same as a built-in one, and rules_engine._scope_page_filter is where that
lives: the built-ins are a subset test ("this page must be nothing but the
kinds I ticked") and adding custom tags to that test would have silently
excluded every newly-tagged page from every existing rule.
"""
import re

from flask import current_app

from . import matching, search
from .db import get_db

# The tags classify.py works out from the image and its text. A custom tag
# cannot take one of these names: the two are matched differently, and a rule
# scoped to "photo" must keep meaning what it has always meant.
BUILTIN_NAMES = {"document", "photo", "gps photo", "has face"}

MAX_NAME = 40
MAX_PATTERN = 500


class DocTagError(ValueError):
    """A tag that cannot be saved as described."""


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def can_manage(user):
    """Only administrators. Tags change what every rule sees."""
    return bool(user and user["is_admin"])


# --------------------------------------------------------------------- read

def match_config(row):
    """The matching controls stored on a tag row, as matching.py wants them.

    A row written before the controls existed has the column defaults, which
    are the new defaults - so an old tag reads as a new one rather than as a
    special case anywhere downstream.
    """
    def column(name, default=None):
        # Rows arrive as sqlite3.Row from the database and as plain dicts
        # from callers building one by hand, and neither supports the other's
        # way of asking for a column that may not be there.
        try:
            value = row[name]
        except (KeyError, IndexError):
            return default
        return default if value is None else value

    return matching.normalize({
        "case_insensitive": not column("case_sensitive", 0),
        "regex": column("match_regex", 1),
        "exact": column("match_exact", 1),
        "fuzzy": column("fuzzy_methods", ",".join(matching.TAG_DEFAULTS["fuzzy"])),
        "fuzzy_threshold": column("fuzzy_threshold"),
    }, matching.TAG_DEFAULTS)


def _as_tag(row):
    """A row as every caller wants it: the stored columns, plus the controls
    they add up to, the phrases the pattern yields and a line describing
    both. Built in one place so the edit form cannot show something different
    from the list above it, which it did when only the list was enriched.
    """
    config = match_config(row)
    return dict(row, match=config,
                terms=matching.derive_terms(row["pattern"], config["regex"]),
                summary=matching.describe(config))


def list_tags():
    """Every tag, by name. Ordered so the rule form's checkboxes do not move
    around as tags are added."""
    rows = get_db().execute("SELECT * FROM doc_tags ORDER BY name COLLATE NOCASE").fetchall()
    return [_as_tag(row) for row in rows]


def get_tag(tag_id):
    row = get_db().execute("SELECT * FROM doc_tags WHERE id=?", (tag_id,)).fetchone()
    return _as_tag(row) if row else None


def tag_names():
    return [tag["name"] for tag in list_tags()]


def signature():
    """What the rollup cache keys on, so editing a tag's pattern recomputes the
    badges it affects. Covers everything that changes which pages match."""
    return [(t["id"], t["name"], t["pattern"], t["case_sensitive"],
             t["match"]["regex"], t["match"]["exact"],
             tuple(t["match"]["fuzzy"]), t["match"]["fuzzy_threshold"])
            for t in list_tags()]


# -------------------------------------------------------------------- write

def _validated(form, tag_id=None):
    name = " ".join((form.get("name") or "").split())
    if not name:
        raise DocTagError("Give the tag a name.")
    if len(name) > MAX_NAME:
        raise DocTagError(f"Keep the tag name under {MAX_NAME} characters.")
    if name.lower() in BUILTIN_NAMES:
        raise DocTagError(
            f"'{name}' is one of the built-in tags; choose a different name.")

    pattern = (form.get("pattern") or "").strip()
    if not pattern:
        raise DocTagError("Give the tag a pattern to look for.")
    if len(pattern) > MAX_PATTERN:
        raise DocTagError(f"Keep the pattern under {MAX_PATTERN} characters.")
    config = matching.from_form(form, defaults=matching.TAG_DEFAULTS)
    try:
        # Caught here rather than at match time: a tag that cannot match would
        # otherwise fail silently on every page, and simply never appear with
        # nothing to say why. validate() also refuses the combination with
        # every stage switched off, which is the other way to get that.
        problem = matching.validate(pattern, config)
    except search.InvalidKeywordPattern as exc:
        raise DocTagError(f"That is not a valid regular expression: {exc}") from exc
    if problem:
        raise DocTagError(problem)

    db = get_db()
    clash = db.execute(
        "SELECT id FROM doc_tags WHERE name = ? COLLATE NOCASE AND id IS NOT ?",
        (name, tag_id)).fetchone()
    if clash:
        raise DocTagError(f"A tag called '{name}' already exists.")

    return {
        "name": name,
        "description": (form.get("description") or "").strip() or None,
        "pattern": pattern,
        "case_sensitive": 0 if config["case_insensitive"] else 1,
        "match_regex": 1 if config["regex"] else 0,
        "match_exact": 1 if config["exact"] else 0,
        "fuzzy_methods": ",".join(config["fuzzy"]),
        "fuzzy_threshold": config["fuzzy_threshold"],
    }


def create_tag(form, user=None):
    fields = _validated(form)
    db = get_db()
    cursor = db.execute(
        "INSERT INTO doc_tags (name, description, pattern, case_sensitive, "
        "match_regex, match_exact, fuzzy_methods, fuzzy_threshold, "
        "created_by_user_id, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (fields["name"], fields["description"], fields["pattern"], fields["case_sensitive"],
         fields["match_regex"], fields["match_exact"], fields["fuzzy_methods"],
         fields["fuzzy_threshold"],
         user["id"] if user else None,
         (user["display_name"] or user["username"]) if user else None, _now()))
    db.commit()
    return cursor.lastrowid


def update_tag(tag_id, form):
    if get_tag(tag_id) is None:
        raise DocTagError("That tag no longer exists.")
    fields = _validated(form, tag_id=tag_id)
    db = get_db()
    db.execute(
        "UPDATE doc_tags SET name=?, description=?, pattern=?, case_sensitive=?, "
        "match_regex=?, match_exact=?, fuzzy_methods=?, fuzzy_threshold=?, updated_at=? "
        "WHERE id=?",
        (fields["name"], fields["description"], fields["pattern"], fields["case_sensitive"],
         fields["match_regex"], fields["match_exact"], fields["fuzzy_methods"],
         fields["fuzzy_threshold"], _now(), tag_id))
    db.commit()


def delete_tag(tag_id):
    """Delete a tag, and report the rules that were scoped to it.

    The rules are left alone: a rule naming a tag that no longer exists simply
    stops narrowing by it, which is the same as not having been narrowed.
    Rewriting other people's rules as a side effect of deleting a tag would be
    the more surprising behaviour, so the caller is told instead.
    """
    tag = get_tag(tag_id)
    if tag is None:
        return []
    affected = rules_using(tag["name"])
    db = get_db()
    db.execute("DELETE FROM doc_tags WHERE id=?", (tag_id,))
    db.commit()
    return affected


def rules_using(name):
    """Names of rules whose document scope mentions this tag."""
    import json
    rows = get_db().execute("SELECT name, config_json FROM rules").fetchall()
    used = []
    for row in rows:
        try:
            config = json.loads(row["config_json"])
        except (TypeError, ValueError):
            continue
        scope = config.get("doc_scope_tags") or []
        if any(str(t).lower() == name.lower() for t in scope):
            used.append(row["name"])
    return used


# ------------------------------------------------------------------- match

def compiled(tags=None):
    """[(name, pattern, config, prepared)] - prepared once per evaluation
    rather than once per page, since a claim can run to hundreds of pages.

    The controls travel with the pattern so a tag is matched exactly the way a
    rule's keyword is: both hand the same three things to matching.run. See
    matching.py, which is the only place that decides what finding means.
    """
    out = []
    for tag in (list_tags() if tags is None else tags):
        config = tag["match"] if "match" in tag else match_config(tag)
        try:
            prepared = (matching.compile_pattern(tag["pattern"], config),
                        matching.derive_terms(tag["pattern"], config["regex"]))
        except search.InvalidKeywordPattern:
            # Saved patterns are validated, but a row edited by hand should
            # not take the claims page down with it.
            current_app.logger.warning("doc tag %r has an invalid pattern", tag["name"])
            continue
        out.append((tag["name"], tag["pattern"], config, prepared))
    return out


def tags_for_page(page, patterns):
    """The custom tags a page carries.

    Matched exactly as a rule's keyword is matched, over the same extracted
    text - form keys and values, query answers, table cells and LAYOUT
    section lines - by whichever of the stages the tag has switched on:

      1. the pattern, against each field on its own. Not against the page's
         text joined together: joined, ^ and $ mean the start and end of the
         whole page, so a tag written ^(final bill|invoice) could only ever
         match the page's first field, and a pattern could run across two
         unrelated fields that happen to be adjacent.
      2. the plain phrases inside the pattern, looked for literally.
      3. those same phrases, looked for tolerantly, so "FlNAL BILL" - which a
         reviewer reads without noticing - still carries the bill tag.

    The tolerant stages compare lower-cased text, so a case-sensitive tag's
    pattern is respected but its fallback is not. That is what a rule's
    keyword already does, and a tag that behaved differently from the same
    words written as a rule is the thing this is here to prevent.
    """
    if not patterns:
        return set()
    kvs = search.page_kv_dict(page)
    if not kvs:
        return set()
    documents = {"page": kvs}
    found = set()
    for name, pattern, config, prepared in patterns:
        if matching.run(documents, pattern, config, prepared=prepared)["best"]:
            found.add(name)
    return found


def matches_for_page(page, patterns, file_name="page"):
    """{tag: [hit, ...]} - which fields on this page each tag matched.

    tags_for_page() answers whether the badge belongs on the page; this
    answers why, so a reviewer can check a tag instead of taking it on faith.
    Both run matching.run over the same primitives, so what is shown here is
    necessarily what put the badge there.

    Deliberately not short-circuited, and deliberately not what the badge is
    computed from: this runs for one document when someone clicks its tag,
    while tags_for_page runs for every page of every claim on the list.
    """
    if not patterns:
        return {}
    kvs = search.page_kv_dict(page)
    if not kvs:
        return {}
    documents = {file_name: kvs}
    locations = search.page_locations(page, file_name)
    found = {}
    for name, pattern, config, prepared in patterns:
        hits = _one_per_field(matching.run(documents, pattern, config,
                                           locations, prepared)["matches"])
        if hits:
            found[name] = hits
    return found


def _one_per_field(hits):
    """One finding per piece of text that matched, however often it is paired.

    The finding is the text the tag matched, so that is what it is counted by.
    Two things otherwise report the same match several times over:

    page_kv_dict indexes a form pair both ways round, so a pattern matching
    "DECLARATION CERTIFICATE" is found once with it as the key and once as the
    value of the reversed pair. Both are true, and in a panel explaining one
    badge they read as two separate findings, which is simply wrong.

    A key also repeats once per value Textract paired it with. A heading
    matched on the key side, sitting above twenty rows, is one finding about
    that heading - not twenty findings that happen to quote twenty unrelated
    values beside it.

    The first of each is kept, which is the pair the way Textract gave it.
    """
    seen, out = set(), []
    for hit in hits:
        # The side is deliberately not part of this. A pair indexed both ways
        # round reports the same text once as a key and once as a value, and
        # those are one finding, not two.
        matched = hit["key"] if hit.get("matched_on") == "key" else hit["value"]
        fingerprint = (hit.get("page_number"), matched or "")
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(hit)
    return out
