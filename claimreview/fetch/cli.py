"""Command-line entry point for the claim-bundle pipeline.

Preserves the original `python daily_claim_bundle_pipeline.py ...` interface so
the scheduled job keeps working unchanged after the code moved into the review
app:

    python -m claimreview.fetch.cli                       # latest 100, keep bundles
    python -m claimreview.fetch.cli --limit 500
    python -m claimreview.fetch.cli --limit false         # no cap
    python -m claimreview.fetch.cli --claims-csv ids.csv
    python -m claimreview.fetch.cli --cleanup

The run itself is pipeline.run_pipeline() - the same function the /fetch page
calls - so the two paths cannot drift apart.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import queries
from .pipeline import DEFAULT_CLAIM_LIMIT, FetchOptions, run_pipeline


def _limit(value: str) -> int | None:
    try:
        return queries.parse_limit(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="claimreview.fetch.cli",
        description="Claim-bundle pipeline: download, extract, and load claims into Redshift.",
    )
    parser.add_argument(
        "--limit",
        type=_limit,
        default=DEFAULT_CLAIM_LIMIT,
        metavar="N",
        help=f"Max number of latest claims to fetch (default {DEFAULT_CLAIM_LIMIT}). "
        "Pass false/none/all/0 for no limit. Ignored when --claims-csv is used.",
    )
    parser.add_argument(
        "--claims-csv",
        type=Path,
        default=None,
        metavar="PATH",
        help="CSV of registration_ids to process instead of the latest claims. "
        "Uses a 'registration_id' column if present, else the first column.",
    )
    parser.add_argument(
        "--procedure-codes",
        default=None,
        metavar="CODES",
        help="Only include claims whose procedure_code list contains these. "
        "Pipe = alternatives, comma = all required: 'A|B' means A or B, 'A,B' "
        "means A and B, 'A|B,C' means (A or B) and C. Each code is matched as a "
        "complete item in the claim's pipe-delimited procedure list, and is "
        "still a case-insensitive regex ('LB.*' for a prefix). "
        "Default: no procedure-code filter.",
    )
    parser.add_argument(
        "--exclude-procedure-codes",
        default=queries.DEFAULT_EXCLUDE_PROCEDURE_CODES,
        metavar="CODES",
        help="Drop claims matching this expression, same syntax as "
        f"--procedure-codes (default {queries.DEFAULT_EXCLUDE_PROCEDURE_CODES!r}). "
        "'A|B' drops claims containing either; 'A,B' drops only claims "
        "containing both. Pass an empty string to exclude nothing.",
    )
    parser.add_argument(
        "--hospital-type",
        default=None,
        choices=sorted(queries.HOSPITAL_TYPES),
        help="Only include claims from hospitals of this type "
        + ", ".join(f"{k} = {v}" for k, v in sorted(queries.HOSPITAL_TYPES.items()))
        + ". Default: no hospital-type filter.",
    )
    parser.add_argument(
        "--convergence",
        action="store_true",
        default=False,
        help="Include non-PMJAY policies by removing the policy_code LIKE "
        "'PMJAY%%' filter. Default: filter to PMJAY policies only.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        default=False,
        help="Delete local claim bundles after a fully successful run. "
        "By default the bundles are kept.",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path.cwd() / "data" / "claim-bundle",
        metavar="PATH",
        help="Where claim bundles are written (default ./data/claim-bundle).",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=Path.cwd() / "pipeline_reports",
        metavar="PATH",
        help="Where the JSON/XLSX run reports are written (default ./pipeline_reports).",
    )
    parser.add_argument(
        "--no-load",
        action="store_true",
        default=False,
        help="Download and extract only; skip the Redshift load (and the XLSX "
        "report, which reads the loaded rows).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path.cwd() / ".env",
        metavar="PATH",
        help="Environment file with the warehouse credentials (default ./.env).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.env_file and Path(args.env_file).is_file():
        load_dotenv(args.env_file, override=False)

    claim_ids: list[str] | None = None
    if args.claims_csv is not None:
        csv_path = args.claims_csv.expanduser()
        if not csv_path.is_file():
            raise FileNotFoundError(f"Claims CSV not found: {csv_path}")
        claim_ids = queries.read_claim_ids_from_csv(csv_path)
        if not claim_ids:
            raise RuntimeError(f"No registration_ids found in {csv_path}")

    options = FetchOptions(
        destination=args.destination,
        claim_ids=claim_ids,
        limit=args.limit,
        convergence=args.convergence,
        procedure_codes=args.procedure_codes,
        exclude_procedure_codes=args.exclude_procedure_codes,
        hospital_type=args.hospital_type,
        load_redshift=not args.no_load,
        write_reports=True,
        report_dir=args.reports_dir,
        cleanup=args.cleanup,
        # The claims dataset is a review-app concept; a CLI run has nowhere to
        # put it, so skip the extra query.
        collect_claim_rows=False,
    )

    def on_event(kind: str, payload: dict) -> None:
        if kind == "log":
            stream = sys.stderr if payload.get("level") in {"error", "warning"} else sys.stdout
            print(payload["message"], file=stream)
        elif kind == "claim_done":
            print(
                f"[{payload['done']}/{payload['total']}] {payload['registration_id']} "
                f"download={payload['download_status']} "
                f"extract={payload['extraction_status']}"
            )

    result = run_pipeline(options, on_event=on_event)
    return 0 if result["claims_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
