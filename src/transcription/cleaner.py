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


def clean(text: str) -> str:
    """清洗英文播客文本。

    - 去除纯语气词行
    - 删除含广告关键词的段落
    - 合并过短句子
    - 统一空白字符

    Args:
        text: 原始英文字幕/转写文本（可含换行）

    Returns:
        清洗后的英文文本
    """
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

    return merged


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
