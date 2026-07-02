"""YouTube 内容提取模块。封装 yt-dlp，提取元数据、字幕和音频。"""

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yt_dlp

from src.config import get_config

logger = logging.getLogger(__name__)


def _find_node_runtime() -> Optional[str]:
    """查找 yt-dlp 可用的 Node.js，兼容服务进程缺少交互式 shell PATH。"""
    candidates = []
    if node_path := shutil.which("node"):
        candidates.append(Path(node_path))
    candidates.extend([
        Path("/opt/homebrew/bin/node"),
        Path("/usr/local/bin/node"),
    ])
    candidates.extend(sorted(
        (Path.home() / ".nvm" / "versions" / "node").glob("*/bin/node"),
        reverse=True,
    ))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            version = subprocess.run(
                [str(candidate), "--version"],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            ).stdout.strip().lstrip("v")
            if int(version.split(".", 1)[0]) >= 22:
                return str(candidate)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return None


@dataclass
class VideoMeta:
    video_id: str
    url: str
    title: str
    channel: str
    duration_seconds: int
    publish_date: str  # ISO-format date


@dataclass
class SubtitleEntry:
    start: str  # "hh:mm:ss"
    end: str
    text: str


@dataclass
class ExtractionResult:
    meta: VideoMeta
    subtitles: list[SubtitleEntry] = field(default_factory=list)
    audio_path: Optional[Path] = None
    has_captions: bool = False


def _parse_video_id(url_or_id: str) -> str:
    """从 YouTube URL 或纯 video_id 中提取 video_id。"""
    # 已经是纯 ID（11 位字母数字-_）
    if re.match(r"^[A-Za-z0-9_-]{11}$", url_or_id):
        return url_or_id

    # 标准 URL 格式
    patterns = [
        r"(?:v=|/v/|youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:embed/|shorts/)([A-Za-z0-9_-]{11})",
    ]
    for pat in patterns:
        match = re.search(pat, url_or_id)
        if match:
            return match.group(1)

    raise ValueError(f"无法从输入中解析 YouTube video_id: {url_or_id}")


def _subtitle_to_entries(raw_subs: list[dict]) -> list[SubtitleEntry]:
    """将 yt-dlp 原始字幕转换为统一 SubtitleEntry 列表。"""
    entries: list[SubtitleEntry] = []
    for sub in raw_subs:
        start_sec = float(sub.get("start", 0))
        end_sec = start_sec + float(sub.get("duration", 0))
        text = sub.get("text", "").strip()
        if not text:
            continue
        entries.append(SubtitleEntry(
            start=_seconds_to_timestamp(start_sec),
            end=_seconds_to_timestamp(end_sec),
            text=text,
        ))
    return entries


def _seconds_to_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _try_ydl(opts: dict, url: str, download: bool, proxy: str) -> dict:
    """执行 yt-dlp 提取，代理失败时自动回退直连。

    Args:
        opts: yt-dlp 选项（不含 proxy）
        url: 视频 URL
        download: 是否下载
        proxy: 代理 URL（空字符串 = 不用代理）

    Returns:
        info dict
    """
    import time as _time
    cfg = get_config()
    retries = cfg["processing"]["retry_times"]
    backoff = cfg["processing"]["retry_backoff_base"]

    # 新版 yt-dlp 需要 JS runtime 处理 YouTube 播放器挑战；优先复用本机 Node。
    base_opts = dict(opts)
    if "js_runtimes" not in base_opts:
        node_path = _find_node_runtime()
        if node_path:
            base_opts["js_runtimes"] = {"node": {"path": node_path}}

    # 优先用代理，失败则直连
    for use_proxy in ([True, False] if proxy else [False]):
        cur_opts = dict(base_opts)
        if use_proxy and proxy:
            cur_opts["proxy"] = proxy

        for attempt in range(retries):
            try:
                with yt_dlp.YoutubeDL(cur_opts) as ydl:
                    return ydl.extract_info(url, download=download)
            except Exception as e:
                msg = str(e)
                # 代理连接错误，或代理出口拿到与当前 IP 不匹配的媒体 URL，
                # 都应重新直连提取；只重试同一个代理无法修复这类 403。
                proxy_failed = any(kw in msg for kw in (
                    "SSL", "EOF occurred", "proxy", "Proxy",
                    "Connection refused", "Connection reset",
                ))
                proxy_media_forbidden = use_proxy and "HTTP Error 403" in msg
                if proxy_failed or proxy_media_forbidden:
                    if use_proxy and proxy:
                        logger.warning("Proxy failed (%s), retrying direct...", str(e)[:100])
                        break  # 跳出重试循环，进入直连模式
                if attempt < retries - 1:
                    _time.sleep(backoff ** attempt)
                else:
                    raise

    raise RuntimeError("yt-dlp failed")


def check_connectivity() -> tuple[bool, str]:
    """快速检测 YouTube 连通性。

    Returns:
        (ok: bool, detail: str) — ok=False 时直接报错，不要开始处理。
    """
    import urllib.request

    cfg = get_config()
    proxy = cfg.get("network", {}).get("proxy", "")

    test_url = "https://www.youtube.com"
    methods = []

    if proxy:
        methods.append(("代理 " + proxy, lambda: _probe_url(test_url, proxy)))
    methods.append(("直连", lambda: _probe_url(test_url, None)))

    for label, fn in methods:
        try:
            fn()
            return True, f"{label} 连通 ✓"
        except Exception as e:
            logger.info("连通性检测 %s 失败: %s", label, str(e)[:100])

    return False, "无法访问 YouTube：代理和直连均不可用。请检查代理/VPN 是否正常"


