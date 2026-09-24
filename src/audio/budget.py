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
