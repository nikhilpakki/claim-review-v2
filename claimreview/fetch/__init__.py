"""Claim fetching: pull claim bundles from client S3 into a review root.

Vendored from the claim-bundle-extraction-v2 repo:

    downloader.py  <- sync_claim_bundles.py         (S3 download + base64 decode)
    extractor.py   <- export_claim_bundle_excels.py (BundleContext + 8 extractors)
    tables.py      <- the destination-table constants
    queries.py     <- claim selection against Redshift
    pipeline.py    <- daily_claim_bundle_pipeline.py, as run_pipeline()
    cli.py         <- the original command-line interface

Nothing is imported here on purpose: pipeline.py pulls in psycopg, boto3,
pandas and openpyxl, and the review side of the app must keep working (minus
/fetch) if those are not installed yet.
"""
