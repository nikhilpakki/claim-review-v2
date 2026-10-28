"""Admin-defined page tags, on top of the four built-in ones.

A tag is a name and a regex. A page carries the tag when the pattern is found
in that page's extracted text - the same text a keyword rule searches - which
is what lets a rule be scoped to, say, only the bills in a claim rather than to
every document in it.

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

from . import search
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

def list_tags():
    """Every tag, by name. Ordered so the rule form's checkboxes do not move
    around as tags are added."""
    rows = get_db().execute("SELECT * FROM doc_tags ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(row) for row in rows]


def get_tag(tag_id):
    row = get_db().execute("SELECT * FROM doc_tags WHERE id=?", (tag_id,)).fetchone()
    return dict(row) if row else None


def tag_names():
    return [tag["name"] for tag in list_tags()]


def signature():
    """What the rollup cache keys on, so editing a tag's pattern recomputes the
    badges it affects. Covers everything that changes which pages match."""
    return [(t["id"], t["name"], t["pattern"], t["case_sensitive"]) for t in list_tags()]


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
    try:
        re.compile(pattern)
    except re.error as exc:
        # Caught here rather than at match time: a pattern that cannot compile
        # would otherwise fail silently on every page, and the tag would simply
        # never appear with nothing to say why.
        raise DocTagError(f"That is not a valid regular expression: {exc}") from exc

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
        "case_sensitive": 1 if form.get("case_sensitive") in ("on", "1", "true", True) else 0,
    }


def create_tag(form, user=None):
    fields = _validated(form)
    db = get_db()
    cursor = db.execute(
        "INSERT INTO doc_tags (name, description, pattern, case_sensitive, "
        "created_by_user_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (fields["name"], fields["description"], fields["pattern"], fields["case_sensitive"],
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
        "UPDATE doc_tags SET name=?, description=?, pattern=?, case_sensitive=?, updated_at=? "
        "WHERE id=?",
        (fields["name"], fields["description"], fields["pattern"], fields["case_sensitive"],
         _now(), tag_id))
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
    """[(name, compiled_pattern)] - compiled once per evaluation rather than
    once per page, since a claim can run to hundreds of pages."""
    out = []
    for tag in (list_tags() if tags is None else tags):
        flags = 0 if tag["case_sensitive"] else re.IGNORECASE
        try:
            out.append((tag["name"], re.compile(tag["pattern"], flags)))
        except re.error:
            # Saved patterns are validated, but a row edited by hand should not
            # take the claims page down with it.
            current_app.logger.warning("doc tag %r has an invalid pattern", tag["name"])
    return out


def tags_for_page(page, patterns):
    """The custom tags a page carries.

    Matched against the same extracted text a keyword rule searches - form
    keys and values, query answers, table cells and LAYOUT section text - so a
    tag finds what a reviewer reading the page would.
    """
    if not patterns:
        return set()
    kvs = search.page_kv_dict(page)
    if not kvs:
        return set()
    haystack = []
    for key, values in kvs.items():
        if key:
            haystack.append(key)
        haystack.extend(v for v in values if v)
    text = "\n".join(haystack)
    return {name for name, pattern in patterns if pattern.search(text)}
