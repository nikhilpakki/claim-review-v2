"""Glue between the /fetch page and the pipeline.

pipeline.py knows nothing about Flask; this module owns everything that does:
building a run's options from the submitted form, starting the worker thread,
and fanning the pipeline's progress events out to the live registry
(fetch_progress), the durable run record (runs.py) and the claims dataset
(csv_data).
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Any

from .. import claim_extract, csv_data, fetch_progress, users
from . import queries, runs
from .pipeline import FetchOptions, new_run_id, preview_claims, run_pipeline


class FetchInputError(ValueError):
    """A problem with what the user submitted, safe to show them verbatim."""


def _checked(form, name):
    """Read a checkbox out of a submitted form.

    An unchecked checkbox is simply absent from the submission, so absence
    means False - never a configured default. Defaults belong to the *rendered*
    form (the template ticks the box); applying them again here would make
    unticking a default-on box impossible, which is how an early version
    silently loaded to Redshift after the user had turned that off.
    """
    return str(form.get(name, "")).strip().lower() not in {"", "0", "false", "off", "no"}


def _resolve_hypothesis(raw_id, config):
    """(id, hypothesis, windows) for a selected hypothesis, or raise.

    A hypothesis has to have been processed before a fetch can run under it:
    the point of fetching under one is to work towards its sample size, and an
    unprocessed hypothesis has no sample size to work towards.

    The period is resolved against the *warehouse's* current_date rather than
    this machine's - they are different hosts, and "today" has to mean what the
    warehouse means by it or the window silently covers the wrong days.
    """
    from .. import hypotheses  # local: hypotheses imports queries, so this would cycle

    try:
        hypothesis_id = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise FetchInputError(f"{raw_id!r} is not a hypothesis id.") from exc

    hypothesis = hypotheses.get_hypothesis(hypothesis_id)
    if hypothesis is None:
        raise FetchInputError("That hypothesis no longer exists.")
    if not hypothesis["is_processed"]:
        raise FetchInputError(
            f"'{hypothesis['title']}' has not been processed yet, so it has no sample size. "
            "Process it on the Hypotheses page first.")

    with queries.connect(config) as connection:
        windows, _from, _to = hypotheses.windows_for(hypothesis, connection, config)
    if not windows:
        raise FetchInputError(
            f"'{hypothesis['title']}' covers no dates that the warehouse holds claims for.")
    return hypothesis_id, hypothesis, windows


def build_options(form, files=None, config=None, run_id=None) -> FetchOptions:
    """Turn the /fetch form into a FetchOptions. Raises FetchInputError with a
    message meant for the form, not a stack trace."""
    config = config or {}

    destination = (form.get("destination") or "").strip() or config.get("BUNDLES_ROOT")
    if not destination:
        raise FetchInputError("Choose a destination folder for the downloaded claims.")

    source = (form.get("source") or "latest").strip()
    claim_ids: list[str] | None = None
    limit: int | None = None

    if source == "ids":
        claim_ids = queries.read_claim_ids_from_text(form.get("claim_ids") or "")
        upload = (files or {}).get("claims_csv") if files is not None else None
        if upload is not None and getattr(upload, "filename", ""):
            from_csv = queries.read_claim_ids_from_bytes(upload.read())
            seen = set(claim_ids)
            claim_ids.extend(cid for cid in from_csv if cid not in seen)
        if not claim_ids:
            raise FetchInputError(
                "No registration IDs found. Paste them (one per line or "
                "comma-separated) or upload a CSV with a registration_id column."
            )
    else:
        try:
            limit = queries.parse_limit(form.get("limit", ""))
        except ValueError as exc:
            raise FetchInputError(str(exc)) from exc

    # Passed through verbatim - ClaimFilters owns the comma/pipe grammar, so
    # the form, the CLI and the preview all read an expression the same way.
    procedure_codes = (form.get("procedure_codes") or "").strip() or None
    exclude_codes = (form.get("exclude_procedure_codes") or "").strip() or None
    hospital_type = (form.get("hospital_type") or "").strip().upper() or None

    # Hypothesis mode. The overlapping fields are taken from the stored
    # hypothesis, never from the submitted form: the form greys them out, and a
    # greyed field is a UI courtesy, not a guarantee - a disabled input is
    # simply absent from the submission and a crafted one could say anything.
    # Reading them from the row is what makes the claims actually match the
    # hypothesis the reviews will be counted against.
    hypothesis_id, hypothesis_windows, hypothesis_label = None, None, None
    convergence_override = False
    raw_hypothesis = (form.get("hypothesis_id") or "").strip()
    if raw_hypothesis:
        hypothesis_id, hypothesis, hypothesis_windows = _resolve_hypothesis(raw_hypothesis, config)
        procedure_codes = hypothesis["procedure_codes"] or None
        exclude_codes = hypothesis["exclude_procedure_codes"] or None
        convergence_override = bool(hypothesis["include_non_pmjay"])
        hypothesis_label = hypothesis["title"]
    if hospital_type and hospital_type not in queries.HOSPITAL_TYPES:
        raise FetchInputError(
            f"Unknown hospital type {hospital_type!r}; expected one of "
            + ", ".join(sorted(queries.HOSPITAL_TYPES))
        )

    return FetchOptions(
        destination=Path(destination),
        claim_ids=claim_ids,
        limit=limit,
        convergence=(convergence_override if raw_hypothesis
                     else _checked(form, "convergence")),
        hypothesis_id=hypothesis_id,
        hypothesis_windows=hypothesis_windows,
        hypothesis_period_column=config.get("HYPOTHESIS_PERIOD_COLUMN", "claim_init_date"),
        hypothesis_label=hypothesis_label,
        procedure_codes=procedure_codes,
        exclude_procedure_codes=exclude_codes,
        hospital_type=hospital_type,
        load_redshift=_checked(form, "load_redshift"),
        # Ticked by default in the form; unticking forces a re-download of
        # claims already on disk.
        skip_existing=_checked(form, "skip_existing"),
        write_reports=_checked(form, "write_reports"),
        report_dir=Path(config["PIPELINE_REPORT_DIR"]) if config.get("PIPELINE_REPORT_DIR") else None,
        # Never delete bundles from a run started in the app: those files are
        # what the review side displays. The CLI's --cleanup still exists.
        cleanup=False,
        source_table=config.get("CLAIM_SOURCE_TABLE") or FetchOptions.source_table,
        lookup_tables=config.get("CLAIM_LOOKUP_TABLES"),
        target_schema=config.get("CLAIM_TARGET_SCHEMA") or FetchOptions.target_schema,
        source_bucket=config.get("S3_SOURCE_BUCKET") or FetchOptions.source_bucket,
        claim_workers=int(config.get("FETCH_CLAIM_WORKERS", 8)),
        download_workers=int(config.get("FETCH_DOWNLOAD_WORKERS", 4)),
        insert_batch_rows=int(config.get("FETCH_INSERT_BATCH_ROWS", 500)),
        run_id=run_id,
        db_config=config,
    )


def describe_options(options: FetchOptions) -> dict[str, Any]:
    """The run's parameters, for the run record and the UI - no credentials."""
    return {
        "destination": str(options.destination),
        "claim_ids": options.claim_ids,
        "limit": options.limit,
        "convergence": options.convergence,
        "hypothesis_id": options.hypothesis_id,
        "hypothesis_label": options.hypothesis_label,
        "hypothesis_windows": [[table, queries.window_bound(lo), queries.window_bound(hi)]
                               for table, lo, hi in (options.hypothesis_windows or [])],
        "procedure_codes": options.procedure_codes,
        "exclude_procedure_codes": options.exclude_procedure_codes,
        "hospital_type": options.hospital_type,
        "filters": options.filters().describe(),
        "load_redshift": options.load_redshift,
        "write_reports": options.write_reports,
        "source_table": options.source_table,
        "skip_existing": options.skip_existing,
    }


