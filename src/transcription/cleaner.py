"""英文文本清洗模块。去除噪音、合并短句，保留语义完整性。"""

import re
import logging

logger = logging.getLogger(__name__)

# 广告与互动套话关键词（支持英文与中文平台常见话术）
AD_KEYWORDS = [
    # 英文求赞/关注/订阅/链接
    "subscribe", "like and subscribe", "hit the bell", "hit the subscribe", "hit that subscribe",
    "check out my", "use my code", "discount code", "promo code", "coupon code",
    "sponsor", "sponsored by", "this episode is brought to you", "brought to you by",
    "patreon.com", "patreon", "buy me a coffee",
    "link in the description", "link in description", "links in the description", "link in bio",
    "link below", "links below",
    "leave a comment", "let me know in the comments", "hit the like button",
    "free trial", "get 10% off", "get 20% off",
    # 中文一键三连/求关注/求点赞
    "一键三连", "点赞关注", "关注博主", "记得点赞", "点个赞", "投币", "求三连", "点赞收藏",
    "求点赞", "别忘了点赞", "双击关注",
    # 中文留评互动
    "在评论区告诉我", "评论区见", "留评互动", "评论区留言", "写在评论区",
    # 中文商业赞助/带货引流
    "赞助", "赞助商", "赞助播出", "本期赞助", "感谢赞助", "商务合作", "优惠码", "折扣码", "福利码",
    "置顶评论", "主页链接", "左下角链接", "点击下方链接", "简介区链接",
]

_AD_PATTERNS = [
    re.compile(r"\b(?:sponsor(?:ed|ship)?|brought to you by)\b", re.IGNORECASE),
    re.compile(r"\b(?:discount|promo(?:tion)?|coupon)\s+code\b", re.IGNORECASE),
    re.compile(r"\b(?:use|enter)\s+(?:the\s+)?code\b", re.IGNORECASE),
    re.compile(r"\blink\s+(?:is\s+)?in\s+(?:the\s+)?(?:description|bio)\b", re.IGNORECASE),
    re.compile(r"\b(?:like\s+and\s+subscribe|hit\s+(?:the|that)\s+(?:bell|like|subscribe))\b", re.IGNORECASE),
    re.compile(r"\bdownload\s+.+\s+for\s+free\b", re.IGNORECASE),
    re.compile(r"\b(?:let\s+me\s+know|leave\s+a\s+comment)\s+in\s+the\s+comments\b", re.IGNORECASE),
    re.compile(r"(?:一键三连|点赞关注|点个赞|求点赞|求三连|别忘了点赞|投币收藏|双击关注)", re.IGNORECASE),
    re.compile(r"(?:赞助|赞助商|赞助播出|商务合作|优惠码|折扣码|福利码)", re.IGNORECASE),
    re.compile(r"(?:置顶评论|评论区见|点击(?:下方|左下角)?链接|简介区链接)", re.IGNORECASE),
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

        # 去除行内 URL（防止 raw link 泄漏到大模型翻译及 TTS 读出 "HTTP"）
        line = re.sub(r"https?://\S+", "", line).strip()
        if not line:
            continue

        # 纯语气词行（仅含语气词 + 标点）
        if _is_filler_only(line):
            continue

        # 广告与互动套话段落
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
    """检查是否包含广告或互动套话内容。"""
    lower = line.lower()
    if any(kw in lower for kw in AD_KEYWORDS):
        return True
    return any(bool(p.search(line)) for p in _AD_PATTERNS)


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
