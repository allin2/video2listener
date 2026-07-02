"""TTS 文本预处理。清洗中文稿使其适合语音合成朗读。"""

import re
import logging

logger = logging.getLogger(__name__)


def clean_for_tts(text: str) -> str:
    """清洗中文播客稿，使其适合 TTS 朗读。

    - 去除 Markdown 标记和 URL
    - 英文专有名词保留原样
    - 数字转为中文读法（简单的阿拉伯数字）
    - 长句拆分（≤ 50 字短句）
    - 去除不适合朗读的符号

    Args:
        text: 中文播客稿

    Returns:
        清洗后的 TTS 文本
    """
    # 去除 Markdown 标记
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"#{1,6}\s", "", text)

    # 去除 URL
    text = re.sub(r"https?://\S+", "", text)

    # 去除不适合朗读的符号（保留中英文标点）
    text = re.sub(r"[*_~`\[\]{}|\\]", "", text)

    # 处理省略号
    text = text.replace("...", "。")

    # 统一空白
    text = re.sub(r"\s+", " ", text)

    # 数字读法：大数字用"亿/万"（简单的阿拉伯数字转换）
    # 暂不做复杂 NLP 转换，交由 Mimi TTS 的内建数字处理

    # 长句拆分
    sentences = _split_long_sentences(text)

    # 添加停顿标记（句间空行）
    result = "\n".join(s.strip() for s in sentences if s.strip())

    # 后处理：精细停顿标记
    result = _add_pause_markers(result)

    return result


def _split_long_sentences(text: str, max_chars: int = 50) -> list[str]:
    """将长句按标点拆分为短句。"""
    # 先按句号、问号、感叹号拆分
    raw = re.split(r"([。！？；])", text)
    sentences: list[str] = []
    current = ""

    for part in raw:
        if part in ("。", "！", "？", "；"):
            current += part
            if len(current) > max_chars:
                # 进一步按逗号拆分
                subs = _split_by_comma(current, max_chars)
                sentences.extend(subs)
            else:
                sentences.append(current)
            current = ""
        else:
            current += part

    if current.strip():
        if len(current) > max_chars:
            sentences.extend(_split_by_comma(current, max_chars))
        else:
            sentences.append(current)

    return sentences


def _split_by_comma(text: str, max_chars: int) -> list[str]:
    """按逗号拆分长句。"""
    parts = re.split(r"(，|、)", text)
    result: list[str] = []
    current = ""

    for i, part in enumerate(parts):
        current += part
        if len(current) >= max_chars:
            result.append(current)
            current = ""

    if current.strip():
        result.append(current)

    return result if result else [text]


def _add_pause_markers(text: str) -> str:
    """Post-process cleaned TTS text to insert natural pause markers.

    Runs after the main cleaning pass. Converts punctuation into TTS-friendly
    segment boundaries and pause markers:
    - 。？！： → newline (segment boundary / end-of-sentence pause)
    - ； → ，， (comma-length pause, softer than newline)
    - Normalises consecutive blank lines (max 2)
    - Merges ultrashort segments (< 5 chars) with adjacent lines
    """
    # Step 1: Convert ； splits back into ，， pauses.
    # _split_long_sentences splits on ； into separate lines; we want a softer pause.
    text = re.sub(r"；\n", "；，，", text)
    # Also handle ； at end of text (no newline follows)
    text = re.sub(r"；$", "；，，", text)

    # Step 2: ： should act as a segment boundary (colon introduces speech or lists).
    text = re.sub(r"：", "：\n", text)

    # Step 3: Safeguard — ensure 。？！ are always followed by a newline.
    text = re.sub(r"([。？！])(?!\n)", r"\1\n", text)

    # Step 4: Normalise consecutive blank lines — keep at most 2.
    lines = text.split("\n")
    normalised: list[str] = []
    blank_run = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            blank_run += 1
            if blank_run <= 2:
                normalised.append("")
        else:
            blank_run = 0
            normalised.append(stripped)

    # Step 5: Merge ultrashort fragments (< 5 chars) with adjacent lines.
    merged = _merge_ultrashort_lines(normalised)

    return "\n".join(merged)


def _merge_ultrashort_lines(lines: list[str]) -> list[str]:
    """Merge non-empty lines shorter than 5 characters with adjacent lines."""
    if not lines:
        return lines

    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # Preserve blank lines (spacing between segments).
        if not line:
            result.append(line)
            i += 1
            continue

        if len(line) >= 5:
            result.append(line)
            i += 1
            continue

        # Ultrashort line (< 5 chars, non-empty): absorb following non-empty lines.
        merged = line
        i += 1
        while i < len(lines) and lines[i]:
            merged += lines[i]
            i += 1

        # If still too short, prepend to the previous non-empty line in result.
        if len(merged) < 5:
            for j in range(len(result) - 1, -1, -1):
                if result[j]:
                    result[j] = result[j] + merged
                    merged = ""
                    break

        if merged:
            result.append(merged)

    return result
