"""One reviewer's own review history, grouped by the hypothesis it served.

The Hypotheses page answers "how far has the team got"; this answers "what have
I decided", which is a different question and needs a different shape: per
claim, the decision that currently stands, and which campaign it counted
towards.

A claim reaches a hypothesis through the fetch run it was downloaded in
(fetch_runs.hypothesis_id), so a claim fetched twice under two hypotheses
legitimately belongs to both, and a claim fetched outside any hypothesis
belongs to none - that last group is real work and is reported under "No
hypothesis" rather than dropped.
"""
from .db import get_db

# The bucket for reviews on claims that were not fetched under any hypothesis.
NO_HYPOTHESIS = "No hypothesis"


def _latest_decisions(user_id):
    """The decision that currently stands per claim, for this user.

    reviews is append-only - a reviewer can approve, look again and flag - so
    the newest row per claim is the answer, and the earlier ones are history.
    Ordered by id, not created_at: two decisions inside the same second are
    ordinary, and created_at has second resolution.
    """
    rows = get_db().execute("""
        SELECT r.claim_id, r.status, r.notes, r.created_at, r.id
        FROM reviews r
        WHERE r.reviewer_user_id = ?
          AND r.id = (SELECT MAX(r2.id) FROM reviews r2
                       WHERE r2.claim_id = r.claim_id AND r2.reviewer_user_id = r.reviewer_user_id)
        ORDER BY r.id DESC
    """, (user_id,)).fetchall()
    return [dict(row) for row in rows]


def _hypotheses_by_claim():
    """{claim_id: [(hypothesis_id, title), ...]} from the fetch runs.

    A claim can appear under more than one hypothesis if it was fetched under
    each; listing it in both is the honest reading, since the review counts
    towards both campaigns' progress.
    """
    rows = get_db().execute("""
        SELECT DISTINCT c.registration_id AS claim_id, h.id AS hid, h.title
        FROM fetch_run_claims c
        JOIN fetch_runs r ON r.run_id = c.run_id
        JOIN hypotheses h ON h.id = r.hypothesis_id
    """).fetchall()
    out = {}
    for row in rows:
        out.setdefault(row["claim_id"], []).append((row["hid"], row["title"]))
    return out


def grouped_for(user_id):
    """[{hypothesis_id, title, claims: [...], counts: {...}}] for this user.

    Newest decision first within each group, and the groups ordered by their
    most recent activity so whatever is being worked on now is at the top.
    """
    if not user_id:
        return []
    decisions = _latest_decisions(user_id)
    by_claim = _hypotheses_by_claim()

    groups = {}
    for decision in decisions:
        targets = by_claim.get(decision["claim_id"]) or [(None, NO_HYPOTHESIS)]
        for hid, title in targets:
            group = groups.setdefault(hid, {"hypothesis_id": hid, "title": title,
                                            "claims": [], "counts": {}})
            group["claims"].append(decision)
            group["counts"][decision["status"]] = group["counts"].get(decision["status"], 0) + 1

    ordered = sorted(
        groups.values(),
        # Most recently touched first; the unattributed bucket last, since it is
        # a leftover rather than a campaign.
        key=lambda g: (g["hypothesis_id"] is None,
                       -max(c["id"] for c in g["claims"])),
    )
    for group in ordered:
        group["total"] = len(group["claims"])
    return ordered


def totals_for(user_id):
    """{status: count} over the standing decisions, plus a claim total."""
    groups = grouped_for(user_id)
    # A claim counted under two hypotheses must not be counted twice here.
    seen, counts = set(), {}
    for group in groups:
        for claim in group["claims"]:
            if claim["claim_id"] in seen:
                continue
            seen.add(claim["claim_id"])
            counts[claim["status"]] = counts.get(claim["status"], 0) + 1
    return {"by_status": counts, "claims": len(seen)}
