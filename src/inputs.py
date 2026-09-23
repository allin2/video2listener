"""多平台视频输入清洗与路由模块。

支持 YouTube、B站、抖音、小红书等平台的 URL 与复杂分享文案解析。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Optional
from urllib.parse import urlparse


class Platform(StrEnum):
    YOUTUBE = "youtube"
    BILIBILI = "bilibili"
    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"


# ── 域名到平台的映射 ──────────────────────────────────────────────────────────

_DOMAIN_MAP: dict[str, Platform] = {
    "youtube.com": Platform.YOUTUBE,
    "www.youtube.com": Platform.YOUTUBE,
    "m.youtube.com": Platform.YOUTUBE,
    "youtu.be": Platform.YOUTUBE,
    "bilibili.com": Platform.BILIBILI,
    "www.bilibili.com": Platform.BILIBILI,
    "b23.tv": Platform.BILIBILI,
    "douyin.com": Platform.DOUYIN,
    "www.douyin.com": Platform.DOUYIN,
    "v.douyin.com": Platform.DOUYIN,
    "xiaohongshu.com": Platform.XIAOHONGSHU,
    "www.xiaohongshu.com": Platform.XIAOHONGSHU,
    "xhslink.com": Platform.XIAOHONGSHU,
    "xhslink.cn": Platform.XIAOHONGSHU,
    "www.xhslink.cn": Platform.XIAOHONGSHU,
}

# 提取纯 URL 与 Markdown 语法 URL
_URL_RE = re.compile(r"""https?://[^\s<>"']+""", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_BVID_RE = re.compile(r"BV[a-zA-Z0-9]{10}")
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_DOUYIN_ID_RE = re.compile(r"/video/(\d+)")


def extract_urls(raw_text: str) -> list[str]:
    """从任意混合文案（包含 Markdown 或 App 分享文字）中提取 HTTP(S) URL。"""
    if not raw_text or not raw_text.strip():
        return []

    seen: set[str] = set()
    result: list[str] = []

    # 1. 优先提取 Markdown [text](url) 语法中的链接
    text_stripped = raw_text
    for match in _MD_LINK_RE.finditer(text_stripped):
        url = match.group(2).strip()
        if url.startswith(("http://", "https://")):
            if url not in seen:
                seen.add(url)
                result.append(url)

    text_stripped = _MD_LINK_RE.sub(" ", text_stripped)

    # 2. 提取文本中出现的直接 URL
    for match in _URL_RE.finditer(text_stripped):
        url = match.group(0).rstrip(".,;:!?)，。！？】）》\"'")
        if url not in seen:
            seen.add(url)
            result.append(url)

    return result


def identify_source(url: str) -> Optional[Platform]:
    """根据 URL 域名识别所属平台。"""
    if not url:
        return None

    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except Exception:
        return None

    hostname = (parsed.hostname or "").lower()

    if hostname in _DOMAIN_MAP:
        return _DOMAIN_MAP[hostname]

    for domain, platform in _DOMAIN_MAP.items():
        if hostname == domain or hostname.endswith("." + domain):
            return platform

    return None


