"""测试多平台提取与适配器。"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from src.inputs import Platform
from src.sources.base import VideoMeta, SubtitleEntry, ExtractionResult, seconds_to_timestamp
from src.sources.bilibili import resolve_bvid, extract_bilibili
from src.sources.douyin import resolve_douyin_url
from src.sources.xiaohongshu import resolve_xhs_url
from src.sources import router


def test_seconds_to_timestamp():
    assert seconds_to_timestamp(0) == "00:00:00"
    assert seconds_to_timestamp(65) == "00:01:05"
    assert seconds_to_timestamp(3665) == "01:01:05"


def test_resolve_bvid():
    bv, url = resolve_bvid("BV1xx411c7X5")
    assert bv == "BV1xx411c7X5"
    assert url == "https://www.bilibili.com/video/BV1xx411c7X5"

    bv, url = resolve_bvid("https://www.bilibili.com/video/BV1xx411c7X5?p=1")
    assert bv == "BV1xx411c7X5"


def test_resolve_douyin_url():
    dy_id, url = resolve_douyin_url("https://www.douyin.com/video/7234567890123456789")
    assert dy_id == "7234567890123456789"
    assert url == "https://www.douyin.com/video/7234567890123456789"


def test_resolve_douyin_url_shortlink_garbage_raises():
    """短链重定向后无法提取数字 ID 时必须抛错，而不是产出 douyin_ 伪 ID。"""
    import src.sources.douyin as douyin_mod

    class FakeResp:
        url = "https://www.douyin.com/"  # 重定向到首页，无 /video/{id}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            return FakeResp()

    with patch.object(douyin_mod.httpx, "Client", FakeClient):
        with pytest.raises(ValueError, match="无法从抖音短链解析视频 ID"):
            resolve_douyin_url("https://v.douyin.com/iAbCdEfGh/")


def test_resolve_xhs_url():
    note_id, canonical, full = resolve_xhs_url(
        "https://www.xiaohongshu.com/discovery/item/64f1234567890?xsec_token=AB123&share_id=track_999"
    )
    assert note_id == "64f1234567890"
    assert canonical == "https://www.xiaohongshu.com/discovery/item/64f1234567890"
    assert "xsec_token=AB123" in full
    assert "share_id" not in full


@patch("src.sources.bilibili._bili_get")
@patch("src.sources.bilibili._get_cid")
def test_extract_bilibili_with_subtitles(mock_get_cid, mock_bili_get, tmp_path):
    mock_bili_get.side_effect = [
        # view
        {
            "code": 0,
            "data": {
                "title": "B站测试视频",
                "owner": {"name": "测试UP主"},
                "duration": 120,
                "pubdate": 1700000000,
            },
        },
        # player wbi
        {
            "code": 0,
            "data": {
                "subtitle": {
                    "subtitles": [
                        {"lan_doc": "中文（简体）", "subtitle_url": "https://example.com/sub.json"}
                    ]
                }
            },
        },
    ]
    mock_get_cid.return_value = 123456

    with patch("src.sources.bilibili.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "body": [
                {"from": 0.0, "to": 3.0, "content": "你好，欢迎收听。"},
                {"from": 3.0, "to": 6.0, "content": "这是测试字幕。"},
            ]
        }
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = extract_bilibili("BV1xx411c7X5", output_dir=tmp_path)

        assert result.meta.video_id == "BV1xx411c7X5"
        assert result.meta.title == "B站测试视频"
        assert result.meta.channel == "测试UP主"
        assert result.has_captions is True
        assert len(result.subtitles) == 2
        assert result.source_language == "zh"
        assert (tmp_path / "metadata.json").exists()
        assert (tmp_path / "captions_en.json").exists()
        assert (tmp_path / "captions_zh.json").exists()


def test_router_connectivity():
    # 测试国内平台探活直连（mock 避免外部真实网络抖动）
    with patch("urllib.request.build_opener") as mock_build:
        ok, msg = router.check_connectivity(Platform.BILIBILI)
        assert ok is True
    # 必须带空 ProxyHandler 直连，不读环境变量代理
    handler = mock_build.call_args[0][0]
    assert handler.proxies == {}


def test_fetch_view_prefers_wbi_endpoint_and_falls_back_on_412():
    from src.sources import bilibili

    calls = []

    def fake_get(path, params):
        calls.append(path)
        if path == "/x/web-interface/wbi/view":
            return None  # 模拟 412
        return {"code": 0, "data": {"title": "标题", "owner": {"name": "UP"}, "duration": 90}}

    with patch.object(bilibili, "_bili_get", side_effect=fake_get):
        view = bilibili.fetch_view("BV1xx411c7X5")

    assert calls == ["/x/web-interface/wbi/view", "/x/web-interface/view"]
    assert view["title"] == "标题" and view["duration"] == 90


def test_fetch_view_uses_bilibili_url_for_ytdlp_fallback():
    from src.sources import bilibili

    ydl = MagicMock()
    ydl.__enter__.return_value.extract_info.return_value = {"title": "T", "duration": 60}
    with patch.object(bilibili, "_bili_get", return_value=None), \
            patch.object(bilibili.yt_dlp, "YoutubeDL", return_value=ydl):
        view = bilibili.fetch_view("BV1xx411c7X5")

    url = ydl.__enter__.return_value.extract_info.call_args[0][0]
    assert url == "https://www.bilibili.com/video/BV1xx411c7X5"
    assert view["duration"] == 60


def test_resolve_title_never_sends_non_youtube_ids_to_youtube(tmp_path, monkeypatch):
    from src.pipeline import orchestrator
    from src.sources import bilibili

    monkeypatch.setattr(orchestrator.db, "get_episode", lambda vid: None)
    monkeypatch.setattr(bilibili, "fetch_view", lambda bvid: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("不应调用 YouTube yt-dlp")

    monkeypatch.setattr("src.youtube.extractor._try_ydl", forbidden)

    assert orchestrator._resolve_title("BV1xx411c7X5", tmp_path) == "BV1xx411c7X5"
    assert orchestrator._resolve_title("dy_123", tmp_path) == "dy_123"
    assert orchestrator._resolve_title("xhs_abc", tmp_path) == "xhs_abc"


def test_bilibili_requests_bypass_env_proxy(monkeypatch):
    from src.sources import bilibili

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    with bilibili._client() as client:
        assert client._trust_env is False
    assert bilibili._YDL_DIRECT == {"proxy": ""}
