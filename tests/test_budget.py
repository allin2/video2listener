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


def _run_condense(outputs, tmp_path, model="m", source="字" * 1000):
    """outputs: 每次调用 translate 依次返回的文稿；记录 (instruction, scale)。"""
    import asyncio

    from src.pipeline import orchestrator

    calls, messages = [], []

    async def translate(instruction="", length_scale=1.0):
        calls.append((instruction, length_scale))
        return outputs[len(calls) - 1], {"pass": len(calls)}

    result = asyncio.run(orchestrator._condense_with_budget(
        translate, source, "zh", tmp_path / "no-metadata", messages.append,
        model=model, calibration_dir=tmp_path,
    ))
    return result, calls, messages


def test_condense_within_budget_runs_once(tmp_path):
    result, calls, _ = _run_condense(["字" * 400], tmp_path)
    assert result == ("字" * 400, {"pass": 1}) and calls == [("", 1.0)]


def test_condense_rewrites_with_scaled_targets_and_keeps_matching_audit(tmp_path):
    result, calls, messages = _run_condense(["字" * 870, "字" * 420], tmp_path)
    assert result == ("字" * 420, {"pass": 2})
    instruction, scale = calls[1]
    assert "87%" in instruction and "重写后篇幅已达标" in messages
    assert round(scale, 2) == 0.46  # 目标中值 40% / 实际 87%


def test_condense_keeps_first_version_when_retry_is_longer(tmp_path):
    result, _, messages = _run_condense(["字" * 870, "字" * 900], tmp_path)
    assert result == ("字" * 870, {"pass": 1})
    assert any("仍超标" in m for m in messages)


def test_condense_uses_model_calibration_to_skip_first_pass(tmp_path):
    # 第一次：无校准，按 1.0 起步，68% → 重写
    _run_condense(["字" * 680, "字" * 440], tmp_path, model="flash")
    # 第二次：同模型按校准系数起步，一遍达标
    result, calls, messages = _run_condense(["字" * 420], tmp_path, model="flash")

    assert len(calls) == 1 and calls[0][1] < 0.7
    assert any("校准系数" in m for m in messages)
    # 其他模型不受影响，仍从 1.0 起步
    _, other_calls, _ = _run_condense(["字" * 400], tmp_path, model="chat")
    assert other_calls[0][1] == 1.0


def test_calibration_is_separate_per_source_language(tmp_path):
    import json

    _run_condense(["字" * 680, "字" * 440], tmp_path, model="flash")
    data = json.loads((tmp_path / "condensed_calibration.json").read_text(encoding="utf-8"))
    assert list(data) == ["flash|zh"]

    from src.audio.budget import calibration_key, initial_length_scale

    assert initial_length_scale(calibration_key("flash", "en"), 0.40, tmp_path) == 1.0


def test_rewrite_length_scale_corrects_measured_overshoot():
    from src.audio.budget import CondensedMeasure, rewrite_length_scale

    # BV1Ccbs6SERa 实测：62%，目标中值 40% → 目标缩到约 65%
    assert round(rewrite_length_scale(CondensedMeasure(0.62, 0.40, 0.55, "")), 2) == 0.65
    assert rewrite_length_scale(CondensedMeasure(3.0, 0.40, 0.55, "")) == 0.3  # 下限
    assert rewrite_length_scale(CondensedMeasure(0.30, 0.40, 0.55, "")) == 1.0  # 不放宽


def test_measure_condensed_uses_duration_budget_midpoint():
    from src.audio.budget import measure_condensed

    m = measure_condensed("原文", "字" * int(620 * CHARS_PER_SECOND), "zh", 1000)
    assert round(m.ratio, 2) == 0.62 and m.target_mid == 0.40 and m.gate_max == 0.55


def test_calibration_blends_runs_per_model(tmp_path):
    from src.audio.budget import initial_length_scale, record_condensed_run

    assert initial_length_scale("flash", 0.40, tmp_path) == 1.0
    record_condensed_run("flash", 1.0, 0.68, tmp_path)   # k = 0.68
    record_condensed_run("flash", 0.58, 0.44, tmp_path)  # k = 0.759 → 平均 0.7193
    assert round(initial_length_scale("flash", 0.40, tmp_path), 2) == 0.56
    assert initial_length_scale("other", 0.40, tmp_path) == 1.0


def test_calibration_ignores_corrupt_file(tmp_path):
    from src.audio.budget import initial_length_scale

    (tmp_path / "condensed_calibration.json").write_text("{not json", encoding="utf-8")
    assert initial_length_scale("flash", 0.40, tmp_path) == 1.0


def test_predict_duration_accounts_for_tts_speed():
    text = "字" * int(100 * CHARS_PER_SECOND)
    assert round(predict_duration(text, speed=0.9)) == 111
