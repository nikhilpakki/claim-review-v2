"""Claim selection against the warehouse (Redshift).

Vendored from claim-bundle-extraction-v2/daily_claim_bundle_pipeline.py, with
the module-level SOURCE_TABLE constant replaced by an explicit `source_table`
argument so the same functions serve the CLI, the /fetch preview endpoint and
the pipeline run itself.

Everything here is plain psycopg - no Flask - so it can be used from a worker
thread or a script.
"""
from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import psycopg
from psycopg import _encodings, sql

# Amazon Redshift may report its encoding as UNICODE, which psycopg 3 does not
# know as an alias. Set here because this is the module that opens connections.
_encodings.py_codecs[b"UNICODE"] = "utf-8"


def _pick(config: Mapping[str, Any] | None, *names: str) -> Any:
    """First non-empty value for `names`, preferring an explicit config mapping
    (Flask's app.config) and falling back to the process environment.

    Two naming conventions are accepted because the two repos disagreed: the
    review app's .env.example already used REDSHIFT_*, while the extraction
    pipeline's .env uses DB_*. Both keep working.
    """
    for name in names:
        if config is not None:
            value = config.get(name)
            if value not in (None, ""):
                return value
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return None


def connection_kwargs(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """psycopg.connect() kwargs for the warehouse."""
    host = _pick(config, "REDSHIFT_HOST", "DB_HOST")
    dbname = _pick(config, "REDSHIFT_DB", "REDSHIFT_DBNAME", "DB_NAME")
    user = _pick(config, "REDSHIFT_USER", "DB_USER")
    password = _pick(config, "REDSHIFT_PASSWORD", "DB_PASSWORD")

    missing = [
        name
        for name, value in (
            ("REDSHIFT_HOST/DB_HOST", host),
            ("REDSHIFT_DB/DB_NAME", dbname),
            ("REDSHIFT_USER/DB_USER", user),
            ("REDSHIFT_PASSWORD/DB_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("Missing database settings: " + ", ".join(missing))

    return {
        "host": host,
        "dbname": dbname,
        "user": user,
        "password": password,
        "port": int(_pick(config, "REDSHIFT_PORT", "DB_PORT") or 5439),
        "connect_timeout": int(
            _pick(config, "REDSHIFT_CONNECT_TIMEOUT", "DB_CONNECT_TIMEOUT") or 15
        ),
        "sslmode": _pick(config, "REDSHIFT_SSLMODE", "DB_SSLMODE") or "require",
    }


def connect(config: Mapping[str, Any] | None = None) -> psycopg.Connection[Any]:
    return psycopg.connect(**connection_kwargs(config))


def _table(source_table: str) -> sql.Identifier:
    return sql.Identifier(*source_table.split("."))


# procedure_code holds a claim's whole procedure list in one pipe-delimited
# string (measured: 9,330 of 22,018 rows contain a pipe, up to 571 characters),
# which is what makes "claims containing BOTH of these codes" a meaningful
# filter rather than an impossible one.
#
# Filter syntax, the same for inclusions and exclusions:
#   "a"        -> contains a
#   "a|b"      -> contains a OR b        (alternatives within one requirement)
#   "a,b"      -> contains a AND b       (two separate requirements)
#   "a|b,c"    -> (contains a OR b) AND contains c
# Comma never appears in the data (0 of 22,018 rows), so it is unambiguous as
# the AND separator.
PROCEDURE_AND_SEPARATOR = ","
PROCEDURE_OR_SEPARATOR = "|"

# Applied unless the caller overrides it - this was previously a hardcoded
# NOT (procedure_code ~* 'lm100') buried in the query. It is now the default
# value of the exclusion filter, so it is visible on the form and editable.
DEFAULT_EXCLUDE_PROCEDURE_CODES = "lm100"

HOSPITAL_TYPES = {"P": "Private", "G": "Government"}


def parse_procedure_filter(value: str | None) -> list[list[str]]:
    """Parse a filter expression into AND groups of OR alternatives.

    'MG110,ABC23' -> [['MG110'], ['ABC23']]  (both must be present)
    'MG110|ABC23' -> [['MG110', 'ABC23']]    (either will do)
    Empty input -> [] (no condition).
    """
    groups: list[list[str]] = []
    for chunk in str(value or "").split(PROCEDURE_AND_SEPARATOR):
        alternatives = [part.strip() for part in chunk.split(PROCEDURE_OR_SEPARATOR) if part.strip()]
        if alternatives:
            groups.append(alternatives)
    return groups


def procedure_group_pattern(alternatives: list[str]) -> str:
    """Regex matching any of `alternatives` as a *complete* item in the
    pipe-delimited list.

    Anchoring on the delimiters is what stops 'LB05' from also matching
    'LB055': the column is a list, so a bare substring test asks the wrong
    question. Each alternative is still a regex, so 'LB.*' remains available
    when prefix matching is what you actually want.
    """
    return r"(^|\|)(" + "|".join(alternatives) + r")(\||$)"


def describe_procedure_filter(groups: list[list[str]]) -> str:
    """Plain-English reading of a parsed filter, echoed back in the preview so
    the comma/pipe distinction never has to be taken on trust."""
    if not groups:
        return ""
    parts = [
        alternatives[0] if len(alternatives) == 1 else "(" + " or ".join(alternatives) + ")"
        for alternatives in groups
    ]
    return " and ".join(parts)


@dataclass
class ClaimFilters:
    """The selection filters, shared by the preview and the run so the two can
    never disagree about what was asked for."""

    convergence: bool = False
    procedure_codes: str | None = None
    exclude_procedure_codes: str | None = DEFAULT_EXCLUDE_PROCEDURE_CODES
    hospital_type: str | None = None

    def include_groups(self) -> list[list[str]]:
        return parse_procedure_filter(self.procedure_codes)

    def exclude_groups(self) -> list[list[str]]:
        return parse_procedure_filter(self.exclude_procedure_codes)

    def normalized_hospital_type(self) -> str | None:
        value = (self.hospital_type or "").strip().upper()
        return value if value in HOSPITAL_TYPES else None

    def clause(self) -> tuple[Any, list[Any]]:
        """The conditions and their bound parameters, already prefixed with
        " AND ...", ready to splice into either selection query.

        Every value is bound rather than inlined - the PMJAY LIKE pattern
        contains a '%' that psycopg would otherwise read as a placeholder, and
        the procedure patterns are user input.
        """
        conditions: list[Any] = []
        params: list[Any] = []

        if not self.convergence:
            conditions.append(sql.SQL("policy_code LIKE %s"))
            params.append("PMJAY%")

        hospital_type = self.normalized_hospital_type()
        if hospital_type:
            conditions.append(sql.SQL("hospital_type = %s"))
            params.append(hospital_type)

        # Each include group is its own condition, ANDed: that is what makes
        # 'MG110,ABC23' mean "has both" instead of "has either".
        for alternatives in self.include_groups():
            conditions.append(sql.SQL("procedure_code ~* %s"))
            params.append(procedure_group_pattern(alternatives))

        # The exclusion is the negation of the whole expression, so it reads
        # the same way as the inclusion box: 'a|b' drops claims with either,
        # 'a,b' drops only claims carrying both.
        exclude_groups = self.exclude_groups()
        if exclude_groups:
            # COALESCE matters: procedure_code is NULL on 202 of 22,018 rows,
            # and NOT (NULL ~* pattern) is NULL, not TRUE - so a bare NOT drops
            # every codeless claim along with the excluded ones. (The pipeline's
            # original hardcoded lm100 exclusion had exactly this bug: it
            # removed 202 claims that carry no procedure code at all on top of
            # the 43 that actually match.) A claim with no code cannot contain
            # an excluded code, so it must survive.
            inner = sql.SQL(" AND ").join(
                sql.SQL("COALESCE(procedure_code, '') ~* %s") for _ in exclude_groups
            )
            conditions.append(sql.SQL("NOT ({})").format(inner))
            params.extend(procedure_group_pattern(alts) for alts in exclude_groups)

        if not conditions:
            return sql.SQL(""), []
        clause = sql.SQL(" AND ") + sql.SQL(" AND ").join(conditions)
        return clause, params

    def describe(self) -> dict[str, str]:
        """What the UI shows back to the user."""
        hospital_type = self.normalized_hospital_type()
        return {
            "policy": "convergence (all policies)" if self.convergence else "PMJAY policies",
            "hospital_type": (f"{hospital_type} - {HOSPITAL_TYPES[hospital_type]}"
                               if hospital_type else "any"),
            "include": describe_procedure_filter(self.include_groups()) or "any procedure code",
            "exclude": describe_procedure_filter(self.exclude_groups()) or "nothing excluded",
        }


def claim_filter_clause(filters: ClaimFilters | None) -> tuple[Any, list[Any]]:
    return (filters or ClaimFilters()).clause()


def fetch_latest_claims(
    connection: psycopg.Connection[Any],
    limit: int | None,
    source_table: str,
    filters: ClaimFilters | None = None,
) -> list[tuple[str, str]]:
    """Return (registration_id, json_object_perauth) for the latest claims.

    Selecting json_object_perauth here lets each claim download skip its own
    per-claim Redshift lookup (previously one fresh connection + multi-table
    scan per claim). ``limit`` caps the number of claims; ``None`` means no cap.
    """
    filter_clause, filter_params = claim_filter_clause(filters)
    query = sql.SQL("""
        SELECT registration_id, json_object_perauth
        FROM (
            SELECT registration_id,
                   json_object_perauth,
                   last_insert_dt,
                   ROW_NUMBER() OVER (
                       PARTITION BY registration_id
                       ORDER BY last_insert_dt DESC
                   ) AS rn
            FROM {}
            WHERE registration_id IS NOT NULL
              AND json_object_perauth IS NOT NULL
              AND json_object_perauth <> ''{}
        ) ranked
        WHERE rn = 1
        ORDER BY last_insert_dt DESC, registration_id DESC
    """).format(_table(source_table), filter_clause)

    params: list[Any] = list(filter_params)
    if limit is not None:
        query = query + sql.SQL(" LIMIT %s")
        params.append(limit)

    with connection.cursor() as cursor:
        cursor.execute(query, tuple(params))
        return [(str(row[0]), str(row[1])) for row in cursor.fetchall()]


def fetch_claims_by_ids(
    connection: psycopg.Connection[Any],
    claim_ids: list[str],
    source_table: str,
    filters: ClaimFilters | None = None,
) -> list[tuple[str, str | None]]:
    """Return (registration_id, json_object_perauth) for the given IDs, in the
    input order. IDs not found in the source table are still returned (preauth
    None) so the downloader can fall back to its own multi-table lookup."""
    preauth_by_id: dict[str, str] = {}
    chunk_size = 1000
    for start in range(0, len(claim_ids), chunk_size):
        chunk = claim_ids[start:start + chunk_size]
        filter_clause, filter_params = claim_filter_clause(filters)
        query = sql.SQL("""
            SELECT registration_id, json_object_perauth
            FROM (
                SELECT registration_id,
                       json_object_perauth,
                       last_insert_dt,
                       ROW_NUMBER() OVER (
                           PARTITION BY registration_id
                           ORDER BY last_insert_dt DESC
                       ) AS rn
                FROM {}
                WHERE registration_id IN ({})
                  AND json_object_perauth IS NOT NULL
                  AND json_object_perauth <> ''{}
            ) ranked
            WHERE rn = 1
        """).format(
            _table(source_table),
            sql.SQL(", ").join(sql.Placeholder() for _ in chunk),
            filter_clause,
        )
        params = list(chunk) + list(filter_params)
        with connection.cursor() as cursor:
            cursor.execute(query, tuple(params))
            for row in cursor.fetchall():
                preauth_by_id[str(row[0])] = str(row[1])

    return [(claim_id, preauth_by_id.get(claim_id)) for claim_id in claim_ids]


def _as_text(value: Any) -> str:
    """Render a warehouse value the way the equivalent CSV export would.

    The claims dataset those rows land in (csv_claims_data) is otherwise
    populated by csv.DictReader, so every value there is a string and the rules
    engine compares strings. Fetched rows are stringified to match, rather than
    leaving rules to behave differently depending on how the data arrived.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def fetch_claim_rows(
    connection: psycopg.Connection[Any],
    claim_ids: list[str],
    source_table: str,
) -> list[dict[str, str]]:
    """Full latest source-table row per claim, as {column: text}.

    This is the same data an analyst would otherwise export to
    claims_paid_t.csv and upload by hand, so fetching it here lets the claims
    dataset populate itself. No policy/procedure filter is applied - the IDs
    have already been selected by the time this is called.
    """
    if not claim_ids:
        return []

    rows_by_id: dict[str, dict[str, str]] = {}
    chunk_size = 500
    for start in range(0, len(claim_ids), chunk_size):
        chunk = claim_ids[start:start + chunk_size]
        query = sql.SQL("""
            SELECT * FROM (
                SELECT source.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY registration_id
                           ORDER BY last_insert_dt DESC
                       ) AS _rn
                FROM {} AS source
                WHERE registration_id IN ({})
            ) ranked
            WHERE _rn = 1
        """).format(
            _table(source_table),
            sql.SQL(", ").join(sql.Placeholder() for _ in chunk),
        )
        with connection.cursor() as cursor:
            cursor.execute(query, tuple(chunk))
            columns = [desc[0] for desc in cursor.description]
            for record in cursor.fetchall():
                row = {
                    column: _as_text(value)
                    for column, value in zip(columns, record)
                    if column != "_rn"
                }
                claim_id = row.get("registration_id", "").strip()
                if claim_id:
                    rows_by_id[claim_id] = row

    return [rows_by_id[claim_id] for claim_id in claim_ids if claim_id in rows_by_id]


# --------------------------------------------------------------- CLI parsing


def parse_limit(value: str) -> int | None:
    """Parse a claim limit: an integer cap, or one of false/none/all/0 for no
    cap. Raises ValueError on anything else (argparse turns that into a usage
    error; the /fetch form turns it into a field error)."""
    text = str(value).strip().lower()
    if text in {"false", "none", "all", "0", "no", ""}:
        return None
    try:
        parsed = int(text)
    except ValueError:
        raise ValueError(
            f"limit must be an integer or one of false/none/all: got {value!r}"
        )
    if parsed < 0:
        return None
    return parsed


def _claim_ids_from_rows(rows: list[list[str]]) -> list[str]:
    """Shared parsing for a pasted or uploaded list of registration IDs.
    Prefers a 'registration_id' column, otherwise uses the first column;
    tolerates a header-less list. Values that look like case_ids (contain '/')
    are trimmed to the trailing id."""
    def norm(text: str) -> str:
        return "".join(ch for ch in str(text).lower() if ch.isalnum())

    def clean(value: str) -> str:
        text = str(value).strip().rstrip("/")
        return text.rsplit("/", 1)[-1] if text else ""

    if not rows:
        return []

    header = rows[0]
    column_index = 0
    start_row = 1
    known = {"registrationid", "registrationids", "claimid", "claimids", "regid"}
    matched = next((i for i, name in enumerate(header) if norm(name) in known), None)
    if matched is not None:
        column_index = matched
    else:
        first = clean(header[0]) if header else ""
        if first.isdigit() and len(first) >= 8:
            start_row = 0  # no header row; the first line is already data

    ids: list[str] = []
    seen: set[str] = set()
    for row in rows[start_row:]:
        if not row or column_index >= len(row):
            continue
        claim_id = clean(row[column_index])
        if claim_id and claim_id not in seen:
            seen.add(claim_id)
            ids.append(claim_id)
    return ids


def read_claim_ids_from_csv(path: Path) -> list[str]:
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        return _claim_ids_from_rows(list(csv.reader(handle)))


def read_claim_ids_from_text(text: str) -> list[str]:
    """IDs pasted into the /fetch form: any run of whitespace, commas, semicolons
    or pipes separates them, so a column copied out of Excel, a comma-separated
    line and a one-per-line list all work.

    Deliberately not routed through _claim_ids_from_rows(): that reader takes a
    single column, which would silently drop every id but the first from a
    comma-separated line. Tokens with no digit in them (a pasted
    'registration_id' header) are skipped.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,;|]+", text or ""):
        cleaned = token.strip().rstrip("/").rsplit("/", 1)[-1]
        if not cleaned or not any(ch.isdigit() for ch in cleaned):
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            ids.append(cleaned)
    return ids


def read_claim_ids_from_bytes(raw: bytes) -> list[str]:
    """For a CSV uploaded through the browser (a werkzeug FileStorage read into
    memory) rather than read off disk."""
    text = raw.decode("utf-8-sig", errors="replace")
    return _claim_ids_from_rows(list(csv.reader(io.StringIO(text))))
