"""U7 测试。管道编排模块。"""
import pytest
from pathlib import Path

from src.storage import db
from src.pipeline import orchestrator
from src.pipeline.state import (
    TaskStatus,
    check_stage_file,
    check_variant_stage_file,
    next_status,
)
from src.pipeline.orchestrator import (
    _commit_variant_workspace,
    _cleanup_variant_outputs,
    _ensure_distinct_mode_script,
    _existing_stage_paths,
    _variant_dir,
)


def test_status_order():
    assert next_status(TaskStatus.NEW) == TaskStatus.METADATA_FETCHED
    assert next_status(TaskStatus.METADATA_FETCHED) == TaskStatus.TEXT_READY
    assert next_status(TaskStatus.TEXT_READY) == TaskStatus.TRANSLATED
    assert next_status(TaskStatus.TRANSLATED) == TaskStatus.TTS_DONE
    assert next_status(TaskStatus.TTS_DONE) == TaskStatus.DONE
    assert next_status(TaskStatus.DONE) is None


def test_status_values_unique():
    values = [s.value for s in TaskStatus]
    assert len(values) == len(set(values))


def test_failed_not_in_order():
    """FAILED 不在正常流转顺序中。"""
    with pytest.raises(ValueError):
        from src.pipeline.state import STATUS_ORDER
        STATUS_ORDER.index(TaskStatus.FAILED)
    # FAILED 不在 STATUS_ORDER 中是一个设计决定


def test_all_statuses_have_string_values():
    for status in TaskStatus:
        assert isinstance(status.value, str)
        assert len(status.value) > 0


def test_partial_tts_segments_do_not_mark_stage_complete(tmp_path):
    segments = tmp_path / "tts_segments"
    segments.mkdir()
    (segments / "segment_0000.wav").write_bytes(b"partial")

    assert check_stage_file(tmp_path, TaskStatus.TTS_DONE) is False

    (tmp_path / "A_Video_Title.mp3").write_bytes(b"complete")
    assert check_stage_file(tmp_path, TaskStatus.TTS_DONE) is True


def test_existing_stage_paths_recovers_transcript_for_resume(tmp_path):
    transcript = tmp_path / "transcript_clean.txt"
    transcript.write_text("complete transcript", encoding="utf-8")
    (tmp_path / "empty.json").write_bytes(b"")

    recovered = _existing_stage_paths(tmp_path)

    assert recovered["transcript_clean_path"] == str(transcript)
    assert "script_zh_path" not in recovered


def test_mp3_older_than_script_is_not_tts_complete(tmp_path):
    import os

    segments = tmp_path / "tts_segments"
    segments.mkdir()
    (segments / "segment_0000.wav").write_bytes(b"old audio")
    output = tmp_path / "output.mp3"
    output.write_bytes(b"old output")
    script = tmp_path / "script_zh.txt"
    script.write_text("new translation", encoding="utf-8")
    os.utime(output, (1, 1))
    os.utime(script, (2, 2))

    assert check_stage_file(tmp_path, TaskStatus.TTS_DONE) is False


def test_variant_directories_are_mode_specific(tmp_path):
    faithful = _variant_dir(tmp_path, "faithful")
    podcast = _variant_dir(tmp_path, "podcast")

    assert faithful == tmp_path / "variants" / "faithful"
    assert podcast == tmp_path / "variants" / "podcast"
    assert faithful != podcast


def test_variant_stage_check_does_not_see_other_mode_mp3(tmp_path):
    faithful = _variant_dir(tmp_path, "faithful")
    faithful.mkdir(parents=True)
    (faithful / "script_zh.txt").write_text("忠实译文", encoding="utf-8")
    segments = faithful / "tts_segments"
    segments.mkdir()
    (segments / "segment_0000.wav").write_bytes(b"audio")
    (faithful / "video_faithful.mp3").write_bytes(b"output")

    condensed = _variant_dir(tmp_path, "condensed")
    condensed.mkdir(parents=True)

    assert check_variant_stage_file(faithful, TaskStatus.TTS_DONE) is True
    assert check_variant_stage_file(condensed, TaskStatus.TTS_DONE) is False


