from __future__ import annotations

import copy
import hashlib
import json
import math
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image


EXPORT_FILENAME = "solar_demo_export.zip"
FINALIZED_IMAGES_FILENAME = "images_finalized.geojson"
FINALIZED_IMAGES_SCHEMA = "solaroly.image-placement.v1"
CORNER_ORDER = ["top_left", "top_right", "bottom_right", "bottom_left"]


def delete_solar_demo_export(job_dir: Path) -> dict[str, Any]:
    export_path = Path(job_dir).resolve() / EXPORT_FILENAME
    if not export_path.is_file():
        raise FileNotFoundError(export_path)
    size = export_path.stat().st_size
    export_path.unlink()
    return {"path": str(export_path), "deleted_size": size}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return payload


def _workspace_output(workspace: Path, status: dict[str, Any], stage: str) -> Path:
    raw_path = str(((status.get("outputs") or {}).get(stage) or {}).get("path") or "")
    if not raw_path:
        raise ValueError(f"The completed {stage.replace('_', ' ')} output is unavailable.")
    output = (workspace / raw_path).resolve()
    try:
        output.relative_to(workspace.resolve())
    except ValueError as exc:
        raise ValueError(f"The {stage.replace('_', ' ')} output path is invalid.") from exc
    if not output.is_file():
        raise ValueError(f"The {stage.replace('_', ' ')} output file was not found.")
    return output


def _bound_workflow(job_dir: Path, job: dict[str, Any], kind: str) -> tuple[Path, dict[str, Any]]:
    binding = (job.get("workflows") or {}).get(kind) or {}
    workflow_id = str(binding.get("workflow_id") or "")
    if not workflow_id:
        raise ValueError(f"Complete the {kind} post-processing workflow before exporting.")
    workspace = (job_dir / "snapshots" / kind).resolve()
    workflow_dir = (workspace / "postprocess" / workflow_id).resolve()
    try:
        workflow_dir.relative_to((workspace / "postprocess").resolve())
    except ValueError as exc:
        raise ValueError(f"The bound {kind} workflow path is invalid.") from exc
    status = _read_json(workflow_dir / "status.json")
    if status.get("status") != "complete":
        raise ValueError(f"The {kind} post-processing workflow is not complete.")
    return workspace, status


