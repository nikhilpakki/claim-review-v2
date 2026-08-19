"""Claim-bundle pipeline: select claims, download their bundles from S3,
extract the eight structured datasets, and load them into Redshift.

Vendored from claim-bundle-extraction-v2/daily_claim_bundle_pipeline.py. The
behaviour is the original's; what changed is the shape:

- `main()` became `run_pipeline(options, on_event=..., cancel_event=...)`, so
  the Flask app can drive a run from a worker thread and stream progress, and
  `cli.py` can still drive the identical run from the command line.
- The `Path.cwd()`-relative module constants (bundles root, report dir, script
  paths) became fields on `FetchOptions`, because a web app's working directory
  is not a meaningful anchor for where claim bundles live.
- The importlib-by-file-path module loading (`_load_module`) became ordinary
  imports of the vendored `downloader` / `extractor` modules.
- `TARGET_SCHEMA` / `INSERT_BATCH_ROWS` became arguments rather than globals,
  since a request thread and a fetch thread can now be alive at the same time
  and mutable module state would be shared between them.

Deliberately Flask-free: it takes plain values and reports progress through a
callback, so the persistence/UI concerns live in claimreview/fetch/service.py.
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

import psycopg
from psycopg import sql

from . import downloader, extractor, queries
from .tables import (
    DEDUPLICATION_RULES,
    EXTRACTABLE_STATUSES,
    EXTRACTOR_FUNCTIONS,
    MAX_STATEMENT_PARAMS,
    REPORT_VIEWS,
    SUMMARY_COLUMNS,
    TABLE_COLUMNS,
    TABLE_ORDER,
)

DEFAULT_SOURCE_TABLE = "dmart_solution.claim_paid_t"
DEFAULT_TARGET_SCHEMA = "public"
DEFAULT_CLAIM_LIMIT = 100


@dataclass
class FetchOptions:
    """One pipeline run's configuration.

    `destination` is the bundle root: claims land in
    `<destination>/<registration_id>/{payer,provider}`, which is exactly the
    folder shape the review side scans, so the destination doubles as a review
    root folder.
    """

    destination: Path
    # Claim source: an explicit id list wins; otherwise the latest `limit`
    # claims (None = no cap), matching --claims-csv / --limit.
    claim_ids: list[str] | None = None
    limit: int | None = DEFAULT_CLAIM_LIMIT
    # Selection filters (see queries.ClaimFilters for the syntax).
    convergence: bool = False
    procedure_codes: str | None = None
    exclude_procedure_codes: str | None = queries.DEFAULT_EXCLUDE_PROCEDURE_CODES
    hospital_type: str | None = None

    load_redshift: bool = True
    write_reports: bool = True
    report_dir: Path | None = None
    # Deleting the bundles removes the very files the review side displays, so
    # this stays off unless a caller explicitly asks (the CLI's --cleanup).
    cleanup: bool = False
    collect_claim_rows: bool = True

    source_table: str = DEFAULT_SOURCE_TABLE
    target_schema: str = DEFAULT_TARGET_SCHEMA
    source_bucket: str = downloader.DEFAULT_SOURCE_BUCKET
    claim_workers: int = 8
    download_workers: int = 4
    insert_batch_rows: int = 500

    run_id: str | None = None
    # psycopg connect kwargs; None means "build them from config/environment".
    connect_kwargs: dict[str, Any] | None = None
    db_config: Mapping[str, Any] | None = None

    def resolved_report_dir(self) -> Path:
        return self.report_dir or (Path(self.destination).parent / "pipeline_reports")

    def connection_kwargs(self) -> dict[str, Any]:
        return self.connect_kwargs or queries.connection_kwargs(self.db_config)

    def filters(self) -> queries.ClaimFilters:
        return queries.ClaimFilters(
            convergence=self.convergence,
            procedure_codes=self.procedure_codes,
            exclude_procedure_codes=self.exclude_procedure_codes,
            hospital_type=self.hospital_type,
        )


EventCallback = Callable[[str, dict[str, Any]], None]


def now() -> datetime:
    return datetime.now()


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]


class _Emitter:
    """Progress fan-out. Swallows callback errors: a failing progress sink
    (a closed browser, a locked SQLite file) must never abort a run that is
    otherwise downloading claims fine."""

    def __init__(self, callback: EventCallback | None):
        self._callback = callback

    def __call__(self, kind: str, **payload: Any) -> None:
        if self._callback is None:
            return
        try:
            self._callback(kind, payload)
        except Exception:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)

    def log(self, message: str, level: str = "info") -> None:
        self("log", message=message, level=level)


# ------------------------------------------------------------- extraction


def reduce_rows(table_name: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse a table's rows before loading: aggregate descriptive tables
    (pipe-join provenance), snapshot-dedup financial tables, pass through others."""
    short = table_name.replace("claim_bundle_", "")
    rule = getattr(extractor, "AGGREGATION_RULES", {}).get(short)
    if rule:
        # The rule decides both what to drop and how to collapse; see
        # extractor.AGGREGATION_RULES.
        return extractor.reduce_by_rule(rows, rule)
    ignored = DEDUPLICATION_RULES.get(table_name)
    if ignored:
        return extractor.deduplicate_rows(rows, ignored)
    return rows


