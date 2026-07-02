"""音频合并模块。使用 ffmpeg 合并 TTS 片段并标准化。"""

import logging
import subprocess
from pathlib import Path
from typing import Optional

from src.config import get_config

logger = logging.getLogger(__name__)


def merge(
    segments: list[Path],
    output_path: Path,
    on_progress: Optional[callable] = None,
) -> list[Path]:
    """合并多段音频为 MP3 文件。

    单集 > 60 分钟时自动拆分为多集。

    Args:
        segments: 按顺序排列的音频文件列表
        output_path: 输出 MP3 路径（不含扩展名，自动加 .mp3）
        on_progress: 进度回调

    Returns:
        输出 MP3 文件路径列表（通常为 1 个，超长时多个）
    """
    if not segments:
        raise ValueError("No audio segments to merge")

    cfg = get_config()
    max_minutes = cfg["audio"]["max_output_minutes"]
    bitrate = cfg["audio"]["bitrate"]

    # 估算总时长（假设平均语速 ~250 字/分钟，128kbps≈1MB/分钟）
    # 更准确的方式是用 ffprobe 获取每个片段时长
    total_duration_minutes = _estimate_duration(segments, bitrate)

    if on_progress:
        on_progress(f"合并 {len(segments)} 个音频片段（估计总时长 {total_duration_minutes:.0f} 分钟）...")

    output_files: list[Path] = []

    if total_duration_minutes <= max_minutes:
        out = output_path.parent / f"{output_path.stem}.mp3"
        _concat_segments(segments, out, bitrate)
        output_files.append(out)
    else:
        # 拆分多集
        part_count = int(total_duration_minutes / max_minutes) + 1
        segs_per_part = len(segments) // part_count + 1
        for part_idx in range(part_count):
            start = part_idx * segs_per_part
            end = min(start + segs_per_part, len(segments))
            part_segs = segments[start:end]
            if not part_segs:
                break
            out = output_path.parent / f"{output_path.stem}_Part{part_idx + 1}.mp3"
            _concat_segments(part_segs, out, bitrate)
            output_files.append(out)
            logger.info("Part %d/%d written: %s", part_idx + 1, part_count, out)

    return output_files


def _concat_segments(segments: list[Path], output: Path, bitrate: str) -> None:
    """使用 ffmpeg concat + loudnorm 合并片段。"""
    # 创建 concat 文件列表
    concat_list = output.parent / "_concat_list.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for seg in segments:
            # ffmpeg concat 格式用单引号包裹路径，路径中的单引号需转义为 '\''
            safe_path = str(seg.absolute()).replace("'", "'\\''")
            f.write(f"file '{safe_path}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-af", "loudnorm=I=-16:LRA=11:TP=-1.5,compand=attacks=0.3:decays=0.8:points=-80/-80|-45/-15|-27/-9|0/-7|20/-7:gain=5,afade=t=in:d=0.1,afade=t=out:d=0.3",
        "-b:a", bitrate,
        "-ar", "44100",
        "-ac", "1",
        str(output),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr[:500]}")
    finally:
        if concat_list.exists():
            concat_list.unlink()

    logger.info("Audio merged: %s", output)


def _estimate_duration(segments: list[Path], bitrate: str) -> float:
    """估算总时长（分钟）。

    优先用 ffprobe 获取精确时长；回退到文件大小估算。
    """
    # 尝试 ffprobe 获取实际时长（精确，支持 WAV/MP3）
    try:
        import subprocess as _sp
        total_sec = 0.0
        for p in segments:
            if not p.exists():
                continue
            result = _sp.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                total_sec += float(result.stdout.strip())
        if total_sec > 0:
            return total_sec / 60.0
    except Exception:
        pass

    # 回退：文件大小估算（WAV 用 ~706kbps，MP3 用配置值）
    total_bytes = sum(p.stat().st_size for p in segments if p.exists())
    first_ext = segments[0].suffix.lower() if segments else ""
    if first_ext == ".wav":
        # WAV: 44100 Hz * 16 bit * 1 channel = 705.6 kbps
        bps = 705600
    else:
        bps = int(bitrate.replace("k", "")) * 1000
    if bps == 0:
        return len(segments) * 0.5
    return (total_bytes * 8 / bps) / 60.0
