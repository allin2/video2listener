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


def test_progress_page_has_return_button_and_deferred_session_restore():
    html = (Path(server.STATIC_DIR) / "index.html").read_text(encoding="utf-8")

    assert 'id="progressBackBtn"' in html
    assert "if (savedVideoId) restoreTaskSession(savedVideoId, savedMode);" in html
    assert html.index("class PipelineTimeline") < html.index(
        "if (savedVideoId) restoreTaskSession(savedVideoId, savedMode);"
    )


def test_transient_poll_failure_does_not_show_terminal_error():
    html = (Path(server.STATIC_DIR) / "index.html").read_text(encoding="utf-8")

    assert "markConnectionInterrupted" in html
    assert "连接暂时中断，正在重试" in html
    assert "showError('连接中断')" not in html


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
    html = (Path(server.STATIC_DIR) / "index.html").read_text(encoding="utf-8")

    assert "parts.push(s.meta)" in html
    assert "parts.push(formatElapsed(s.duration_s))" in html
    assert "this.setStageDone(s.stage_id, s.duration_s, s.meta)" in html
    assert "timeline.clearAllActions()" in html


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
    html = (Path(server.STATIC_DIR) / "index.html").read_text(encoding="utf-8")

    assert "addEventListener('state_snapshot'" in html
    assert "timeline.updateFromPoll(state.stages)" in html


def test_frontend_uses_mode_aware_routes_and_distinct_reuse_copy():
    html = (Path(server.STATIC_DIR) / "index.html").read_text(encoding="utf-8")

    assert "reuse_available" in html
    assert "variant_exists" in html
    assert "生成' + (labels[currentMode]" in html
    assert "重新生成此模式" in html
    assert "sessionStorage.setItem('v2l_mode',currentMode)" in html
    assert "'/api/status/' + vid + '/' + mode + '/stream'" in html
    assert "`/api/download/${videoId}/${currentMode}`" in html
    assert "case 'reused':" in html


def test_frontend_reconnects_to_existing_task_instead_of_showing_submit_failed():
    html = (Path(server.STATIC_DIR) / "index.html").read_text(encoding="utf-8")

    assert "resp.status === 409" in html
    assert "restoreTaskSession(data.video_id, data.mode)" in html
    assert "data.error||data.message||'提交失败'" in html


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

    assert status.status_code == 200
    assert status.json()["mode"] == "faithful"
    assert status.json()["status"] == "done"
    assert download.status_code == 200
    assert download.content == b"faithful"
