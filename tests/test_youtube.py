"""U2 测试。YouTube 提取模块。"""
import pytest
from src.youtube import extractor
from src.youtube.extractor import _parse_video_id


def test_parse_standard_url():
    assert _parse_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_parse_short_url():
    assert _parse_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_parse_raw_id():
    assert _parse_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_parse_embed_url():
    assert _parse_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_invalid_url_raises():
    with pytest.raises(ValueError):
        _parse_video_id("not a valid url or id")


def test_parse_url_with_params():
    assert _parse_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=120") == "dQw4w9WgXcQ"


def test_proxy_403_reextracts_with_direct_connection(monkeypatch):
    """代理媒体 URL 返回 403 时，应重新直连提取而非重复使用代理。"""
    calls = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            calls.append({"proxy": self.options.get("proxy"), "download": download})
            if self.options.get("proxy"):
                raise RuntimeError("unable to download video data: HTTP Error 403: Forbidden")
            return {"id": "dQw4w9WgXcQ"}

    monkeypatch.setattr(extractor.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    result = extractor._try_ydl(
        {"quiet": True},
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        download=True,
        proxy="http://127.0.0.1:7897",
    )

    assert result["id"] == "dQw4w9WgXcQ"
    assert calls == [
        {"proxy": "http://127.0.0.1:7897", "download": True},
        {"proxy": None, "download": True},
    ]
