"""Hypotheses: the question a review campaign is trying to answer.

A hypothesis pins down a claim population - procedure codes, whether non-PMJAY
schemes count, and a date range - and, once processed, how many of those claims
have to be reviewed for the answer to carry weight. Fetching under a hypothesis
stamps its id on the fetch run, and that is the whole linkage: a hypothesis's
claims are the claims of its runs, so progress needs no re-matching of claims
against criteria that may since have been edited.

Two things are deliberately frozen rather than live:

- `claim_count` and `sample_size` are stored when Process is pressed. The
  warehouse tables underneath are live - today's table grew from 13,069 to
  30,571 rows over one afternoon - so a denominator recomputed on every page
  load would make progress appear to move backwards while nobody was reviewing.
- The window actually queried is stored alongside them, because "last 3 months"
  resolves differently tomorrow. Without it, a KPI from last week could not be
  explained.

Editing the criteria after processing does not silently re-run anything: it
stamps `criteria_changed_at`, and the UI reports the KPIs as stale until
somebody processes again.
"""
from datetime import datetime, timezone

from flask import current_app

from . import hypothesis_period
from .db import get_db
from .fetch import queries

# What a reviewer may leave blank, and what they may not.
MAX_TITLE = 200


class HypothesisError(ValueError):
    """A hypothesis that cannot be saved or processed as described."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def can_manage(user):
    """Whether `user` may create, edit or delete hypotheses.

    Admins always may; a reviewer needs the grant. Unlike rules - where the
    author keeps their own - a hypothesis is a team-level artefact, so this is a
    single capability rather than per-row ownership: whoever can manage
    hypotheses can manage all of them.
    """
    if not user:
        return False
    return bool(user["is_admin"] or user.get("can_manage_hypotheses"))


# --------------------------------------------------------------------- read

def _row_to_hypothesis(row):
    if row is None:
        return None
    h = dict(row)
    h["include_non_pmjay"] = bool(h["include_non_pmjay"])
    h["period_label"] = hypothesis_period.PERIOD_LABELS.get(h["period_kind"], h["period_kind"])
    h["period_description"] = hypothesis_period.describe_period(
        h["period_kind"], h["period_from"], h["period_to"])
    h["is_processed"] = h["claim_count"] is not None
    # Stale means: processed once, then the criteria moved. The numbers on
    # screen describe the old criteria and must say so.
    #
    # Presence of criteria_changed_at is the whole test - process_hypothesis
    # clears it. Comparing it against processed_at would be the obvious
    # alternative and is wrong: both are second-resolution, so processing and
    # then editing inside the same second compared equal and the edit was
    # reported as fresh.
    h["is_stale"] = bool(h["is_processed"] and h["criteria_changed_at"])
    return h


def list_hypotheses():
    """Every hypothesis, newest first, each with its review progress.

    Visible to everyone regardless of who may edit them - the point of a
    hypothesis is that the team can see what is being investigated.
    """
    db = get_db()
    rows = db.execute("SELECT * FROM hypotheses ORDER BY id DESC").fetchall()
    progress = _progress_by_hypothesis()
    result = []
    for row in rows:
        h = _row_to_hypothesis(row)
        result.append(_with_derived_progress(h, progress.get(h["id"], {})))
    return result


def get_hypothesis(hypothesis_id):
    row = get_db().execute("SELECT * FROM hypotheses WHERE id=?", (hypothesis_id,)).fetchone()
    h = _row_to_hypothesis(row)
    if h is not None:
        _with_derived_progress(h, _progress_by_hypothesis(hypothesis_id).get(h["id"], {}))
    return h


def processed_hypotheses():
    """Those a fetch can be run under: processed, so a sample size exists."""
    return [h for h in list_hypotheses() if h["is_processed"]]


def _with_derived_progress(hypothesis, counts):
    """Merge fetched/reviewed counts into a hypothesis and derive the rest.

    Kept separate from the SQL because the derived figures depend on the
    hypothesis's own sample size: a processed hypothesis nobody has fetched for
    yet has the whole sample outstanding, which is a real and useful statement,
    where the counts query alone would say nothing at all.
    """
    sample = hypothesis["sample_size"]
    reviewed = counts.get("claims_reviewed", 0)
    hypothesis.update({
        "claims_fetched": counts.get("claims_fetched", 0),
        "claims_reviewed": reviewed,
        "run_count": counts.get("run_count", 0),
        "reviews_remaining": max(0, sample - reviewed) if sample else None,
        # Capped at 100: reviewing past the sample is allowed and should not
        # render as 140% of a progress bar.
        "progress_pct": min(100, round(reviewed * 100 / sample)) if sample else None,
    })
    return hypothesis


def _progress_by_hypothesis(hypothesis_id=None):
    """{hypothesis_id: progress} from the fetch runs linked to each one.

    `claims_fetched` counts distinct claims brought down under the hypothesis -
    distinct because re-fetching a claim in a later run must not inflate it -
    and `claims_reviewed` counts how many of those carry at least one review
    decision. One decision is enough: a reviewer either has or has not judged
    the claim, and the review table records a history rather than a state.
    """
    db = get_db()
    where, params = "WHERE r.hypothesis_id IS NOT NULL", []
    if hypothesis_id is not None:
        where += " AND r.hypothesis_id=?"
        params.append(hypothesis_id)
    rows = db.execute(f"""
        SELECT r.hypothesis_id                        AS hid,
               COUNT(DISTINCT c.registration_id)      AS claims_fetched,
               COUNT(DISTINCT CASE WHEN v.claim_id IS NOT NULL
                                   THEN c.registration_id END) AS claims_reviewed,
               COUNT(DISTINCT r.run_id)               AS run_count
        FROM fetch_runs r
        JOIN fetch_run_claims c ON c.run_id = r.run_id
        LEFT JOIN reviews v     ON v.claim_id = c.registration_id
        {where}
        GROUP BY r.hypothesis_id
    """, tuple(params)).fetchall()

    return {row["hid"]: {"claims_fetched": row["claims_fetched"],
                         "claims_reviewed": row["claims_reviewed"],
                         "run_count": row["run_count"]} for row in rows}


# -------------------------------------------------------------------- write

def _validated(form):
    """The savable fields from a submitted form, or raise HypothesisError."""
    title = (form.get("title") or "").strip()
    if not title:
        raise HypothesisError("Give the hypothesis a title.")
    if len(title) > MAX_TITLE:
        raise HypothesisError(f"Keep the title under {MAX_TITLE} characters.")

    period_kind = (form.get("period_kind") or hypothesis_period.DEFAULT_PERIOD).strip().upper()
    period_from = (form.get("period_from") or "").strip() or None
    period_to = (form.get("period_to") or "").strip() or None
    if period_kind != "CUSTOM":
        # Only a custom period stores dates; keeping stale ones would make the
        # stored row contradict itself.
        period_from = period_to = None
    try:
        hypothesis_period.resolve_period(period_kind, period_from, period_to)
    except hypothesis_period.PeriodError as exc:
        raise HypothesisError(str(exc)) from exc

    confidence = int(form.get("confidence_level") or hypothesis_period.DEFAULT_CONFIDENCE)
    if confidence not in hypothesis_period.CONFIDENCE_LEVELS:
        raise HypothesisError("Confidence level must be one of "
                             + ", ".join(f"{c}%" for c in sorted(hypothesis_period.CONFIDENCE_LEVELS)))
    try:
        margin = float(form.get("margin_of_error") or hypothesis_period.DEFAULT_MARGIN)
    except (TypeError, ValueError) as exc:
        raise HypothesisError("Margin of error must be a number.") from exc
    if not 0 < margin < 1:
        raise HypothesisError("Margin of error must be between 0 and 1 (0.05 is 5%).")

    return {
        "title": title,
        "description": (form.get("description") or "").strip() or None,
        "procedure_codes": (form.get("procedure_codes") or "").strip() or None,
        "exclude_procedure_codes": (form.get("exclude_procedure_codes") or "").strip() or None,
        # Defaults on: a hypothesis is about a population, and excluding every
        # non-PMJAY scheme by default would silently narrow it. Note this is the
        # opposite of the fetch form's default, which starts PMJAY-only.
        "include_non_pmjay": 1 if form.get("include_non_pmjay") in ("on", "1", "true", True) else 0,
        "period_kind": period_kind,
        "period_from": period_from,
        "period_to": period_to,
        "confidence_level": confidence,
        "margin_of_error": margin,
    }


# The fields that decide which claims a hypothesis covers. Changing one of
# these invalidates the stored KPIs; changing the title or description does not.
CRITERIA_FIELDS = ("procedure_codes", "exclude_procedure_codes", "include_non_pmjay",
                   "period_kind", "period_from", "period_to",
                   "confidence_level", "margin_of_error")


def create_hypothesis(form, user=None):
    fields = _validated(form)
    db = get_db()
    columns = list(fields) + ["created_by_user_id", "created_by", "created_at"]
    values = list(fields.values()) + [
        user["id"] if user else None,
        (user["display_name"] or user["username"]) if user else None,
        _now(),
    ]
    cursor = db.execute(
        f"INSERT INTO hypotheses ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})", values)
    db.commit()
    return cursor.lastrowid


def update_hypothesis(hypothesis_id, form):
    """Save an edit, marking the KPIs stale if the criteria moved."""
    existing = get_hypothesis(hypothesis_id)
    if existing is None:
        raise HypothesisError("That hypothesis no longer exists.")
    fields = _validated(form)

    criteria_moved = any(
        _comparable(fields[key]) != _comparable(existing[key]) for key in CRITERIA_FIELDS)
    fields["updated_at"] = _now()
    if criteria_moved and existing["is_processed"]:
        fields["criteria_changed_at"] = _now()

    db = get_db()
    assignments = ", ".join(f"{key}=?" for key in fields)
    db.execute(f"UPDATE hypotheses SET {assignments} WHERE id=?",
               list(fields.values()) + [hypothesis_id])
    db.commit()
    return criteria_moved


def _comparable(value):
    """Normalise for change detection so 0/False and 0.05/'0.05' do not read as
    edits."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return round(value, 6)
    return value


