"""
Clip analysis endpoint.

POST /v1/analyze
    Accepts multipart/form-data:
        video        – MP4 file (the recorded clip)
        requirements – string (user-entered analysis instructions)
    Returns JSON:
        summary       – string
        conclusions   – array of strings
        key_events    – array of {t_sec: number, event: string}
        confidence    – 0-1 float
        diagnostics   – {frames_used, transcript_used, audio_present}

The video is processed inside a TemporaryDirectory and never persisted.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

logger = logging.getLogger("uvicorn.error")

router = APIRouter()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_FRAMES = 20
_FRAME_SCALE = "512:-2"  # max 512 px wide, preserve aspect ratio
_ANALYSIS_MODEL = "gpt-4o"
_TRANSCRIPTION_MODEL = "whisper-1"

_ANALYSIS_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "conclusions": {"type": "array", "items": {"type": "string"}},
        "key_events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "t_sec": {"type": "number"},
                    "event": {"type": "string"},
                },
                "required": ["t_sec", "event"],
                "additionalProperties": False,
            },
        },
        "confidence": {"type": "number"},
    },
    "required": ["summary", "conclusions", "key_events", "confidence"],
    "additionalProperties": False,
}

_SYSTEM_INSTRUCTIONS = (
    "You are a video analysis assistant. "
    "You will be given a series of video frames extracted at 1 frame per second "
    "(up to 20 frames) and optionally an audio transcript. "
    "Analyze the visual content according to the provided requirements. "
    "For key_events, estimate the time in seconds based on frame order (frame N ≈ N seconds). "
    "For confidence, return a value between 0 (uncertain) and 1 (very confident) "
    "reflecting how clearly the frames support your conclusions. "
    "Return only valid JSON matching the requested schema."
)

# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/v1/analyze")
async def analyze_video(
    video: UploadFile,
    requirements: str = Form(...),
) -> JSONResponse:
    """
    Accept a video clip and requirements string, analyze using OpenAI, and return
    structured conclusions.  The video is discarded after processing.
    """
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not openai_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY not configured")

    video_bytes = await video.read()

    with tempfile.TemporaryDirectory() as work_dir:
        work = Path(work_dir)

        # Write clip to temp file — discarded when the context exits.
        clip_path = work / "clip.mp4"
        clip_path.write_bytes(video_bytes)

        # Extract frames (1 fps, max 20, scaled to 512 px wide).
        frames_dir = work / "frames"
        frames_dir.mkdir()
        frames = _extract_frames(clip_path, frames_dir)

        # Detect audio and transcribe if present.
        audio_present = _has_audio(clip_path)
        transcript = ""
        if audio_present:
            audio_path = work / "audio.wav"
            transcript = _transcribe_audio(clip_path, audio_path, openai_key)

        # Call OpenAI Responses API (frames are read inside this context while
        # the temp directory still exists).
        try:
            analysis = _call_openai_analysis(frames, requirements, transcript, openai_key)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("OpenAI analysis failed")
            raise HTTPException(status_code=502, detail=f"Analysis failed: {exc}") from exc

    # Temp directory (and all frames/audio) is now deleted.
    analysis["diagnostics"] = {
        "frames_used": len(frames),
        "transcript_used": bool(transcript),
        "audio_present": audio_present,
    }
    return JSONResponse(content=analysis)


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------


def _extract_frames(clip_path: Path, frames_dir: Path) -> list[Path]:
    """Extract up to _MAX_FRAMES at 1 fps, scaled to _FRAME_SCALE, using ffmpeg."""
    out_pattern = str(frames_dir / "frame_%04d.jpg")
    result = subprocess.run(
        [
            "ffmpeg",
            "-i", str(clip_path),
            "-vf", f"fps=1,scale={_FRAME_SCALE}",
            "-frames:v", str(_MAX_FRAMES),
            "-f", "image2",
            out_pattern,
        ],
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        logger.warning(
            "ffmpeg frame extraction non-zero exit: %s",
            result.stderr.decode(errors="replace"),
        )
    return sorted(frames_dir.glob("frame_*.jpg"))


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------


def _has_audio(clip_path: Path) -> bool:
    """Return True if the clip contains at least one audio stream."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type",
            "-of", "json",
            str(clip_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
        return bool(data.get("streams"))
    except (json.JSONDecodeError, KeyError):
        return False


def _transcribe_audio(clip_path: Path, audio_path: Path, openai_key: str) -> str:
    """Extract mono 16 kHz WAV and transcribe with OpenAI Whisper-1."""
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-i", str(clip_path),
                "-vn",
                "-acodec", "pcm_s16le",
                "-ar", "16000",
                "-ac", "1",
                str(audio_path),
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("Audio extraction failed: %s", exc)
        return ""

    try:
        from openai import OpenAI  # noqa: PLC0415

        client = OpenAI(api_key=openai_key)
        with audio_path.open("rb") as fh:
            result = client.audio.transcriptions.create(
                model=_TRANSCRIPTION_MODEL,
                file=fh,
                response_format="text",
            )
        return result if isinstance(result, str) else str(result)
    except Exception as exc:
        logger.warning("Transcription failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# OpenAI Responses API call
# ---------------------------------------------------------------------------


def _call_openai_analysis(
    frames: list[Path],
    requirements: str,
    transcript: str,
    openai_key: str,
) -> dict:
    """
    Call the OpenAI Responses API with frames + optional transcript.
    Returns the parsed analysis dict (without the 'diagnostics' key).
    """
    from openai import OpenAI  # noqa: PLC0415

    client = OpenAI(api_key=openai_key)

    # Build user-facing text prompt.
    text_parts: list[str] = [f"Requirements: {requirements}"]
    if transcript:
        text_parts.append(f"Audio transcript:\n{transcript}")
    text_parts.append(
        f"The following {len(frames)} image(s) are video frames at 1 frame/second. "
        "Analyze them per the requirements above."
    )

    content: list[dict] = [{"type": "input_text", "text": "\n\n".join(text_parts)}]

    # Attach frames as base64-encoded JPEG images.
    for frame_path in frames:
        img_b64 = base64.b64encode(frame_path.read_bytes()).decode()
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{img_b64}",
            }
        )

    response = client.responses.create(
        model=_ANALYSIS_MODEL,
        input=[{"role": "user", "content": content}],
        instructions=_SYSTEM_INSTRUCTIONS,
        text={
            "format": {
                "type": "json_schema",
                "name": "video_analysis",
                "schema": _ANALYSIS_JSON_SCHEMA,
                "strict": True,
            }
        },
    )

    return json.loads(response.output_text)
