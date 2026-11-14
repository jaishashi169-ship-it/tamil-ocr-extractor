import io
import uuid
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image
from psd_tools import PSDImage

from app.services.document_model import (
    BoundingBox,
    DocumentModel,
    PageModel,
    SourceReference,
    TextBlock,
)
from app.services.importers import CorruptFileError, ProgressCallback
from app.services.ocr_engine import OCROptions, run_ocr_on_image
from app.services.tamil_utils import detect_script_language, normalize_unicode
from app.storage import storage


def import_psd(
    file_path: Path,
    job_id: str,
    filename: str,
    ocr_opts: OCROptions,
    progress_cb: ProgressCallback = None,
) -> DocumentModel:
    if progress_cb:
        progress_cb(1, 1, "Inspecting PSD layers and composite image")

    try:
        psd = PSDImage.open(str(file_path))
    except Exception as exc:
        raise CorruptFileError(f"Unable to open PSD '{filename}': {exc}") from exc

    warnings: List[str] = []
    color_mode = str(getattr(psd, "color_mode", "UNKNOWN"))
    depth = getattr(psd, "depth", 8)
    if "RGB" not in color_mode.upper() or depth != 8:
        warnings.append(
            f"PSD uses color mode '{color_mode}' ({depth}-bit). Preview is converted to 8-bit RGB; exact Photoshop color fidelity is not guaranteed."
        )

    # Render composite preview
    composite_img: Image.Image = psd.composite()
    if composite_img.mode != "RGB":
        composite_img = composite_img.convert("RGB")

    preview_buf = io.BytesIO()
    composite_img.save(preview_buf, format="PNG")
    preview_key = f"jobs/{job_id}/pages/page_0001_preview.png"
    storage.save_bytes(preview_key, preview_buf.getvalue())

    thumb = composite_img.copy()
    thumb.thumbnail((320, 320))
    thumb_buf = io.BytesIO()
    thumb.save(thumb_buf, format="PNG")
    thumb_key = f"jobs/{job_id}/pages/page_0001_thumb.png"
    storage.save_bytes(thumb_key, thumb_buf.getvalue())

    # Inspect layers
    layer_tree: List[Dict[str, Any]] = []
    text_blocks: List[TextBlock] = []
    text_layer_metadata: List[Dict[str, Any]] = []
    unsupported_features: List[str] = []

    def _walk_layers(layers, depth_lvl: int = 0) -> None:
        for layer in layers:
            kind = str(getattr(layer, "kind", "unknown"))
            name = str(getattr(layer, "name", "Unnamed Layer"))
            visible = bool(getattr(layer, "visible", True))
            bbox_tuple = getattr(layer, "bbox", (0, 0, 0, 0))
            bx0, by0, bx1, by1 = bbox_tuple
            has_effects = bool(getattr(layer, "effects", None)) and len(getattr(layer, "effects", [])) > 0

            if has_effects:
                unsupported_features.append(f"Layer '{name}' has Photoshop layer effects that are not editable on export.")
            if kind in {"smartobject", "shape", "adjustment"}:
                unsupported_features.append(f"Layer '{name}' is of type '{kind}' (read-only metadata; cannot be restored as editable PSD).")

            layer_info: Dict[str, Any] = {
                "name": name,
                "kind": kind,
                "visible": visible,
                "depth": depth_lvl,
                "bbox": {"x": bx0, "y": by0, "width": max(0, bx1 - bx0), "height": max(0, by1 - by0)},
                "has_effects": has_effects,
            }

            if kind == "type":
                raw_text = normalize_unicode(str(getattr(layer, "text", "") or "").strip())
                layer_info["has_editable_text"] = bool(raw_text)
                if raw_text:
                    bid = str(uuid.uuid4())
                    bbox = BoundingBox(
                        x=float(bx0),
                        y=float(by0),
                        width=float(max(1, bx1 - bx0)),
                        height=float(max(1, by1 - by0)),
                    )
                    text_blocks.append(
                        TextBlock(
                            id=bid,
                            block_type="paragraph",
                            text=raw_text,
                            language=detect_script_language(raw_text),
                            extraction_method="psd_text_layer",
                            confidence=100.0,
                            bbox=bbox,
                            source_ref=SourceReference(
                                filename=filename,
                                page_number=1,
                                layer_name=name,
                                block_id=bid,
                                bbox=bbox,
                            ),
                        )
                    )
                    text_layer_metadata.append(
                        {
                            "layer_name": name,
                            "visible": visible,
                            "text_preview": raw_text[:120],
                            "bbox": layer_info["bbox"],
                        }
                    )

            layer_tree.append(layer_info)
            if layer.is_group():
                _walk_layers(layer, depth_lvl + 1)

    _walk_layers(psd)

    if unsupported_features:
        warnings.extend(sorted(set(unsupported_features))[:10])

    # Fallback to OCR on composite image when no editable text layers are present
    if text_blocks:
        extraction_method = "direct"
        conf_avg = 100.0
        source_type = "psd_text_layers"
    else:
        warnings.append(
            "No editable text layers were found in this PSD (flattened or raster-only). Running OCR on composite image; flattened artwork cannot be restored to original editable layers."
        )
        preview_path = storage.get_local_file(preview_key)
        ocr_blocks, conf_avg, ocr_warns = run_ocr_on_image(preview_path, filename, 1, ocr_opts)
        warnings.extend(ocr_warns)
        text_blocks = ocr_blocks
        extraction_method = "ocr"
        source_type = "psd_flattened"

    combined = "\n".join(b.text for b in text_blocks)
    page = PageModel(
        page_number=1,
        width=float(psd.width),
        height=float(psd.height),
        source_type=source_type,
        extraction_method=extraction_method,
        language_detected=detect_script_language(combined),
        confidence_avg=conf_avg,
        blocks=text_blocks,
        warnings=warnings,
        metadata={
            "preview_key": preview_key,
            "thumbnail_key": thumb_key,
            "psd_layers": layer_tree,
            "text_layer_metadata": text_layer_metadata,
            "color_mode": color_mode,
            "depth": depth,
        },
    )

    return DocumentModel(
        job_id=job_id,
        source_filename=filename,
        format_type="psd",
        pages=[page],
        warnings=warnings,
        metadata={
            "total_pages": 1,
            "layer_count": len(layer_tree),
            "editable_text_layers": len(text_layer_metadata),
            "color_mode": color_mode,
        },
    )
