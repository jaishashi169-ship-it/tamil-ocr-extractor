import io
from pathlib import Path
from typing import List

from PIL import Image, ImageSequence

from app.config import get_settings
from app.services.document_model import DocumentModel, PageModel
from app.services.importers import CorruptFileError, ProgressCallback
from app.services.ocr_engine import OCROptions, run_ocr_on_image
from app.services.tamil_utils import detect_script_language
from app.storage import storage

settings = get_settings()


def import_image(
    file_path: Path,
    job_id: str,
    filename: str,
    format_type: str,
    ocr_opts: OCROptions,
    progress_cb: ProgressCallback = None,
) -> DocumentModel:
    Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS
    try:
        img = Image.open(str(file_path))
        img.verify()
        img = Image.open(str(file_path))
    except Exception as exc:
        raise CorruptFileError(f"Unable to read image '{filename}': {exc}") from exc

    frames = list(ImageSequence.Iterator(img))
    total_pages = len(frames)
    pages: List[PageModel] = []

    for idx, frame in enumerate(frames):
        page_num = idx + 1
        if progress_cb:
            progress_cb(page_num, total_pages, f"Running OCR on image page {page_num} of {total_pages}")

        rgb = frame.convert("RGB")
        w, h = rgb.size
        if w * h > settings.MAX_IMAGE_PIXELS:
            raise ValueError(f"Image resolution {w}x{h} exceeds maximum allowed pixels ({settings.MAX_IMAGE_PIXELS}).")

        # Save preview & thumbnail
        preview_buf = io.BytesIO()
        rgb.save(preview_buf, format="PNG")
        preview_key = f"jobs/{job_id}/pages/page_{page_num:04d}_preview.png"
        storage.save_bytes(preview_key, preview_buf.getvalue())

        thumb = rgb.copy()
        thumb.thumbnail((320, 320))
        thumb_buf = io.BytesIO()
        thumb.save(thumb_buf, format="PNG")
        thumb_key = f"jobs/{job_id}/pages/page_{page_num:04d}_thumb.png"
        storage.save_bytes(thumb_key, thumb_buf.getvalue())

        preview_path = storage.get_local_file(preview_key)
        blocks, conf_avg, warns = run_ocr_on_image(preview_path, filename, page_num, ocr_opts)
        combined = "\n".join(b.text for b in blocks)

        pages.append(
            PageModel(
                page_number=page_num,
                width=float(w),
                height=float(h),
                source_type="image",
                extraction_method="ocr",
                language_detected=detect_script_language(combined),
                confidence_avg=conf_avg,
                blocks=blocks,
                warnings=warns,
                metadata={"preview_key": preview_key, "thumbnail_key": thumb_key},
            )
        )

    return DocumentModel(
        job_id=job_id,
        source_filename=filename,
        format_type=format_type,
        pages=pages,
        metadata={"total_pages": total_pages},
    )