def extract_claim(
    claim_id: str,
    bundle: Path,
    run_id: str,
    ingestion_date: date,
    started_at: datetime,
) -> dict[str, list[dict[str, Any]]]:
    if not bundle.is_dir():
        raise FileNotFoundError(f"Claim bundle not found: {bundle}")

    # One shared context per claim: every JSON is parsed exactly once.
    ctx = extractor.BundleContext(bundle)

    tables: dict[str, list[dict[str, Any]]] = {}
    summary = extractor.build_summary(claim_id, claim_id, bundle, ctx)
    summary.update({
        "extraction_status": "SUCCESS",
        "extraction_error": None,
        "extraction_started_at": started_at,
        "extraction_completed_at": now(),
        "redshift_load_status": "PENDING",
        "redshift_load_error": None,
        "redshift_loaded_at": None,
        "pipeline_run_id": run_id,
        "ingestion_date": ingestion_date,
    })
    tables["claim_bundle_summary"] = [summary]

    for table_name, function_name in EXTRACTOR_FUNCTIONS.items():
        function: Callable[..., list[dict[str, Any]]] = getattr(extractor, function_name)
        rows = function(claim_id, bundle, ctx)
        rows = reduce_rows(table_name, rows)
        for row in rows:
            row["pipeline_run_id"] = run_id
            row["ingestion_date"] = ingestion_date
        tables[table_name] = rows

    return tables


def process_claim(
    s3_client: Any,
    claim_id: str,
    preauth_path: str | None,
    run_id: str,
    ingestion_date: date,
    options: FetchOptions,
) -> dict[str, Any]:
    """Download + extract a single claim. Runs inside a worker thread and does
    NOT touch Redshift; the returned tables are loaded later on the main
    thread. Never raises: failures are captured in the result dict."""
    started_at = now()
    download_status = "NOT_STARTED"
    extraction_status = "NOT_STARTED"
    error: str | None = None
    tables: dict[str, list[dict[str, Any]]] | None = None
    bundles_root = Path(options.destination)

    try:
        manifest = downloader.download_bundle(
            claim_id,
            preauth_path=preauth_path,
            s3_client=s3_client,
            destination=bundles_root,
            source_bucket=options.source_bucket,
            dry_run=False,
            download_workers=options.download_workers,
            verbose=False,
        )
        download_status = str(getattr(manifest, "status", None) or "failed")
        if download_status not in EXTRACTABLE_STATUSES:
            raise RuntimeError(
                getattr(manifest, "error", None)
                or getattr(manifest, "skip_reason", None)
                or "Claim download failed"
            )

        tables = extract_claim(
            claim_id, bundles_root / claim_id, run_id, ingestion_date, started_at
        )

        if download_status == "partially_completed":
            extraction_status = "PARTIAL"
            summary = tables["claim_bundle_summary"][0]
            summary["extraction_status"] = "PARTIAL"
            summary["extraction_error"] = (
                getattr(manifest, "skip_reason", None)
                or "Provider bundle unavailable; payer-side data only."
            )
        else:
            extraction_status = "SUCCESS"

    except Exception as exc:  # noqa: BLE001 - captured into the result
        extraction_status = "FAILED"
        error = f"{type(exc).__name__}: {exc}"

    return {
        "registration_id": claim_id,
        "download_status": download_status,
        "extraction_status": extraction_status,
        "tables": tables,
        "error": error,
        "started_at": started_at,
        "completed_at": now(),
    }


# ---------------------------------------------------------- Redshift loading


