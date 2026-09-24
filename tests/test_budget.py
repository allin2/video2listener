from src.audio.budget import CHARS_PER_SECOND, duration_budget, predict_duration


def test_predict_duration_counts_non_whitespace_chars():
    assert predict_duration("你好，GPT\n\n 4。") == 8 / CHARS_PER_SECOND


def test_condensed_budget_is_relative_to_source():
    budget = duration_budget("condensed", 1000)
    assert (budget.target_min, budget.target_max) == (300, 500)
    assert budget.within_gate(260) and not budget.within_target(260)
    assert not budget.within_gate(240)


def test_condensed_budget_for_long_source_is_absolute():
    budget = duration_budget("condensed", 3 * 3600)
    assert (budget.target_min, budget.target_max) == (20 * 60, 30 * 60)


def test_faithful_has_no_budget_and_missing_source_has_none():
    assert duration_budget("faithful", 1000) is None
    assert duration_budget("condensed", 0) is None


def test_pipeline_estimate_message_flags_condensed_outside_target(tmp_path):
    import json

    from src.pipeline.orchestrator import _duration_estimate_message

    (tmp_path / "metadata.json").write_text(json.dumps({"duration_seconds": 600}), encoding="utf-8")
    on_target = _duration_estimate_message("condensed", "字" * int(240 * CHARS_PER_SECOND), tmp_path)
    too_long = _duration_estimate_message("condensed", "字" * int(500 * CHARS_PER_SECOND), tmp_path)

    assert "预计音频时长约 4.0 分钟（原视频 10.0 分钟）" == on_target
    assert "偏离目标 3–5 分钟" in too_long


def test_pipeline_estimate_message_without_metadata(tmp_path):
    from src.pipeline.orchestrator import _duration_estimate_message

    assert _duration_estimate_message("faithful", "字" * 566, tmp_path) == "预计音频时长约 1.7 分钟"


def test_synthesized_duration_error_flags_spoken_markup_and_truncation():
    from src.audio.budget import synthesized_duration_error

    assert synthesized_duration_error(1000, 1100) is None
    # Obsidian 播客版事故：SSML 标签被念出，实际约为预估的 2 倍
    assert "念了出来" in synthesized_duration_error(1400, 3680)
    assert "截断" in synthesized_duration_error(1000, 300)
    assert synthesized_duration_error(0, 100) is None


def test_pipeline_duration_check_raises_on_bloated_audio(monkeypatch, tmp_path):
    import pytest

    from src.pipeline import orchestrator

    monkeypatch.setattr(orchestrator, "_probe_duration", lambda path: 60.0)
    segments = [tmp_path / "a.mp3", tmp_path / "b.mp3"]
    text = "字" * int(40 * CHARS_PER_SECOND)  # 预估 40 秒，实际 120 秒

    with pytest.raises(RuntimeError, match="合成音频时长异常"):
        orchestrator._check_synthesized_duration(text, segments)


def test_pipeline_duration_check_skips_when_unprobeable(monkeypatch, tmp_path):
    from src.pipeline import orchestrator

    def boom(path):
        raise OSError("no ffprobe")

    monkeypatch.setattr(orchestrator, "_probe_duration", boom)
    orchestrator._check_synthesized_duration("字" * 100, [tmp_path / "a.mp3"])
