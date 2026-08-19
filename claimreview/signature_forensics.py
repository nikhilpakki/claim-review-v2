"""Heuristic, local (no-AWS-cost) signature forensics.

Everything here is a signal to flag for a human reviewer to look closer at,
never a verdict. In particular, two documents sharing an identical signature
image is NOT inherently fraud - doctors routinely reuse the same rubber
stamp across every genuine document they sign. "possible_duplicate_of" is
deliberately phrased as a hedge, not an accusation. Likewise the pasted-patch
heuristic (recompression-error + border-edge analysis) can misfire on
legitimately noisy scans; it is only ever a prompt to double-check.
"""
import cv2
import imagehash
import numpy as np
from flask import current_app
from PIL import Image


def _decode(jpeg_bytes):
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _bbox_to_pixels(bbox, width, height, margin=0.0):
    left = max(0.0, bbox["Left"] - margin) * width
    top = max(0.0, bbox["Top"] - margin) * height
    right = min(1.0, bbox["Left"] + bbox["Width"] + margin) * width
    bottom = min(1.0, bbox["Top"] + bbox["Height"] + margin) * height
    return int(left), int(top), max(int(right), int(left) + 1), max(int(bottom), int(top) + 1)


def crop_signature(jpeg_bytes, bbox, margin=0.02):
    """Crop the signature's pixel region (with a small margin) out of the
    full page image. Returns a BGR numpy array, or None if decoding fails."""
    img = _decode(jpeg_bytes)
    if img is None:
        return None
    h, w = img.shape[:2]
    left, top, right, bottom = _bbox_to_pixels(bbox, w, h, margin)
    crop = img[top:bottom, left:right]
    return crop if crop.size else None


def compute_hash(crop):
    """Perceptual hash (dhash) of a signature crop, as a hex string - robust
    to minor rescaling/recompression, so the same stamp scanned twice still
    hashes the same or very close."""
    if crop is None:
        return None
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(rgb)
    size = current_app.config["SIGNATURE_HASH_SIZE"]
    return str(imagehash.dhash(pil_img, hash_size=size))


def hash_distance(hex_a, hex_b):
    """Hamming distance between two dhash hex strings."""
    return imagehash.hex_to_hash(hex_a) - imagehash.hex_to_hash(hex_b)


def detect_pasted_patch(jpeg_bytes, bbox):
    """Two independent, approximate RAW measurements of whether a
    signature-sized region might be a digitally pasted patch rather than
    part of the original scan - thresholding into a "paste_suspicious" tag
    happens live in classify.py, not here:

    1. ela_ratio - a lightweight form of Error Level Analysis: re-encode the
       whole page at a fixed JPEG quality, diff against the original, and
       compare the mean error inside the bbox vs. a ring just outside it.
       A patch pasted in from a separately-compressed source often shows a
       different error level than the page around it. None if it couldn't
       be computed (e.g. an all-background ring).
    2. border_edge_density - Canny edge detection on a thin ring around the
       bbox border. A pasted rectangle often leaves a visible seam -
       unusually dense straight edges hugging the box - whereas genuine
       page content rarely aligns edges so cleanly with an arbitrary
       rectangle.

    Returns {ela_ratio, border_edge_density}.
    """
    cfg = current_app.config
    img = _decode(jpeg_bytes)
    if img is None:
        return {"ela_ratio": None, "border_edge_density": None}

    h, w = img.shape[:2]
    left, top, right, bottom = _bbox_to_pixels(bbox, w, h)

    # --- 1. Recompression mismatch -----------------------------------
    ok, encoded = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, cfg["ELA_QUALITY"]])
    ela_ratio = None
    if ok:
        recompressed = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        diff = cv2.absdiff(img, recompressed).astype(np.float32).mean(axis=2)

        ring_margin = 0.05
        rl, rt, rr, rb = _bbox_to_pixels(bbox, w, h, margin=ring_margin)
        inside = diff[top:bottom, left:right]
        ring = diff[rt:rb, rl:rr].copy()
        ring[top - rt:bottom - rt, left - rl:right - rl] = np.nan

        inside_mean = float(np.nanmean(inside)) if inside.size else 0.0
        ring_vals = ring[~np.isnan(ring)]
        outside_mean = float(ring_vals.mean()) if ring_vals.size else 0.0

        if outside_mean > 0.01:
            ela_ratio = round(max(inside_mean, outside_mean) / max(min(inside_mean, outside_mean), 0.01), 2)

    # --- 2. Border artifact -------------------------------------------
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    ring_px = max(3, int(0.01 * min(w, h)))
    rl, rt = max(0, left - ring_px), max(0, top - ring_px)
    rr, rb = min(w, right + ring_px), min(h, bottom + ring_px)
    ring_region = gray[rt:rb, rl:rr]
    edges = cv2.Canny(ring_region, 50, 150)

    # only count edges that fall in the thin frame around the bbox, not the
    # signature's own interior strokes
    mask = np.ones_like(edges, dtype=bool)
    mask[(top - rt):(bottom - rt), (left - rl):(right - rl)] = False
    frame_edges = edges[mask]
    border_edge_density = round(float((frame_edges > 0).mean()), 3) if frame_edges.size else 0.0

    return {"ela_ratio": ela_ratio, "border_edge_density": border_edge_density}
