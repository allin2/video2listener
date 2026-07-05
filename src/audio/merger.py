"""音频合并模块。使用 ffmpeg 合并 TTS 片段并标准化。"""

import asyncio
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from src.config import get_config

logger = logging.getLogger(__name__)


def _resolve_media_tool(name: str) -> str:
    """解析 ffmpeg/ffprobe，兼容 launchd 缺少 Homebrew PATH 的环境。"""
    found = shutil.which(name)
    if found:
        return found
    for directory in (Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError(f"未找到 {name}，请确认已安装 ffmpeg")


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


async def merge_async(
    segments: list[Path],
    output_path: Path,
    on_progress: Optional[callable] = None,
) -> list[Path]:
    """异步包装——将同步 merge 卸载到线程池。"""
    return await asyncio.to_thread(merge, segments, output_path, on_progress)


def _concat_segments(segments: list[Path], output: Path, bitrate: str) -> None:
    """使用 ffmpeg concat + loudnorm 合并片段，并校验输出可播放性。"""
    # 创建 concat 文件列表
    concat_list = output.parent / "_concat_list.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for seg in segments:
            # ffmpeg concat 格式用单引号包裹路径，路径中的单引号需转义为 '\''
            safe_path = str(seg.absolute()).replace("'", "'\\''")
            f.write(f"file '{safe_path}'\n")

    # 先写临时文件，只有校验通过后才替换最终产物，避免失败时覆盖可用 MP3。
    temp_output = output.with_name(f".{output.stem}.merging.mp3")
    cmd = [
        _resolve_media_tool("ffmpeg"), "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        # 不使用无起始时间的 fade-out；它会在 0.3 秒后把整段节目变成静音。
        # compand 也会显著压低正常的 MiMo WAV，因此只保留响度标准化。
        "-af", "loudnorm=I=-16:LRA=11:TP=-1.5",
        "-b:a", bitrate,
        "-ar", "44100",
        "-ac", "1",
        str(temp_output),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr[:500]}")
        _validate_output(segments, temp_output)
        temp_output.replace(output)
    finally:
        if concat_list.exists():
            concat_list.unlink()
        if temp_output.exists():
            temp_output.unlink()

    logger.info("Audio merged: %s", output)


def _probe_duration(path: Path) -> float:
    """返回音频时长（秒）；无法读取时抛出异常。"""
    result = subprocess.run(
        [_resolve_media_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"无法读取合并音频时长: {path.name}")
    return float(result.stdout.strip())


def _probe_volume(path: Path) -> tuple[float, float]:
    """返回 (平均音量, 峰值音量)，单位 dB。"""
    result = subprocess.run(
        [_resolve_media_tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )
    report = f"{result.stdout}\n{result.stderr}"
    mean_match = re.search(r"mean_volume:\s*(-?[\d.]+) dB", report)
    max_match = re.search(r"max_volume:\s*(-?[\d.]+) dB", report)
    if not mean_match or not max_match:
        raise RuntimeError(f"无法检测合并音频音量: {path.name}")
    return float(mean_match.group(1)), float(max_match.group(1))


def _validate_output(segments: list[Path], output: Path) -> None:
    """阻止近静音、空文件或明显截断的合并结果进入完成状态。"""
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("合并音频为空")

    expected_duration = sum(_probe_duration(path) for path in segments)
    actual_duration = _probe_duration(output)
    tolerance = max(2.0, expected_duration * 0.02)
    if expected_duration <= 0 or abs(actual_duration - expected_duration) > tolerance:
        raise RuntimeError(
            f"合并音频时长异常: 期望 {expected_duration:.1f}s，实际 {actual_duration:.1f}s"
        )

    mean_volume, max_volume = _probe_volume(output)
    if mean_volume < -45.0 or max_volume < -20.0:
        raise RuntimeError(
            f"合并音频音量异常: 平均 {mean_volume:.1f}dB，峰值 {max_volume:.1f}dB"
        )


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
                [_resolve_media_tool("ffprobe"), "-v", "quiet", "-show_entries", "format=duration",
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
