"""A reviewer's own shortlist of claims, under labels they choose.

Going through a few hundred claims, the useful thing is to be able to set one
aside and come back to it - so bookmarking is personal, and nobody sees anybody
else's. That is the one rule this module enforces everywhere: every read and
write is scoped by user_id, with no unscoped variant to reach for by mistake.

Labels are stored on the bookmark row rather than in a table of names. A label
exists exactly as long as some claim carries it, so there is nothing to prune
and a name typed by mistake goes away with the bookmark that introduced it. The
four defaults below are always offered even though no row has ever used them.
"""
from flask import current_app

from .db import get_db

# Offered to everyone, first, before whatever they have made up themselves.
# These are the four the reviewers asked for; they are not written to the
# database, so they cost nothing until used and can be changed here freely.
DEFAULT_LABELS = ["Suspicious", "Abuse", "To be reviewed", "Other"]

MAX_LABEL = 60


class BookmarkError(ValueError):
    """A bookmark that cannot be saved as described."""


def _clean_label(label):
    label = " ".join(str(label or "").split())
    if not label:
        raise BookmarkError("Choose a bookmark name, or type a new one.")
    if len(label) > MAX_LABEL:
        raise BookmarkError(f"Keep the bookmark name under {MAX_LABEL} characters.")
    return label


def _canonical(label, known):
    """Match a typed label to an existing one differing only in case.

    Without this, "suspicious" and "Suspicious" become two separate groups on
    the Bookmarks page, which reads as a bug rather than a choice.
    """
    lowered = label.lower()
    for existing in known:
        if existing.lower() == lowered:
            return existing
    return label


def labels_for(user_id):
    """Every label this user can pick from: the defaults, then their own.

    Ordered defaults-first so the common choices stay where the reviewer
    expects them however many labels they have invented.
    """
    rows = get_db().execute(
        "SELECT DISTINCT label FROM bookmarks WHERE user_id=? ORDER BY label",
        (user_id,)).fetchall()
    theirs = [row["label"] for row in rows]
    lowered = {label.lower() for label in DEFAULT_LABELS}
    return DEFAULT_LABELS + [label for label in theirs if label.lower() not in lowered]


def add(user_id, claim_id, label, note=None):
    """Bookmark a claim under `label`. Idempotent.

    Returns the label as stored, which may differ in case from what was typed -
    see _canonical.
    """
    if not user_id:
        raise BookmarkError("Sign in to bookmark a claim.")
    label = _canonical(_clean_label(label), labels_for(user_id))
    db = get_db()
    db.execute(
        "INSERT INTO bookmarks (user_id, claim_id, label, note) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, claim_id, label) DO UPDATE SET note=excluded.note",
        (user_id, str(claim_id), label, (note or "").strip() or None))
    db.commit()
    return label


def remove(user_id, claim_id, label):
    db = get_db()
    removed = db.execute(
        "DELETE FROM bookmarks WHERE user_id=? AND claim_id=? AND label=?",
        (user_id, str(claim_id), label)).rowcount
    db.commit()
    return removed


def labels_for_claim(user_id, claim_id):
    """The labels this user has already filed this claim under."""
    if not user_id:
        return []
    rows = get_db().execute(
        "SELECT label FROM bookmarks WHERE user_id=? AND claim_id=? ORDER BY label",
        (user_id, str(claim_id))).fetchall()
    return [row["label"] for row in rows]


def grouped(user_id):
    """[{label, claims: [{claim_id, note, created_at}]}] for this user.

    Grouped in Python rather than by a second query per label: a reviewer's
    bookmark list is small, and one pass keeps the ordering - defaults first,
    then their own labels - identical to the picker on the claim page.
    """
    if not user_id:
        return []
    rows = get_db().execute(
        "SELECT claim_id, label, note, created_at FROM bookmarks "
        "WHERE user_id=? ORDER BY created_at DESC, id DESC", (user_id,)).fetchall()
    by_label = {}
    for row in rows:
        by_label.setdefault(row["label"], []).append({
            "claim_id": row["claim_id"], "note": row["note"],
            "created_at": row["created_at"],
        })
    # Defaults first (only if used), then anything else alphabetically.
    ordered = [label for label in DEFAULT_LABELS if label in by_label]
    ordered += sorted(label for label in by_label if label not in ordered)
    return [{"label": label, "claims": by_label[label]} for label in ordered]


def count_for(user_id):
    if not user_id:
        return 0
    return get_db().execute(
        "SELECT COUNT(*) FROM bookmarks WHERE user_id=?", (user_id,)).fetchone()[0]


def claim_exists(claim_id):
    """Whether a bookmarked claim is still in the reviewer's active folder.

    Bookmarks outlive the folder they were made in - a claim's bundle can be
    deleted to reclaim disk, or the reviewer can point at a different root - so
    the Bookmarks page says which entries can still be opened instead of
    offering links that 404.
    """
    from . import root_state
    try:
        root_state.get_claim_path(claim_id)
        return True
    except (ValueError, FileNotFoundError):
        return False
    except Exception:  # no active folder set yet, etc.
        current_app.logger.debug("bookmark existence check failed", exc_info=True)
        return False
