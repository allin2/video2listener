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


def test_condensed_overshoot_uses_duration_when_source_known():
    from src.audio.budget import condensed_overshoot

    ok = "字" * int(400 * CHARS_PER_SECOND)  # 400 秒 / 1000 秒 = 40%
    long = "字" * int(900 * CHARS_PER_SECOND)  # 90%
    assert condensed_overshoot("原文", ok, "zh", 1000) is None
    assert "目标是 5–8 分钟" in condensed_overshoot("原文", long, "zh", 1000)


def test_condensed_overshoot_falls_back_to_text_ratio_without_duration():
    from src.audio.budget import condensed_overshoot

    source = "字" * 12828
    # BV1Ccbs6SERa 实测：12828 → 11144 字（87%）必须被判超标
    assert "87%" in condensed_overshoot(source, "字" * 11144, "zh", 0)
    assert condensed_overshoot(source, "字" * 5000, "zh", 0) is None
    # 英文原稿按 1 词 ≈ 1.85 字折算：1000 词 → 1850 字，800 字 ≈ 43%
    assert condensed_overshoot("word " * 1000, "字" * 800, "en", 0) is None


def _run_rewrite(script, retried, source="字" * 1000):
    import asyncio
    from pathlib import Path

    from src.pipeline import orchestrator

    calls, messages = [], []

    async def rewrite(instruction):
        calls.append(instruction)
        return retried, {"v": "retry"}

    result = asyncio.run(orchestrator._rewrite_if_condensed_too_long(
        script, source, "zh", Path("/nonexistent"), messages.append, rewrite,
    ))
    return result, calls, messages


def test_condensed_rewrite_skipped_when_within_budget():
    result, calls, _ = _run_rewrite("字" * 400, "unused")
    assert result == ("字" * 400, None) and calls == []


def test_condensed_rewrite_uses_shorter_retry_and_its_audit():
    result, calls, messages = _run_rewrite("字" * 870, "字" * 420)
    assert result == ("字" * 420, {"v": "retry"})
    assert "87%" in calls[0] and "重写后篇幅已达标" in messages


def test_condensed_rewrite_keeps_original_when_retry_is_longer():
    result, _, messages = _run_rewrite("字" * 870, "字" * 900)
    assert result == ("字" * 870, None)
    assert any("仍超标" in m for m in messages)