def _feature_image_stem(feature: dict[str, Any]) -> str:
    properties = feature.get("properties") or {}
    for key in ("image_id", "image", "src", "file", "name"):
        value = str(properties.get(key) or "").strip()
        if value:
            return Path(value).stem
    return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _valid_point(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    longitude = _finite_number(value[0])
    latitude = _finite_number(value[1])
    if longitude is None or latitude is None:
        return None
    return [longitude, latitude]


def _normalized_corners(properties: dict[str, Any], image_id: str) -> list[list[float]]:
    raw_corners = properties.get("corners")
    if not isinstance(raw_corners, list) or len(raw_corners) != 4:
        raise ValueError(f"Image {image_id} does not have exactly four placement corners.")
    corners = [_valid_point(corner) for corner in raw_corners]
    if any(corner is None for corner in corners):
        raise ValueError(f"Image {image_id} has an invalid placement corner.")
    normalized = [corner for corner in corners if corner is not None]
    top_left, top_right, _bottom_right, bottom_left = normalized
    determinant = (
        (top_right[0] - top_left[0]) * (bottom_left[1] - top_left[1])
        - (top_right[1] - top_left[1]) * (bottom_left[0] - top_left[0])
    )
    if abs(determinant) < 1e-18:
        raise ValueError(f"Image {image_id} has a degenerate placement footprint.")
    return normalized


def _geometry_points(coordinates: Any) -> list[list[float]]:
    point = _valid_point(coordinates)
    if point is not None and len(coordinates) == 2:
        return [point]
    if not isinstance(coordinates, list):
        return []
    points: list[list[float]] = []
    for child in coordinates:
        points.extend(_geometry_points(child))
    return points


def _point_in_image_footprint(point: list[float], corners: list[list[float]]) -> bool:
    top_left, top_right, _bottom_right, bottom_left = corners
    horizontal = [top_right[0] - top_left[0], top_right[1] - top_left[1]]
    vertical = [bottom_left[0] - top_left[0], bottom_left[1] - top_left[1]]
    delta = [point[0] - top_left[0], point[1] - top_left[1]]
    determinant = horizontal[0] * vertical[1] - horizontal[1] * vertical[0]
    if abs(determinant) < 1e-18:
        return False
    u = (delta[0] * vertical[1] - delta[1] * vertical[0]) / determinant
    v = (horizontal[0] * delta[1] - horizontal[1] * delta[0]) / determinant
    tolerance = 0.02
    return -tolerance <= u <= 1.0 + tolerance and -tolerance <= v <= 1.0 + tolerance


def _alignment_mode(properties: dict[str, Any]) -> str:
    method = str(
        properties.get("row_alignment_method")
        or properties.get("image_alignment_method")
        or ""
    ).strip()
    return "lightglue" if "lightglue" in method.casefold() else "none"


def _build_finalized_images(
    anomalies_path: Path,
    images_path: Path,
    image_dir: Path,
) -> tuple[dict[str, Any], list[Path], int]:
    anomalies = _read_json(anomalies_path).get("features")
    images = _read_json(images_path).get("features")
    if not isinstance(anomalies, list) or not isinstance(images, list):
        raise ValueError("Anomalies and images must be GeoJSON FeatureCollections.")

    png_by_stem = {
        path.stem.casefold(): path
        for path in image_dir.glob("*.png")
        if path.is_file()
    }
    metadata_by_stem: dict[str, dict[str, Any]] = {}
    for feature in images:
        if not isinstance(feature, dict):
            continue
        stem = _feature_image_stem(feature)
        if not stem:
            continue
        key = stem.casefold()
        if key in metadata_by_stem:
            raise ValueError(f"images.geojson contains duplicate records for image {stem}.")
        metadata_by_stem[key] = feature

    referenced = {
        Path(str((feature.get("properties") or {}).get("image") or "")).stem
        for feature in anomalies
        if isinstance(feature, dict) and (feature.get("properties") or {}).get("image")
    }
    missing_metadata = sorted(stem for stem in referenced if stem.casefold() not in metadata_by_stem)
    missing_images = sorted(stem for stem in referenced if stem.casefold() not in png_by_stem)
    if missing_metadata:
        raise ValueError(f"images.geojson is missing {len(missing_metadata)} referenced image records.")
    if missing_images:
        raise ValueError(f"The image folder is missing {len(missing_images)} referenced PNG files.")

    finalized_features: list[dict[str, Any]] = []
    exported_images: list[Path] = []
    finalized_by_stem: dict[str, dict[str, Any]] = {}
    for stem_key, source_feature in sorted(metadata_by_stem.items()):
        image_path = png_by_stem.get(stem_key)
        if image_path is None:
            continue
        source_properties = source_feature.get("properties") or {}
        image_id = image_path.stem
        corners = _normalized_corners(source_properties, image_id)
        geometry = copy.deepcopy(source_feature.get("geometry"))
        if not isinstance(geometry, dict) or geometry.get("type") != "Point":
            raise ValueError(f"Image {image_id} does not have a corrected center point.")
        center = _valid_point(geometry.get("coordinates"))
        if center is None:
            raise ValueError(f"Image {image_id} has an invalid corrected center point.")
        try:
            with Image.open(image_path) as image:
                width, height = image.size
                image.verify()
        except OSError as exc:
            raise ValueError(f"Could not read rotated image {image_path.name}: {exc}") from exc
        if width <= 0 or height <= 0:
            raise ValueError(f"Rotated image {image_path.name} has invalid dimensions.")

        effective_mpp = _finite_number(source_properties.get("meters_per_pixel"))
        if effective_mpp is None or effective_mpp <= 0:
            raise ValueError(f"Image {image_id} has no valid meters-per-pixel value.")
        map_rotation = _finite_number(
            source_properties.get("rotation", source_properties.get("rotation_heading", 0.0))
        )
        map_rotation = map_rotation if map_rotation is not None else 0.0
        alignment_method = str(
            source_properties.get("row_alignment_method")
            or source_properties.get("image_alignment_method")
            or "none"
        )
        alignment_status = str(
            source_properties.get("row_alignment_status")
            or source_properties.get("image_alignment_status")
            or "not_requested"
        )

        # Retain useful source diagnostics, but remove installation-specific URLs.
        properties = {
            key: copy.deepcopy(value)
            for key, value in source_properties.items()
            if key not in {
                "prepared_image", "overlay", "thumb", "corners", "w", "h",
                "image_id", "image", "src", "file", "name",
            }
        }
        source_image = str(source_properties.get("src") or source_properties.get("image") or image_id)
        source_image = source_image.replace("\\", "/").rsplit("/", 1)[-1]
        properties.update({
            "image_id": image_id,
            "image": image_path.name,
            "file": image_path.name,
            "source_image": source_image,
            "width": int(width),
            "height": int(height),
            "effective_meters_per_pixel": float(effective_mpp),
            "map_rotation_deg": float(map_rotation),
            "map_rotation_baked_into_pixels": abs(map_rotation) < 1e-9,
            "raster_orientation": "prepared_north_up",
            "alignment_mode": _alignment_mode(source_properties),
            "alignment_method": alignment_method,
            "alignment_status": alignment_status,
            "lens_corrected": str(source_properties.get("lens_correction_status") or "").casefold() == "corrected",
            "corners": corners,
            "sha256": _sha256(image_path),
        })
        finalized_feature = {
            "type": "Feature",
            "id": image_id,
            "geometry": {"type": "Point", "coordinates": center},
            "properties": properties,
        }
        finalized_features.append(finalized_feature)
        finalized_by_stem[stem_key] = finalized_feature
        exported_images.append(image_path)

    outside: list[str] = []
    validated_anomalies = 0
    for anomaly in anomalies:
        if not isinstance(anomaly, dict):
            continue
        properties = anomaly.get("properties") or {}
        image_value = str(properties.get("image") or "").strip()
        if not image_value:
            continue
        image_id = Path(image_value).stem
        image_feature = finalized_by_stem.get(image_id.casefold())
        if image_feature is None:
            continue
        points = _geometry_points((anomaly.get("geometry") or {}).get("coordinates"))
        if not points:
            raise ValueError(f"Anomaly {properties.get('anomaly_id') or '?'} has invalid geometry.")
        center = [
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        ]
        corners = image_feature["properties"]["corners"]
        if not _point_in_image_footprint(center, corners):
            outside.append(str(properties.get("anomaly_id") or image_id))
        validated_anomalies += 1
    if outside:
        examples = ", ".join(outside[:5])
        raise ValueError(
            f"{len(outside)} anomaly locations fall outside their linked finalized image footprint "
            f"(examples: {examples})."
        )

    finalized = {
        "type": "FeatureCollection",
        "export_schema": FINALIZED_IMAGES_SCHEMA,
        "schema_version": 1,
        "coordinate_system": "EPSG:4326",
        "pixel_origin": "top_left",
        "corner_order": CORNER_ORDER,
        "placement_source": "corners",
        "validation": {
            "image_count": len(finalized_features),
            "referenced_anomaly_count": validated_anomalies,
            "all_referenced_anomalies_inside_image_footprint": True,
        },
        "features": finalized_features,
    }
    return finalized, exported_images, len(anomalies)


def create_solar_demo_export(job_dir: Path, sessions_dir: Path, *, replace: bool = False) -> dict[str, Any]:
    job_dir = Path(job_dir).resolve()
    sessions_dir = Path(sessions_dir).resolve()
    job = _read_json(job_dir / "job.json")
    segmentation_workspace, segmentation_status = _bound_workflow(job_dir, job, "segmentation")
    anomaly_workspace, anomaly_status = _bound_workflow(job_dir, job, "anomaly")

    panels_path = _workspace_output(segmentation_workspace, segmentation_status, "regularized")
    rows_output = (segmentation_status.get("outputs") or {}).get("solar_rows") or {}
    rows_path = (
        _workspace_output(segmentation_workspace, segmentation_status, "solar_rows")
        if rows_output.get("path") and segmentation_status.get("assignment_mode") != "no_rows"
        else None
    )
    anomalies_path = _workspace_output(anomaly_workspace, anomaly_status, "associated")

    anomaly_result_id = str(((job.get("sources") or {}).get("anomaly") or {}).get("result_id") or "")
    anomaly_result = (sessions_dir / anomaly_result_id).resolve()
    if not anomaly_result_id or anomaly_result.parent != sessions_dir or not anomaly_result.is_dir():
        raise ValueError("The bound anomaly test result is unavailable.")
    images_path = anomaly_result / "images.geojson"
    image_dir = anomaly_result / "rotated_images"
    if not images_path.is_file() or not image_dir.is_dir():
        raise ValueError("The bound anomaly result has no images.geojson or rotated_images folder.")

    finalized_images, exported_images, anomaly_count = _build_finalized_images(
        anomalies_path,
        images_path,
        image_dir,
    )
    image_count = len(exported_images)
    export_path = job_dir / EXPORT_FILENAME
    if export_path.exists() and not replace:
        raise FileExistsError(export_path)
    temporary = job_dir / f".{EXPORT_FILENAME}.tmp"
    if temporary.exists():
        temporary.unlink()
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            archive.write(panels_path, "vector/solar_panels.geojson", compress_type=zipfile.ZIP_DEFLATED)
            if rows_path is not None:
                archive.write(rows_path, "vector/solar_rows.geojson", compress_type=zipfile.ZIP_DEFLATED)
            else:
                archive.writestr(
                    "vector/solar_rows.geojson",
                    json.dumps({
                        "type": "FeatureCollection",
                        "features": [],
                        "row_id_note": "No Rows layer was used; standalone panels use row_id 0000.",
                    }, indent=2),
                    compress_type=zipfile.ZIP_DEFLATED,
                )
            archive.write(anomalies_path, "vector/anomalies.geojson", compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr(
                f"anomaly_overlays/{FINALIZED_IMAGES_FILENAME}",
                json.dumps(finalized_images, indent=2),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            for image_path in exported_images:
                archive.write(image_path, f"anomaly_overlays/{image_path.name}", compress_type=zipfile.ZIP_STORED)
        temporary.replace(export_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    stat = export_path.stat()
    return {
        "path": str(export_path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "anomaly_count": anomaly_count,
        "image_count": image_count,
        "images_geojson": f"anomaly_overlays/{FINALIZED_IMAGES_FILENAME}",
        "validated_anomaly_count": finalized_images["validation"]["referenced_anomaly_count"],
    }
