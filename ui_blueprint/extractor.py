"""
ui_blueprint.extractor
======================
Converts an Android screen-recording MP4 (or synthetic metadata) into a
structured Blueprint JSON that conforms to schema/blueprint.schema.json (v1).

Real ML hooks (detection / OCR / tracking / curve-fitting) are provided as
placeholder stubs so that the pipeline structure is ready for model plug-in.

Usage (CLI):
    python -m ui_blueprint extract video.mp4 -o out.json
    python -m ui_blueprint extract --synthetic -o out.json
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------
SCHEMA_VERSION = "1.0"
DEFAULT_CHUNK_MS = 1000          # 1-second chunks
DEFAULT_SAMPLE_FPS = 10          # frames sampled per second for analysis


# ---------------------------------------------------------------------------
# Placeholder ML hooks — replace each with real implementation later
# ---------------------------------------------------------------------------

def _detect_elements(frame_rgb: bytes, width: int, height: int) -> list[dict[str, Any]]:
    """
    Placeholder: detect UI elements in a raw RGB frame.

    Args:
        frame_rgb: raw RGB bytes (width * height * 3).
        width: frame width in pixels.
        height: frame height in pixels.

    Returns:
        List of detection dicts with keys: id, type, bbox {x,y,w,h}.
    """
    _ = frame_rgb, width, height
    return []


def _ocr_region(frame_rgb: bytes, bbox: dict[str, float], width: int, height: int) -> str:
    """
    Placeholder: OCR text from a bounding box region.

    Args:
        frame_rgb: raw RGB bytes.
        bbox: bounding box {x, y, w, h}.
        width: frame width.
        height: frame height.

    Returns:
        Extracted text string (empty if not implemented).
    """
    _ = frame_rgb, bbox, width, height
    return ""


def _track_elements(
    prev_elements: list[dict[str, Any]],
    curr_elements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Placeholder: assign stable IDs by matching prev→curr element detections.

    Args:
        prev_elements: detections from the previous frame.
        curr_elements: detections from the current frame.

    Returns:
        curr_elements list with stable 'id' fields assigned.
    """
    _ = prev_elements
    return curr_elements


def _fit_track_curve(
    timestamps_ms: list[float],
    values: list[float],
) -> dict[str, Any]:
    """
    Placeholder: fit the simplest animation model to a property time series.

    Args:
        timestamps_ms: list of time offsets (ms from chunk t0).
        values: corresponding property values.

    Returns:
        Track dict with 'model' and 'params' keys.
    """
    if not timestamps_ms:
        return {"model": "step", "params": {}, "keyframes": []}
    keyframes = [{"t_ms": t, "value": v} for t, v in zip(timestamps_ms, values)]
    return {"model": "sampled", "params": {}, "keyframes": keyframes}


def _infer_events(
    chunks_elements: list[list[dict[str, Any]]],
    sample_timestamps_ms: list[float],
) -> list[dict[str, Any]]:
    """
    Placeholder: infer tap / swipe / scroll events from element motion.

    Args:
        chunks_elements: list of element lists, one per sampled frame in chunk.
        sample_timestamps_ms: absolute timestamps corresponding to each list.

    Returns:
        List of event dicts.
    """
    _ = chunks_elements, sample_timestamps_ms
    return []


# ---------------------------------------------------------------------------
# Video metadata helpers
# ---------------------------------------------------------------------------

def _read_mp4_metadata(path: Path) -> dict[str, Any]:
    """
    Parse basic metadata from an MP4 file without external dependencies.

    Reads the ftyp/mvhd atoms to extract duration and—where available—width,
    height, and time scale.  Falls back to safe defaults on any parse error.

    Returns:
        dict with keys: width_px, height_px, fps, duration_ms, source_file.
    """
    meta: dict[str, Any] = {
        "width_px": 1080,
        "height_px": 1920,
        "fps": 30.0,
        "duration_ms": 10_000.0,
        "source_file": path.name,
    }
    try:
        data = path.read_bytes()
        offset = 0
        while offset + 8 <= len(data):
            size = struct.unpack_from(">I", data, offset)[0]
            box_type = data[offset + 4 : offset + 8]
            if size < 8:
                break
            if box_type == b"mvhd":
                # mvhd version 0: offset+8=version(1), flags(3), ctime(4), mtime(4),
                #                  timescale(4), duration(4)
                # mvhd version 1: larger 64-bit times
                version = data[offset + 8]
                if version == 0:
                    timescale = struct.unpack_from(">I", data, offset + 20)[0]
                    duration = struct.unpack_from(">I", data, offset + 24)[0]
                else:
                    timescale = struct.unpack_from(">I", data, offset + 28)[0]
                    duration = struct.unpack_from(">Q", data, offset + 32)[0]
                if timescale > 0:
                    meta["duration_ms"] = round(duration / timescale * 1000, 3)
                break
            if box_type in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                offset += 8
                continue
            offset += size
    except Exception:  # noqa: BLE001 — best-effort parse
        pass
    return meta


