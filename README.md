# ui-blueprint

> Convert 10-second Android screen-recording clips into a structured "blueprint" suitable for near-human-indistinguishable replay in a custom renderer — and optionally for compiling into automation events.

---

## What is a Blueprint?

A **Blueprint** is a compact, machine-readable JSON document that captures everything a custom renderer needs to reproduce a UI interaction at ~99% human-perceived fidelity:

| Section | Contents |
|---|---|
| `meta` | Device, resolution, FPS, clip duration |
| `assets` | Extracted icon/image crops (by perceptual hash) |
| `elements_catalog` | Stable element definitions with inferred type, style, and content |
| `chunks` | Time-ordered 1-second segments, each with a keyframe scene, per-element tracks, and inferred events |

### How chunking works

The clip is divided into **chunks** (default 1 000 ms each).  
Every chunk contains:

1. **`key_scene`** — a full scene-graph snapshot (all elements with bbox, z-order, opacity) at the chunk start time `t0_ms`. A renderer can seek to any time *t* by jumping to the nearest chunk keyframe.
2. **`tracks`** — parametric curves for each element property (`translate_x`, `translate_y`, `opacity`, …). The simplest model that fits the data is chosen: `step → linear → bezier → spring → sampled`. This preserves native easing / scroll inertia.
3. **`events`** — inferred interactions (`tap`, `swipe`, `scroll`, `type`, …) aligned to absolute timestamps.

Chunking gives **O(1) seek**, compact **delta compression** within each segment, and easy **parallel processing** during generation.

---

## Project structure

```
ui-blueprint/
├── schema/
│   └── blueprint.schema.json   # JSON Schema v1
├── ui_blueprint/
│   ├── __init__.py
│   ├── __main__.py             # CLI entry point
│   ├── extractor.py            # Video → Blueprint pipeline
│   └── preview.py              # Blueprint → PNG preview frames
├── tests/
│   └── test_extractor.py       # Unit + CLI integration tests
├── .github/workflows/ci.yml    # GitHub Actions CI
└── pyproject.toml
```

---

## Quick start

### Install

```bash
pip install ".[dev]"   # installs Pillow, jsonschema, ruff, pytest
```

### Extract a Blueprint from a video

```bash
python -m ui_blueprint extract recording.mp4 -o blueprint.json
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--chunk-ms` | 1000 | Chunk duration (ms) |
| `--sample-fps` | 10 | Frame sampling rate for analysis |
| `--assets-dir DIR` | — | Create an asset-crops directory and record paths |
| `--synthetic` | — | Generate from synthetic metadata (no real video) |

### Render a visual preview

```bash
python -m ui_blueprint preview blueprint.json --out preview_frames/
```

Outputs one PNG per chunk — draws bounding boxes and element labels onto a blank canvas — so you can quickly validate the timeline structure.

### Test without a real video (CI / unit tests)

```bash
python -m ui_blueprint extract --synthetic -o /tmp/test.json
```

---

## Running tests

```bash
pytest tests/ -v
```

CI runs automatically on every push and pull request via GitHub Actions (`.github/workflows/ci.yml`).

---

## Constraints and next steps

### Current state (MVP scaffold)

The MVP produces **syntactically valid, schema-conformant blueprints** from synthetic data and real MP4 metadata (duration parsed from the `mvhd` atom).  The following pipeline stages are **placeholder stubs** ready for real models:

| Hook | File | Description |
|---|---|---|
| `_detect_elements()` | `extractor.py` | Replace with a real UI-element detector (e.g., DETR fine-tuned on Android UI) |
| `_ocr_region()` | `extractor.py` | Replace with an OCR engine (e.g., PaddleOCR, Tesseract) |
| `_track_elements()` | `extractor.py` | Replace with tracking-by-detection + appearance embeddings |
| `_fit_track_curve()` | `extractor.py` | Replace with actual bezier / spring fitting (currently stores raw keyframes) |
| `_infer_events()` | `extractor.py` | Replace with motion/highlight analysis for tap/swipe/scroll inference |

### Adding real detectors

1. Decode frames from the MP4 (e.g., with `av` / `imageio` / OpenCV).
2. Pass raw RGB bytes to `_detect_elements()`.
3. Track across frames with `_track_elements()`.
4. Fit curves with `_fit_track_curve()`.
5. Infer events with `_infer_events()`.

### Adding full video decode (no OpenCV required)

```bash
pip install imageio[ffmpeg]
```

Use `imageio.get_reader(path)` to iterate frames, then pass raw bytes to the detection hooks.

### Automation script compilation

The `events` array in each chunk is the foundation.  
Compile to UIAutomator / Accessibility actions by mapping:
- `tap { x, y }` → `adb shell input tap x y`
- `swipe { path }` → `adb shell input swipe …`
- `type { text }` → `adb shell input text "…"`

### Element tracking improvements

- Use a **list-item template** to avoid ID churn in scroll lists.
- Add an **appearance embedding** model for robust re-identification across transitions.

---

## Schema reference

See [`schema/blueprint.schema.json`](schema/blueprint.schema.json) for the full annotated JSON Schema (draft-07).

---

## License

MIT
