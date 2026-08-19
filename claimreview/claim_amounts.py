"""The money side of a claim, from its extracted bundle.

The extracted tables keep every payer/provider x stage x requested/approved
snapshot, so one procedure worth 15,000 shows up as a dozen near-identical
rows. That trail is what makes warehouse analysis possible, and it is exactly
what a reviewer does not want to read. This collapses it to the two questions
a reviewer actually asks:

- what did the claim total come to at preauthorisation, and at claim time?
- per procedure, what did the provider ask for, what did the payer approve,
  and what was deducted?

The same collapse the analytical views do in Redshift (vw_claim_package_amounts
/ vw_claim_treatments), done locally so the claim page needs no warehouse.
"""

CLAIM_TOTAL_ITEM = "CLAIM_TOTAL"
STAGE_LABELS = [
    ("preauthorization", "Preauthorisation"),
    ("enhancement", "Enhancement"),
    ("claim", "Claim"),
]
MONEY_FIELDS = ("requested_amount", "approved_amount", "claimed_amount", "total_deducted_amount")
# A claim is restated at each stage; the latest one is the one that stands.
STAGE_RANK = {"preauthorization": 0, "enhancement": 1, "claim": 2}


def _number(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick(rows, field, prefer_side=None):
    """First non-empty value of `field`, preferring rows from `prefer_side`.

    Payer and provider record the same figure in their own snapshots; when
    they disagree the payer's is the one that governs what was actually paid,
    so the payer row wins where a preference is given.
    """
    ordered = rows
    if prefer_side:
        ordered = ([r for r in rows if r.get("source_side") == prefer_side]
                   + [r for r in rows if r.get("source_side") != prefer_side])
    for row in ordered:
        value = _number(row.get(field))
        if value is not None:
            return value
    return None


def _approved_rows(rows):
    """Rows recording an approval, as opposed to the request snapshot."""
    return [r for r in rows if (r.get("status") or "").strip().lower() == "approved"]


def _totals_by_stage(package_rows):
    totals = []
    claim_rows = [r for r in package_rows if str(r.get("item_id")) == CLAIM_TOTAL_ITEM]
    for stage, label in STAGE_LABELS:
        rows = [r for r in claim_rows if (r.get("source_stage") or "") == stage]
        if not rows:
            continue
        entry = {"stage": stage, "label": label}
        for field in MONEY_FIELDS:
            entry[field] = _pick(rows, field, prefer_side="payer")
        entry["liability_amount"] = _pick(rows, "liability_amount", prefer_side="payer")
        entry["wallet_balance_amount"] = _pick(rows, "wallet_balance_amount", prefer_side="payer")
        totals.append(entry)
    return totals


def _best(rows, field, side=None, status=None):
    """The value of `field` from the latest stage that has one.

    A claim is re-stated at preauthorisation, again at enhancement, and again
    at claim time, and each restatement is its own set of rows. The latest
    stage is the one that stands, so that is what a reviewer should see -
    together with which stage it came from, which is why this returns the
    provenance rather than a bare number.
    """
    best = None
    for row in rows:
        if side and row.get("source_side") != side:
            continue
        if status and (row.get("status") or "").strip().lower() != status:
            continue
        value = _number(row.get(field))
        if value is None:
            continue
        rank = STAGE_RANK.get((row.get("source_stage") or "").strip(), -1)
        if best is None or rank > best[0]:
            best = (rank, value, row.get("source_stage"))
    if best is None:
        return {"value": None, "stage": None}
    return {"value": best[1], "stage": best[2]}


def _line_key(row):
    """The identity of one billed line. item_id ('Procedure/MG072D/2') is what
    distinguishes two lines of the *same* procedure code - grouping by code
    alone merges them, which turns a two-line claim into one line with one
    line's figures."""
    item_id = str(row.get("item_id") or "").strip()
    if item_id and item_id != CLAIM_TOTAL_ITEM:
        return item_id
    code = (row.get("package_code") or row.get("procedure_code") or "").strip()
    return code or None


def _by_procedure(package_rows, treatment_rows):
    """One row per billed line: what was requested, what was approved, and
    where each figure came from.

    Deliberately does not sum across snapshots: the same line is repeated for
    every side/stage/status combination, so adding them up multiplies the
    claim. Each figure is taken from a single latest-stage row instead.
    """
    lines = {}

    def entry(key, code, name):
        item = lines.setdefault(key, {
            "key": key, "code": code, "name": name or "",
            "provider_requested": None, "requested_stage": None,
            "payer_approved": None, "approved_stage": None,
            "claimed": None, "deducted": None,
            "quantity": None, "approved_quantity": None, "status": None,
        })
        if not item["name"] and name:
            item["name"] = name
        return item

    for row in package_rows:
        if str(row.get("item_id")) == CLAIM_TOTAL_ITEM:
            continue
        key = _line_key(row)
        if key:
            entry(key, (row.get("package_code") or "").strip(), row.get("package_description"))
    for row in treatment_rows:
        key = _line_key(row)
        if key:
            entry(key, (row.get("procedure_code") or "").strip(), row.get("procedure_name"))

    for key, item in lines.items():
        packages = [r for r in package_rows
                    if _line_key(r) == key and str(r.get("item_id")) != CLAIM_TOTAL_ITEM]
        treatments = [r for r in treatment_rows if _line_key(r) == key]

        # What the provider asked for: their own Requested rows. Approved
        # figures must come from Approved rows - a Requested snapshot carries
        # approved_amount/approved_quantity as 0 because nothing is approved
        # yet, and reading one shows a fully approved line as approved for zero.
        requested = _best(treatments, "net_amount", side="provider", status="requested")
        if requested["value"] is None:
            requested = _best(packages, "requested_amount", side="provider")
        if requested["value"] is None:
            requested = _best(treatments + packages, "net_amount")
        item["provider_requested"] = requested["value"]
        item["requested_stage"] = requested["stage"]

        approved = _best(treatments, "approved_amount", side="payer", status="approved")
        if approved["value"] is None:
            approved = _best(packages, "approved_amount", side="payer")
        if approved["value"] is None:
            approved = _best(treatments, "approved_amount", status="approved")
        item["payer_approved"] = approved["value"]
        item["approved_stage"] = approved["stage"]

        item["claimed"] = _pick(packages, "claimed_amount", prefer_side="payer")
        item["deducted"] = _pick(packages, "total_deducted_amount", prefer_side="payer")
        item["quantity"] = _best(treatments, "quantity", side="provider", status="requested")["value"]
        if item["quantity"] is None:
            item["quantity"] = _best(treatments, "quantity")["value"]
        item["approved_quantity"] = _best(
            treatments, "approved_quantity", side="payer", status="approved")["value"]
        if item["approved_quantity"] is None:
            item["approved_quantity"] = _best(
                treatments, "approved_quantity", status="approved")["value"]

        approved_rows = _approved_rows(treatments)
        item["status"] = (approved_rows[0] if approved_rows
                          else (treatments[0] if treatments else {})).get("status")

        if item["provider_requested"] is not None and item["payer_approved"] is not None:
            item["reduction"] = round(item["provider_requested"] - item["payer_approved"], 2)
        else:
            item["reduction"] = None

    return sorted(lines.values(), key=lambda i: (i["code"], i["key"]))


def build_amounts_panel(datasets):
    """{totals, procedures, has_data} from the mirrored extraction, or
    has_data False when the claim was not fetched through this app."""
    if not datasets:
        return {"has_data": False, "totals": [], "procedures": []}

    package_rows = datasets.get("claim_bundle_package_amounts") or []
    treatment_rows = datasets.get("claim_bundle_treatments") or []
    if not package_rows and not treatment_rows:
        return {"has_data": False, "totals": [], "procedures": []}

    return {
        "has_data": True,
        "totals": _totals_by_stage(package_rows),
        "procedures": _by_procedure(package_rows, treatment_rows),
    }
