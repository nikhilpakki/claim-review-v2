"""Pages a reviewer marked as proof, and the copy attached to their decision.

A decision of 'flagged' with a paragraph of notes says what the reviewer
concluded. It does not say what they were looking at when they concluded it,
and the next person to pick the claim up has to find that themselves. Marking
a page while reading it is the cheapest moment to record it.

Two stages, deliberately separate:

  - `evidence_marks` is the working list, personal to one reviewer and one
    claim. It is theirs to add to and remove from, like a bookmark.
  - `review_evidence` is what a submitted decision carries. It is a copy
    taken at submission, so re-marking a page next week cannot rewrite what
    a decision made last week was based on.

Everything here is scoped by user_id, with no unscoped variant to reach for
by mistake - the same rule bookmarks.py enforces, and for the same reason.
"""
from .db import get_db

MAX_NOTE = 300
# A claim with hundreds of marked pages is not evidence, it is a copy of the
# claim. The cap is high enough never to be met honestly and low enough that
# a stuck key cannot fill the table.
MAX_MARKS = 200


class EvidenceError(ValueError):
    """A mark that cannot be saved as described."""


def _clean_note(note):
    note = " ".join(str(note or "").split())
    if len(note) > MAX_NOTE:
        raise EvidenceError(f"Keep the note under {MAX_NOTE} characters.")
    return note or None


def _clean_page(page_number):
    try:
        page = int(page_number)
    except (TypeError, ValueError):
        raise EvidenceError("That page number is not a number.") from None
    if page < 1:
        raise EvidenceError("Pages are numbered from 1.")
    return page


def list_marks(user_id, claim_id):
    """This reviewer's marked pages for this claim, in document then page
    order - the order they would be read in, not the order they were found."""
    if not user_id:
        return []
    rows = get_db().execute(
        "SELECT document_path, page_number, note, created_at FROM evidence_marks "
        "WHERE user_id=? AND claim_id=? ORDER BY document_path, page_number",
        (user_id, str(claim_id))).fetchall()
    return [dict(row) for row in rows]


def is_marked(user_id, claim_id, document_path, page_number):
    """The mark on one page, or None. Returned rather than a bare bool so the
    preview can show the note it already carries."""
    if not user_id:
        return None
    row = get_db().execute(
        "SELECT document_path, page_number, note, created_at FROM evidence_marks "
        "WHERE user_id=? AND claim_id=? AND document_path=? AND page_number=?",
        (user_id, str(claim_id), document_path, _clean_page(page_number))).fetchone()
    return dict(row) if row else None


def mark(user_id, claim_id, document_path, page_number, note=None):
    """Mark a page. Idempotent, and marking an already-marked page is how its
    note is edited - there is no separate update to get out of step."""
    if not user_id:
        raise EvidenceError("Sign in to mark a page as evidence.")
    document_path = (document_path or "").strip()
    if not document_path:
        raise EvidenceError("A mark needs a document.")
    page = _clean_page(page_number)
    note = _clean_note(note)
    db = get_db()
    existing = db.execute(
        "SELECT COUNT(*) AS n FROM evidence_marks WHERE user_id=? AND claim_id=?",
        (user_id, str(claim_id))).fetchone()["n"]
    already = is_marked(user_id, claim_id, document_path, page)
    if existing >= MAX_MARKS and not already:
        raise EvidenceError(
            f"That is {MAX_MARKS} marked pages on one claim - remove some before "
            "adding more.")
    db.execute(
        "INSERT INTO evidence_marks (user_id, claim_id, document_path, page_number, note) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, claim_id, document_path, page_number) "
        "DO UPDATE SET note=excluded.note",
        (user_id, str(claim_id), document_path, page, note))
    db.commit()


def unmark(user_id, claim_id, document_path, page_number):
    """Remove one mark. Silent when there was none - the button is a toggle,
    and double-clicking it is not an error worth reporting."""
    if not user_id:
        return
    db = get_db()
    db.execute(
        "DELETE FROM evidence_marks WHERE user_id=? AND claim_id=? "
        "AND document_path=? AND page_number=?",
        (user_id, str(claim_id), (document_path or "").strip(),
         _clean_page(page_number)))
    db.commit()


def clear(user_id, claim_id):
    if not user_id:
        return
    db = get_db()
    db.execute("DELETE FROM evidence_marks WHERE user_id=? AND claim_id=?",
               (user_id, str(claim_id)))
    db.commit()


def attach_to_review(review_id, user_id, claim_id):
    """Copy this reviewer's marks onto a submitted decision, then clear them.

    Both halves or neither: the copy and the clear are one transaction, so a
    failure cannot leave a decision with evidence the reviewer still appears
    to be holding, or a cleared list with nothing recorded.

    Returns how many were attached.
    """
    marks = list_marks(user_id, claim_id)
    if not marks:
        return 0
    db = get_db()
    db.executemany(
        "INSERT INTO review_evidence (review_id, document_path, page_number, note) "
        "VALUES (?, ?, ?, ?)",
        [(review_id, m["document_path"], m["page_number"], m["note"]) for m in marks])
    db.execute("DELETE FROM evidence_marks WHERE user_id=? AND claim_id=?",
               (user_id, str(claim_id)))
    db.commit()
    return len(marks)


def for_reviews(review_ids):
    """{review_id: [evidence, ...]} for a list of decisions, in one query.

    One query rather than one per decision: the Review tab renders the whole
    history at once, and a claim reviewed a dozen times would otherwise do a
    dozen round trips to show it.
    """
    review_ids = [int(rid) for rid in review_ids or []]
    if not review_ids:
        return {}
    placeholders = ",".join("?" for _ in review_ids)
    rows = get_db().execute(
        f"SELECT review_id, document_path, page_number, note FROM review_evidence "
        f"WHERE review_id IN ({placeholders}) ORDER BY document_path, page_number",
        review_ids).fetchall()
    out = {}
    for row in rows:
        out.setdefault(row["review_id"], []).append({
            "document_path": row["document_path"],
            "page_number": row["page_number"],
            "note": row["note"],
        })
    return out