def parse_video_input(raw_input: str) -> tuple[Platform, str, str]:
    """解析用户输入，统一提取平台、规范化 ID 与可访问 URL。

    支持输入类型：
    1. 纯 11 位 YouTube ID（如 dQw4w9WgXcQ）
    2. 纯 B站 BV 号（如 BV1xx411c7X5）
    3. 完整 YouTube URL（watch?v=, youtu.be/, shorts/, embed/）
    4. 完整 B站 URL 或 b23.tv 短链
    5. 完整 抖音 URL 或 v.douyin.com 短链
    6. 完整 小红书 URL 或 xhslink.com 短链
    7. 包含上述链接的 App 分享文案

    Returns:
        (platform: Platform, canonical_id: str, clean_url: str)

    Raises:
        ValueError: 无法识别或不支持的输入格式
    """
    text = (raw_input or "").strip()
    if not text:
        raise ValueError("输入不能为空")

    # 1. 直接输入纯 YouTube ID (11 位)
    if _YOUTUBE_ID_RE.match(text):
        return Platform.YOUTUBE, text, f"https://www.youtube.com/watch?v={text}"

    # 2. 直接输入纯 B站 BV 号 (BV + 10 位字母数字)
    bvid_match = _BVID_RE.search(text)
    if bvid_match and len(text) == 12:
        bvid = bvid_match.group(0)
        return Platform.BILIBILI, bvid, f"https://www.bilibili.com/video/{bvid}"

    # 3. 从文本中提取 URL
    urls = extract_urls(text)
    target_url = urls[0] if urls else text

    # 4. 判断 URL 平台
    platform = identify_source(target_url)

    if platform == Platform.YOUTUBE:
        # YouTube ID 提取
        patterns = [
            r"(?:v=|/v/|youtu\.be/)([A-Za-z0-9_-]{11})",
            r"(?:embed/|shorts/)([A-Za-z0-9_-]{11})",
        ]
        for pat in patterns:
            m = re.search(pat, target_url)
            if m:
                vid = m.group(1)
                return Platform.YOUTUBE, vid, f"https://www.youtube.com/watch?v={vid}"
        raise ValueError(f"无法解析 YouTube 视频 ID: {target_url}")

    if platform == Platform.BILIBILI:
        m = _BVID_RE.search(target_url)
        if m:
            bvid = m.group(0)
            return Platform.BILIBILI, bvid, f"https://www.bilibili.com/video/{bvid}"
        # 短链 b23.tv，此时以短链为 URL，由 adapter resolve 解析真实 BV 号
        return Platform.BILIBILI, "pending_resolve", target_url

    if platform == Platform.DOUYIN:
        m = _DOUYIN_ID_RE.search(target_url)
        if m:
            dy_id = m.group(1)
            return Platform.DOUYIN, dy_id, target_url
        return Platform.DOUYIN, "pending_resolve", target_url

    if platform == Platform.XIAOHONGSHU:
        m = re.search(r"/(?:discovery/item|explore)/([a-zA-Z0-9]+)", target_url)
        if m:
            xhs_id = m.group(1)
            return Platform.XIAOHONGSHU, xhs_id, target_url
        return Platform.XIAOHONGSHU, "pending_resolve", target_url

    # 兜底：如果没提取出 URL 但文本里包含了 BV 号
    if bvid_match:
        bvid = bvid_match.group(0)
        return Platform.BILIBILI, bvid, f"https://www.bilibili.com/video/{bvid}"

    raise ValueError(
        f"不支持或无法识别的视频链接: {raw_input[:100]}。\n"
        "目前支持 YouTube、B站 (BV号/b23短链)、抖音、小红书视频链接或 App 分享文案。"
    )


def resolve_canonical_video_id(raw_input: str) -> tuple[Platform, str, str]:
    """解析并解析短链重定向，返回规范化的 (platform, canonical_id, canonical_url)。"""
    platform, vid, url = parse_video_input(raw_input)
    if vid != "pending_resolve":
        if platform == Platform.DOUYIN and not vid.startswith("dy_"):
            vid = f"dy_{vid}"
        elif platform == Platform.XIAOHONGSHU and not vid.startswith("xhs_"):
            vid = f"xhs_{vid}"
        return platform, vid, url

    if platform == Platform.BILIBILI:
        from src.sources.bilibili import resolve_bvid
        bvid, canonical_url = resolve_bvid(url)
        return platform, bvid, canonical_url
    elif platform == Platform.DOUYIN:
        from src.sources.douyin import resolve_douyin_url
        dy_id, canonical_url = resolve_douyin_url(url)
        return platform, f"dy_{dy_id}", canonical_url
    elif platform == Platform.XIAOHONGSHU:
        from src.sources.xiaohongshu import resolve_xhs_url
        note_id, canonical_url, _ = resolve_xhs_url(url)
        return platform, f"xhs_{note_id}", canonical_url

    return platform, vid, url
