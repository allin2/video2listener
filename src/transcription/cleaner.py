"""英文文本清洗模块。去除噪音、合并短句，保留语义完整性。"""

import re
import logging

logger = logging.getLogger(__name__)

# 广告关键词
AD_KEYWORDS = [
    "subscribe", "like and subscribe", "hit the bell",
    "check out my", "use my code", "discount code",
    "sponsor", "sponsored by", "this episode is brought to you",
    "patreon.com", "buy me a coffee",
]

SHORT_SENTENCE_MIN_WORDS = 8

# 说话人标注标签前缀
_SPEAKER_LABEL_PREFIX = "说话人"


def clean(text: str) -> tuple[str, int]:
    """清洗英文播客文本。

    - 检测说话人线索并标注
    - 去除纯语气词行
    - 删除含广告关键词的段落
    - 合并过短句子
    - 统一空白字符

    Args:
        text: 原始英文字幕/转写文本（可含换行）

    Returns:
        (cleaned_text, speaker_count): 清洗后的文本和检测到的说话人数量（0 表示无说话人）
    """
    # ---- 预处理：说话人检测 ----
    text, speaker_count = _detect_speakers(text)

    lines = text.strip().split("\n")
    cleaned: list[str] = []

    for line in lines:
        line = line.strip()
        if not line:
            cleaned.append("")  # 保留段落空行
            continue

        # 纯语气词行（仅含语气词 + 标点）
        if _is_filler_only(line):
            continue

        # 广告段落
        if _contains_ad(line):
            logger.debug("Ad line removed: %s...", line[:60])
            continue

        cleaned.append(line)

    # 合并过短句子
    merged = _merge_short_sentences("\n".join(cleaned))

    # 清理多余空白
    merged = re.sub(r"\n{3,}", "\n\n", merged)
    merged = merged.strip()

    return merged, speaker_count


def _detect_speakers(text: str) -> tuple[str, int]:
    """检测说话人线索并标注每个说话片段。

    支持的 YouTube 字幕说话人模式：
    - ``>> Speaker N:`` 或 ``>>Speaker N:``（含说话人编号）
    - ``>> 后跟文本``（通用说话人切换标记）
    - ``[Name]:`` 模式（方括号包裹的说话人名称）
    - 行首 ``Speaker N:`` 模式（无 >> 前缀）

    检测到说话人时，为每个唯一说话人分配 ``[说话人 A]:`` /
    ``[说话人 B]:`` 等标签，并替换原说话人标记。
    未检测到任何说话人时，原样返回原文。

    Args:
        text: 原始字幕/转写文本

    Returns:
        (annotated_text, speaker_count): 标注后的文本和唯一说话人数量。
        ``speaker_count`` 为 0 表示未检测到说话人。
    """
    lines = text.split("\n")
    speaker_map: dict[str, str] = {}  # canonical_speaker_id → "说话人 A"
    annotated_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        matched = False
        speaker_id = ""
        rest = ""

        # ---- 模式 1: ">> Speaker N:" 或 ">>Speaker N:" ----
        m = re.match(r"^>>\s*Speaker\s+(\d+)\s*:?\s*(.*)", stripped, re.IGNORECASE)
        if m:
            speaker_id = f"Speaker {m.group(1)}"
            rest = m.group(2).strip()
            matched = True

        # ---- 模式 2: ">> Name:"（>> 后跟英语人名 + 冒号） ----
        if not matched:
            m = re.match(r"^>>\s*([A-Za-z][A-Za-z\s]{0,30}?)\s*:\s*(.*)", stripped)
            if m:
                speaker_id = m.group(1).strip()
                rest = m.group(2).strip()
                matched = True

        # ---- 模式 3: ">> text"（>> 后跟任意文本，无明确说话人标识） ----
        if not matched:
            m = re.match(r"^>>\s*(.+)", stripped)
            if m:
                # 匿名说话人 — 每个 >> 行可能来自不同说话人
                # 使用连续计数的匿名标签
                speaker_id = f"anonymous_{len([k for k in speaker_map if k.startswith('anonymous_')]) + 1}"
                rest = m.group(1).strip()
                matched = True

        # ---- 模式 4: "[Name]:" 方括号格式 ----
        if not matched:
            m = re.match(r"^\[([A-Za-z][A-Za-z\s]{0,30}?)\]:\s*(.*)", stripped)
            if m:
                speaker_id = m.group(1).strip()
                rest = m.group(2).strip()
                matched = True

        # ---- 模式 5: "Speaker N:" 行首格式（无 >> 前缀） ----
        if not matched:
            m = re.match(r"^(Speaker\s+\d+)\s*:?\s*(.*)", stripped, re.IGNORECASE)
            if m:
                speaker_id = m.group(1).strip()
                rest = m.group(2).strip()
                matched = True

        if matched:
            if speaker_id not in speaker_map:
                label_index = len(speaker_map)
                label = f"{_SPEAKER_LABEL_PREFIX} {chr(65 + label_index)}"  # "说话人 A", "说话人 B", ...
                speaker_map[speaker_id] = label
            label = speaker_map[speaker_id]
            annotated_lines.append(f"[{label}]: {rest}")
        else:
            annotated_lines.append(stripped)

    if not speaker_map:
        return (text, 0)

    logger.debug(
        "Detected %d speaker(s): %s",
        len(speaker_map),
        ", ".join(speaker_map.values()),
    )
    return ("\n".join(annotated_lines), len(speaker_map))


def _is_filler_only(line: str) -> bool:
    """判断是否为纯语气词/口头禅行。"""
    fillers = {
        "yeah", "yes", "no", "okay", "ok", "uh", "um", "hmm",
        "right", "sure", "well", "so", "oh", "ah", "mhm",
        "you know", "i mean", "like", "actually", "basically",
        "literally", "honestly", "anyway", "alright",
    }
    # 去除标点后检查
    stripped = re.sub(r"[^\w\s]", "", line.lower()).strip()
    words = set(stripped.split())
    return len(words) <= 2 and all(w in fillers for w in words)


def _contains_ad(line: str) -> bool:
    """检查是否包含广告内容。"""
    lower = line.lower()
    return any(kw in lower for kw in AD_KEYWORDS)


def _merge_short_sentences(text: str) -> str:
    """将过短句子合并到相邻句中。"""
    paragraphs = text.split("\n\n")
    result: list[str] = []

    for para in paragraphs:
        lines = para.strip().split("\n")
        if not lines:
            result.append("")
            continue

        merged: list[str] = []
        buffer = ""

        for line in lines:
            line = line.strip()
            if not line:
                if buffer:
                    merged.append(buffer)
                    buffer = ""
                continue

            words = len(line.split())
            if words < SHORT_SENTENCE_MIN_WORDS:
                buffer = (buffer + " " + line).strip() if buffer else line
            else:
                if buffer:
                    merged.append(buffer)
                    buffer = ""
                merged.append(line)

        if buffer:
            if merged:
                merged[-1] = merged[-1] + " " + buffer
            else:
                merged.append(buffer)

        result.append("\n".join(merged))

    return "\n\n".join(r for r in result if r)
