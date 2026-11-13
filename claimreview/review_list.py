"""Every decision on the board, whoever recorded it.

my_reviews.py answers "what have I decided", scoped to one person and grouped
by campaign. This answers the other question - "what has the team decided" -
and it is a different shape: one flat list, filterable by who and by verdict,
sortable by when, and selectable, because what a reviewer does with it next is
export a batch of cases into a validation summary.

Two things this has to be careful about.

reviews is append-only. A reviewer who looks again and changes their mind adds
a row rather than replacing one, so a claim can carry several decisions from
the same person. The one that stands is the newest; the earlier ones are
history and are hidden unless asked for, because a list that shows both
without saying which is which reports the same claim twice with two different
verdicts.

An administrator editing somebody else's row is the one operation here that is
not append-only, so every edit and every deletion writes a review_audit row
first. Nothing is silently rewritten: the page shows what a decision used to
say and who changed it.
"""
from .db import get_db

STATUSES = ("approved", "flagged", "rejected")

# How a verdict is worded in an exported validation summary. 'flagged' is the
# tool's word for "this needs a second look", which on a case summary reads as
# Suspicious - the other two carry over unchanged.
OUTCOME_LABELS = {
    "approved": "Approved",
    "flagged": "Suspicious",
    "rejected": "Rejected",
}

SORTS = {
    "newest": "r.created_at DESC, r.id DESC",
    "oldest": "r.created_at ASC, r.id ASC",
    "claim": "r.claim_id ASC, r.id DESC",
    "reviewer": "reviewer_name COLLATE NOCASE ASC, r.created_at DESC, r.id DESC",
}
DEFAULT_SORT = "newest"

# A ceiling rather than paging. The page is a work queue for picking a batch
# to export, not an archive to browse, and the filters are the way to narrow
# it. If a deployment ever passes this the list says so instead of quietly
# showing part of the answer.
MAX_ROWS = 2000

# Legacy rows predate reviews.reviewer_user_id and carry only a typed name, so
# they all share this id when deciding which decision supersedes which. That
# groups the unattributed ones per claim together, which is the closest honest
# reading available: there is no way to tell two of them apart.
UNATTRIBUTED = -1

_SELECT = f"""
SELECT r.id, r.claim_id, r.document_path, r.status, r.notes, r.created_at,
       r.reviewer, r.reviewer_user_id,
       COALESCE(NULLIF(u.display_name, ''), u.username, NULLIF(r.reviewer, ''),
                'unknown') AS reviewer_name,
       (SELECT COUNT(*) FROM review_evidence e WHERE e.review_id = r.id)
           AS evidence_count,
       (SELECT COUNT(*) FROM review_audit a
         WHERE a.review_id = r.id AND a.action = 'edited') AS edit_count,
       CASE WHEN r.id = (
              SELECT MAX(r2.id) FROM reviews r2
               WHERE r2.claim_id = r.claim_id
                 AND COALESCE(r2.reviewer_user_id, {UNATTRIBUTED})
                     = COALESCE(r.reviewer_user_id, {UNATTRIBUTED}))
            THEN 0 ELSE 1 END AS superseded
  FROM reviews r
  LEFT JOIN users u ON u.id = r.reviewer_user_id
"""


def _clean(value):
    return (value or "").strip()