def test_cleanup_variant_outputs_keeps_other_modes(tmp_path):
    faithful = _variant_dir(tmp_path, "faithful")
    podcast = _variant_dir(tmp_path, "podcast")
    for directory in (faithful, podcast):
        directory.mkdir(parents=True)
        (directory / "script_zh.txt").write_text(directory.name, encoding="utf-8")
        (directory / f"video_{directory.name}.mp3").write_bytes(b"output")

    _cleanup_variant_outputs(podcast)

    assert (faithful / "script_zh.txt").exists()
    assert (faithful / "video_faithful.mp3").exists()
    assert not (podcast / "script_zh.txt").exists()
    assert not (podcast / "video_podcast.mp3").exists()


def test_committing_variant_workspace_atomically_replaces_old_directory(tmp_path):
    target = tmp_path / "variants" / "faithful"
    target.mkdir(parents=True)
    (target / "audio.mp3").write_bytes(b"old")
    workspace = tmp_path / "variants" / ".faithful.regenerating-test"
    workspace.mkdir()
    (workspace / "audio.mp3").write_bytes(b"new")

    _commit_variant_workspace(workspace, target)

    assert (target / "audio.mp3").read_bytes() == b"new"
    assert not workspace.exists()
    assert not list(target.parent.glob(".faithful.backup-*"))


def test_identical_script_cannot_be_registered_as_a_different_mode(tmp_path, monkeypatch):
    faithful_script = tmp_path / "faithful.txt"
    faithful_script.write_text("同一份 内容", encoding="utf-8")
    monkeypatch.setattr(
        orchestrator.db,
        "list_variants",
        lambda _video_id: [{
            "mode": "faithful",
            "script_zh_path": str(faithful_script),
        }],
    )

    with pytest.raises(RuntimeError, match="内容与已有 faithful 模式完全相同"):
        _ensure_distinct_mode_script("video123456", "podcast", "同一份内容")


def test_cleanup_resume_keeps_translation_audit(tmp_path):
    variant = _variant_dir(tmp_path, "faithful")
    variant.mkdir(parents=True)
    (variant / "script_zh.txt").write_text("译文", encoding="utf-8")
    (variant / "translation_audit.json").write_text("{}", encoding="utf-8")
    (variant / "stale.mp3").write_bytes(b"stale")

    _cleanup_variant_outputs(variant, resume_from="translated")

    assert (variant / "script_zh.txt").exists()
    assert (variant / "translation_audit.json").exists()
    assert not (variant / "stale.mp3").exists()