def _build_synthetic_meta() -> dict[str, Any]:
    """Return synthetic metadata for testing without a real video file."""
    return {
        "width_px": 1080,
        "height_px": 1920,
        "fps": 30.0,
        "duration_ms": 10_000.0,
        "source_file": "synthetic",
        "device": "Synthetic/Android 14",
        "os_version": "14",
    }


# ---------------------------------------------------------------------------
# Asset helpers
# ---------------------------------------------------------------------------

def _asset_id(index: int) -> str:
    return f"asset_{index:04d}"


def _element_id(index: int) -> str:
    return f"el_{index:04d}"


# ---------------------------------------------------------------------------
# Core extractor
# ---------------------------------------------------------------------------

def extract(
    video_path: Path | None,
    *,
    synthetic: bool = False,
    chunk_ms: float = DEFAULT_CHUNK_MS,
    sample_fps: float = DEFAULT_SAMPLE_FPS,
    assets_dir: Path | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """
    Extract a Blueprint from an MP4 file (or synthetic metadata).

    Args:
        video_path: path to the source MP4.  Must be provided unless
                    *synthetic* is True.
        synthetic:  when True, skip video I/O and use synthetic metadata.
                    Useful for CI / unit-testing.
        chunk_ms:   chunk duration in milliseconds.
        sample_fps: frame sampling rate for analysis (frames per second).
        assets_dir: if provided, a placeholder "crops" directory is created
                    and asset paths are recorded in the blueprint.
        created_at: ISO-8601 timestamp for blueprint creation; defaults to now.

    Returns:
        Blueprint dict conforming to schema/blueprint.schema.json v1.
    """
    if not synthetic and video_path is None:
        raise ValueError("Either provide video_path or set synthetic=True.")

    # --- 1. Metadata ---------------------------------------------------------
    if synthetic:
        meta = _build_synthetic_meta()
    else:
        assert video_path is not None
        meta = _read_mp4_metadata(video_path)

    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    meta["created_at"] = created_at

    width = meta["width_px"]
    height = meta["height_px"]
    duration_ms: float = meta["duration_ms"]

    # --- 2. Assets directory (placeholder) -----------------------------------
    assets: list[dict[str, Any]] = []
    if assets_dir is not None:
        assets_dir.mkdir(parents=True, exist_ok=True)
        # Real impl would write cropped PNGs; placeholder records an empty entry.
        placeholder_asset: dict[str, Any] = {
            "id": _asset_id(0),
            "kind": "unknown",
            "path": str(assets_dir / "placeholder.png"),
        }
        assets.append(placeholder_asset)

    # --- 3. Frame sampling plan ----------------------------------------------
    # Determine the set of sample timestamps (absolute ms from clip start).
    frame_interval_ms = 1000.0 / sample_fps
    sample_timestamps: list[float] = []
    t = 0.0
    while t <= duration_ms:
        sample_timestamps.append(round(t, 3))
        t += frame_interval_ms

    # --- 4. Per-frame element extraction (placeholder) -----------------------
    # In real impl: decode frame bytes from MP4 and call _detect_elements.
    # For now, use a deterministic synthetic scene.
    all_elements: list[dict[str, Any]] = []
    element_registry: dict[str, dict[str, Any]] = {}  # id → element_def

    frame_element_sequences: dict[float, list[dict[str, Any]]] = {}

    for ts in sample_timestamps:
        # Synthetic: create two elements whose bbox shifts slightly over time.
        progress = ts / max(duration_ms, 1)
        elements = [
            {
                "id": _element_id(0),
                "type": "container",
                "bbox": {"x": 0, "y": 0, "w": float(width), "h": float(height)},
            },
            {
                "id": _element_id(1),
                "type": "button",
                "bbox": {
                    "x": round(width * 0.25 + progress * 10, 2),
                    "y": round(height * 0.5 + progress * 5, 2),
                    "w": round(width * 0.5, 2),
                    "h": 120.0,
                },
            },
            {
                "id": _element_id(2),
                "type": "text",
                "bbox": {
                    "x": round(width * 0.1, 2),
                    "y": round(height * 0.1 + progress * 2, 2),
                    "w": round(width * 0.8, 2),
                    "h": 60.0,
                },
            },
        ]

        # In real impl: elements = _detect_elements(frame_rgb, width, height)
        #               elements = _track_elements(prev_elements, elements)

        frame_element_sequences[ts] = elements
        all_elements.extend(elements)

    # Build stable elements_catalog from seen element IDs.
    for element_list in frame_element_sequences.values():
        for el in element_list:
            eid = el["id"]
            if eid not in element_registry:
                element_registry[eid] = {
                    "id": eid,
                    "type": el.get("type", "unknown"),
                    "first_ms": 0.0,
                    "last_ms": duration_ms,
                }

    elements_catalog = list(element_registry.values())

    # --- 5. Build chunks -----------------------------------------------------
    chunks: list[dict[str, Any]] = []
    chunk_start = 0.0
    while chunk_start < duration_ms:
        chunk_end = min(chunk_start + chunk_ms, duration_ms)

        # Timestamps within this chunk.
        chunk_timestamps = [t for t in sample_timestamps if chunk_start <= t <= chunk_end]

        # Key scene: snapshot at chunk start (use first sample in chunk).
        key_scene_elements = frame_element_sequences.get(
            chunk_timestamps[0] if chunk_timestamps else chunk_start, []
        )
        key_scene = [
            {
                "element_id": el["id"],
                "bbox": el["bbox"],
                "z": idx,
                "opacity": 1.0,
            }
            for idx, el in enumerate(key_scene_elements)
        ]

        # Tracks: per-element property time series.
        tracks: list[dict[str, Any]] = []
        for eid in element_registry:
            for prop in ("translate_x", "translate_y", "opacity"):
                ts_list: list[float] = []
                val_list: list[float] = []
                for ts in chunk_timestamps:
                    for el in frame_element_sequences.get(ts, []):
                        if el["id"] == eid:
                            offset_ms = round(ts - chunk_start, 3)
                            ts_list.append(offset_ms)
                            if prop == "translate_x":
                                val_list.append(el["bbox"]["x"])
                            elif prop == "translate_y":
                                val_list.append(el["bbox"]["y"])
                            else:
                                val_list.append(1.0)
                if ts_list:
                    fitted = _fit_track_curve(ts_list, val_list)
                    track: dict[str, Any] = {
                        "element_id": eid,
                        "property": prop,
                        "model": fitted["model"],
                        "params": fitted["params"],
                    }
                    if "keyframes" in fitted and fitted["keyframes"]:
                        track["keyframes"] = fitted["keyframes"]
                    tracks.append(track)

        # Events (placeholder inference).
        events = _infer_events(
            [frame_element_sequences.get(t, []) for t in chunk_timestamps],
            chunk_timestamps,
        )

        chunk: dict[str, Any] = {
            "t0_ms": round(chunk_start, 3),
            "t1_ms": round(chunk_end, 3),
            "key_scene": key_scene,
            "tracks": tracks,
            "events": events,
            "quality": {
                "detection_confidence": 0.0,
                "tracking_confidence": 0.0,
                "ocr_confidence": 0.0,
            },
        }
        chunks.append(chunk)
        chunk_start = chunk_end

    # --- 6. Assemble blueprint -----------------------------------------------
    blueprint: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "meta": meta,
        "assets": assets,
        "elements_catalog": elements_catalog,
        "chunks": chunks,
    }
    return blueprint


def save_blueprint(blueprint: dict[str, Any], output_path: Path) -> None:
    """Serialise blueprint dict to a JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(blueprint, fh, indent=2, ensure_ascii=False)


def _placeholder_phash(data: str) -> str:
    """Return a short deterministic hex string as a placeholder perceptual hash."""
    digest = zlib.adler32(data.encode()) & 0xFFFFFFFF
    return hashlib.md5(struct.pack(">I", digest)).hexdigest()[:12]  # noqa: S324