def delete_hypothesis(hypothesis_id):
    """Delete a hypothesis, keeping any fetch runs made under it.

    The runs are real work and their bundles are on disk; unlinking rather than
    cascading means deleting a hypothesis never destroys a record of what was
    downloaded. Those runs simply stop counting toward anything.
    """
    db = get_db()
    unlinked = db.execute("UPDATE fetch_runs SET hypothesis_id=NULL WHERE hypothesis_id=?",
                          (hypothesis_id,)).rowcount
    db.execute("DELETE FROM hypotheses WHERE id=?", (hypothesis_id,))
    db.commit()
    return unlinked


# ----------------------------------------------------------------- process

def filters_for(hypothesis):
    """The hypothesis as warehouse selection filters.

    `include_non_pmjay` maps onto `convergence`, which is what the existing
    filter calls "every policy rather than PMJAY only" - the same clause the
    fetch form drives, so the two cannot drift apart.
    """
    return queries.ClaimFilters(
        convergence=bool(hypothesis["include_non_pmjay"]),
        procedure_codes=hypothesis["procedure_codes"],
        exclude_procedure_codes=hypothesis["exclude_procedure_codes"],
        hospital_type=None,
    )


def windows_for(hypothesis, connection, config=None):
    """(windows, from_date, to_date) for a hypothesis.

    The period is resolved against the *warehouse's* current_date, not the web
    server's: they are different machines and the window has to mean what the
    warehouse thinks it means.
    """
    config = config or current_app.config
    today = queries.warehouse_today(connection)
    period_from, period_to = hypothesis_period.resolve_period(
        hypothesis["period_kind"], hypothesis["period_from"], hypothesis["period_to"], today)
    windows = queries.hypothesis_windows(period_from, period_to, today, config)
    return windows, period_from, period_to


