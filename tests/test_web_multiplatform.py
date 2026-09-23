"""测试多平台 URL 提交与 Web API 交互。"""

import pytest
from starlette.testclient import TestClient
from unittest.mock import MagicMock, patch

from src.web.server import app
from src.storage import db


@pytest.fixture
def client(tmp_path, monkeypatch):
    test_db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "_get_db_path", lambda: test_db_path)
    # mock 后台实际执行，测试聚焦于路由、输入解析与状态决策
    monkeypatch.setattr("src.web.server._process_async", MagicMock())
    db.init_db()
    with TestClient(app) as c:
        yield c


def test_submit_bilibili_url(client):
    """验证提交 B站 链接能成功被识别并进入决策链路。"""
    payload = {
        "url": "https://www.bilibili.com/video/BV1xx411c7X5",
        "mode": "podcast",
        "api_key": "test_key",
        "model": "deepseek-chat",
    }
    resp = client.post("/api/process", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["video_id"] == "BV1xx411c7X5"
    assert data["status"] in ("new", "queued")


def test_submit_bilibili_share_text(client):
    """验证 App 复制的混合文本也能被识别。"""
    payload = {
        "url": "【干货】深度学习入门指南！ https://www.bilibili.com/video/BV1yy411c7Y6 复制打开B站",
        "mode": "condensed",
        "api_key": "test_key",
        "model": "deepseek-chat",
    }
    resp = client.post("/api/process", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["video_id"] == "BV1yy411c7Y6"
    assert data["status"] in ("new", "queued")


def test_submit_douyin_url(client):
    payload = {
        "url": "https://www.douyin.com/video/7234567890123456789",
        "mode": "podcast",
        "api_key": "test_key",
        "model": "deepseek-chat",
    }
    resp = client.post("/api/process", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["video_id"] == "dy_7234567890123456789"
    assert data["status"] in ("new", "queued")


def test_submit_invalid_url_returns_400(client):
    payload = {
        "url": "https://example.com/not-a-supported-video",
        "mode": "podcast",
        "api_key": "test_key",
        "model": "deepseek-chat",
    }
    resp = client.post("/api/process", json=payload)
    assert resp.status_code == 400
    assert "无法识别视频链接" in resp.json()["error"]
