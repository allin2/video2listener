"""Web 进度与刷新恢复回归测试。"""

from pathlib import Path
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from src.web import server
from src.storage import db


def test_tts_message_emits_stage_progress_with_percentage():
    entry = {
        "current_stage_id": 5,
        "completed_stages": {1, 2, 3, 4},
        "stage_started_at": {5: 0},
        "stage_meta": {},
    }

    events = server._detect_stage("TTS 合成中... (18/49)", entry)

    assert events == [{
        "type": "stage_progress",
        "stage_id": 5,
        "message": "TTS 合成中... (18/49)",
        "pct": 36,
    }]
    assert entry["stage_meta"][5] == "TTS 合成中... (18/49)"


def _js(name: str) -> str:
    """读取拆分后的前端 JS 模块源码，用于前端行为断言。"""
    return (Path(server.STATIC_DIR) / "js" / name).read_text(encoding="utf-8")


def test_progress_page_has_return_button_and_deferred_session_restore():
    html = (Path(server.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    main_js = _js("main.js")
    timeline_js = _js("timeline.js")

    # 进度视图保留取消/返回入口（新 UI 以 cancelBtn 承担返回职责）
    assert 'id="cancelBtn"' in html
    # 会话恢复推迟到 main.js 模块加载后执行
    assert "if (savedVid) restoreTaskSession(savedVid, savedMode);" in main_js
    # ESM 依赖先加载：main.js 在顶部导入 timeline.js
    assert "from './timeline.js'" in main_js
    assert "class PipelineTimeline" in timeline_js


def test_transient_poll_failure_does_not_show_terminal_error():
    stream_js = _js("stream.js")
    main_js = _js("main.js")
    all_js = stream_js + main_js

    assert "onInterrupted" in stream_js
    assert "连接暂时中断" in all_js
    assert "showError('连接中断')" not in all_js


def test_public_task_state_excludes_cancel_event():
    video_id = f"test-{uuid.uuid4()}"
    try:
        server._set_task_state(video_id, "faithful", status="processing")

        state = server._get_task_state(video_id, "faithful")

        assert state is not None
        assert "cancel_event" not in state
        assert "stage_durations" not in state
    finally:
        with server._task_lock:
            server._task_states.pop(server._task_key(video_id, "faithful"), None)


def test_poll_ui_keeps_progress_meta_and_clears_finished_actions():
    timeline_js = _js("timeline.js")
    main_js = _js("main.js")

    # 轮询/快照回放时保留阶段 meta 与耗时，完成时清空操作区
    assert "st.msgEl.textContent = s.meta" in timeline_js
    assert "parts.push(formatElapsed(durationS))" in timeline_js
    assert "this.setStageDone(s.stage_id, s.duration_s, s.meta)" in timeline_js
    assert "timeline.clearAllActions()" in main_js


def test_status_does_not_leak_other_variant_error_into_done_variant():
    """已完成变体的状态里不能出现别的变体的错误信息。

    回归：episode 行的 error_message 是共享的，某个模式失败后会把错误写到
    episode 行。此处再回落到 episode 行，会让「已完成的播客版」查询结果变成
    `status: done` 加一条无关的 error_message。
    """
    video_id = f"test-leak-{uuid.uuid4()}"
    try:
        db.create_episode(video_id, url="https://youtu.be/test", title="t")
        db.upsert_variant(video_id, "podcast", status="done")
        db.update_variant_status(video_id, "podcast", "done", audio_zh_path="/tmp/a.mp3")

        # 另一个模式失败，错误写到了共享的 episode 行
        db.update_status(video_id, "failed", error_message="No module named 'faster_whisper'")

        with TestClient(server.app) as client:
            body = client.get(f"/api/status/{video_id}/podcast").json()

        assert body["status"] == "done"
        assert body["error_message"] is None
    finally:
        db.delete_episode(video_id)


def test_status_still_reports_error_when_variant_generation_failed():
    """变体自身失败时，错误信息必须照常透出，不能被上面的修复吞掉。"""
    video_id = f"test-err-{uuid.uuid4()}"
    try:
        db.create_episode(video_id, url="https://youtu.be/test", title="t")
        db.upsert_variant(video_id, "faithful", status="failed")
        db.update_variant_status(video_id, "faithful", "failed", error_message="未配置翻译 API Key")

        with TestClient(server.app) as client:
            body = client.get(f"/api/status/{video_id}/faithful").json()

        assert body["status"] == "failed"
        assert body["error_message"] == "未配置翻译 API Key"
    finally:
        db.delete_episode(video_id)


def test_completed_stage_uses_frozen_duration():
    entry = {
        "status": "processing",
        "current_stage_id": 5,
        "completed_stages": {4},
        "stage_started_at": {4: 1},
        "stage_durations": {4: 42},
        "stage_meta": {},
    }

    stage = server._derive_stages(entry)[3]

    assert stage["status"] == "done"
    assert stage["duration_s"] == 42


@pytest.fixture()
def isolated_web_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_get_db_path", lambda: tmp_path / "web.db")
    db.init_db()
    with server._task_lock:
        server._task_states.clear()
    yield tmp_path
    with server._task_lock:
        server._task_states.clear()


def _seed_shared_video(tmp_path):
    data_dir = tmp_path / "video"
    data_dir.mkdir()
    transcript = data_dir / "transcript_clean.txt"
    transcript.write_text("shared transcript", encoding="utf-8")
    db.create_episode(
        "video123456", "https://youtu.be/video123456", title="Video",
        mode="faithful", data_dir=str(data_dir),
    )
    db.update_status("video123456", "text_ready", transcript_clean_path=str(transcript))
    return data_dir


def test_process_preflight_distinguishes_new_mode_from_existing_mode(isolated_web_db):
    data_dir = _seed_shared_video(isolated_web_db)
    audio = data_dir / "faithful.mp3"
    audio.write_bytes(b"faithful")
    db.upsert_variant("video123456", "faithful", status="done", audio_zh_path=str(audio))
    client = TestClient(server.app)

    reuse = client.post("/api/process", json={
        "url": "https://youtu.be/video123456", "mode": "podcast",
    })
    existing = client.post("/api/process", json={
        "url": "https://youtu.be/video123456", "mode": "faithful",
    })

    assert reuse.status_code == 200
    assert reuse.json()["status"] == "reuse_available"
    assert reuse.json()["reused_stages"] == ["download", "transcribe", "clean"]
    assert "复用下载、转写和清洗缓存" in reuse.json()["message"]
    assert "重新生成" not in reuse.json()["message"]
    assert existing.json()["status"] == "variant_exists"
    assert existing.json()["download_url"].endswith("/video123456/faithful")


def test_task_states_are_isolated_by_mode(isolated_web_db):
    server._set_task_state("video123456", "faithful", status="done", output_path="faithful.mp3")
    server._set_task_state("video123456", "podcast", status="failed", error_message="failed")

    assert server._get_task_state("video123456", "faithful")["output_path"] == "faithful.mp3"
    assert server._get_task_state("video123456", "podcast")["status"] == "failed"


def test_same_video_rejects_concurrent_different_mode(isolated_web_db):
    server._set_task_state(
        "video123456", "faithful", status="processing", started_at=1234.5
    )
    client = TestClient(server.app)

    response = client.post("/api/process", json={
        "url": "https://youtu.be/video123456", "mode": "podcast",
    })

    assert response.status_code == 409
    assert response.json()["mode"] == "faithful"
    assert response.json()["mode_label"] == "忠实翻译版"
    assert response.json()["started_at"] == 1234.5
    assert "正在处理中" in response.json()["message"]


def test_missing_shared_transcript_is_not_reusable(isolated_web_db):
    db.create_episode(
        "video123456", "https://youtu.be/video123456", mode="faithful",
        data_dir=str(isolated_web_db / "missing"),
    )
    db.update_status(
        "video123456", "text_ready",
        transcript_clean_path=str(isolated_web_db / "missing.txt"),
    )

    assert server._shared_source_ready(db.get_episode("video123456")) is False


def test_reused_progress_is_exposed_as_reused_stage():
    entry = {
        "current_stage_id": 0,
        "completed_stages": set(),
        "reused_stages": set(),
        "stage_started_at": {},
        "stage_durations": {},
        "stage_meta": {},
        "status": "processing",
    }

    events = server._detect_stage("♻️ 复用缓存：下载", entry)
    stages = server._derive_stages(entry)

    assert events[0]["status"] == "reused"
    assert stages[0]["status"] == "reused"
    assert stages[0]["meta"] == "复用缓存"


def test_translation_subphase_is_preserved_in_snapshot():
    video_id = f"test-{uuid.uuid4()}"
    try:
        server._set_task_state(video_id, "faithful", status="processing")
        server._append_progress(video_id, "faithful", "开始翻译（模式: faithful）...")
        server._append_progress(video_id, "faithful", {
            "type": "translation_phase", "phase": "semantic_audit",
            "label": "语义审计", "current": 3, "total": 8,
        })
        with server._task_lock:
            entry = server._task_states[server._task_key(video_id, "faithful")]
            snapshot = server._task_state_snapshot(entry)

        assert snapshot["translation_phase"]["phase"] == "semantic_audit"
        assert snapshot["stages"][3]["meta"] == "语义审计 (3/8)"
    finally:
        with server._task_lock:
            server._task_states.pop(server._task_key(video_id, "faithful"), None)


def test_history_exposes_variant_audit_status(isolated_web_db):
    _seed_shared_video(isolated_web_db)
    db.upsert_variant(
        "video123456", "faithful", status="done",
        audit_status="degraded", audit_message="质量审计降级",
    )
    item = TestClient(server.app).get("/api/tasks").json()[0]["variants"][0]

    assert item["audit_status"] == "degraded"
    assert item["audit_message"] == "质量审计降级"


def test_sse_snapshot_recovers_stages_emitted_before_connection():
    entry = {
        "video_id": "video123456",
        "mode": "faithful",
        "status": "processing",
        "current_stage_id": 4,
        "completed_stages": {1, 2, 3},
        "reused_stages": {1, 2, 3},
        "stage_started_at": {4: time.time()},
        "stage_durations": {},
        "stage_meta": {1: "复用缓存", 2: "复用缓存", 3: "复用缓存"},
        "sse_queue": None,
        "sse_loop": None,
        "cancel_event": None,
    }

    snapshot = server._task_state_snapshot(entry)

    assert [stage["status"] for stage in snapshot["stages"][:4]] == [
        "reused", "reused", "reused", "active",
    ]
    assert "completed_stages" not in snapshot


def test_frontend_applies_initial_sse_state_snapshot():
    stream_js = _js("stream.js")

    assert "addEventListener('state_snapshot'" in stream_js
    assert "ctx.timeline.updateFromPoll(state.stages)" in stream_js


def test_frontend_uses_mode_aware_routes_and_distinct_reuse_copy():
    main_js = _js("main.js")
    timeline_js = _js("timeline.js")
    api_js = _js("api.js")

    # 智能拦截三分支 + 模式感知路由 + 区分复用/重生成文案
    assert "reuse_available" in main_js
    assert "variant_exists" in main_js
    assert "⚡ 立即快速生成" in main_js
    assert "🔄 重新生成" in main_js
    assert "前三步 100% 缓存复用" in main_js
    assert "setItem(SESSION_KEYS.mode, mode)" in main_js
    assert "SESSION_KEYS" in main_js and "'v2l_mode'" in main_js
    assert "/api/status/${vid}/${mode}/stream" in api_js
    assert "/api/download/${vid}/${mode}" in api_js
    assert "case 'reused':" in timeline_js


def test_frontend_reconnects_to_existing_task_instead_of_showing_submit_failed():
    main_js = _js("main.js")

    assert "status === 409" in main_js
    assert "restoreTaskSession(data.video_id, data.mode)" in main_js
    assert "data.error || data.message" in main_js
    assert "'提交失败'" in main_js


def test_confirmed_new_mode_uses_mode_keyed_task_state(isolated_web_db, monkeypatch):
    data_dir = _seed_shared_video(isolated_web_db)
    output = data_dir / "podcast.mp3"
    output.write_bytes(b"podcast")

    async def fake_pipeline(video_id, mode, **_kwargs):
        return {
            "status": "done", "video_id": video_id, "mode": mode,
            "output_path": str(output), "message": "done",
        }

    monkeypatch.setattr(server, "_process_async", fake_pipeline)
    client = TestClient(server.app)
    response = client.post("/api/process", json={
        "url": "https://youtu.be/video123456", "mode": "podcast",
        "action": "create_variant",
    })

    assert response.status_code == 200
    assert response.json()["mode"] == "podcast"
    for _ in range(20):
        state = server._get_task_state("video123456", "podcast")
        if state and state["status"] == "done":
            break
        time.sleep(0.01)
    assert state["status"] == "done"
    assert state["output_path"] == str(output)


def test_history_groups_variants_and_single_mode_delete_is_isolated(isolated_web_db):
    data_dir = _seed_shared_video(isolated_web_db)
    faithful_dir = data_dir / "variants" / "faithful"
    podcast_dir = data_dir / "variants" / "podcast"
    faithful_dir.mkdir(parents=True)
    podcast_dir.mkdir(parents=True)
    faithful_audio = faithful_dir / "video_faithful.mp3"
    podcast_audio = podcast_dir / "video_podcast.mp3"
    faithful_audio.write_bytes(b"faithful")
    podcast_audio.write_bytes(b"podcast")
    db.upsert_variant(
        "video123456", "faithful", status="done",
        variant_dir=str(faithful_dir), audio_zh_path=str(faithful_audio),
    )
    db.upsert_variant(
        "video123456", "podcast", status="done",
        variant_dir=str(podcast_dir), audio_zh_path=str(podcast_audio),
    )
    client = TestClient(server.app)

    task = client.get("/api/tasks").json()[0]
    assert {item["mode"] for item in task["variants"]} == {"faithful", "podcast"}
    assert all(item["download_url"].endswith(item["mode"]) for item in task["variants"])

    deleted = client.delete("/api/tasks/video123456/podcast")
    assert deleted.status_code == 200
    assert not podcast_dir.exists()
    assert faithful_audio.read_bytes() == b"faithful"
    assert db.get_variant("video123456", "faithful")["status"] == "done"


def test_status_reports_shared_failure_after_restart(isolated_web_db):
    _seed_shared_video(isolated_web_db)
    db.update_status("video123456", "failed", error_message="download failed")
    db.upsert_variant("video123456", "podcast", status="new")
    client = TestClient(server.app)

    response = client.get("/api/status/video123456/podcast")

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error_message"] == "download failed"


def test_legacy_status_and_download_routes_resolve_existing_variant(isolated_web_db):
    data_dir = _seed_shared_video(isolated_web_db)
    audio = data_dir / "faithful.mp3"
    audio.write_bytes(b"faithful")
    db.upsert_variant(
        "video123456", "faithful", status="done", audio_zh_path=str(audio)
    )
    client = TestClient(server.app)

    status = client.get("/api/status/video123456")
    download = client.get("/api/download/video123456")

    assert download.status_code == 200
    assert download.content == b"faithful"


def test_tts_voices_includes_fish_preset():
    with TestClient(server.app) as client:
        resp = client.post("/api/tts-voices", json={"provider": "fish"})
        assert resp.status_code == 200
        voices = resp.json()["voices"]
        assert any(v["id"] == "7f92f8afb8ec43bf81429cc1c9199cb1" and "御姐" in v["name"] for v in voices)
        assert any(v["id"] == "5c353fdb312f4888836a9a5680099ef0" and "女大" in v["name"] for v in voices)


def test_tts_preview_with_fish_provider(monkeypatch):
    called = []

    def fake_synthesize(text, out_dir, **kwargs):
        called.append((text, kwargs))
        f = Path(out_dir) / "segment_0000.mp3"
        f.write_bytes(b"fake mp3 audio")
        return [f]

    import src.tts.synthesizer as synth
    monkeypatch.setattr(synth, "synthesize", fake_synthesize)

    with TestClient(server.app) as client:
        resp = client.post("/api/tts-preview", json={
            "provider": "fish",
            "api_key": "test-key",
            "voice": "7f92f8afb8ec43bf81429cc1c9199cb1",
        })
        assert resp.status_code == 200
        assert len(called) == 1
        assert called[0][1]["tts_config"]["provider"] == "fish"
        assert called[0][1]["tts_config"]["voice"] == "7f92f8afb8ec43bf81429cc1c9199cb1"
        assert called[0][1]["tts_config"]["speed"] == 1.0

        # 带 speed 参数的试听测试
        resp2 = client.post("/api/tts-preview", json={
            "provider": "fish",
            "api_key": "test-key",
            "speed": 1.25,
        })
        assert resp2.status_code == 200
        assert len(called) == 2
        assert called[1][1]["tts_config"]["speed"] == 1.25


def test_api_process_passes_tts_speed(monkeypatch, isolated_web_db):
    """验证 /api/process 接口能正确提取 tts_speed 参数并注入 tts_config。"""
    captured = {}

    async def fake_pipeline(video_id, mode, **kwargs):
        captured["video_id"] = video_id
        captured["tts_config"] = kwargs.get("tts_config")
        return {
            "status": "done", "video_id": video_id, "mode": mode,
            "output_path": "/tmp/dummy.mp3", "message": "done",
        }

    monkeypatch.setattr(server, "_process_async", fake_pipeline)

    with TestClient(server.app) as client:
        resp = client.post("/api/process", json={
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "mode": "podcast",
            "action": "create_variant",
            "api_key": "test-key",
            "tts_api_key": "test-fish-key",
            "tts_speed": 1.5,
        })
        assert resp.status_code == 200
        for _ in range(50):
            if "tts_config" in captured:
                break
            time.sleep(0.02)
        assert captured.get("tts_config") is not None
        assert captured["tts_config"]["speed"] == 1.5




def test_static_assets_are_revalidated_not_heuristically_cached():
    from fastapi.testclient import TestClient

    from src.web.server import app

    client = TestClient(app)
    for path in ("/", "/static/js/history.js"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"


def test_tasks_expose_platform_thumbnail_and_hide_stale_errors(isolated_web_db, tmp_path):
    from fastapi.testclient import TestClient

    from src.storage import db
    from src.web.server import app

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    db.create_episode(video_id="dQw4w9WgXcQ", url="u", mode="podcast", data_dir=str(tmp_path))
    db.upsert_variant("dQw4w9WgXcQ", "podcast", status="done", audio_zh_path=str(audio),
                      variant_dir=str(tmp_path), error_message="旧的失败信息")
    db.create_episode(video_id="BV1xx411c7X5", url="u", mode="podcast", data_dir=str(tmp_path))

    items = {e["video_id"]: e for e in TestClient(app).get("/api/tasks").json()}

    yt = items["dQw4w9WgXcQ"]
    assert yt["platform"] == "youtube"
    assert yt["thumbnail_url"] == "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"
    assert yt["variants"][0]["error_message"] == ""
    assert "output_seconds" in yt["variants"][0]
    assert items["BV1xx411c7X5"]["platform"] == "bilibili"
    assert items["BV1xx411c7X5"]["thumbnail_url"] == ""