def search(status=None, reviewer_user_id=None, claim_query=None,
           hypothesis_id=None, sort=DEFAULT_SORT, include_superseded=False,
           limit=MAX_ROWS):
    """The decisions matching these filters, newest first by default.

    `reviewer_user_id` may be UNATTRIBUTED to ask for the legacy rows that
    carry a typed name and no account.
    """
    where, params = [], []
    if status in STATUSES:
        where.append("r.status = ?")
        params.append(status)
    if reviewer_user_id is not None:
        if int(reviewer_user_id) == UNATTRIBUTED:
            where.append("r.reviewer_user_id IS NULL")
        else:
            where.append("r.reviewer_user_id = ?")
            params.append(int(reviewer_user_id))
    claim_query = _clean(claim_query)
    if claim_query:
        # A claim id is typed in full or not at all in practice, but a partial
        # one should still find it rather than silently match nothing.
        where.append("r.claim_id LIKE ?")
        params.append(f"%{claim_query}%")
    if not include_superseded:
        where.append(f"""r.id = (SELECT MAX(r3.id) FROM reviews r3
                                  WHERE r3.claim_id = r.claim_id
                                    AND COALESCE(r3.reviewer_user_id, {UNATTRIBUTED})
                                        = COALESCE(r.reviewer_user_id, {UNATTRIBUTED}))""")

    sql = _SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + SORTS.get(sort, SORTS[DEFAULT_SORT])
    sql += " LIMIT ?"
    params.append(int(limit) + 1)

    rows = [dict(row) for row in get_db().execute(sql, params)]
    truncated = len(rows) > limit
    rows = rows[:limit]

    by_claim = hypotheses_by_claim()
    for row in rows:
        row["superseded"] = bool(row["superseded"])
        row["hypotheses"] = by_claim.get(row["claim_id"], [])
    if hypothesis_id is not None:
        hypothesis_id = int(hypothesis_id)
        rows = [r for r in rows
                if any(h["id"] == hypothesis_id for h in r["hypotheses"])]
    return rows, truncated


def hypotheses_by_claim():
    """{claim_id: [{id, title}, ...]} - which campaigns a claim was fetched for.

    A claim fetched twice under two hypotheses belongs to both, the same
    reading my_reviews.py takes.
    """
    out = {}
    for row in get_db().execute("""
            SELECT DISTINCT c.registration_id AS claim_id, h.id, h.title
              FROM fetch_run_claims c
              JOIN fetch_runs r ON r.run_id = c.run_id
              JOIN hypotheses h ON h.id = r.hypothesis_id"""):
        out.setdefault(row["claim_id"], []).append(
            {"id": row["id"], "title": row["title"]})
    return out


def reviewers():
    """Everyone who has recorded a decision, for the filter.

    Taken from reviews rather than from users: an account that was removed
    still has decisions on the board, and leaving them unselectable would make
    a whole reviewer's work unfilterable.
    """
    rows = get_db().execute(f"""
        SELECT COALESCE(r.reviewer_user_id, {UNATTRIBUTED}) AS user_id,
               COALESCE(NULLIF(u.display_name, ''), u.username,
                        NULLIF(r.reviewer, ''), 'unknown') AS name,
               COUNT(*) AS decisions
          FROM reviews r
          LEFT JOIN users u ON u.id = r.reviewer_user_id
         GROUP BY COALESCE(r.reviewer_user_id, {UNATTRIBUTED}), name
         ORDER BY name COLLATE NOCASE""").fetchall()
    return [dict(row) for row in rows]


def hypothesis_options():
    """Campaigns that have at least one claim, for the filter."""
    rows = get_db().execute("""
        SELECT h.id, h.title FROM hypotheses h
         WHERE EXISTS (SELECT 1 FROM fetch_runs r WHERE r.hypothesis_id = h.id)
         ORDER BY h.title COLLATE NOCASE""").fetchall()
    return [dict(row) for row in rows]


def counts(rows):
    """{status: n} over a list of rows, plus the claims they cover.

    Claims, not rows: two reviewers deciding the same claim is two decisions
    but one case, and the export's cover page counts cases.
    """
    by_status, claims = {}, set()
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        claims.add(row["claim_id"])
    return {"by_status": by_status, "decisions": len(rows), "claims": len(claims)}


def get(review_id):
    row = get_db().execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    return dict(row) if row else None