def test_failed_force_regeneration_preserves_previous_variant(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_get_db_path", lambda: tmp_path / "test.db")
    monkeypatch.setattr(
        orchestrator,
        "get_config",
        lambda: {"_project_root": tmp_path, "app": {"data_dir": "data"}},
    )
    db.init_db()
    data_dir = tmp_path / "data" / "Existing_Title"
    data_dir.mkdir(parents=True)
    metadata = data_dir / "metadata.json"
    metadata.write_text('{"title":"Existing Title"}', encoding="utf-8")
    transcript = data_dir / "transcript_clean.txt"
    transcript.write_text("The old version has 11 sections.", encoding="utf-8")
    db.create_episode(
        "video123456", "https://youtu.be/video123456", title="Existing Title",
        mode="faithful", data_dir=str(data_dir),
    )
    db.update_status(
        "video123456", "text_ready", metadata_path=str(metadata),
        transcript_clean_path=str(transcript),
    )

    variant_dir = orchestrator._variant_dir(data_dir, "faithful")
    variant_dir.mkdir(parents=True)
    old_script = variant_dir / "script_zh.txt"
    old_script.write_text("旧版本有十一节。", encoding="utf-8")
    old_audio = variant_dir / "Existing_Title_faithful.mp3"
    old_audio.write_bytes(b"old-audio")
    db.upsert_variant(
        "video123456", "faithful", status="done", variant_dir=str(variant_dir),
        script_zh_path=str(old_script), audio_zh_path=str(old_audio),
    )

    async def fail_translation(*_args, **_kwargs):
        raise RuntimeError("simulated translation failure")

    monkeypatch.setattr(orchestrator, "llm_translate_async", fail_translation)
    result = orchestrator.process("video123456", "faithful", force=True)

    saved = db.get_variant("video123456", "faithful")
    assert result["status"] == "failed"
    assert saved["status"] == "done"
    assert saved["script_zh_path"] == str(old_script)
    assert saved["audio_zh_path"] == str(old_audio)
    assert old_script.read_text(encoding="utf-8") == "旧版本有十一节。"
    assert old_audio.read_bytes() == b"old-audio"
    assert not list(variant_dir.parent.glob(".faithful.regenerating-*"))


def test_new_mode_reuses_shared_source_and_preserves_existing_variant(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_get_db_path", lambda: tmp_path / "test.db")
    monkeypatch.setattr(
        orchestrator,
        "get_config",
        lambda: {"_project_root": tmp_path, "app": {"data_dir": "data"}},
    )
    db.init_db()
    data_dir = tmp_path / "data" / "Existing_Title"
    data_dir.mkdir(parents=True)
    (data_dir / "metadata.json").write_text('{"title":"Existing Title"}', encoding="utf-8")
    transcript = data_dir / "transcript_clean.txt"
    transcript.write_text("complete source transcript", encoding="utf-8")
    db.create_episode(
        "video123456", "https://youtu.be/video123456", title="Existing Title",
        mode="faithful", data_dir=str(data_dir),
    )
    db.update_status(
        "video123456", "text_ready", metadata_path=str(data_dir / "metadata.json"),
        transcript_clean_path=str(transcript),
    )
    faithful_dir = orchestrator._variant_dir(data_dir, "faithful")
    faithful_dir.mkdir(parents=True)
    faithful_audio = faithful_dir / "Existing_Title_faithful.mp3"
    faithful_audio.write_bytes(b"faithful")
    db.upsert_variant(
        "video123456", "faithful", status="done", variant_dir=str(faithful_dir),
        audio_zh_path=str(faithful_audio),
    )

    monkeypatch.setattr(
        orchestrator, "youtube_extract",
        lambda *_args, **_kwargs: pytest.fail("download must be reused"),
    )
    monkeypatch.setattr(
        orchestrator, "whisper_transcribe",
        lambda *_args, **_kwargs: pytest.fail("transcription must be reused"),
    )
    monkeypatch.setattr(orchestrator, "clean_text", lambda *_: pytest.fail("cleaning must be reused"))
    translated_modes = []

    def fake_translate(_text, selected_mode, *_args, **_kwargs):
        translated_modes.append(selected_mode)
        return "播客模式译文"

    async def fake_translate_async(_text, selected_mode, *_args, **_kwargs):
        translated_modes.append(selected_mode)
        return "播客模式译文"

    monkeypatch.setattr(orchestrator, "llm_translate", fake_translate)
    monkeypatch.setattr(orchestrator, "llm_translate_async", fake_translate_async)
    monkeypatch.setattr(orchestrator, "llm_summarize", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(orchestrator, "_extract_terms", lambda *_: None)
    monkeypatch.setattr(orchestrator, "_quality_check", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "clean_for_tts", lambda text: text)

    def fake_synthesize(_text, output_dir, **_kwargs):
        segment = output_dir / "segment_0000.wav"
        segment.write_bytes(b"segment")
        return [segment]

    async def fake_synthesize_async(_text, output_dir, **_kwargs):
        return fake_synthesize(_text, output_dir, **_kwargs)

    def fake_merge(_segments, output_base, **_kwargs):
        output = Path(f"{output_base}.mp3")
        output.write_bytes(b"podcast")
        return [output]

    async def fake_merge_async(_segments, output_base, **_kwargs):
        return fake_merge(_segments, output_base, **_kwargs)

    monkeypatch.setattr(orchestrator, "synthesize", fake_synthesize)
    monkeypatch.setattr(orchestrator, "synthesize_async", fake_synthesize_async)
    monkeypatch.setattr(orchestrator, "merge", fake_merge)
    monkeypatch.setattr(orchestrator, "merge_async", fake_merge_async)

    messages = []
    result = orchestrator.process("video123456", "podcast", on_progress=messages.append)

    assert result["status"] == "done"
    assert result["mode"] == "podcast"
    assert Path(result["output_path"]).parent == orchestrator._variant_dir(data_dir, "podcast")
    assert faithful_audio.read_bytes() == b"faithful"
    assert db.get_variant("video123456", "faithful")["status"] == "done"
    assert db.get_variant("video123456", "podcast")["status"] == "done"
    assert translated_modes == ["podcast"]
    assert messages[:3] == [
        "♻️ 复用缓存：下载", "♻️ 复用缓存：转写", "♻️ 复用缓存：清洗",
    ]


def test_summarize_does_not_block_pipeline(tmp_path, monkeypatch):
    """U3: 验证 summarize 已移入后台线程，不阻塞管道主路径。"""
    import threading
    import time

    monkeypatch.setattr(db, "_get_db_path", lambda: tmp_path / "test.db")
    monkeypatch.setattr(
        orchestrator,
        "get_config",
        lambda: {"_project_root": tmp_path, "app": {"data_dir": "data"}},
    )
    db.init_db()
    data_dir = tmp_path / "data" / "Test_Title"
    data_dir.mkdir(parents=True)
    (data_dir / "metadata.json").write_text('{"title":"Test Title"}', encoding="utf-8")
    transcript = data_dir / "transcript_clean.txt"
    transcript.write_text("source transcript", encoding="utf-8")
    db.create_episode(
        "test_vid", "https://youtu.be/test_vid", title="Test Title",
        mode="podcast", data_dir=str(data_dir),
    )
    db.update_status(
        "test_vid", "text_ready", metadata_path=str(data_dir / "metadata.json"),
        transcript_clean_path=str(transcript),
    )

    monkeypatch.setattr(orchestrator, "youtube_extract", lambda *_args, **_kwargs: pytest.fail("should be reused"))
    monkeypatch.setattr(orchestrator, "whisper_transcribe", lambda *_args, **_kwargs: pytest.fail("should be reused"))
    monkeypatch.setattr(orchestrator, "clean_text", lambda *_: pytest.fail("should be reused"))
    monkeypatch.setattr(orchestrator, "llm_translate", lambda *_args, **_kwargs: "播客译文")

    async def _fake_translate_async(*_args, **_kwargs):
        return "播客译文"

    monkeypatch.setattr(orchestrator, "llm_translate_async", _fake_translate_async)

    # 模拟被阻塞的摘要调用；主管道完成后再释放后台线程。
    summary_release = threading.Event()
    summary_finished = threading.Event()

    def slow_summarize(*_args, **_kwargs):
        summary_release.wait(timeout=3)
        summary_finished.set()
        return {"title_zh": "标题", "summary": "摘要", "key_points": []}

    monkeypatch.setattr(orchestrator, "llm_summarize", slow_summarize)
    monkeypatch.setattr(orchestrator, "_extract_terms", lambda *_: None)
    monkeypatch.setattr(orchestrator, "_quality_check", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "clean_for_tts", lambda text: text)

    def fake_synthesize(_text, output_dir, **_kwargs):
        (output_dir / "segment_0000.wav").write_bytes(b"segment")
        return [output_dir / "segment_0000.wav"]

    async def fake_synthesize_async(_text, output_dir, **_kwargs):
        return fake_synthesize(_text, output_dir, **_kwargs)

    def fake_merge(_segments, output_base, **_kwargs):
        output = Path(f"{output_base}.mp3")
        output.write_bytes(b"output")
        return [output]

    async def fake_merge_async(_segments, output_base, **_kwargs):
        return fake_merge(_segments, output_base, **_kwargs)

    monkeypatch.setattr(orchestrator, "synthesize", fake_synthesize)
    monkeypatch.setattr(orchestrator, "synthesize_async", fake_synthesize_async)
    monkeypatch.setattr(orchestrator, "merge", fake_merge)
    monkeypatch.setattr(orchestrator, "merge_async", fake_merge_async)

    t0 = time.time()
    result = orchestrator.process("test_vid", "podcast")
    elapsed = time.time() - t0

    assert result["status"] == "done"
    # Pipeline should complete quickly — summarize is async, doesn't block
    assert elapsed < 1.0, f"pipeline took {elapsed:.1f}s, expected <1s (summarize should not block)"
    summary_release.set()
    assert summary_finished.wait(timeout=1)

    deadline = time.time() + 1
    while time.time() < deadline:
        if db.get_variant("test_vid", "podcast")["summary_path"]:
            break
        time.sleep(0.01)
    assert db.get_variant("test_vid", "podcast")["status"] == "done"
    import os
    os.environ.pop("_thread_cleanup", None)
