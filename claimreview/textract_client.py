import threading

import boto3
from botocore.config import Config

FEATURE_TYPES = ["FORMS", "TABLES", "SIGNATURES", "LAYOUT", "QUERIES"]

_client = None
_client_region = None


def _get_client(region):
    """One boto3 Textract client per (process, region); rebuilt if the
    configured region changes (e.g. under test). Takes `region` explicitly
    (rather than reading current_app.config itself) so this - and
    analyze_page below - work from a plain worker thread with no Flask
    request/app context, which is what ThreadPoolExecutor-submitted calls
    run in. "adaptive" retry mode adds client-side rate limiting on top of
    exponential backoff, which pairs well with running several calls
    concurrently (see analyze_page_throttled below) - if AWS starts
    throttling, boto3 automatically slows down rather than just failing
    after max_attempts."""
    global _client, _client_region
    if _client is None or _client_region != region:
        _client = boto3.Session().client(
            "textract", region_name=region,
            config=Config(retries={"max_attempts": 8, "mode": "adaptive"}),
        )
        _client_region = region
    return _client


_semaphore_lock = threading.Lock()
_semaphore = None
_semaphore_size = None


def _get_semaphore(max_concurrency):
    """One process-wide semaphore, resized on the fly if the configured
    concurrency changes between calls. Shared across every claim being
    processed at once (not scoped per claim/thread-pool), so batch-
    processing several claims simultaneously still respects a single
    overall cap on in-flight Textract calls - the whole point of the cap
    is staying under the AWS account's Textract TPS quota, which applies
    account-wide, not per claim."""
    global _semaphore, _semaphore_size
    with _semaphore_lock:
        if _semaphore is None or _semaphore_size != max_concurrency:
            _semaphore = threading.Semaphore(max_concurrency)
            _semaphore_size = max_concurrency
        return _semaphore


def _child_text(block, blocks_map):
    text = ""
    if "Relationships" not in block:
        return text
    for rel in block["Relationships"]:
        if rel["Type"] != "CHILD":
            continue
        for child_id in rel["Ids"]:
            child = blocks_map.get(child_id)
            if not child:
                continue
            if child["BlockType"] in ("WORD", "LINE"):
                text += child["Text"] + " "
            elif child["BlockType"] == "SELECTION_ELEMENT" and child["SelectionStatus"] == "SELECTED":
                text += "[X] "
    return text.strip()


def analyze_page(jpeg_bytes, region, queries):
    """One analyze_document call for a single page image, combining
    FORMS+TABLES+SIGNATURES+LAYOUT+QUERIES, parsed into a page-level dict:

    {forms: [{key, value, confidence, key_bbox, value_bbox}],
     tables: [2D matrix, ...],
     signatures: [{signature_id, confidence, bbox}],
     queries: {alias: {answer, confidence}}}

    `region`/`queries` are passed in explicitly (from current_app.config by
    callers that have a Flask context) rather than read from current_app
    here, so this also works unchanged from a plain worker thread with no
    app context - see _get_client's docstring.
    """
    client = _get_client(region)
    queries_config = {"Queries": queries}

    response = client.analyze_document(
        Document={"Bytes": jpeg_bytes},
        FeatureTypes=FEATURE_TYPES,
        QueriesConfig=queries_config,
    )

    blocks_map = {b["Id"]: b for b in response["Blocks"]}
    result = {"forms": [], "tables": [], "signatures": [], "queries": {}}

    value_blocks = {}
    key_blocks = []

    for block in response["Blocks"]:
        block_type = block["BlockType"]

        if block_type == "QUERY":
            alias = block["Query"]["Alias"]
            for rel in block.get("Relationships", []):
                if rel["Type"] != "ANSWER":
                    continue
                for target_id in rel["Ids"]:
                    ans = blocks_map.get(target_id)
                    if ans and ans["BlockType"] == "QUERY_RESULT":
                        result["queries"][alias] = {
                            "answer": ans["Text"],
                            "confidence": ans["Confidence"],
                        }

        elif block_type == "SIGNATURE":
            result["signatures"].append({
                "signature_id": block["Id"],
                "confidence": block["Confidence"],
                "bbox": block["Geometry"]["BoundingBox"],
            })

        elif block_type == "KEY_VALUE_SET":
            if "KEY" in block.get("EntityTypes", []):
                key_blocks.append(block)
            else:
                value_blocks[block["Id"]] = block

        elif block_type == "TABLE":
            table_grid = {}
            max_row = max_col = 0
            for rel in block.get("Relationships", []):
                if rel["Type"] != "CHILD":
                    continue
                for cell_id in rel["Ids"]:
                    cell = blocks_map.get(cell_id)
                    if cell and cell["BlockType"] == "CELL":
                        r, c = cell["RowIndex"], cell["ColumnIndex"]
                        table_grid[(r, c)] = _child_text(cell, blocks_map)
                        max_row, max_col = max(max_row, r), max(max_col, c)
            matrix = [[table_grid.get((r, c), "") for c in range(1, max_col + 1)]
                      for r in range(1, max_row + 1)]
            result["tables"].append(matrix)

    for key_block in key_blocks:
        key_text = _child_text(key_block, blocks_map)
        if not key_text:
            continue
        value_text, value_confidence, value_bbox = "", 0.0, None
        for rel in key_block.get("Relationships", []):
            if rel["Type"] != "VALUE":
                continue
            for value_id in rel["Ids"]:
                value_block = value_blocks.get(value_id)
                if value_block:
                    value_text = _child_text(value_block, blocks_map)
                    value_confidence = value_block.get("Confidence", 0.0)
                    value_bbox = value_block["Geometry"]["BoundingBox"]

        result["forms"].append({
            "key": key_text,
            "value": value_text,
            "confidence": value_confidence,
            "key_bbox": key_block["Geometry"]["BoundingBox"],
            "value_bbox": value_bbox,
        })

    return result


def analyze_page_throttled(jpeg_bytes, region, queries, max_concurrency):
    """Same as analyze_page, but acquires the shared process-wide semaphore
    (see _get_semaphore) first, so at most `max_concurrency` Textract calls
    - across every claim being processed at once, not just the caller's -
    are ever in flight at a time. Meant to be handed to a thread pool so a
    batch of pages/documents can be analyzed concurrently instead of one
    call at a time, while still respecting the account's Textract TPS
    quota via this cap."""
    semaphore = _get_semaphore(max_concurrency)
    with semaphore:
        return analyze_page(jpeg_bytes, region, queries)