def process_hypothesis(hypothesis_id, config=None):
    """Count the population, derive the sample size, and freeze both.

    Returns the updated hypothesis. Runs synchronously: measured against the
    live warehouse this is 0.6s for a single day and 2.5s for three months
    across all three tables, which does not warrant a background job.
    """
    config = config or current_app.config
    hypothesis = get_hypothesis(hypothesis_id)
    if hypothesis is None:
        raise HypothesisError("That hypothesis no longer exists.")

    with queries.connect(config) as connection:
        windows, period_from, period_to = windows_for(hypothesis, connection, config)
        claim_count = queries.count_hypothesis_claims(
            connection, windows, filters_for(hypothesis),
            config.get("HYPOTHESIS_PERIOD_COLUMN", "claim_init_date"))

    size = hypothesis_period.sample_size(
        claim_count, hypothesis["confidence_level"], hypothesis["margin_of_error"])

    db = get_db()
    db.execute("""UPDATE hypotheses
                     SET claim_count=?, sample_size=?, processed_at=?,
                         processed_window_from=?, processed_window_to=?,
                         criteria_changed_at=NULL
                   WHERE id=?""",
               (claim_count, size, _now(), period_from.isoformat(), period_to.isoformat(),
                hypothesis_id))
    db.commit()
    return get_hypothesis(hypothesis_id)


def summary_for_fetch(hypothesis):
    """What the fetch form needs to prefill and explain itself: the values that
    get locked, and how many reviews are still outstanding."""
    return {
        "id": hypothesis["id"],
        "title": hypothesis["title"],
        "procedure_codes": hypothesis["procedure_codes"] or "",
        "exclude_procedure_codes": hypothesis["exclude_procedure_codes"] or "",
        "include_non_pmjay": bool(hypothesis["include_non_pmjay"]),
        "period_description": hypothesis["period_description"],
        "claim_count": hypothesis["claim_count"],
        "sample_size": hypothesis["sample_size"],
        "claims_reviewed": hypothesis["claims_reviewed"],
        "reviews_remaining": hypothesis["reviews_remaining"],
        "is_stale": hypothesis["is_stale"],
    }
