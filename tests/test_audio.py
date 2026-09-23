"""音频合并回归测试。"""

import math
import struct
import wave
from pathlib import Path

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


def test_group_by_duration_never_exceeds_part_limit():
    """按实际时长切分：片段长度差异大时也不允许某一集超过单集上限。

    回归：此前按片段**个数**平均分（segs_per_part = len//part_count + 1），
    长度差异大时某一集会达到 80 分钟，超过 max_output_minutes=60。
    """
    segments = [Path(f"seg{i}.wav") for i in range(6)]
    minutes = [40.0, 1.0, 1.0, 40.0, 40.0, 1.0]
    durations = [m * 60.0 for m in minutes]

    groups = merger._group_by_duration(segments, durations, max_minutes=60)

    for group in groups:
        part_seconds = sum(durations[segments.index(s)] for s in group)
        assert part_seconds <= 60 * 60.0

    # 顺序与内容都不得丢失
    assert [s for group in groups for s in group] == segments


def test_group_by_duration_keeps_oversized_single_segment_intact():
    """单个片段自身超上限时不再切分——从句子中间断开听感更差。"""
    segments = [Path("long.wav"), Path("short.wav")]
    durations = [90 * 60.0, 5 * 60.0]

    groups = merger._group_by_duration(segments, durations, max_minutes=60)

    assert groups == [[segments[0]], [segments[1]]]


def test_group_by_duration_keeps_short_batch_in_one_part():
    segments = [Path(f"seg{i}.wav") for i in range(3)]
    durations = [60.0, 60.0, 60.0]

    groups = merger._group_by_duration(segments, durations, max_minutes=60)

    assert groups == [segments]


def test_probe_durations_falls_back_for_missing_segment(tmp_path):
    """缺失文件按 0 处理，不因一个坏文件丢掉整批时长。"""
    real = tmp_path / "ok.wav"
    _write_tone(real, duration=1.0)
    missing = tmp_path / "missing.wav"

    durations = merger._probe_durations([real, missing], "128k")

    assert abs(durations[0] - 1.0) < 0.1
    assert durations[1] == 0.0