def preview(options: FetchOptions) -> dict[str, Any]:
    return preview_claims(options)


def start(app, options: FetchOptions) -> str:
    """Start a fetch in a background thread and return its run id.

    Only one fetch runs at a time - it owns the S3 client pool and the
    warehouse connection, and two concurrent runs writing the same destination
    would race on the same claim folders.
    """
    active = fetch_progress.active_run_id()
    if active:
        state = fetch_progress.get(active) or {}
        who = (state.get("params") or {}).get("started_by")
        progress = f"{state.get('done', 0)}/{state.get('total', 0)} claims"
        raise FetchInputError(
            f"A fetch is already running ({progress})"
            + (f", started by {who}" if who else "")
            + ". Only one runs at a time - it owns the S3 connections and the "
              "destination folder. Watch it below, or try again when it finishes.")

    run_id = options.run_id or new_run_id()
    options.run_id = run_id
    params = describe_options(options)

    user = users.current_user()
    params["started_by"] = user["display_name"] if user else None
    cancel_event = fetch_progress.start(run_id, str(options.destination), params)
    runs.create_run(run_id, options.destination, params,
                    user_id=user["id"] if user else None,
                    display_name=user["display_name"] if user else None,
                    hypothesis_id=options.hypothesis_id)

    thread = threading.Thread(
        target=_run, args=(app, options, run_id, cancel_event), daemon=True
    )
    thread.start()
    return run_id


