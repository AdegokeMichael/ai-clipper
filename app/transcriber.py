"""
Transcription using faster-whisper (local, free, fast).
Returns full transcript with per-segment timestamps.

Production hardening applied:
  - threading.Lock on model initialisation — prevents double-loading when
    two pipeline workers start simultaneously (race on _model = None).
  - Explicit language detection logging at INFO level.
"""
import logging
import os
import threading

from faster_whisper import WhisperModel

from app.models import TranscriptSegment

logger = logging.getLogger(__name__)

_model: WhisperModel | None = None
_model_lock = threading.Lock()   # Guards _model initialisation


def get_model() -> WhisperModel:
    """
    Return the shared WhisperModel, loading it on first call.
    Thread-safe: only one worker ever loads the model even if two pipeline
    runs start at exactly the same time.
    """
    global _model
    if _model is not None:
        return _model

    with _model_lock:
        # Double-checked locking: re-test after acquiring the lock
        # in case another thread already loaded the model while we waited.
        if _model is None:
            model_size = os.getenv("WHISPER_MODEL", "base")
            logger.info("Loading faster-whisper model: %s", model_size)
            _model = WhisperModel(model_size, device="cpu", compute_type="int8")
            logger.info("Whisper model loaded (%s).", model_size)

    return _model


def transcribe_video(video_path: str) -> tuple[str, list[TranscriptSegment]]:
    """
    Transcribe a video file.

    Returns:
        full_text  — complete transcript as a single string
        segments   — list of TranscriptSegment with timestamps
    """
    model = get_model()
    logger.info("Transcribing: %s", video_path)

    segments_raw, info = model.transcribe(
        video_path,
        beam_size=5,
        word_timestamps=True,   # Per-word timing for precise clip cuts
        vad_filter=True,        # Filter out silence
        vad_parameters=dict(min_silence_duration_ms=500),
    )

    segments: list[TranscriptSegment] = []
    full_text_parts: list[str] = []

    for seg in segments_raw:
        text = seg.text.strip()
        if not text:
            continue
        segments.append(TranscriptSegment(
            start=round(seg.start, 2),
            end=round(seg.end, 2),
            text=text,
        ))
        full_text_parts.append(text)

    full_text = " ".join(full_text_parts)
    logger.info(
        "Transcription complete — %d segments, %d chars. Language: %s (confidence: %.2f)",
        len(segments), len(full_text), info.language, info.language_probability,
    )

    return full_text, segments