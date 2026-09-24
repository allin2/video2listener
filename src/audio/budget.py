"""时长预算：合成前预估 + 各模式相对原视频时长的目标区间。

口径统一以「原视频时长」为基准（用户可感知的基准），见
docs/plans/2026-09-14-001-feat-duration-budget-plan.md。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 每秒朗读的非空白字符数（含标点——标点处的停顿计入时长）。
# MiMo 苏打音色 4 个成品实测 5.57–5.71（2026-09-24）；Fish 样本少，
# 实测约 4.7–5.1，预估会偏短。换引擎或音色后需重新标定。
CHARS_PER_SECOND = 5.66

# 超过此时长的原视频，浓缩版改以绝对时长为目标（见 CONCEPTS.md）
LONG_SOURCE_SECONDS = 60 * 60

# 英文原稿折算中文字数：1 词 ≈ 1.85 字（9_Free 忠实版实测 10779 字 / 5841 词）
ZH_CHARS_PER_EN_WORD = 1.85

# 验收门禁在目标区间外留的余量
_RATIO_TOLERANCE = 0.05
_LONG_TOLERANCE_SECONDS = 5 * 60

_WHITESPACE_RE = re.compile(r"\s+")


def predict_duration(text: str, chars_per_second: float = CHARS_PER_SECOND) -> float:
    """按非空白字符数预估 TTS 时长（秒）。纯本地计算，不调 API。"""
    return len(_WHITESPACE_RE.sub("", text)) / chars_per_second


@dataclass(frozen=True)
class DurationBudget:
    """某模式的目标时长区间（target）与验收门禁区间（gate），单位秒。"""

    target_min: float
    target_max: float
    gate_min: float
    gate_max: float

    def within_target(self, seconds: float) -> bool:
        return self.target_min <= seconds <= self.target_max

    def within_gate(self, seconds: float) -> bool:
        return self.gate_min <= seconds <= self.gate_max


def _ratio_budget(source_seconds: float, low: float, high: float) -> DurationBudget:
    return DurationBudget(
        target_min=source_seconds * low,
        target_max=source_seconds * high,
        gate_min=source_seconds * (low - _RATIO_TOLERANCE),
        gate_max=source_seconds * (high + _RATIO_TOLERANCE),
    )


def duration_budget(mode: str, source_seconds: float) -> DurationBudget | None:
    """返回模式相对原视频的时长预算；faithful 不设上限约束，返回 None。"""
    if source_seconds <= 0:
        return None
    if mode == "condensed":
        if source_seconds > LONG_SOURCE_SECONDS:
            return DurationBudget(
                target_min=20 * 60,
                target_max=30 * 60,
                gate_min=20 * 60 - _LONG_TOLERANCE_SECONDS,
                gate_max=30 * 60 + _LONG_TOLERANCE_SECONDS,
            )
        return _ratio_budget(source_seconds, 0.30, 0.50)
    if mode == "podcast":
        return _ratio_budget(source_seconds, 0.55, 1.25)
    return None


# 合成后实际时长 / 合成前预估的合理区间。越界说明引擎读了不该读的东西
# （如把 SSML 标签当文本念出，曾导致约 2 倍）或音频被截断。
SYNTH_RATIO_MIN = 0.5
SYNTH_RATIO_MAX = 1.5


def synthesized_duration_error(predicted: float, actual: float) -> str | None:
    """实际合成时长明显偏离预估时返回错误说明，正常返回 None。"""
    if predicted <= 0:
        return None
    ratio = actual / predicted
    if SYNTH_RATIO_MIN <= ratio <= SYNTH_RATIO_MAX:
        return None
    hint = (
        "可能把标记/标签当文字念了出来，或语速设置异常"
        if ratio > SYNTH_RATIO_MAX else "可能有片段被截断或为空"
    )
    return (
        f"合成音频时长异常：实际 {actual / 60:.1f} 分钟，预估 {predicted / 60:.1f} 分钟"
        f"（{ratio:.1f} 倍），{hint}"
    )


# 无原视频时长时的篇幅上界，与时长门禁上界（50% + 余量）一致
_CONDENSED_TEXT_RATIO_MAX = 0.50 + _RATIO_TOLERANCE


def _content_units(text: str, source_language: str) -> float:
    """原稿篇幅折算为中文字数，便于与中文产出稿直接比较。"""
    if source_language == "en":
        return len(text.split()) * ZH_CHARS_PER_EN_WORD
    return float(len(_WHITESPACE_RE.sub("", text)))


def condensed_overshoot(
    source_text: str,
    output_text: str,
    source_language: str,
    source_seconds: float,
) -> str | None:
    """浓缩稿篇幅超出预算时返回给模型的反馈，未超标返回 None。

    有原视频时长时按预估音频时长判断；否则退回按文字篇幅比。
    """
    budget = duration_budget("condensed", source_seconds)
    if budget:
        estimate = predict_duration(output_text)
        if estimate <= budget.gate_max:
            return None
        return (
            f"上一版预计 {estimate / 60:.0f} 分钟，约为原视频（{source_seconds / 60:.0f} 分钟）的 "
            f"{estimate / source_seconds:.0%}，目标是 {budget.target_min / 60:.0f}–"
            f"{budget.target_max / 60:.0f} 分钟"
        )
    source_units = _content_units(source_text, source_language)
    if source_units <= 0:
        return None
    ratio = len(_WHITESPACE_RE.sub("", output_text)) / source_units
    if ratio <= _CONDENSED_TEXT_RATIO_MAX:
        return None
    return f"上一版篇幅约为原文的 {ratio:.0%}，目标是 30%–50%"
