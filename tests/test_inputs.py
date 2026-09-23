"""测试 inputs.py 多平台输入解析。"""

import pytest
from src.inputs import Platform, extract_urls, identify_source, parse_video_input


def test_extract_urls_from_plain_and_markdown():
    text = (
        "【推荐】快来看 [这个视频](https://www.bilibili.com/video/BV1xx411c7X5) 超好看！\n"
        "还有油管的 https://youtu.be/dQw4w9WgXcQ 也很不错。"
    )
    urls = extract_urls(text)
    assert urls == [
        "https://www.bilibili.com/video/BV1xx411c7X5",
        "https://youtu.be/dQw4w9WgXcQ",
    ]


def test_identify_source():
    assert identify_source("https://www.youtube.com/watch?v=123") == Platform.YOUTUBE
    assert identify_source("https://youtu.be/123") == Platform.YOUTUBE
    assert identify_source("https://www.bilibili.com/video/BV1xx411c7X5") == Platform.BILIBILI
    assert identify_source("https://b23.tv/abcde") == Platform.BILIBILI
    assert identify_source("https://v.douyin.com/abcde/") == Platform.DOUYIN
    assert identify_source("https://www.douyin.com/video/723456789") == Platform.DOUYIN
    assert identify_source("http://xhslink.com/a/bCdEfG") == Platform.XIAOHONGSHU
    assert identify_source("https://www.xiaohongshu.com/discovery/item/64f123") == Platform.XIAOHONGSHU
    assert identify_source("https://example.com/video") is None


def test_parse_video_input_youtube():
    # 纯 11 位
    p, vid, url = parse_video_input("dQw4w9WgXcQ")
    assert p == Platform.YOUTUBE
    assert vid == "dQw4w9WgXcQ"
    assert "youtube.com/watch?v=dQw4w9WgXcQ" in url

    # 完整 URL
    p, vid, url = parse_video_input("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s")
    assert p == Platform.YOUTUBE
    assert vid == "dQw4w9WgXcQ"

    # 短链
    p, vid, url = parse_video_input("https://youtu.be/dQw4w9WgXcQ")
    assert p == Platform.YOUTUBE
    assert vid == "dQw4w9WgXcQ"


def test_parse_video_input_bilibili():
    # 纯 BV
    p, vid, url = parse_video_input("BV1xx411c7X5")
    assert p == Platform.BILIBILI
    assert vid == "BV1xx411c7X5"
    assert url == "https://www.bilibili.com/video/BV1xx411c7X5"

    # 完整 URL
    p, vid, url = parse_video_input("https://www.bilibili.com/video/BV1xx411c7X5?spm_id_from=333.999")
    assert p == Platform.BILIBILI
    assert vid == "BV1xx411c7X5"

    # 分享文案
    p, vid, url = parse_video_input("【高能】千万别眨眼！ https://www.bilibili.com/video/BV1xx411c7X5 复制此链接进入B站")
    assert p == Platform.BILIBILI
    assert vid == "BV1xx411c7X5"

    # b23 短链
    p, vid, url = parse_video_input("https://b23.tv/abc1234")
    assert p == Platform.BILIBILI
    assert vid == "pending_resolve"


def test_parse_video_input_douyin_and_xhs():
    p, vid, url = parse_video_input("https://www.douyin.com/video/7234567890123456789")
    assert p == Platform.DOUYIN
    assert vid == "7234567890123456789"

    p, vid, url = parse_video_input("7.24 复制打开抖音，看看【谁的作品】 https://v.douyin.com/iJabcde/ 01/22")
    assert p == Platform.DOUYIN
    assert vid == "pending_resolve"

    p, vid, url = parse_video_input("http://xhslink.com/a/abcdef")
    assert p == Platform.XIAOHONGSHU
    assert vid == "pending_resolve"


def test_parse_video_input_invalid():
    with pytest.raises(ValueError):
        parse_video_input("not a valid video url or text")
