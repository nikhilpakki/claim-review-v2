"""Face detection for the 'has face' tag - OpenCV's YuNet DNN face
detector (the same approach as the standalone Fraud-Analytics/face-
finder.py script), falling back to the bundled Haar cascade if the YuNet
model file is missing.

Detection is local/CPU-only (no AWS cost) and follows the same "store raw,
classify live" pattern as every other quality check: hits are captured
here at a low, permissive confidence floor and cached as-is; classify.py's
classify_faces() applies the *current* FACE_CONFIDENCE_THRESHOLD setting
live, so retuning it re-labels every already-processed page instantly with
no reprocessing.
"""
import os

import cv2
import numpy as np

MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "face_detection_yunet_2023mar.onnx")

# The YuNet backend needs a score threshold at construction time (it isn't
# just a post-filter) - a low floor here keeps headroom for
# FACE_CONFIDENCE_THRESHOLD to be tuned up or down later without needing to
# reprocess. Only going *below* this floor would require it - the same
# tradeoff SIGNATURE_HASH_STORE_LIMIT makes for duplicate-signature matches.
_CAPTURE_FLOOR = 0.3


class _Backend:
    def __init__(self):
        self.yunet = None
        self.cascade = None
        self.backend = None
        self._yunet_size = None

        if os.path.exists(MODEL_PATH):
            self.yunet = cv2.FaceDetectorYN.create(
                MODEL_PATH, "", (320, 320), score_threshold=_CAPTURE_FLOOR,
            )
            self.backend = "yunet"
        elif hasattr(cv2, "CascadeClassifier"):
            cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
            self.cascade = cv2.CascadeClassifier(cascade_path)
            self.backend = "haar"
        # else: no usable backend - detect() below just returns [] always.

    def detect(self, image_bgr):
        if self.backend is None:
            return []
        h, w = image_bgr.shape[:2]
        results = []

        if self.backend == "yunet":
            if self._yunet_size != (w, h):
                self.yunet.setInputSize((w, h))
                self._yunet_size = (w, h)
            _retval, faces = self.yunet.detect(image_bgr)
            if faces is not None:
                for row in faces:
                    x, y, fw, fh = row[:4].astype(int)
                    confidence = float(row[-1])
                    x, y = max(int(x), 0), max(int(y), 0)
                    fw, fh = min(int(fw), w - x), min(int(fh), h - y)
                    if fw <= 0 or fh <= 0:
                        continue
                    results.append({"box": [x, y, fw, fh], "confidence": confidence})
        else:
            gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
            faces = self.cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
            for (x, y, fw, fh) in faces:
                # Haar has no continuous confidence - every hit is reported
                # at 1.0, so it always clears whatever threshold is set.
                results.append({"box": [int(x), int(y), int(fw), int(fh)], "confidence": 1.0})

        return results


_backend = None


def _get_backend():
    global _backend
    if _backend is None:
        _backend = _Backend()
    return _backend


def detect_faces(jpeg_bytes):
    """Every face detected on one page image, as raw {box: [x, y, w, h],
    confidence} dicts, at the fixed internal _CAPTURE_FLOOR - unthresholded
    beyond that. [] if the image can't be decoded or no usable detector
    backend is available."""
    backend = _get_backend()
    data = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    image_bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image_bgr is None:
        return []
    return backend.detect(image_bgr)
