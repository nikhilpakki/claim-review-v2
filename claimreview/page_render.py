import io
import os

from pdf2image import convert_from_path
from PIL import Image, ImageOps


# Textract's synchronous API takes an image of limited size. These are
# deliberately under the documented ceilings: a phone photo that trips them
# fails the whole document, and a slightly smaller image costs nothing in
# extraction quality compared with not being read at all.
MAX_IMAGE_PIXELS = 9000        # longest side, px
MAX_ENCODED_BYTES = 5_000_000  # encoded JPEG
JPEG_QUALITY = 90
JPEG_QUALITY_FLOOR = 60


def _fit_for_textract(image):
    """Scale an image down until it is within the size Textract accepts.

    Returns (jpeg_bytes, width, height). Quality is reduced first (cheaper in
    detail than resolution for OCR), then the resolution, until it fits.
    """
    if max(image.size) > MAX_IMAGE_PIXELS:
        scale = MAX_IMAGE_PIXELS / max(image.size)
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS)

    quality = JPEG_QUALITY
    while True:
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= MAX_ENCODED_BYTES:
            return data, image.width, image.height
        if quality > JPEG_QUALITY_FLOOR:
            quality -= 10
            continue
        # Still too big at the quality floor: halve the resolution and retry.
        image = image.resize((max(1, image.width // 2), max(1, image.height // 2)),
                             Image.LANCZOS)
        quality = JPEG_QUALITY


def load_page_images(file_path, dpi=None):
    """Return a list of (jpeg_bytes, width, height), one per page.

    PDFs are rasterized page-by-page via pdf2image; plain image files
    (.jpg/.jpeg) are treated as a single page (dpi is meaningless for an
    already-fixed-resolution photo, so it's ignored there). `dpi` defaults
    to pdf2image's own default (200) when not given.

    Camera photos carry their rotation in an EXIF flag rather than in the
    pixels, so a portrait photo is *stored* landscape. Without applying that
    flag the image reaches Textract on its side, where text extracts poorly and
    face detection fails outright - which is why photos ("POST X RAY.jpg",
    "INTRA PIC.jpg") behaved worse than scanned PDFs. pdf2image output has no
    EXIF, so this only ever affects real photographs.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        images = convert_from_path(file_path, dpi=dpi) if dpi else convert_from_path(file_path)
    else:
        images = [ImageOps.exif_transpose(Image.open(file_path))]

    pages = []
    for image in images:
        pages.append(_fit_for_textract(image.convert("RGB")))
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
