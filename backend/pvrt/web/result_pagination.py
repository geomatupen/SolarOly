from __future__ import annotations

import math
from typing import Any


def _has_detections(item: dict[str, Any]) -> bool:
    try:
        return float(item.get("n") or 0) > 0
    except (TypeError, ValueError):
        return False


def paginate_result_manifest(
    manifest: list[dict[str, Any]],
    *,
    page: int,
    page_size: int,
    detected_only: bool,
) -> dict[str, Any]:
    """Filter and paginate a result manifest without mutating it."""
    safe_page_size = max(1, min(int(page_size), 100))
    source = [item for item in manifest if isinstance(item, dict)]
    detected_total = sum(1 for item in source if _has_detections(item))
    filtered = [item for item in source if _has_detections(item)] if detected_only else source
    total = len(filtered)
    page_count = math.ceil(total / safe_page_size) if total else 0
    safe_page = min(max(1, int(page)), max(1, page_count))
    start = (safe_page - 1) * safe_page_size
    return {
        "items": filtered[start:start + safe_page_size],
        "page": safe_page,
        "page_size": safe_page_size,
        "page_count": page_count,
        "total": total,
        "unfiltered_total": len(source),
        "detected_total": detected_total,
        "detected_only": bool(detected_only),
    }
