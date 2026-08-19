import io
import os

from pdf2image import convert_from_path
from PIL import Image


def load_page_images(file_path, dpi=None):
    """Return a list of (jpeg_bytes, width, height), one per page.

    PDFs are rasterized page-by-page via pdf2image; plain image files
    (.jpg/.jpeg) are treated as a single page (dpi is meaningless for an
    already-fixed-resolution photo, so it's ignored there). `dpi` defaults
    to pdf2image's own default (200) when not given.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        images = convert_from_path(file_path, dpi=dpi) if dpi else convert_from_path(file_path)
    else:
        images = [Image.open(file_path)]

    pages = []
    for image in images:
        rgb = image.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG")
        pages.append((buf.getvalue(), rgb.width, rgb.height))
    return pages


def save_page_images(file_path, dest_dir, dpi=None):
    """Render every page of file_path to <dest_dir>/page-<n>.jpg.

    Returns a list of {page_number, image_path, width, height, jpeg_bytes}
    so callers can both persist the image and hand the same bytes to
    Textract without re-reading from disk.
    """
    os.makedirs(dest_dir, exist_ok=True)
    pages = []
    for idx, (jpeg_bytes, width, height) in enumerate(load_page_images(file_path, dpi=dpi), start=1):
        image_path = os.path.join(dest_dir, f"page-{idx}.jpg")
        with open(image_path, "wb") as f:
            f.write(jpeg_bytes)
        pages.append({
            "page_number": idx,
            "image_path": image_path,
            "width": width,
            "height": height,
            "jpeg_bytes": jpeg_bytes,
        })
    return pages
