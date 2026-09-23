#!/usr/bin/env python3
"""Evaluate TTS request volume and three-mode output sanity on one real episode.

The command prints one JSON object to stdout so it can be used by the
Compound Engineering optimization harness and CI without external services.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.audio.merger import _resolve_media_tool
from src.tts.synthesizer import _split_tts_segments


MODES = ("faithful", "podcast", "condensed")
LONG_ENGLISH_RUN_RE = re.compile(
    r"(?:\b[A-Za-z][A-Za-z0-9'’-]*\b[\s,.;:!?()\[\]\"/\\-]*){20,}"
)


def _normalise_number(token: str) -> str:
    return token.replace(",", "").rstrip("%")


def _numbers(text: str) -> set[str]:
    return {
        _normalise_number(token)
        for token in re.findall(r"\b\d[\d,]*(?:\.\d+)?%?\b", text)
    }


def _audio_duration(path: Path) -> float:
    # 用与生产代码相同的解析器：ffprobe 常常不在 PATH 上（launchd、CI、
    # 只装了 Homebrew 但未导出 PATH 的 shell），裸调用会让验收脚本直接崩溃。
    completed = subprocess.run(
        [
            _resolve_media_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(completed.stdout.strip())


def _normalised_text(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _assess(metrics: dict[str, object]) -> list[str]:
    """返回违反 PRD 内容验收门槛的原因；空列表表示硬门禁通过。"""
    failures: list[str] = []
    if metrics.get("outputs_exist") != 1:
        failures.append("三种模式的脚本、TTS 文本或 MP3 不完整")
    if metrics.get("translation_audits_complete") != 1:
        failures.append("缺少逐段翻译审计，无法证明全部源片段均已处理")
    if metrics.get("faithful_semantic_audit_passed") != 1:
        failures.append("忠实版缺少通过的双向语义审计")
    for mode in MODES:
        if float(metrics.get(f"{mode}_long_english_runs", 0)) > 0:
            failures.append(f"{mode} 存在连续未翻译英文")
    if float(metrics.get("faithful_han_per_source_word", 0)) < 1.0:
        failures.append("忠实版中文信息量异常偏低")
    if float(metrics.get("max_mode_similarity", 1)) >= 0.9:
        failures.append("至少两个模式的脚本高度相同")
    condensed_ratio = float(metrics.get("condensed_to_faithful_duration_ratio", 0))
    if not 0.25 <= condensed_ratio <= 0.55:
        failures.append("浓缩版时长不在忠实版的 25%–55% 范围")
    podcast_ratio = float(metrics.get("podcast_to_faithful_duration_ratio", 0))
    if not 0.5 <= podcast_ratio <= 1.2:
        failures.append("播客版时长相对忠实版异常")
    return failures


def evaluate(episode_dir: Path) -> dict[str, object]:
    source_path = episode_dir / "transcript_clean.txt"
    metadata_path = episode_dir / "metadata.json"
    variants_dir = episode_dir / "variants"
    if not source_path.exists() or not metadata_path.exists():
        raise FileNotFoundError(f"缺少共享源文件: {episode_dir}")

    source = source_path.read_text(encoding="utf-8")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    source_duration = float(metadata.get("duration_seconds") or 0)
    source_words = max(1, len(source.split()))
    source_numbers = _numbers(source)

    scripts: dict[str, str] = {}
    durations: dict[str, float] = {}
    request_counts: dict[str, int] = {}
    max_segment_chars = 0
    outputs_exist = 1
    completed_audits = 0
    faithful_semantic_audit_passed = 0
    faithful_audit_status = "missing"
    result: dict[str, object] = {}

    for mode in MODES:
        mode_dir = variants_dir / mode
        script_path = mode_dir / "script_zh.txt"
        tts_path = mode_dir / "tts_text.txt"
        audit_path = mode_dir / "translation_audit.json"
        audio_paths = sorted(mode_dir.glob("*.mp3"))
        if not script_path.exists() or not tts_path.exists() or not audio_paths:
            outputs_exist = 0
            scripts[mode] = ""
            durations[mode] = 0.0
            request_counts[mode] = 0
            result[f"{mode}_long_english_runs"] = 0
            continue

        if audit_path.is_file():
            try:
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                current_script = script_path.read_text(encoding="utf-8")
                translation_hash_matches = (
                    not audit.get("translation_sha256")
                    or audit.get("translation_sha256")
                    == hashlib.sha256(current_script.encode("utf-8")).hexdigest()
                )
                if (
                    audit.get("all_segments_present") is True
                    and audit.get("source_segment_count")
                    == audit.get("translation_segment_count")
                    and audit.get("source_sha256")
                    == hashlib.sha256(source.encode("utf-8")).hexdigest()
                    and translation_hash_matches
                ):
                    completed_audits += 1
                    if mode == "faithful":
                        semantic = audit.get("semantic_audit") or {}
                        faithful_audit_status = (
                            audit.get("quality_status") or semantic.get("status") or "unknown"
                        )
                        faithful_semantic_audit_passed = int(
                            faithful_audit_status == "passed"
                            and semantic.get("passed") is True
                            and semantic.get("checked_segments")
                            == audit.get("source_segment_count")
                            and semantic.get("failed_segments") == 0
                        )
            except (OSError, json.JSONDecodeError):
                pass

        script = script_path.read_text(encoding="utf-8")
        tts_text = tts_path.read_text(encoding="utf-8")
        segments = _split_tts_segments(tts_text)
        scripts[mode] = script
        request_counts[mode] = len(segments)
        max_segment_chars = max(max_segment_chars, max(map(len, segments), default=0))
        durations[mode] = sum(_audio_duration(path) for path in audio_paths)
        result[f"{mode}_long_english_runs"] = len(LONG_ENGLISH_RUN_RE.findall(script))
        result[f"{mode}_han_chars"] = len(re.findall(r"[\u4e00-\u9fff]", script))

    translated_numbers = _numbers(scripts.get("faithful", ""))
    numeric_recall = (
        len(source_numbers & translated_numbers) / len(source_numbers)
        if source_numbers else 1.0
    )
    faithful_han_per_source_word = (
        len(re.findall(r"[\u4e00-\u9fff]", scripts.get("faithful", ""))) / source_words
    )

    similarities = []
    for index, left in enumerate(MODES):
        for right in MODES[index + 1:]:
            similarity = SequenceMatcher(
                None,
                _normalised_text(scripts.get(left, "")),
                _normalised_text(scripts.get(right, "")),
                autojunk=False,
            ).ratio()
            result[f"similarity_{left}_{right}"] = round(similarity, 4)
            similarities.append(similarity)

    result.update({
        "outputs_exist": outputs_exist,
        "translation_audits_complete": int(completed_audits == len(MODES)),
        "faithful_semantic_audit_passed": faithful_semantic_audit_passed,
        "faithful_audit_status": faithful_audit_status,
        "tts_requests_total": sum(request_counts.values()),
        "tts_requests_faithful": request_counts.get("faithful", 0),
        "tts_requests_podcast": request_counts.get("podcast", 0),
        "tts_requests_condensed": request_counts.get("condensed", 0),
        "max_tts_segment_chars": max_segment_chars,
        "faithful_numeric_recall": round(numeric_recall, 4),
        "faithful_han_per_source_word": round(faithful_han_per_source_word, 4),
        "max_mode_similarity": round(max(similarities, default=1.0), 4),
        "source_duration_seconds": round(source_duration, 3),
        "faithful_duration_seconds": round(durations.get("faithful", 0), 3),
        "podcast_duration_seconds": round(durations.get("podcast", 0), 3),
        "condensed_duration_seconds": round(durations.get("condensed", 0), 3),
        "condensed_to_faithful_duration_ratio": round(
            durations.get("condensed", 0) / max(1.0, durations.get("faithful", 0)), 4
        ),
        "podcast_to_faithful_duration_ratio": round(
            durations.get("podcast", 0) / max(1.0, durations.get("faithful", 0)), 4
        ),
    })
    failures = _assess(result)
    result["validation_passed"] = int(not failures)
    result["quality_failure_count"] = len(failures)
    result["quality_failures"] = failures
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "episode_dir",
        type=Path,
        nargs="?",
        default=Path("data/9_Free_AI_Skills_That_Feel_Like_Cheat_Codes"),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="验收失败时返回非零退出码",
    )
    args = parser.parse_args()
    metrics = evaluate(args.episode_dir)
    print(json.dumps(metrics, ensure_ascii=False, sort_keys=True))
    if args.strict and metrics["validation_passed"] != 1:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
