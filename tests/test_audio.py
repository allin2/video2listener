"""音频合并回归测试。"""

import math
import struct
import wave

from src.audio import merger
from src.audio.merger import _concat_segments, _probe_duration, _probe_volume


def _write_tone(path, duration=1.0, sample_rate=24000):
    frames = bytearray()
    for i in range(int(duration * sample_rate)):
        sample = int(12000 * math.sin(2 * math.pi * 440 * i / sample_rate))
        frames.extend(struct.pack("<h", sample))

    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(frames)


def test_concat_does_not_fade_entire_program_to_silence(tmp_path):
    source = tmp_path / "segment.wav"
    output = tmp_path / "output.mp3"
    _write_tone(source)

    _concat_segments([source], output, "64k")

    mean_volume, max_volume = _probe_volume(output)
    assert output.stat().st_size > 0
    assert abs(_probe_duration(output) - 1.0) < 0.1
    assert mean_volume > -45.0
    assert max_volume > -20.0


def test_media_tool_uses_resolved_absolute_path(monkeypatch):
    monkeypatch.setattr(merger.shutil, "which", lambda name: f"/tools/{name}")

    assert merger._resolve_media_tool("ffmpeg") == "/tools/ffmpeg"
    assert merger._resolve_media_tool("ffprobe") == "/tools/ffprobe"