def _handle_event(app, run_id: str, kind: str, payload: dict[str, Any]) -> None:
    """Fan one pipeline event out to the live registry and the run record.

    Two things to know about where this runs:

    - `claim_done` is emitted from the pipeline's ThreadPoolExecutor workers,
      not from the thread that called run_pipeline(). Flask contexts are
      thread-local, so the `with app.app_context()` in _run() does NOT cover
      those threads - every database write here has to push its own context or
      it dies with "Working outside of application context".
    - The in-memory update is done first and unconditionally. If the database
      write fails, the live progress the user is watching must still be right.

    Persistence failures are logged into the run's own log rather than raised:
    the pipeline's emitter swallows exceptions anyway, and a locked SQLite file
    must not cost us a download that is already on disk.
    """
    try:
        if kind == "log":
            fetch_progress.log(run_id, payload["message"], payload.get("level", "info"))

        elif kind == "selected":
            fetch_progress.set_selected(run_id, payload["claim_ids"], payload.get("source"))
            fetch_progress.update(run_id, phase="downloading")

        elif kind == "claim_done":
            fetch_progress.claim_done(
                run_id, payload["registration_id"],
                payload["download_status"], payload["extraction_status"],
                payload["extraction_status"] in {"SUCCESS", "PARTIAL"},
            )

        elif kind == "phase":
            fetch_progress.update(run_id, phase=payload["phase"])

    except Exception:  # noqa: BLE001
        traceback.print_exc()

    if kind not in {"selected", "claim_rows", "claim_done", "claim_extracted"}:
        return

    try:
        with app.app_context():
            if kind == "selected":
                runs.record_selection(run_id, payload["claim_ids"], payload.get("source"))

            elif kind == "claim_rows":
                rows = payload.get("rows") or []
                if rows:
                    added = csv_data.upsert_claim_rows(rows, source=f"fetch run {run_id}")
                    fetch_progress.log(
                        run_id,
                        f"claims dataset updated: {added} row(s) from the source table",
                    )

            elif kind == "claim_extracted":
                claim_extract.put(
                    payload["registration_id"], payload["run_id"], payload["tables"]
                )

            elif kind == "claim_done":
                runs.record_claim(
                    run_id, payload["registration_id"],
                    payload["download_status"], payload["extraction_status"],
                    payload.get("error"),
                )
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        fetch_progress.log(
            run_id, f"recording a {kind} event failed (the run continues)", level="warning"
        )


def _run(app, options: FetchOptions, run_id: str, cancel_event: threading.Event) -> None:
    """Worker-thread body. The app context gives this thread its own SQLite
    connection (they cannot be shared between threads) and access to config."""
    with app.app_context():
        try:
            result = run_pipeline(
                options,
                on_event=lambda kind, payload: _handle_event(app, run_id, kind, payload),
                cancel_event=cancel_event,
            )
            try:
                runs.record_report_rows(run_id, result["report_rows"])
            except Exception:  # noqa: BLE001
                traceback.print_exc()

            status = "cancelled" if result.get("cancelled") else "completed"
            runs.finish_run(run_id, status, result=result)
            fetch_progress.finish(run_id, status, result=_public_result(result))

        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            fetch_progress.log(run_id, error, level="error")
            try:
                runs.finish_run(run_id, "failed", error=error)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            fetch_progress.finish(run_id, "failed", error=error)


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    """The finished-run summary the browser gets: counts and report paths, not
    the full per-claim table payloads."""
    return {
        "run_id": result["run_id"],
        "destination": result["destination"],
        "claims_total": result["claims_total"],
        "claims_ok": result["claims_ok"],
        "claims_failed": result["claims_failed"],
        "succeeded_claim_ids": result["succeeded_claim_ids"],
        "json_report": result["json_report"],
        "xlsx_report": result["xlsx_report"],
        "cleanup": result["cleanup"],
        "cancelled": result["cancelled"],
    }
