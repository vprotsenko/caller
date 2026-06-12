"""Resampler + format verification from the v1-ported tts.py (§16 level 1).

Only the pure audio-path functions — synthesis itself needs the baked model
and is exercised by /preview at level 2.
"""

import math
import wave

import pytest

from app import tts


def make_sine_wav(path, rate=44100, channels=1, seconds=0.25, freq=440):
    frames = int(rate * seconds)
    data = bytearray()
    for i in range(frames):
        sample = int(20000 * math.sin(2 * math.pi * freq * i / rate))
        for _ in range(channels):
            data += sample.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(data))
    return str(path)


def test_resample_produces_telephony_format(tmp_path):
    src = make_sine_wav(tmp_path / "src.wav")
    dst = tts.resample(src, str(tmp_path / "dst.wav"))
    tts._verify(dst)  # must not raise: 8000 Hz / mono / 16-bit
    assert tts.wav_seconds(dst) == pytest.approx(0.3, abs=0.1)


def test_resample_stereo_to_mono(tmp_path):
    src = make_sine_wav(tmp_path / "src.wav", channels=2)
    dst = tts.resample(src, str(tmp_path / "dst.wav"))
    tts._verify(dst)


def test_resample_lead_in_prepends_silence(tmp_path):
    src = make_sine_wav(tmp_path / "src.wav", seconds=0.5)
    dst = tts.resample(src, str(tmp_path / "dst.wav"), lead_in=tts.LEAD_IN_SECONDS)
    assert tts.wav_seconds(dst) == pytest.approx(0.5 + tts.LEAD_IN_SECONDS, abs=0.1)
    with wave.open(dst, "rb") as w:
        head = w.readframes(int(tts.LEAD_IN_SECONDS * 8000) - 10)
    assert set(head) == {0}  # the lead-in really is silence


def test_verify_rejects_wrong_rate(tmp_path):
    bad = make_sine_wav(tmp_path / "bad.wav", rate=16000)
    with pytest.raises(ValueError):
        tts._verify(bad)


def test_clamp_speed_stays_in_model_safe_range():
    """Нижче 0.7 Supertonic кидає ValueError, вище ~1.3 мовчки ковтає слова
    («чую лише середину повідомлення») — clamp мусить тримати робочі межі."""
    assert tts.clamp(0.5, 8, 0.3)[0] == tts.SPEED_MIN
    assert tts.clamp(2.0, 8, 0.3)[0] == tts.SPEED_MAX
    assert tts.clamp(1.05, 8, 0.3) == (1.05, 8, 0.3)
    assert tts.clamp(1.0, 999, -1)[1:] == (32, 0.0)
