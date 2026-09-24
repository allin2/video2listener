"""验收脚本本身的测试：门禁必须能通过，改坏任一硬指标时必须失败。"""
import importlib.util
import json
from pathlib import Path

import pytest

from src.translation.client import _build_translation_audit

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_mode_outputs.py"
_spec = importlib.util.spec_from_file_location("evaluate_mode_outputs", _SCRIPT)
evaluate_mode_outputs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evaluate_mode_outputs)

SOURCE_SECONDS = 1000.0
SOURCE_SEGMENTS = [
    "The model has 11 layers and was trained on 50,000 examples.",
    "Latency matters more than accuracy for most users.",
]
MODE_SEGMENTS = {
    "faithful": ["这个模型有十一层，在五万个样本上训练。", "对多数用户来说，延迟比准确率更重要。"],
    "podcast": ["聊聊这个模型：它有十一层，用了五万个样本来训练。", "而且你会发现，用户更在乎快不快。"],
    "condensed": ["十一层模型，五万样本训练。", "结论：延迟优先于准确率。"],
}
MODE_SECONDS = {"faithful": 1090.0, "podcast": 1100.0, "condensed": 400.0}


def _write_episode(root: Path, mode_seconds: dict[str, float]) -> Path:
    source = "\n\n".join(SOURCE_SEGMENTS)
    (root / "transcript_clean.txt").write_text(source, encoding="utf-8")
    (root / "metadata.json").write_text(
        json.dumps({"duration_seconds": SOURCE_SECONDS}), encoding="utf-8",
    )
    for mode, segments in MODE_SEGMENTS.items():
        mode_dir = root / "variants" / mode
        mode_dir.mkdir(parents=True)
        script = "\n\n".join(segments)
        (mode_dir / "script_zh.txt").write_text(script, encoding="utf-8")
        (mode_dir / "tts_text.txt").write_text(script, encoding="utf-8")
        (mode_dir / f"{mode}.mp3").write_text(str(mode_seconds[mode]), encoding="utf-8")
        audit = _build_translation_audit(source, mode, SOURCE_SEGMENTS, segments)
        (mode_dir / "translation_audit.json").write_text(json.dumps(audit), encoding="utf-8")
    return root


@pytest.fixture
def episode(tmp_path, monkeypatch):
    # 假 MP3 里直接写时长，避免依赖 ffprobe
    monkeypatch.setattr(
        evaluate_mode_outputs, "_audio_duration",
        lambda path: float(path.read_text(encoding="utf-8")),
    )
    return _write_episode(tmp_path, MODE_SECONDS)


def test_gate_passes_on_healthy_episode(episode):
    metrics = evaluate_mode_outputs.evaluate(episode)

    assert metrics["quality_failures"] == []
    assert metrics["validation_passed"] == 1
    assert metrics["faithful_audit_status"] == "passed"
    assert metrics["condensed_to_source_duration_ratio"] == 0.4


def test_gate_fails_when_script_edited_after_audit(episode):
    script = episode / "variants" / "faithful" / "script_zh.txt"
    script.write_text(script.read_text(encoding="utf-8") + "新增的一句。", encoding="utf-8")

    metrics = evaluate_mode_outputs.evaluate(episode)

    assert metrics["translation_audits_complete"] == 0
    assert metrics["validation_passed"] == 0


def test_legacy_v1_audit_is_verified_segment_by_segment(episode):
    audit_path = episode / "variants" / "faithful" / "translation_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    for key in ("translation_sha256", "quality_status", "quality_message"):
        audit.pop(key)
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    assert evaluate_mode_outputs.evaluate(episode)["validation_passed"] == 1

    script = episode / "variants" / "faithful" / "script_zh.txt"
    script.write_text(script.read_text(encoding="utf-8").replace("十一", "十二"), encoding="utf-8")
    assert evaluate_mode_outputs.evaluate(episode)["translation_audits_complete"] == 0


@pytest.mark.parametrize("condensed_seconds", [200.0, 600.0])
def test_gate_fails_when_condensed_outside_source_budget(tmp_path, monkeypatch, condensed_seconds):
    monkeypatch.setattr(
        evaluate_mode_outputs, "_audio_duration",
        lambda path: float(path.read_text(encoding="utf-8")),
    )
    episode = _write_episode(tmp_path, {**MODE_SECONDS, "condensed": condensed_seconds})

    metrics = evaluate_mode_outputs.evaluate(episode)

    assert "浓缩版时长超出相对原视频的预算区间" in metrics["quality_failures"]
    assert metrics["validation_passed"] == 0


def test_gate_fails_without_source_duration(episode):
    (episode / "metadata.json").write_text("{}", encoding="utf-8")

    metrics = evaluate_mode_outputs.evaluate(episode)

    assert metrics["validation_passed"] == 0


def test_gate_fails_on_untranslated_english(episode):
    script = episode / "variants" / "podcast" / "script_zh.txt"
    script.write_text(" ".join(["word"] * 25), encoding="utf-8")

    assert evaluate_mode_outputs.evaluate(episode)["validation_passed"] == 0
