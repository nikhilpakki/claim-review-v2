"""WSGI entry point for gunicorn.

Deliberately a single worker with threads (see deploy/claim-review.service).
The app keeps per-run progress, the fetch registry and the Textract
concurrency cap in module-level state, so a second worker process would poll
progress it cannot see and would multiply the Textract cap by the worker
count. The work here is I/O-bound - S3, Textract, disk - so threads are the
right lever anyway; raise the thread count, not the worker count.
"""
from claimreview import create_app

app = create_app()
