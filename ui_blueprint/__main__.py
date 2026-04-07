"""
ui_blueprint.__main__
=====================
Command-line entry point for the ui_blueprint package.

Sub-commands
------------
extract
    Convert an MP4 file (or synthetic data) into a blueprint JSON.

    Examples::

        python -m ui_blueprint extract recording.mp4 -o blueprint.json
        python -m ui_blueprint extract --synthetic -o blueprint.json

preview
    Render a directory of PNG preview frames from a blueprint JSON.

    Example::

        python -m ui_blueprint preview blueprint.json --out preview_frames/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _cmd_extract(args: argparse.Namespace) -> int:
    from ui_blueprint.extractor import extract, save_blueprint

    video_path: Path | None = None
    if not args.synthetic:
        if args.video is None:
            print("error: provide a video path or use --synthetic", file=sys.stderr)
            return 2
        video_path = Path(args.video)
        if not video_path.exists():
            print(f"error: video file not found: {video_path}", file=sys.stderr)
            return 1

    assets_dir: Path | None = None
    if args.assets_dir:
        assets_dir = Path(args.assets_dir)

    blueprint = extract(
        video_path,
        synthetic=args.synthetic,
        chunk_ms=float(args.chunk_ms),
        sample_fps=float(args.sample_fps),
        assets_dir=assets_dir,
    )

    output_path = Path(args.output)
    save_blueprint(blueprint, output_path)
    print(f"Blueprint written to: {output_path}")
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
    from ui_blueprint.preview import render_preview

    blueprint_path = Path(args.blueprint)
    if not blueprint_path.exists():
        print(f"error: blueprint file not found: {blueprint_path}", file=sys.stderr)
        return 1

    output_dir = Path(args.out)
    written = render_preview(blueprint_path, output_dir)
    print(f"Preview frames written to: {output_dir}  ({len(written)} files)")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ui_blueprint",
        description="UI Blueprint tools — extract blueprints from video and render previews.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # --- extract -------------------------------------------------------------
    p_extract = sub.add_parser(
        "extract",
        help="Extract a blueprint JSON from an MP4 or synthetic data.",
    )
    p_extract.add_argument(
        "video",
        nargs="?",
        default=None,
        metavar="VIDEO",
        help="Path to source MP4 file (omit when using --synthetic).",
    )
    p_extract.add_argument(
        "-o", "--output",
        required=True,
        metavar="OUT_JSON",
        help="Output path for the blueprint JSON file.",
    )
    p_extract.add_argument(
        "--synthetic",
        action="store_true",
        help="Generate blueprint from synthetic metadata (no real video needed).",
    )
    p_extract.add_argument(
        "--chunk-ms",
        dest="chunk_ms",
        type=float,
        default=1000,
        metavar="MS",
        help="Chunk duration in milliseconds (default: 1000).",
    )
    p_extract.add_argument(
        "--sample-fps",
        dest="sample_fps",
        type=float,
        default=10,
        metavar="FPS",
        help="Frame sampling rate for analysis (default: 10).",
    )
    p_extract.add_argument(
        "--assets-dir",
        dest="assets_dir",
        default=None,
        metavar="DIR",
        help="If provided, create an asset-crops directory and record paths.",
    )
    p_extract.set_defaults(func=_cmd_extract)

    # --- preview -------------------------------------------------------------
    p_preview = sub.add_parser(
        "preview",
        help="Render PNG preview frames from a blueprint JSON.",
    )
    p_preview.add_argument(
        "blueprint",
        metavar="BLUEPRINT_JSON",
        help="Path to blueprint JSON file.",
    )
    p_preview.add_argument(
        "--out",
        required=True,
        metavar="OUT_DIR",
        help="Output directory for PNG preview frames.",
    )
    p_preview.set_defaults(func=_cmd_preview)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
