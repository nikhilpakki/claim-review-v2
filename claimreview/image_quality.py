"""Local, no-AWS-cost raw image measurements for each rendered page image.

This module only computes raw, continuous metrics - it never thresholds
them into a boolean. Turning a metric into a tag ("blurry", "too clean",
"document" vs "photo") happens live in classify.py against the current
settings, so retuning a threshold re-labels every already-processed page
instantly without recomputing anything here or touching Textract.
"""
import cv2
import numpy as np

# Fixed kernel for Immerkaer's fast noise estimator - a standard
# no-reference noise-level estimate immune to the image's real edges,
# unlike naively looking at raw pixel variance.
_NOISE_KERNEL = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float64)


def estimate_noise_level(gray):
    """Immerkaer (1996) fast noise estimator: convolve with a fixed
    Laplacian-like kernel designed to respond to sensor/scan noise while
    staying largely blind to genuine image edges, then normalize by image
    size. A pristine, natively-digital page has almost no measurable noise
    here; a physical scan or photo always has some."""
    h, w = gray.shape[:2]
    if h < 3 or w < 3:
        return 0.0
    conv = cv2.filter2D(gray.astype(np.float64), -1, _NOISE_KERNEL)
    sigma = np.sum(np.abs(conv)) * np.sqrt(0.5 * np.pi) / (6 * (w - 2) * (h - 2))
    return float(sigma)


def analyze_page_quality(jpeg_bytes):
    """Return raw measurements for one page image:
    {blur_score, white_ratio, mean_saturation, noise_sigma}."""
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return {"blur_score": None, "white_ratio": None,
                "mean_saturation": None, "noise_sigma": None}

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mean_saturation = float(hsv[:, :, 1].mean())
    white_ratio = float((gray > 200).mean())
    noise_sigma = estimate_noise_level(gray)

    return {
        "blur_score": round(blur_score, 1),
        "white_ratio": round(white_ratio, 3),
        "mean_saturation": round(mean_saturation, 1),
        "noise_sigma": round(noise_sigma, 3),
    }
