"""B站 (Bilibili) 视频提取模块。

参考 evey2obs-ds 实现：
- 使用 B站官方公开 API 获取元数据（无需登录与 Cookie）。
- 优先获取官方中文字幕（存在时 0 ASR 算力成本）。
- 使用 /x/player/playurl DASH 音频流直取 .m4a，无需下载视频画面。
- 兜底支持 yt-dlp。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import yt_dlp

from src.config import get_config
from src.sources.base import (
    ExtractionResult,
    SubtitleEntry,
    VideoMeta,
    save_metadata,
    seconds_to_timestamp,
)

logger = logging.getLogger(__name__)

_BVID_RE = re.compile(r"BV[a-zA-Z0-9]{10}")
_BILI_API = "https://api.bilibili.com"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _client(**kwargs) -> httpx.Client:
    """B站请求一律直连：国内服务无需代理，走代理反而易触发风控或 SSL 中断。"""
    return httpx.Client(trust_env=False, **kwargs)


# yt-dlp 中 proxy 为空串表示强制直连（忽略环境变量代理）
_YDL_DIRECT = {"proxy": ""}


def resolve_bvid(url_or_bvid: str) -> tuple[str, str]:
    """解析并提取 BV 号与标准视频 URL。

    支持纯 BV 号、bilibili.com/video/BV...、以及 b23.tv 短链（自动跟踪 302 重定向）。
    """
    m = _BVID_RE.search(url_or_bvid)
    if m:
        bvid = m.group(0)
        return bvid, f"https://www.bilibili.com/video/{bvid}"

    # 处理 b23.tv 短链
    if "b23.tv" in url_or_bvid:
        url = url_or_bvid
        if not url.startswith("http"):
            url = f"https://{url}"
        try:
            with _client(follow_redirects=False, timeout=10.0) as client:
                resp = client.get(url, headers={"User-Agent": _USER_AGENT})
                if 300 <= resp.status_code < 400:
                    loc = resp.headers.get("Location", "")
                    m = _BVID_RE.search(loc)
                    if m:
                        bvid = m.group(0)
                        return bvid, f"https://www.bilibili.com/video/{bvid}"
        except Exception as e:
            logger.warning("解析 b23.tv 短链重定向失败: %s", e)

    raise ValueError(f"无法从输入中解析 B站 BV 号: {url_or_bvid}")


def _bili_get(path: str, params: dict) -> Optional[dict]:
    """请求 B站 公共 API。"""
    try:
        with _client(timeout=15.0) as client:
            resp = client.get(
                f"{_BILI_API}{path}",
                params=params,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Referer": "https://www.bilibili.com",
                },
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("B站 API %s 请求失败: %s", path, e)
        return None


# 旧接口 /x/web-interface/view 对无 Cookie 请求常返回 412（风控），
# wbi/view 在不签名时仍可用，故优先；两者都失败才回退 yt-dlp。
_VIEW_ENDPOINTS = ("/x/web-interface/wbi/view", "/x/web-interface/view")


def fetch_view(bvid: str) -> Optional[dict]:
    """获取视频元数据，返回 {title, channel, duration, pubdate}；全部失败返回 None。"""
    for path in _VIEW_ENDPOINTS:
        data = _bili_get(path, {"bvid": bvid})
        if data and data.get("code") == 0 and data.get("data"):
            info = data["data"]
            return {
                "title": info.get("title") or bvid,
                "channel": (info.get("owner") or {}).get("name") or "B站UP主",
                "duration": int(info.get("duration") or 0),
                "pubdate": int(info.get("pubdate") or 0),
            }

    logger.warning("B站公共 API 获取元数据失败，回退 yt-dlp...")
    try:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, **_YDL_DIRECT}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.bilibili.com/video/{bvid}", download=False)
        return {
            "title": info.get("title") or bvid,
            "channel": info.get("uploader") or "B站UP主",
            "duration": int(info.get("duration") or 0),
            "pubdate": int(info.get("timestamp") or 0),
        }
    except Exception as e:
        logger.warning("yt-dlp 获取 B站元数据失败: %s", e)
        return None


def _get_cid(bvid: str) -> Optional[int]:
    """获取视频首个分 P 的 cid。"""
    data = _bili_get("/x/player/pagelist", {"bvid": bvid})
    if data and data.get("code") == 0 and data.get("data"):
        return data["data"][0].get("cid")
    return None


def extract_bilibili(
    video_url_or_bvid: str,
    output_dir: Optional[Path] = None,
) -> ExtractionResult:
    """提取 B站视频的元数据、中文字幕或音频。"""
    bvid, canonical_url = resolve_bvid(video_url_or_bvid)

    cfg = get_config()
    root = cfg["_project_root"]
    if output_dir is None:
        output_dir = root / cfg["app"]["data_dir"] / bvid
    output_dir.mkdir(parents=True, exist_ok=True)

    meta_path = output_dir / "metadata.json"
    subs_path = output_dir / "captions_en.json"  # 保持文件名与现有管线兼容
    subs_zh_path = output_dir / "captions_zh.json"
    audio_path = output_dir / f"{bvid}.m4a"

    # --- 1. 获取元数据 ---
    view = fetch_view(bvid)
    if view:
        title = view["title"]
        channel = view["channel"]
        duration = view["duration"]
        publish_date = (
            datetime.fromtimestamp(view["pubdate"], tz=timezone.utc).strftime("%Y-%m-%d")
            if view["pubdate"]
            else ""
        )
    else:
        title = bvid
        channel = "B站"
        duration = 0
        publish_date = ""

    meta = VideoMeta(
        video_id=bvid,
        url=canonical_url,
        title=title,
        channel=channel,
        duration_seconds=duration,
        publish_date=publish_date,
        platform="bilibili",
    )
    save_metadata(meta, meta_path)

    result = ExtractionResult(meta=meta, source_language="zh")

    # --- 2. 尝试获取官方字幕 ---
    cid = _get_cid(bvid)
    if cid:
        player_data = _bili_get("/x/player/wbi/v2", {"bvid": bvid, "cid": cid})
        if player_data and player_data.get("code") == 0:
            sub_list = (
                player_data.get("data", {}).get("subtitle", {}).get("subtitles", [])
            )
            if sub_list:
                sub_url = ""
                for s in sub_list:
                    if "中文" in s.get("lan_doc", ""):
                        sub_url = s.get("subtitle_url", "")
                        break
                if not sub_url:
                    sub_url = sub_list[0].get("subtitle_url", "")
                if sub_url:
                    if sub_url.startswith("//"):
                        sub_url = "https:" + sub_url
                    try:
                        with _client(timeout=15.0, follow_redirects=True) as client:
                            sub_resp = client.get(sub_url)
                            sub_resp.raise_for_status()
                            sub_json = sub_resp.json()
                            entries: list[SubtitleEntry] = []
                            for item in sub_json.get("body", []):
                                s_sec = float(item.get("from", 0))
                                e_sec = float(item.get("to", 0))
                                t_str = item.get("content", "").strip()
                                if t_str:
                                    entries.append(
                                        SubtitleEntry(
                                            start=seconds_to_timestamp(s_sec),
                                            end=seconds_to_timestamp(e_sec),
                                            text=t_str,
                                        )
                                    )
                            if entries:
                                result.subtitles = entries
                                result.has_captions = True
                                subs_data = json.dumps(
                                    [{"start": e.start, "end": e.end, "text": e.text} for e in entries],
                                    ensure_ascii=False,
                                    indent=2,
                                )
                                subs_path.write_text(subs_data, encoding="utf-8")
                                subs_zh_path.write_text(subs_data, encoding="utf-8")
                                logger.info("B站官方字幕获取成功: %d 条", len(entries))
                                return result
                    except Exception as e:
                        logger.warning("下载 B站 官方字幕失败: %s，改用音频下载", e)

    # --- 3. 字幕不可用，下载音频 ---
    # 优先用 playurl DASH 音频流
    downloaded = False
    if cid:
        playurl_data = _bili_get(
            "/x/player/playurl",
            {
                "bvid": bvid,
                "cid": cid,
                "qn": "0",
                "fnval": "16",
                "fnver": "0",
                "fourk": "1",
                "platform": "web",
            },
        )
        if playurl_data and playurl_data.get("code") == 0:
            dash = playurl_data.get("data", {}).get("dash", {})
            audio_tracks = dash.get("audio", [])
            audio_stream_url = ""
            if audio_tracks:
                best = max(audio_tracks, key=lambda t: t.get("bandwidth", 0))
                audio_stream_url = best.get("baseUrl") or best.get("base_url", "")
            if not audio_stream_url:
                durl = playurl_data.get("data", {}).get("durl", [])
                if durl:
                    audio_stream_url = durl[0].get("url", "")

            if audio_stream_url:
                try:
                    logger.info("开始通过 B站 DASH 流下载纯音频...")
                    with _client(
                        timeout=120.0,
                        follow_redirects=True,
                        headers={
                            "User-Agent": _USER_AGENT,
                            "Referer": "https://www.bilibili.com",
                        },
                    ) as client:
                        with client.stream("GET", audio_stream_url) as stream_resp:
                            stream_resp.raise_for_status()
                            with open(audio_path, "wb") as f:
                                for chunk in stream_resp.iter_bytes(chunk_size=65536):
                                    f.write(chunk)
                    if audio_path.exists() and audio_path.stat().st_size > 1024:
                        result.audio_path = audio_path
                        downloaded = True
                        logger.info("B站 DASH 音频下载完成: %s", audio_path)
                except Exception as e:
                    logger.warning("B站 DASH 音频下载失败: %s，回退 yt-dlp", e)

    if not downloaded:
        # 回退使用 yt-dlp 下载音频
        logger.info("使用 yt-dlp 下载 B站音频...")
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            **_YDL_DIRECT,
            "format": "bestaudio/best",
            "outtmpl": str(output_dir / f"{bvid}.%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                }
            ],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([canonical_url])

        if audio_path.exists():
            result.audio_path = audio_path
        else:
            wav_path = output_dir / f"{bvid}.wav"
            if wav_path.exists():
                result.audio_path = wav_path

    return result
