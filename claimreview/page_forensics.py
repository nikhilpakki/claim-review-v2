"""Page-level structural heuristics: cropped/cut-off edges and possible
text corrections/strikethroughs. Like image_quality.py and
signature_forensics.py, these return raw measurements only - thresholding
into a tag happens live in classify.py against current settings.

The correction-hotspot check is the least reliable of all the checks in
this app: cv2 alone can't distinguish "someone struck through a word" from
"there's a stamp/table/dense signature here". It's off by default
(config.ENABLE_CORRECTION_CHECK) and labeled low-confidence in the UI.
"""
import cv2
import numpy as np


def _decode(jpeg_bytes):
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _bbox_to_pixels(bbox, width, height, margin=0.0):
    left = max(0.0, bbox["Left"] - margin) * width
    top = max(0.0, bbox["Top"] - margin) * height
    right = min(1.0, bbox["Left"] + bbox["Width"] + margin) * width
    bottom = min(1.0, bbox["Top"] + bbox["Height"] + margin) * height
    return int(left), int(top), max(int(right), int(left) + 1), max(int(bottom), int(top) + 1)


def _ink_mask(gray):
    """Binary mask, 255 where a pixel is "ink" (dark relative to the page's
    own Otsu threshold) - works across scans of varying overall brightness."""
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return mask


def detect_cropped_edges(jpeg_bytes):
    """For each of the 4 edges: how close (px) genuine content comes to
    that edge, and how dense the content is in a thin strip along it.
    Distinguishes a natural margin (content stops well before the edge)
    from a page that's visibly cut off (content runs right up to it).

    Returns {edges: {"top"/"bottom"/"left"/"right": {margin_px, density}}}.
    """
    img = _decode(jpeg_bytes)
    if img is None:
        return {"edges": None}

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    ink = _ink_mask(gray)

    strip_px = max(3, int(round(0.02 * min(h, w))))
    scan_cap = max(strip_px, int(round(0.05 * min(h, w))))

    def line_density(line):
        return float((line > 0).mean())

    def margin_to(get_line):
        for i in range(scan_cap):
            if line_density(get_line(i)) > 0.01:
                return i
        return scan_cap

    edges = {
        "top": {
            "margin_px": margin_to(lambda i: ink[i, :]),
            "density": line_density(ink[:strip_px, :].reshape(-1)),
        },
        "bottom": {
            "margin_px": margin_to(lambda i: ink[h - 1 - i, :]),
            "density": line_density(ink[h - strip_px:, :].reshape(-1)),
        },
        "left": {
            "margin_px": margin_to(lambda i: ink[:, i]),
            "density": line_density(ink[:, :strip_px].reshape(-1)),
        },
        "right": {
            "margin_px": margin_to(lambda i: ink[:, w - 1 - i]),
            "density": line_density(ink[:, w - strip_px:].reshape(-1)),
        },
    }
    return {"edges": edges}


def detect_correction_hotspots(jpeg_bytes, exclude_bboxes=None, grid_n=30):
    """Grids the page and flags small clusters of cells whose ink density is
    an outlier relative to the page's own distribution - candidate signal
    for a struck-through/overwritten correction. Known signature regions are
    excluded (they're expected to be ink-dense). Returns a generous list of
    candidates (down to a permissive exploratory z-score) so classify.py can
    filter to the *current*, stricter threshold live, the same pattern used
    for duplicate-signature matching.

    Returns {correction_candidates: [{zscore, area}, ...]}.
    """
    img = _decode(jpeg_bytes)
    if img is None:
        return {"correction_candidates": []}

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    ink = (_ink_mask(gray) > 0).astype(np.float32)

    cell_h = max(1, h // grid_n)
    cell_w = max(1, w // grid_n)
    rows, cols = h // cell_h, w // cell_w
    if rows < 3 or cols < 3:
        return {"correction_candidates": []}

    density = np.zeros((rows, cols), dtype=np.float32)
    for r in range(rows):
        for c in range(cols):
            cell = ink[r * cell_h:(r + 1) * cell_h, c * cell_w:(c + 1) * cell_w]
            density[r, c] = cell.mean() if cell.size else 0.0

    exclude_mask = np.zeros((rows, cols), dtype=bool)
    for bbox in (exclude_bboxes or []):
        left, top, right, bottom = _bbox_to_pixels(bbox, w, h, margin=0.01)
        r0, r1 = top // cell_h, min(rows, bottom // cell_h + 1)
        c0, c1 = left // cell_w, min(cols, right // cell_w + 1)
        exclude_mask[r0:r1, c0:c1] = True

    considered = density[(~exclude_mask) & (density > 0.05)]
    if considered.size < 10:
        return {"correction_candidates": []}

    mean, std = float(considered.mean()), float(considered.std())
    if std < 1e-6:
        return {"correction_candidates": []}

    zmap = (density - mean) / std
    exploratory_floor = 1.5  # permissive - classify.py applies the real threshold
    flagged = (zmap >= exploratory_floor) & (~exclude_mask) & (density > 0.05)

    num_labels, labels = cv2.connectedComponents(flagged.astype(np.uint8), connectivity=8)
    candidates = []
    for label in range(1, num_labels):
        cluster = labels == label
        area = int(cluster.sum())
        if 1 <= area <= 12:  # rules out large blocks (tables, stamps, photos)
            candidates.append({"zscore": round(float(zmap[cluster].max()), 2), "area": area})

    return {"correction_candidates": candidates}
