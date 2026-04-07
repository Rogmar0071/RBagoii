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
pip install ".[dev]"    # test/lint deps, includes imageio[ffmpeg] for video decoding
pip install ".[video]"  # runtime optional video decoder path
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

### Current extractor behavior

The extractor now runs a real baseline pipeline:

1. **Frame decode** — samples frames with `imageio[ffmpeg]` when installed; otherwise falls back to MP4 metadata parsing.
2. **Baseline detection** — uses deterministic heuristics over background difference, edge masks, and dark-text proposals to find UI regions.
3. **Tracking** — matches detections frame-to-frame with IoU + simple appearance similarity.
4. **Motion fitting** — fits `step`, `linear`, `bezier`, or `sampled` tracks and stores `residual_error`.
5. **Event inference** — currently emits heuristic `scroll` and tap-like events.

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

### Current state (baseline video extractor)

The extractor now produces **schema-conformant blueprints** from synthetic frames and real MP4 frame samples. The current implementation is intentionally lightweight and deterministic:

| Hook | File | Description |
|---|---|---|
| `_detect_elements()` | `extractor.py` | Background/edge/text-region heuristics; ready to replace with a learned detector |
| `_ocr_region()` | `extractor.py` | Still a stub; add Tesseract/EasyOCR behind a feature flag next |
| `_track_elements()` | `extractor.py` | IoU + mean-color / edge-density appearance matching |
| `_fit_track_curve()` | `extractor.py` | Fits `step`, `linear`, `bezier`, else falls back to `sampled` |
| `_infer_events()` | `extractor.py` | Heuristic scroll and tap-like inference from tracked motion/appearance |

### Adding real detectors

1. Add real OCR content to detections.
2. Improve detection quality with learned UI region proposals.
3. Add list-row stabilization and re-identification for scrolling content.
4. Add spring fitting for Android-native motion.
5. Expand event inference beyond scroll/tap to drag/swipe/type.

### Adding full video decode (no OpenCV required)

```bash
pip install imageio[ffmpeg]
```

The optional `video` extra already installs `imageio[ffmpeg]`, and the extractor will use it automatically when present.

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
