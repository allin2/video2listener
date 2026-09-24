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
