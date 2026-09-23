"""统一视频提取路由与平台探活分流模块。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from src.config import get_config
from src.inputs import Platform, identify_source, parse_video_input
from src.sources.base import ExtractionResult
from src.sources.bilibili import extract_bilibili
from src.sources.douyin import extract_douyin
from src.sources.xiaohongshu import extract_xiaohongshu
from src.youtube.extractor import check_connectivity as check_youtube_connectivity
from src.youtube.extractor import extract as extract_youtube

logger = logging.getLogger(__name__)


def check_connectivity(platform: Platform = Platform.YOUTUBE) -> tuple[bool, str]:
    """根据目标平台分流进行网络探活。

    YouTube 探活海外连通性/代理；
    国内平台（B站、抖音、小红书）探活国内直连网络，无需翻墙代理。
    """
    if platform == Platform.YOUTUBE:
        return check_youtube_connectivity()

    # 国内平台连通性测试
    import urllib.request

    test_url = "https://www.bilibili.com"
    try:
        req = urllib.request.Request(
            test_url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        urllib.request.urlopen(req, timeout=5)
        return True, "国内网络连通 ✓"
    except Exception as e:
        logger.warning("国内站点连通性测试失败: %s", e)
        return False, f"国内网络访问异常: {e}"


def extract(
    url_or_input: str,
    output_dir: Optional[Path] = None,
    platform: Optional[Platform] = None,
) -> ExtractionResult:
    """根据用户输入统一分发到对应平台的提取适配器。

    Args:
        url_or_input: 纯 ID、视频 URL、或包含分享口令/文案的文本
        output_dir: 输出目录
        platform: 可选指定平台，不指定时自动解析

    Returns:
        ExtractionResult
    """
    if platform is None:
        p, vid, url = parse_video_input(url_or_input)
    else:
        p = platform
        vid = url_or_input
        url = url_or_input

    logger.info("分流至平台适配器: %s (ID/URL: %s)", p.value, vid)

    if p == Platform.BILIBILI:
        return extract_bilibili(url, output_dir)
    elif p == Platform.DOUYIN:
        return extract_douyin(url, output_dir)
    elif p == Platform.XIAOHONGSHU:
        return extract_xiaohongshu(url, output_dir)
    elif p == Platform.YOUTUBE:
        return extract_youtube(vid, output_dir)
    else:
        raise ValueError(f"未知的视频平台: {p}")