def get_many(review_ids):
    """Full rows for an explicit selection, in claim order.

    Claim order, not selection order: the export numbers its cases, and the
    order checkboxes happened to be ticked in is not an order anybody reading
    the document would recognise.
    """
    review_ids = [int(rid) for rid in review_ids or []]
    if not review_ids:
        return []
    placeholders = ",".join("?" for _ in review_ids)
    rows = get_db().execute(
        _SELECT + f" WHERE r.id IN ({placeholders}) ORDER BY r.claim_id, r.id",
        review_ids).fetchall()
    return [dict(row) for row in rows]


def last_edits(review_ids):
    """{review_id: audit row} - the most recent edit of each of these rows.

    Only the latest: the page says "edited by X, was Y", and an edit of an
    edit is still best described by what it replaced.
    """
    review_ids = [int(rid) for rid in review_ids or []]
    if not review_ids:
        return {}
    placeholders = ",".join("?" for _ in review_ids)
    rows = get_db().execute(f"""
        SELECT a.* FROM review_audit a
         WHERE a.action = 'edited' AND a.review_id IN ({placeholders})
           AND a.id = (SELECT MAX(a2.id) FROM review_audit a2
                        WHERE a2.review_id = a.review_id AND a2.action = 'edited')
        """, review_ids).fetchall()
    return {row["review_id"]: dict(row) for row in rows}


def deletions(limit=50):
    """Decisions an administrator has removed, most recent first.

    Shown on the page because a deleted decision leaves no other trace: the
    claim simply stops appearing, and somebody looking for their own work
    deserves to see that it was removed rather than conclude it was never
    saved.
    """
    rows = get_db().execute(
        "SELECT * FROM review_audit WHERE action='deleted' "
        "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(row) for row in rows]


# ------------------------------------------------------- administrator edits


class ReviewError(ValueError):
    """A change that cannot be applied as described."""


def _actor(user):
    if not user:
        return None, None
    return user["id"], (user["display_name"] or user["username"])


def update(review_id, status, notes, actor_user):
    """Change a decision, recording what it said before.

    The audit row is written in the same transaction as the change, so there
    is no window in which the row has been rewritten and nothing says what it
    replaced.
    """
    status = _clean(status)
    if status not in STATUSES:
        raise ReviewError(f"Status must be one of {', '.join(STATUSES)}.")
    notes = _clean(notes)

    db = get_db()
    row = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    if row is None:
        raise ReviewError("That decision no longer exists.")
    if row["status"] == status and (row["notes"] or "") == notes:
        return False

    actor_id, actor_name = _actor(actor_user)
    db.execute(
        "INSERT INTO review_audit (review_id, claim_id, action, old_status, "
        "new_status, old_notes, new_notes, reviewer, reviewer_user_id, actor, "
        "actor_user_id) VALUES (?, ?, 'edited', ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["id"], row["claim_id"], row["status"], status, row["notes"], notes,
         row["reviewer"], row["reviewer_user_id"], actor_name, actor_id))
    db.execute("UPDATE reviews SET status=?, notes=? WHERE id=?",
               (status, notes, review_id))
    db.commit()
    return True


def delete(review_id, actor_user):
    """Remove a decision and the evidence copied onto it.

    The evidence goes with it deliberately: review_evidence is the record of
    what *that decision* was based on, and keeping it attached to a decision
    that no longer exists would leave rows nothing can reach or explain. The
    reviewer's own working marks were cleared when they submitted and are not
    affected either way.
    """
    db = get_db()
    row = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    if row is None:
        raise ReviewError("That decision no longer exists.")

    actor_id, actor_name = _actor(actor_user)
    db.execute(
        "INSERT INTO review_audit (review_id, claim_id, action, old_status, "
        "old_notes, reviewer, reviewer_user_id, actor, actor_user_id) "
        "VALUES (?, ?, 'deleted', ?, ?, ?, ?, ?, ?)",
        (row["id"], row["claim_id"], row["status"], row["notes"],
         row["reviewer"], row["reviewer_user_id"], actor_name, actor_id))
    db.execute("DELETE FROM review_evidence WHERE review_id=?", (review_id,))
    db.execute("DELETE FROM reviews WHERE id=?", (review_id,))
    db.commit()
    return True