def _probe_url(url: str, proxy: Optional[str]):
    """尝试访问 URL，5 秒超时。"""
    import urllib.request, socket
    if proxy:
        req = urllib.request.Request(url)
        req.set_proxy(proxy, "http")
        req.set_proxy(proxy, "https")
    else:
        req = urllib.request.Request(url)
    urllib.request.urlopen(req, timeout=5)
    return True


def extract(video_url: str, output_dir: Optional[Path] = None) -> ExtractionResult:
    """从 YouTube 提取视频内容和元数据。

    Args:
        video_url: YouTube URL 或 video_id
        output_dir: 输出目录（默认 data/<video_id>/）

    Returns:
        ExtractionResult 包含元数据、字幕（如有）和音频路径（如无字幕）

    Raises:
        ValueError: URL 无效
        RuntimeError: 视频不可用（私享、已删除等）
    """
    video_id = _parse_video_id(video_url)
    cfg = get_config()
    root = cfg["_project_root"]

    if output_dir is None:
        output_dir = root / cfg["app"]["data_dir"] / video_id
    output_dir.mkdir(parents=True, exist_ok=True)

    url = f"https://www.youtube.com/watch?v={video_id}"
    meta_path = output_dir / "metadata.json"
    subs_path = output_dir / "captions_en.json"
    audio_template = str(output_dir / "%(id)s.%(ext)s")

    proxy = cfg.get("network", {}).get("proxy", "")

    # --- Pass 1: 获取元数据 ---
    ydl_opts_info = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "writesubtitles": False,
    }
    try:
        info = _try_ydl(ydl_opts_info, url, download=False, proxy=proxy)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Private video" in msg or "unavailable" in msg.lower():
            raise RuntimeError(f"视频不可用（私享或已删除）: {video_url}")
        raise RuntimeError(f"无法访问视频: {msg}")

    # --- 构建元数据 ---
    duration = int(info.get("duration", 0)) if info.get("duration") else 0
    publish = info.get("upload_date", "")
    if publish and len(publish) == 8:
        publish = f"{publish[:4]}-{publish[4:6]}-{publish[6:8]}"

    meta = VideoMeta(
        video_id=video_id,
        url=url,
        title=info.get("title", ""),
        channel=info.get("channel", "") or info.get("uploader", ""),
        duration_seconds=duration,
        publish_date=publish,
    )

    # 保存元数据
    meta_path.write_text(json.dumps({
        "video_id": meta.video_id,
        "url": meta.url,
        "title": meta.title,
        "channel": meta.channel,
        "duration_seconds": meta.duration_seconds,
        "publish_date": meta.publish_date,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    result = ExtractionResult(meta=meta)

    # --- Pass 2: 优先字幕，兜底音频 ---
    subtitles_raw = info.get("subtitles", {})
    auto_subs_raw = info.get("automatic_captions", {})
    has_manual_subs = "en" in subtitles_raw or "en-US" in subtitles_raw or "en-GB" in subtitles_raw
    has_auto_subs = "en" in auto_subs_raw or "en-US" in auto_subs_raw

    if has_manual_subs or has_auto_subs:
        # 尝试下载字幕
        logger.info("字幕可用，尝试下载...")
        ydl_opts_subs = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": not has_manual_subs,
            "subtitleslangs": ["en"],
            "subtitlesformat": "json3",
        }
        info_subs = _try_ydl(ydl_opts_subs, url, download=False, proxy=proxy)

        subs = info_subs.get("subtitles", {}) or info_subs.get("automatic_captions", {})
        en_subs_raw = subs.get("en") or subs.get("en-US") or subs.get("en-GB") or []
        if en_subs_raw:
            entries = _subtitle_to_entries(en_subs_raw)
            if entries:
                result.subtitles = entries
                result.has_captions = True
                subs_path.write_text(json.dumps(
                    [{"start": e.start, "end": e.end, "text": e.text} for e in entries],
                    ensure_ascii=False, indent=2,
                ), encoding="utf-8")
                logger.info("字幕下载成功: %d 条", len(entries))
                return result
            else:
                logger.warning("字幕条目全为空文本，改用音频")
        else:
            logger.warning("字幕检测到但下载为空，改用音频")

    # 字幕不可用或下载失败 → 下载音频
    logger.info("下载音频用于 ASR...")
    ydl_opts_audio = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": audio_template,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav",
            "preferredquality": "192",
        }],
    }
    try:
        _try_ydl(ydl_opts_audio, url, download=True, proxy=proxy)
    except Exception as e:
        raise RuntimeError(f"音频下载失败: {e}")

    # 查找下载的音频文件
    wav_path = output_dir / f"{video_id}.wav"
    m4a_path = output_dir / f"{video_id}.m4a"
    if wav_path.exists():
        result.audio_path = wav_path
    elif m4a_path.exists():
        result.audio_path = m4a_path

    return result