def normalize_value(value: Any) -> Any:
    if value is None:
        return None
    # numpy/pandas scalar compatibility without importing either package here.
    if hasattr(value, "item") and callable(value.item):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def delete_claim_rows(
    cursor: psycopg.Cursor[Any],
    claim_ids: list[str],
    schema: str,
) -> None:
    """Delete all existing rows for the given claim IDs from every table in one
    statement per table (instead of one DELETE per claim per table)."""
    if not claim_ids:
        return
    for table_name in TABLE_ORDER:
        query = sql.SQL("DELETE FROM {}.{} WHERE registration_id IN ({})").format(
            sql.Identifier(schema),
            sql.Identifier(table_name),
            sql.SQL(", ").join(sql.Placeholder() for _ in claim_ids),
        )
        cursor.execute(query, tuple(claim_ids))


def insert_rows(
    cursor: psycopg.Cursor[Any],
    table_name: str,
    rows: list[dict[str, Any]],
    schema: str,
    batch_rows: int,
) -> None:
    """Insert rows using multi-row VALUES statements.

    Redshift is a bulk-load engine; singleton INSERTs are very slow. Each
    statement packs as many rows as the 32k bound-parameter limit allows.
    """
    if not rows:
        return
    columns = TABLE_COLUMNS[table_name]
    n_cols = len(columns)
    max_rows = max(1, min(batch_rows, MAX_STATEMENT_PARAMS // max(1, n_cols)))

    base = sql.SQL("INSERT INTO {}.{} ({}) VALUES ").format(
        sql.Identifier(schema),
        sql.Identifier(table_name),
        sql.SQL(", ").join(map(sql.Identifier, columns)),
    )
    row_placeholder = sql.SQL("({})").format(
        sql.SQL(", ").join(sql.Placeholder() for _ in columns)
    )

    for start in range(0, len(rows), max_rows):
        chunk = rows[start:start + max_rows]
        values_clause = sql.SQL(", ").join(row_placeholder for _ in chunk)
        query = base + values_clause
        params: list[Any] = []
        for row in chunk:
            params.extend(normalize_value(row.get(column)) for column in columns)
        cursor.execute(query, params)


def _mark_loaded(tables: dict[str, list[dict[str, Any]]], loaded_at: datetime) -> None:
    summary = tables["claim_bundle_summary"][0]
    summary["redshift_load_status"] = "SUCCESS"
    summary["redshift_load_error"] = None
    summary["redshift_loaded_at"] = loaded_at


def load_successful_claim(
    connection: psycopg.Connection[Any],
    claim_id: str,
    tables: dict[str, list[dict[str, Any]]],
    options: FetchOptions,
) -> None:
    """Load a single claim in its own transaction (fallback / isolation path)."""
    _mark_loaded(tables, now())
    with connection.transaction():
        with connection.cursor() as cursor:
            delete_claim_rows(cursor, [claim_id], options.target_schema)
            for table_name in TABLE_ORDER:
                insert_rows(
                    cursor, table_name, tables.get(table_name, []),
                    options.target_schema, options.insert_batch_rows,
                )


def load_successful_batch(
    connection: psycopg.Connection[Any],
    claims: list[tuple[str, dict[str, list[dict[str, Any]]]]],
    options: FetchOptions,
) -> None:
    """Load many claims in one transaction: batched deletes then batched,
    multi-row inserts across all claims per table."""
    if not claims:
        return
    loaded_at = now()
    claim_ids = [claim_id for claim_id, _ in claims]
    for _, tables in claims:
        _mark_loaded(tables, loaded_at)

    with connection.transaction():
        with connection.cursor() as cursor:
            delete_claim_rows(cursor, claim_ids, options.target_schema)
            for table_name in TABLE_ORDER:
                combined: list[dict[str, Any]] = []
                for _, tables in claims:
                    combined.extend(tables.get(table_name, []))
                insert_rows(
                    cursor, table_name, combined,
                    options.target_schema, options.insert_batch_rows,
                )


def load_failed_summary(
    connection: psycopg.Connection[Any],
    claim_id: str,
    run_id: str,
    ingestion_date: date,
    started_at: datetime,
    extraction_error: str,
    options: FetchOptions,
) -> None:
    completed_at = now()
    failed_row = {column: None for column in SUMMARY_COLUMNS}
    failed_row.update({
        "registration_id": claim_id,
        "case_id": claim_id,
        "extraction_status": "FAILED",
        "extraction_error": extraction_error[:65535],
        "extraction_started_at": started_at,
        "extraction_completed_at": completed_at,
        "redshift_load_status": "NOT_ATTEMPTED",
        "redshift_load_error": None,
        "redshift_loaded_at": completed_at,
        "pipeline_run_id": run_id,
        "ingestion_date": ingestion_date,
    })
    with connection.transaction():
        with connection.cursor() as cursor:
            delete_claim_rows(cursor, [claim_id], options.target_schema)
            insert_rows(
                cursor, "claim_bundle_summary", [failed_row],
                options.target_schema, options.insert_batch_rows,
            )


# ------------------------------------------------------------------ reports


def row_counts(tables: dict[str, list[dict[str, Any]]] | None) -> dict[str, int]:
    tables = tables or {}
    return {
        "summary_rows": len(tables.get("claim_bundle_summary", [])),
        "form_field_rows": len(tables.get("claim_bundle_form_fields", [])),
        "document_rows": len(tables.get("claim_bundle_documents", [])),
        "diagnosis_rows": len(tables.get("claim_bundle_diagnosis", [])),
        "treatment_rows": len(tables.get("claim_bundle_treatments", [])),
        "investigation_rows": len(tables.get("claim_bundle_investigations", [])),
        "care_team_rows": len(tables.get("claim_bundle_care_team", [])),
        "package_amount_rows": len(tables.get("claim_bundle_package_amounts", [])),
    }


def build_report_row(run_id: str, result: dict[str, Any], load_status: str) -> dict[str, Any]:
    return {
        "pipeline_run_id": run_id,
        "registration_id": result["registration_id"],
        "download_status": result["download_status"],
        "extraction_status": result["extraction_status"],
        "redshift_load_status": load_status,
        **row_counts(result.get("tables")),
        "error": result.get("error"),
        "started_at": result["started_at"],
        "completed_at": result["completed_at"],
    }


def save_json_report(report_dir: Path, run_id: str, report_rows: list[dict[str, Any]]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"claim_bundle_pipeline_{run_id}.json"
    json_path.write_text(json.dumps(report_rows, indent=2, default=str), encoding="utf-8")
    return json_path


def _sheet_name(table_or_view: str) -> str:
    """Excel sheet name: <=31 chars, no reserved characters."""
    name = table_or_view.replace("claim_bundle_", "").replace("vw_claim_", "vw_")
    for char in r"[]:*?/\\":
        name = name.replace(char, "_")
    return name[:31] or "sheet"


def _fetch_dataframe(pd: Any, connection: psycopg.Connection[Any], query: Any, params: tuple[Any, ...]) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        rows = [
            [float(value) if isinstance(value, Decimal) else value for value in row]
            for row in cursor.fetchall()
        ]
    return pd.DataFrame(rows, columns=columns)


def save_excel_report(
    connection: psycopg.Connection[Any],
    run_id: str,
    claim_ids: list[str],
    report_rows: list[dict[str, Any]],
    options: FetchOptions,
) -> Path:
    """Write one .xlsx workbook for the run: a run_report sheet, one sheet per
    claim_bundle_* table (this run's rows), and one sheet per analytical view
    (this run's collapsed rows). Requires an open connection (queries Redshift)."""
    import pandas as pd

    report_dir = options.resolved_report_dir()
    report_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = report_dir / f"claim_bundle_pipeline_{run_id}.xlsx"
    schema = options.target_schema

    # Summary first, then the detail tables.
    ordered_tables = ["claim_bundle_summary"] + [
        t for t in TABLE_ORDER if t != "claim_bundle_summary"
    ]

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        pd.DataFrame(report_rows or [{"pipeline_run_id": run_id}]).to_excel(
            writer, sheet_name="run_report", index=False
        )

        for table in ordered_tables:
            query = sql.SQL(
                "SELECT * FROM {}.{} WHERE pipeline_run_id = %s ORDER BY registration_id"
            ).format(sql.Identifier(schema), sql.Identifier(table))
            try:
                frame = _fetch_dataframe(pd, connection, query, (run_id,))
            except Exception as exc:  # noqa: BLE001
                frame = pd.DataFrame({"error": [f"{type(exc).__name__}: {exc}"]})
            frame.to_excel(writer, sheet_name=_sheet_name(table), index=False)

        if claim_ids:
            placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in claim_ids)
            for view in REPORT_VIEWS:
                query = sql.SQL(
                    "SELECT * FROM {}.{} WHERE registration_id IN ({}) ORDER BY registration_id"
                ).format(
                    sql.Identifier(schema),
                    sql.Identifier(view),
                    placeholders,
                )
                try:
                    frame = _fetch_dataframe(pd, connection, query, tuple(claim_ids))
                except Exception as exc:  # noqa: BLE001
                    # View not created yet (or query error) - record a note, keep going.
                    frame = pd.DataFrame(
                        {"note": [f"view unavailable: {type(exc).__name__}: {exc}"]}
                    )
                frame.to_excel(writer, sheet_name=_sheet_name(view), index=False)

    return xlsx_path


def cleanup_bundles(destination: Path) -> None:
    destination = Path(destination)
    if not destination.exists():
        return
    for child in destination.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        elif child.is_file():
            child.unlink()


# -------------------------------------------------------------------- run


def select_claims(
    connection: psycopg.Connection[Any],
    options: FetchOptions,
) -> tuple[list[tuple[str, str | None]], str]:
    """Resolve the run's claims to (registration_id, preauth_path) pairs plus a
    human-readable description of where they came from. Shared by the /fetch
    preview endpoint and the run itself, so a preview cannot disagree with what
    a subsequent run selects."""
    filters = options.filters()
    if options.claim_ids:
        claims = queries.fetch_claims_by_ids(
            connection, list(options.claim_ids), options.source_table, filters,
        )
        source_desc = f"specified claim ids ({len(options.claim_ids)})"
    else:
        claims = queries.fetch_latest_claims(
            connection, options.limit, options.source_table, filters,
        )
        limit_desc = "none" if options.limit is None else str(options.limit)
        source_desc = f"latest claims (limit: {limit_desc})"
    return claims, source_desc


def preview_claims(options: FetchOptions) -> dict[str, Any]:
    """Run the selection query only - no downloads, no writes. Backs the
    "Preview claims" button, so a large or mis-filtered selection is visible
    before anything is pulled from S3."""
    with psycopg.connect(**options.connection_kwargs()) as connection:
        connection.autocommit = True
        claims, source_desc = select_claims(connection, options)

    claim_ids = [claim_id for claim_id, _ in claims]
    missing: list[str] = []
    if options.claim_ids:
        found = {claim_id for claim_id, preauth in claims if preauth}
        missing = [claim_id for claim_id in options.claim_ids if claim_id not in found]

    return {
        "claim_ids": claim_ids,
        "count": len(claim_ids),
        "source": source_desc,
        # How the filters were actually read, so a comma-vs-pipe mistake shows
        # up before anything is downloaded.
        "filters": options.filters().describe(),
        # Ids with no json_object_perauth in the source table: still processed,
        # but the downloader has to fall back to its own lookup for them.
        "without_preauth_path": missing,
    }


def run_pipeline(
    options: FetchOptions,
    on_event: EventCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Download, extract and (optionally) load one batch of claims.

    Returns a result dict: run_id, the selected claim ids, the per-claim report
    rows, report paths and counts. Progress is streamed through `on_event`
    (kinds: log, selected, claim_rows, claim_done, phase, finished).
    """
    emit = _Emitter(on_event)
    run_id = options.run_id or new_run_id()
    ingestion_date = date.today()
    destination = Path(options.destination)
    destination.mkdir(parents=True, exist_ok=True)

    def cancelled() -> bool:
        return bool(cancel_event and cancel_event.is_set())

    # ---- Selection (own connection: the download phase below can run for a
    # long time, and holding an idle warehouse connection across it is how you
    # get a dead socket at load time).
    with psycopg.connect(**options.connection_kwargs()) as connection:
        connection.autocommit = True
        claims, source_desc = select_claims(connection, options)
        claim_ids = [claim_id for claim_id, _ in claims]

        claim_rows: list[dict[str, str]] = []
        if options.collect_claim_rows and claim_ids:
            try:
                claim_rows = queries.fetch_claim_rows(
                    connection, claim_ids, options.source_table
                )
            except Exception as exc:  # noqa: BLE001 - metadata is a nice-to-have
                emit.log(
                    f"claim metadata lookup failed: {type(exc).__name__}: {exc}",
                    level="warning",
                )

    described = options.filters().describe()
    emit.log(f"pipeline_run_id: {run_id}")
    emit.log(f"source: {source_desc}")
    emit.log(f"filters: {described['policy']}; hospital type: {described['hospital_type']}")
    emit.log(f"procedure codes: include {described['include']}; exclude {described['exclude']}")
    emit.log(f"claims selected: {len(claims)}")
    emit(
        "selected",
        run_id=run_id,
        claim_ids=claim_ids,
        source=source_desc,
        filters=described,
        destination=str(destination),
    )
    if claim_rows:
        emit("claim_rows", rows=claim_rows)

    # ---- Phase 1: download + extract concurrently (no DB access) ------------
    emit("phase", phase="downloading", total=len(claims))
    pool_size = options.claim_workers * options.download_workers + 4
    s3_client = downloader.build_s3_client(max_pool_connections=pool_size)

    results: list[dict[str, Any]] = []
    total = len(claims)
    done_lock = threading.Lock()
    done = 0

    def _work(item: tuple[str, str | None]) -> dict[str, Any]:
        nonlocal done
        claim_id, preauth_path = item
        if cancelled():
            result = {
                "registration_id": claim_id,
                "download_status": "CANCELLED",
                "extraction_status": "CANCELLED",
                "tables": None,
                "error": "Cancelled before this claim started",
                "started_at": now(),
                "completed_at": now(),
            }
        else:
            result = process_claim(
                s3_client, claim_id, preauth_path, run_id, ingestion_date, options
            )
        with done_lock:
            done += 1
            position = done
        if result["tables"] is not None:
            # Hand the extracted datasets to the caller as soon as they exist,
            # rather than only in the final return value: a run that is
            # cancelled or dies during the load has still done this work, and
            # the review side can use it.
            emit(
                "claim_extracted",
                registration_id=claim_id,
                run_id=run_id,
                tables=result["tables"],
            )
        emit(
            "claim_done",
            registration_id=claim_id,
            download_status=result["download_status"],
            extraction_status=result["extraction_status"],
            error=result["error"],
            done=position,
            total=total,
        )
        return result

    if claims:
        workers = max(1, min(options.claim_workers, len(claims)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_work, claims))

    # ---- Phase 2: load into Redshift (main thread, batched) ----------------
    report_rows: list[dict[str, Any]] = []
    successful = [
        (r["registration_id"], r["tables"])
        for r in results
        if r["extraction_status"] in {"SUCCESS", "PARTIAL"} and r["tables"] is not None
    ]
    load_status_by_claim: dict[str, str] = {}
    xlsx_report: Path | None = None

    if not options.load_redshift:
        emit("phase", phase="skipping_load")
        emit.log("Redshift load skipped for this run (extract-only).")
        for result in results:
            status = result["extraction_status"]
            load_status_by_claim[result["registration_id"]] = (
                "SKIPPED" if status in {"SUCCESS", "PARTIAL"} else "NOT_ATTEMPTED"
            )
    else:
        emit("phase", phase="loading", total=len(successful))
        with psycopg.connect(**options.connection_kwargs()) as connection:
            connection.autocommit = True

            if successful:
                try:
                    load_successful_batch(connection, successful, options)
                    for claim_id, _ in successful:
                        load_status_by_claim[claim_id] = "SUCCESS"
                    emit.log(f"batch load committed: {len(successful)} claims")
                except Exception as exc:  # noqa: BLE001
                    emit.log(
                        f"batch load failed ({type(exc).__name__}: {exc}); "
                        "retrying claim-by-claim to isolate the failure",
                        level="error",
                    )
                    for claim_id, tables in successful:
                        try:
                            load_successful_claim(connection, claim_id, tables, options)
                            load_status_by_claim[claim_id] = "SUCCESS"
                        except Exception as claim_exc:  # noqa: BLE001
                            err = (
                                f"Redshift load failed: "
                                f"{type(claim_exc).__name__}: {claim_exc}"
                            )
                            emit.log(f"{claim_id}: {err}", level="error")
                            # find the originating result to preserve its timing
                            origin = next(
                                r for r in results if r["registration_id"] == claim_id
                            )
                            try:
                                load_failed_summary(
                                    connection, claim_id, run_id, ingestion_date,
                                    origin["started_at"], err, options,
                                )
                                load_status_by_claim[claim_id] = "FAILED_SUMMARY_RECORDED"
                                origin["error"] = err
                                origin["extraction_status"] = "FAILED"
                            except Exception as summary_exc:  # noqa: BLE001
                                load_status_by_claim[claim_id] = "FAILED"
                                origin["error"] = (
                                    f"{err} | Failed to record summary: "
                                    f"{type(summary_exc).__name__}: {summary_exc}"
                                )

            # Record claims that failed download/extraction.
            for result in results:
                claim_id = result["registration_id"]
                if claim_id in load_status_by_claim:
                    continue
                if result["extraction_status"] == "CANCELLED":
                    load_status_by_claim[claim_id] = "NOT_ATTEMPTED"
                    continue
                error = result.get("error") or "Claim download or extraction failed"
                try:
                    load_failed_summary(
                        connection, claim_id, run_id, ingestion_date,
                        result["started_at"], error, options,
                    )
                    load_status_by_claim[claim_id] = "FAILED_SUMMARY_RECORDED"
                except Exception as load_exc:  # noqa: BLE001
                    load_status_by_claim[claim_id] = "FAILED"
                    result["error"] = (
                        f"{error} | Failed to record summary: "
                        f"{type(load_exc).__name__}: {load_exc}"
                    )
                    emit.log(result["error"], level="error")

            for result in results:
                report_rows.append(
                    build_report_row(
                        run_id, result,
                        load_status_by_claim.get(result["registration_id"], "NOT_STARTED"),
                    )
                )

            # The Excel report reads this run's loaded rows and views, so it must
            # be written while the connection is still open.
            if options.write_reports:
                emit("phase", phase="reporting")
                try:
                    xlsx_report = save_excel_report(
                        connection, run_id, claim_ids, report_rows, options
                    )
                    emit.log(f"XLSX report: {xlsx_report}")
                except Exception as exc:  # noqa: BLE001
                    emit.log(
                        f"XLSX report failed: {type(exc).__name__}: {exc}", level="error"
                    )

    if not report_rows:
        report_rows = [
            build_report_row(
                run_id, result,
                load_status_by_claim.get(result["registration_id"], "NOT_STARTED"),
            )
            for result in results
        ]

    json_report: Path | None = None
    if options.write_reports:
        try:
            json_report = save_json_report(
                options.resolved_report_dir(), run_id, report_rows
            )
            emit.log(f"JSON report: {json_report}")
        except Exception as exc:  # noqa: BLE001
            emit.log(f"JSON report failed: {type(exc).__name__}: {exc}", level="error")

    # Cleanup only after every claim has a recorded Redshift status and the
    # batch report has been written.
    ok_statuses = (
        {"SUCCESS", "FAILED_SUMMARY_RECORDED"}
        if options.load_redshift
        else {"SKIPPED", "NOT_ATTEMPTED"}
    )
    cleanup_allowed = bool(report_rows) and all(
        row["redshift_load_status"] in ok_statuses for row in report_rows
    )
    if not options.cleanup:
        cleanup_message = "disabled (bundles kept)"
    elif cancelled():
        cleanup_message = "skipped because the run was cancelled"
    elif cleanup_allowed:
        emit("phase", phase="cleanup")
        cleanup_bundles(destination)
        cleanup_message = "completed"
    else:
        cleanup_message = "skipped because one or more Redshift status records failed"
    emit.log(f"local bundle cleanup: {cleanup_message}")

    succeeded = [
        row["registration_id"] for row in report_rows
        if row["redshift_load_status"] in {"SUCCESS", "SKIPPED"}
    ]
    failures = len(report_rows) - len(succeeded)
    emit.log(f"successful claims: {len(succeeded)}")
    emit.log(f"non-success claims: {failures}")

    result = {
        "run_id": run_id,
        "claim_ids": claim_ids,
        "succeeded_claim_ids": succeeded,
        "report_rows": report_rows,
        "claim_rows": claim_rows,
        "destination": str(destination),
        "source": source_desc,
        "json_report": str(json_report) if json_report else None,
        "xlsx_report": str(xlsx_report) if xlsx_report else None,
        "cleanup": cleanup_message,
        "cancelled": cancelled(),
        "claims_total": len(report_rows),
        "claims_ok": len(succeeded),
        "claims_failed": failures,
    }
    emit("finished", **{k: v for k, v in result.items() if k not in {"report_rows", "claim_rows"}})
    return result
