"""Supertonic TTS + resample to telephony format.

Synthesizes text with the on-device Supertonic-3 model (ONNX, no GPU, no cloud
call), then resamples the result to what PJSIP's AudioMediaPlayer needs:
8000 Hz, mono, 16-bit PCM. (PJSIP requires 16-bit PCM mono and resamples
internally to the call codec.)

We resample with the stdlib `audioop` module, which is why the project pins
Python 3.11 (audioop was removed from the stdlib in 3.13).
"""

import audioop
import logging
import os
import threading
import wave

from supertonic import TTS

logger = logging.getLogger(__name__)

VOICES = ["F1", "F2", "F3", "F4", "F5", "M1", "M2", "M3", "M4", "M5"]
DEFAULT_VOICE = "F3"
DEFAULT_LANG = os.environ.get("LANG_CODE", "uk")
# Languages Supertonic-3 can synthesize (ISO codes per the library README),
# plus "na" — the model's own fallback for unknown/unsupported languages.
LANGS = frozenset([
    "ar", "bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "hi",
    "hr", "hu", "id", "it", "ja", "ko", "lt", "lv", "na", "nl", "pl", "pt",
    "ro", "ru", "sk", "sl", "sv", "tr", "uk", "vi",
])

# Generation parameters (bounds live in clamp): speech speed, diffusion steps
# (more = better quality and slower synthesis), pause between sentences.
DEFAULT_SPEED = 1.05
DEFAULT_STEPS = 8
DEFAULT_SILENCE = 0.3
# The speed bounds are NARROWER than the model's (0.7..2.0): below 0.7
# Supertonic raises ValueError, and above ~1.3 it SILENTLY swallows words
# (measured on «Один. Два. Три. Чотири. Пять.»: 1.3 → all 5 speech segments,
# 1.4 → 3, 2.0 → 2 — the callee hears «only the middle of the message»).
# Do not widen without re-measuring.
SPEED_MIN = 0.7
SPEED_MAX = 1.3

TARGET_RATE = 8000
TARGET_CHANNELS = 1
TARGET_WIDTH = 2  # bytes per sample -> 16-bit (PJSIP requires 16-bit PCM mono)

# Silence prepended to the call WAV: the callee needs a moment to bring the
# phone to their ear after answering, or the start of the message is lost.
LEAD_IN_SECONDS = 2.0

_tts = None
_tts_lock = threading.Lock()  # guards the lazy model load
_gen_lock = threading.Lock()  # onnxruntime synthesis: one at a time


def _get_tts():
    """Load the Supertonic model once per process (assets are baked in the image)."""
    global _tts
    with _tts_lock:
        if _tts is None:
            logger.info("Loading Supertonic model ...")
            _tts = TTS(auto_download=True)
            logger.info("Model ready")
        return _tts


def clamp(speed, steps, silence):
    """Clamp generation params to safe ranges so bad input can't wedge the model."""
    return (
        min(max(float(speed), SPEED_MIN), SPEED_MAX),
        min(max(int(steps), 1), 32),
        min(max(float(silence), 0.0), 2.0),
    )


def synthesize_native(text, voice, out_path, lang=DEFAULT_LANG,
                      speed=DEFAULT_SPEED, steps=DEFAULT_STEPS,
                      silence=DEFAULT_SILENCE):
    """Render `text` to a native-rate (44.1 kHz) WAV at out_path. Returns out_path."""
    tts = _get_tts()
    with _gen_lock:
        style = tts.get_voice_style(voice_name=voice)
        # max_chunk_length=10 (the library's minimum) = «one sentence — one
        # chunk»: Supertonic inserts silence_duration ONLY between chunks, and
        # with the default limit (300 chars) a short text is a single chunk,
        # so the pause slider has no effect at all. chunk_text never cuts a
        # sentence from the inside.
        result = tts.synthesize(text, voice_style=style, lang=lang,
                                speed=speed, total_steps=steps,
                                silence_duration=silence,
                                max_chunk_length=10)
        wav = result[0] if isinstance(result, (tuple, list)) else result
        tts.save_audio(wav, out_path)
    return out_path


def synthesize_telephony(text, voice, native_path, tel_path,
                         lead_in=LEAD_IN_SECONDS, **params):
    """Render `text` and produce a telephony-ready WAV at tel_path.

    The WAV is guaranteed to be 8000 Hz, mono, 16-bit PCM (verified before
    returning) and starts with `lead_in` seconds of silence. The default suits
    the ad-hoc single-call path (&playback right at answer); IVR prompts are
    rendered with lead_in=0 — the dead air between menu rounds reads as
    «it's broken», so the IVR plays one silence_stream lead-in itself instead.
    The full-quality native render is left at native_path. Returns tel_path.
    """
    synthesize_native(text, voice, native_path, **params)
    resample(native_path, tel_path, TARGET_RATE, lead_in=lead_in)
    _verify(tel_path)
    logger.info("Synthesized telephony WAV: %s", tel_path)
    return tel_path


def resample(src_path, dst_path, rate=TARGET_RATE, lead_in=0.0):
    """Convert any mono/stereo PCM WAV to `rate` Hz, mono, 16-bit PCM.

    `lead_in` seconds of silence are prepended. Returns dst_path.
    """
    with wave.open(src_path, "rb") as w:
        nch = w.getnchannels()
        sw = w.getsampwidth()
        in_rate = w.getframerate()
        data = w.readframes(w.getnframes())

    if nch == 2:
        data = audioop.tomono(data, sw, 0.5, 0.5)

    data = audioop.lin2lin(data, sw, TARGET_WIDTH)                        # -> 16-bit signed
    data, _ = audioop.ratecv(data, TARGET_WIDTH, 1, in_rate, rate, None)  # -> `rate` Hz

    silence = b"\x00" * (int(lead_in * rate) * TARGET_WIDTH)

    with wave.open(dst_path, "wb") as w:
        w.setnchannels(TARGET_CHANNELS)
        w.setsampwidth(TARGET_WIDTH)
        w.setframerate(rate)
        w.writeframes(silence + data)
    return dst_path


def _verify(path):
    """Assert the WAV is exactly 8000 Hz / mono / 16-bit, else raise ValueError."""
    with wave.open(path, "rb") as w:
        rate, nch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
    if (rate, nch, sw) != (TARGET_RATE, TARGET_CHANNELS, TARGET_WIDTH):
        raise ValueError(
            f"bad telephony format for {path}: "
            f"rate={rate} channels={nch} sampwidth={sw} "
            f"(expected {TARGET_RATE}/{TARGET_CHANNELS}/{TARGET_WIDTH})"
        )


def wav_frame_count(path):
    """Number of frames; at 8000 Hz the duration in seconds is frames / 8000."""
    with wave.open(path, "rb") as w:
        return w.getnframes()


def wav_seconds(path):
    """Duration of a WAV in seconds, rounded to one decimal."""
    with wave.open(path, "rb") as w:
        return round(w.getnframes() / w.getframerate(), 1)
