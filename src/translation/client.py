"""LLM 翻译模块。支持动态 API 凭证和模型选择。"""

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

from openai import AsyncOpenAI, OpenAI

from src.config import get_config

logger = logging.getLogger(__name__)

MODE_PROMPTS = {
    "faithful": "translate_faithful.txt",
    "podcast": "translate_podcast.txt",
    "condensed": "translate_condensed.txt",
}


def _load_prompt(filename: str) -> str:
    cfg = get_config()
    root = cfg["_project_root"]
    prompt_path = root / "prompts" / filename
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def _resolve_llm(llm_config: Optional[dict] = None) -> tuple[OpenAI, str, str]:
    """解析 LLM 客户端和模型。

    llm_config 优先，兜底使用 config.yaml / 环境变量。
    返回 (client, model, api_key)。
    """
    cfg = get_config()
    if llm_config and llm_config.get("api_key"):
        client = OpenAI(
            api_key=llm_config["api_key"],
            base_url=llm_config.get("base_url", "https://api.deepseek.com"),
            timeout=120.0,
        )
        model = llm_config.get("model", "deepseek-chat")
        return client, model, llm_config["api_key"]
    else:
        api_key = cfg["llm"].get("api_key", "")
        client = OpenAI(
            api_key=api_key or "placeholder",
            base_url=cfg["llm"]["base_url"],
            timeout=120.0,
        )
        model = cfg["llm"]["model"]
        return client, model, api_key


def _resolve_llm_async(llm_config: Optional[dict] = None) -> tuple[AsyncOpenAI, str, str]:
    """解析异步 LLM 客户端和模型。

    llm_config 优先，兜底使用 config.yaml / 环境变量。
    返回 (AsyncOpenAI, model, api_key)。
    """
    cfg = get_config()
    if llm_config and llm_config.get("api_key"):
        client = AsyncOpenAI(
            api_key=llm_config["api_key"],
            base_url=llm_config.get("base_url", "https://api.deepseek.com"),
            timeout=120.0,
        )
        model = llm_config.get("model", "deepseek-chat")
        return client, model, llm_config["api_key"]
    else:
        api_key = cfg["llm"].get("api_key", "")
        client = AsyncOpenAI(
            api_key=api_key or "placeholder",
            base_url=cfg["llm"]["base_url"],
            timeout=120.0,
        )
        model = cfg["llm"]["model"]
        return client, model, api_key


def _make_batches(segments: list[str], batch_size: int) -> list[list[str]]:
    """将 segments 按 batch_size 分组。"""
    batches: list[list[str]] = []
    for i in range(0, len(segments), batch_size):
        batches.append(segments[i : i + batch_size])
    return batches


BATCH_DELIMITER = "---SEGMENT---"
TRANSLATION_MAX_WORDS = 500
TRANSLATION_MAX_SPLIT_DEPTH = 5

class TranslationTruncatedError(RuntimeError):
    """模型因输出长度限制截断翻译。"""


class TranslationQualityError(RuntimeError):
    """模型返回了空内容、未翻译原文或未通过语义质量检查。"""


_LONG_ENGLISH_RUN_RE = re.compile(
    r"(?:\b[A-Za-z][A-Za-z0-9'’-]*\b[\s,.;:!?()\[\]\"/\\-]*){20,}"
)


def _validate_translated_part(translated: str) -> None:
    """拒绝空结果和明显整段未翻译的英文，允许产品名与技术缩写。"""
    if not translated.strip():
        raise TranslationQualityError("模型返回了空译文")
    match = _LONG_ENGLISH_RUN_RE.search(translated)
    if match:
        preview = " ".join(match.group(0).split())[:120]
        raise TranslationQualityError(f"译文包含连续未翻译英文: {preview}")


def _normalise_number(token: str) -> str:
    return token.replace(",", "").rstrip("%")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_translation_audit(
    source: str,
    mode: str,
    source_segments: list[str],
    translated_segments: list[str],
) -> dict:
    """生成不包含密钥的逐段完整性证据。"""
    if len(source_segments) != len(translated_segments):
        raise TranslationQualityError(
            f"翻译片段数量不一致: 原文 {len(source_segments)}，译文 {len(translated_segments)}"
        )

    items = []
    for index, (source_segment, translated_segment) in enumerate(
        zip(source_segments, translated_segments), start=1,
    ):
        source_numbers = _numbers_in_text(source_segment)
        translated_numbers = _numbers_in_text(translated_segment)
        missing_numbers = _missing_source_numbers(source_numbers, translated_segment)
        items.append({
            "index": index,
            "source_sha256": _sha256_text(source_segment),
            "source_words": len(source_segment.split()),
            "source_chars": len(source_segment),
            "translation_sha256": _sha256_text(translated_segment),
            "translation_chars": len(translated_segment),
            "source_numbers": sorted(source_numbers),
            "translated_numbers": sorted(translated_numbers),
            "missing_numbers": sorted(missing_numbers),
        })

    source_number_total = sum(len(item["source_numbers"]) for item in items)
    missing_number_total = sum(len(item["missing_numbers"]) for item in items)
    numeric_recall = (
        1 - missing_number_total / source_number_total if source_number_total else 1.0
    )
    quality_status, quality_message = _deterministic_quality(
        mode, len(items), source_number_total, missing_number_total, numeric_recall, items,
    )

    return {
        "version": 2,
        "mode": mode,
        "source_sha256": _sha256_text(source),
        # 与写入 script_zh.txt 的全文一致，供验收脚本校验译文是否被改动
        "translation_sha256": _sha256_text("\n\n".join(translated_segments)),
        "source_segment_count": len(source_segments),
        "translation_segment_count": len(translated_segments),
        "all_segments_present": len(source_segments) == len(translated_segments),
        "numeric_recall": round(numeric_recall, 4),
        "missing_number_total": missing_number_total,
        # 仅确定性检查（零 API 成本）；逐句 LLM 语义审计已按设计移除，
        # 见 docs/solutions/best-practices/avoid-blocking-llm-audit-loops.md
        "quality_status": quality_status,
        "quality_message": quality_message,
        "segments": items,
    }


# 忠实模式下原文数字保留率低于此值时标记为 degraded（不阻塞，只提示）
FAITHFUL_NUMERIC_RECALL_WARN = 0.8


def _deterministic_quality(
    mode: str,
    segment_count: int,
    source_number_total: int,
    missing_number_total: int,
    numeric_recall: float,
    items: list[dict],
) -> tuple[str, str]:
    """根据确定性证据给出忠实模式的质量状态；其他模式不适用。"""
    if mode != "faithful":
        return "not_applicable", ""
    numbers_note = (
        f"原文数字保留 {source_number_total - missing_number_total}/{source_number_total}"
        if source_number_total else "原文无数字"
    )
    if numeric_recall < FAITHFUL_NUMERIC_RECALL_WARN:
        examples = [n for item in items for n in item["missing_numbers"]][:5]
        return "degraded", (
            f"全部 {segment_count} 段均已翻译，但{numbers_note}"
            f"（缺失如 {', '.join(examples)}），建议抽查相关段落"
        )
    return "passed", f"全部 {segment_count} 段均已翻译，{numbers_note}"


def _numbers_in_text(text: str) -> set[str]:
    return {
        _normalise_number(token)
        for token in re.findall(r"\b\d[\d,]*(?:\.\d+)?%?\b", text)
    }


_CHINESE_DIGITS = "零一二三四五六七八九"


def _integer_to_chinese(value: int) -> str:
    """把非负整数转换为常见中文计数写法（覆盖翻译质量门禁所需范围）。"""
    if value == 0:
        return "零"
    if value >= 100_000_000:
        high, low = divmod(value, 100_000_000)
        suffix = _integer_to_chinese(low) if low else ""
        if low and low < 10_000_000:
            suffix = "零" + suffix
        return _integer_to_chinese(high) + "亿" + suffix
    if value >= 10_000:
        high, low = divmod(value, 10_000)
        suffix = _integer_to_chinese(low) if low else ""
        if low and low < 1_000:
            suffix = "零" + suffix
        return _integer_to_chinese(high) + "万" + suffix

    units = ((1000, "千"), (100, "百"), (10, "十"), (1, ""))
    parts: list[str] = []
    remaining = value
    pending_zero = False
    for unit_value, unit_name in units:
        digit, remaining = divmod(remaining, unit_value)
        if digit:
            if pending_zero and parts:
                parts.append("零")
            # 10-19 通常写作“十一”，而不是“一十一”。
            if not (unit_value == 10 and digit == 1 and not parts):
                parts.append(_CHINESE_DIGITS[digit])
            parts.append(unit_name)
            pending_zero = False
        elif parts and remaining:
            pending_zero = True
    return "".join(parts)


def _number_text_variants(token: str) -> set[str]:
    """返回一个阿拉伯数字在中文译文中的等价常见写法。"""
    variants = {token}
    if not token.isdigit():
        return variants
    value = int(token)
    variants.add(_integer_to_chinese(value))
    # 年份、型号等经常逐位读；与计数写法同时接受。
    variants.add("".join(_CHINESE_DIGITS[int(ch)] for ch in token))
    if value and value % 10_000 == 0:
        wan = value // 10_000
        variants.update({f"{wan}万", f"{_integer_to_chinese(wan)}万"})
    return {item for item in variants if item}


def _contains_number_variant(text: str, variant: str) -> bool:
    """匹配完整数字表达，避免把“二十三”中的“十”误认成数字 10。"""
    chinese_number_chars = _CHINESE_DIGITS + "十百千万亿两"
    escaped = re.escape(variant)
    if all(char in chinese_number_chars for char in variant):
        return re.search(
            rf"(?<![{chinese_number_chars}]){escaped}(?![{chinese_number_chars}])",
            text,
        ) is not None
    if variant[0].isdigit():
        return re.search(rf"(?<!\d){escaped}(?!\d)", text) is not None
    return variant in text


def _missing_source_numbers(source_numbers: set[str], translated: str) -> set[str]:
    translated_numbers = _numbers_in_text(translated)
    return {
        token for token in source_numbers
        if token not in translated_numbers
        and not any(
            _contains_number_variant(translated, variant)
            for variant in _number_text_variants(token) - {token}
        )
    }


def _validate_translation_output(source: str, translated: str, mode: str) -> None:
    """执行无需额外 API 的基础格式检查；语义完整性由独立审计判断。"""
    _validate_translated_part(translated)


def _format_batch_source(batch: list[str]) -> str:
    """为批量输入加入不可混淆的边界，避免模型复制或错配片段。"""
    return "\n\n".join(
        f"<SOURCE_SEGMENT_{index}>\n{segment}\n</SOURCE_SEGMENT_{index}>"
        for index, segment in enumerate(batch, start=1)
    )


def _ensure_response_complete(response) -> None:
    """拒绝把被模型截断的响应当作完整译文。"""
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise TranslationTruncatedError("模型输出达到长度上限，译文可能不完整")



def _segment_unit_count(piece: str) -> int:
    words = piece.split()
    if len(words) <= 1 and len(piece) > 10:
        return len(piece) // 2
    return len(words)


def _split_translation_segments(text: str, max_words: int = TRANSLATION_MAX_WORDS) -> list[str]:
    """按单词或汉字上限切分翻译输入，超长单段也必须继续拆分。"""
    if max_words <= 0:
        raise ValueError("max_words must be positive")

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for paragraph in paragraphs:
        words = paragraph.split()
        if not words:
            continue
        # 中文长段落（空格很少但字符多）
        if len(words) <= 1 and len(paragraph) > max_words * 2:
            step = max_words * 2
            for start in range(0, len(paragraph), step):
                pieces.append(paragraph[start:start + step])
        else:
            for start in range(0, len(words), max_words):
                pieces.append(" ".join(words[start:start + max_words]))

    segments: list[str] = []
    current: list[str] = []
    current_words = 0
    for piece in pieces:
        piece_words = _segment_unit_count(piece)
        if current and current_words + piece_words > max_words:
            segments.append("\n\n".join(current))
            current = []
            current_words = 0
        current.append(piece)
        current_words += piece_words
    if current:
        segments.append("\n\n".join(current))
    return segments


def _split_truncated_segment(text: str) -> list[str]:
    """将仍被模型截断的段落二分，供自适应重试使用。优先按段落切分，保持语义完整。"""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) > 1:
        middle = len(paragraphs) // 2
        return ["\n\n".join(paragraphs[:middle]), "\n\n".join(paragraphs[middle:])]
    words = text.split()
    if len(words) > 1:
        middle = len(words) // 2
        return [" ".join(words[:middle]), " ".join(words[middle:])]
    if len(text) > 2:
        middle = len(text) // 2
        return [text[:middle], text[middle:]]
    return []


def _translate_single(
    *,
    client: OpenAI,
    model_name: str,
    prompt_template: str,
    meta_str: str,
    seg: str,
    idx: int,
    total: int,
    cfg: dict,
    retries: int,
    backoff: float,
    translated_segments: list[str],
    on_progress: Optional[Callable[[str], None]] = None,
    _split_depth: int = 0,
) -> None:
    """翻译单个 segment（原始逐段路径复用）。"""
    prompt = prompt_template.replace("{{content}}", seg).replace("{{metadata}}", meta_str)
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "你是一个专业的中文播客编导。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=cfg["llm"]["temperature"],
                max_tokens=cfg["llm"]["max_tokens"],
            )
            _ensure_response_complete(response)
            result = response.choices[0].message.content or ""
            _validate_translated_part(result)
            translated_segments.append(result.strip())
            if on_progress:
                on_progress(f"翻译中... ({idx + 1}/{total})")
            return
        except TranslationTruncatedError as e:
            subsegments = _split_truncated_segment(seg)
            if not subsegments or _split_depth >= TRANSLATION_MAX_SPLIT_DEPTH:
                raise RuntimeError(f"翻译被截断且无法继续拆分: {e}") from e
            logger.warning(
                "Translation segment %d/%d truncated; splitting into %d smaller requests (depth=%d)",
                idx + 1, total, len(subsegments), _split_depth + 1,
            )
            if on_progress:
                on_progress(f"第 {idx + 1}/{total} 段过长，自动拆分重试...")
            for subsegment in subsegments:
                _translate_single(
                    client=client,
                    model_name=model_name,
                    prompt_template=prompt_template,
                    meta_str=meta_str,
                    seg=subsegment,
                    idx=idx,
                    total=total,
                    cfg=cfg,
                    retries=retries,
                    backoff=backoff,
                    translated_segments=translated_segments,
                    on_progress=on_progress,
                    _split_depth=_split_depth + 1,
                )
            return
        except Exception as e:
            logger.warning("Translate attempt %d/%d failed: %s", attempt + 1, retries, e)
            if attempt < retries - 1:
                time.sleep(backoff ** attempt)
            else:
                raise RuntimeError(f"翻译失败，已重试 {retries} 次: {e}")


def _translate_batch(
    *,
    client: OpenAI,
    model_name: str,
    prompt_template: str,
    meta_str: str,
    batch: list[str],
    batch_idx: int,
    total_batches: int,
    cfg: dict,
    retries: int,
    backoff: float,
    on_progress: Optional[Callable[[str], None]] = None,
) -> list[str]:
    """批量翻译一组 segments，返回对应数量的翻译结果。"""
    n = len(batch)
    logger.info("Batch %d/%d: translating %d segments in one call", batch_idx + 1, total_batches, n)

    # 构造批量 prompt
    joined = _format_batch_source(batch)
    batch_instruction = (
        f"将以下 {n} 段英文翻译为中文。每段翻译结果之间用 {BATCH_DELIMITER} 分隔。\n\n"
    )
    prompt = (
        prompt_template.replace("{{content}}", batch_instruction + joined).replace("{{metadata}}", meta_str)
    )

    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "你是一个专业的中文播客编导。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=cfg["llm"]["temperature"],
                max_tokens=cfg["llm"]["max_tokens"],
            )
            _ensure_response_complete(response)
            raw = response.choices[0].message.content or ""
            parts = [p.strip() for p in raw.split(BATCH_DELIMITER)]

            if len(parts) == n:
                try:
                    for part in parts:
                        _validate_translated_part(part)
                except TranslationQualityError as exc:
                    logger.warning(
                        "Batch %d/%d failed quality validation (%s); falling back to sequential",
                        batch_idx + 1, total_batches, exc,
                    )
                    return _fallback_sequential(
                        client=client, model_name=model_name, prompt_template=prompt_template,
                        meta_str=meta_str, batch=batch, batch_idx=batch_idx,
                        total_batches=total_batches, cfg=cfg, retries=retries,
                        backoff=backoff, on_progress=on_progress,
                    )
                if on_progress:
                    on_progress(f"翻译中... (batch {batch_idx + 1}/{total_batches}, {n} 段)")
                return parts

            # 段数不匹配 → 回退逐段翻译
            logger.warning(
                "Batch %d/%d: expected %d segments, got %d — falling back to sequential for this batch",
                batch_idx + 1,
                total_batches,
                n,
                len(parts),
            )
            return _fallback_sequential(
                client=client,
                model_name=model_name,
                prompt_template=prompt_template,
                meta_str=meta_str,
                batch=batch,
                batch_idx=batch_idx,
                total_batches=total_batches,
                cfg=cfg,
                retries=retries,
                backoff=backoff,
                on_progress=on_progress,
            )
        except TranslationTruncatedError:
            logger.warning(
                "Batch %d/%d was truncated; falling back to adaptive sequential translation",
                batch_idx + 1, total_batches,
            )
            return _fallback_sequential(
                client=client,
                model_name=model_name,
                prompt_template=prompt_template,
                meta_str=meta_str,
                batch=batch,
                batch_idx=batch_idx,
                total_batches=total_batches,
                cfg=cfg,
                retries=retries,
                backoff=backoff,
                on_progress=on_progress,
            )
        except Exception as e:
            logger.warning(
                "Batch %d/%d attempt %d/%d failed: %s",
                batch_idx + 1, total_batches, attempt + 1, retries, e,
            )
            if attempt < retries - 1:
                time.sleep(backoff ** attempt)
            else:
                raise RuntimeError(f"批量翻译失败，已重试 {retries} 次: {e}")

    return []  # unreachable, 但让类型检查器满意


def _fallback_sequential(
    *,
    client: OpenAI,
    model_name: str,
    prompt_template: str,
    meta_str: str,
    batch: list[str],
    batch_idx: int,
    total_batches: int,
    cfg: dict,
    retries: int,
    backoff: float,
    on_progress: Optional[Callable[[str], None]] = None,
) -> list[str]:
    """逐段翻译回退路径，用于批量段数不匹配时。"""
    results: list[str] = []
    for i, seg in enumerate(batch):
        _translate_single(
            client=client,
            model_name=model_name,
            prompt_template=prompt_template,
            meta_str=meta_str,
            seg=seg,
            idx=i,
            total=len(batch),
            cfg=cfg,
            retries=retries,
            backoff=backoff,
            translated_segments=results,
            on_progress=on_progress,
        )
    return results


# ---------------------------------------------------------------------------
# 异步翻译路径
# ---------------------------------------------------------------------------

class TokenBucket:
    """异步 token-bucket 限流器。"""

    def __init__(self, rate: float, burst: Optional[int] = None) -> None:
        self.rate = rate  # tokens per second
        self.burst = burst or int(rate * 2)
        self._tokens = float(self.burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """获取一个令牌，若桶空则等待补充。"""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._updated
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
            self._updated = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self.rate
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


async def _translate_single_async(
    *,
    client: AsyncOpenAI,
    model_name: str,
    prompt_template: str,
    meta_str: str,
    seg: str,
    idx: int,
    total: int,
    cfg: dict,
    retries: int,
    backoff: float,
    cancel_event: Optional[asyncio.Event] = None,
    _split_depth: int = 0,
) -> str:
    """异步翻译单个 segment。"""
    prompt = prompt_template.replace("{{content}}", seg).replace("{{metadata}}", meta_str)
    for attempt in range(retries):
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError("翻译被用户取消")
        try:
            response = await client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "你是一个专业的中文播客编导。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=cfg["llm"]["temperature"],
                max_tokens=cfg["llm"]["max_tokens"],
            )
            _ensure_response_complete(response)
            result = (response.choices[0].message.content or "").strip()
            _validate_translated_part(result)
            return result
        except TranslationTruncatedError as e:
            subsegments = _split_truncated_segment(seg)
            if not subsegments or _split_depth >= TRANSLATION_MAX_SPLIT_DEPTH:
                raise RuntimeError(f"翻译被截断且无法继续拆分: {e}") from e
            logger.warning(
                "Translation segment %d/%d truncated; splitting into %d smaller requests (depth=%d)",
                idx + 1, total, len(subsegments), _split_depth + 1,
            )
            results = []
            for subsegment in subsegments:
                result = await _translate_single_async(
                    client=client, model_name=model_name, prompt_template=prompt_template,
                    meta_str=meta_str, seg=subsegment, idx=idx, total=total,
                    cfg=cfg, retries=retries, backoff=backoff,
                    cancel_event=cancel_event, _split_depth=_split_depth + 1,
                )
                results.append(result)
            return "\n".join(results)
        except Exception as e:
            logger.warning("Translate attempt %d/%d failed: %s", attempt + 1, retries, e)
            if attempt < retries - 1:
                jitter = random.uniform(0.5, 1.5)
                await asyncio.sleep(backoff ** attempt * jitter)
            else:
                raise RuntimeError(f"翻译失败，已重试 {retries} 次: {e}")
    return ""  # unreachable


async def _translate_batch_async(
    *,
    client: AsyncOpenAI,
    model_name: str,
    prompt_template: str,
    meta_str: str,
    batch: list[str],
    batch_idx: int,
    total_batches: int,
    cfg: dict,
    retries: int,
    backoff: float,
    cancel_event: Optional[asyncio.Event] = None,
) -> list[str]:
    """异步批量翻译一组 segments。"""
    n = len(batch)
    logger.info("Async batch %d/%d: translating %d segments in one call", batch_idx + 1, total_batches, n)

    joined = _format_batch_source(batch)
    batch_instruction = (
        f"下面共有 {n} 个带编号边界的英文片段。逐段处理且不得遗漏、复制或交换。"
        f"输出必须正好包含 {n} 段，各段之间只用 {BATCH_DELIMITER} 分隔。\n\n"
    ) if n > 1 else ""
    content = batch[0] if n == 1 else batch_instruction + joined
    prompt = (
        prompt_template.replace("{{content}}", content).replace("{{metadata}}", meta_str)
    )

    for attempt in range(retries):
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError("翻译被用户取消")
        try:
            response = await client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "你是一个专业的中文播客编导。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=cfg["llm"]["temperature"],
                max_tokens=cfg["llm"]["max_tokens"],
            )
            _ensure_response_complete(response)
            raw = response.choices[0].message.content or ""
            parts = [p.strip() for p in raw.split(BATCH_DELIMITER)]

            if len(parts) == n:
                try:
                    for part in parts:
                        _validate_translated_part(part)
                except TranslationQualityError as exc:
                    logger.warning(
                        "Async batch %d/%d failed quality validation (%s); falling back to sequential",
                        batch_idx + 1, total_batches, exc,
                    )
                else:
                    return parts

            logger.warning(
                "Async batch %d/%d: expected %d segments, got %d — falling back to sequential",
                batch_idx + 1, total_batches, n, len(parts),
            )
            return await _fallback_sequential_async(
                client=client, model_name=model_name, prompt_template=prompt_template,
                meta_str=meta_str, batch=batch, batch_idx=batch_idx, total_batches=total_batches,
                cfg=cfg, retries=retries, backoff=backoff, cancel_event=cancel_event,
            )
        except TranslationTruncatedError:
            logger.warning(
                "Async batch %d/%d was truncated; falling back to sequential",
                batch_idx + 1, total_batches,
            )
            return await _fallback_sequential_async(
                client=client, model_name=model_name, prompt_template=prompt_template,
                meta_str=meta_str, batch=batch, batch_idx=batch_idx, total_batches=total_batches,
                cfg=cfg, retries=retries, backoff=backoff, cancel_event=cancel_event,
            )
        except Exception as e:
            logger.warning(
                "Async batch %d/%d attempt %d/%d failed: %s",
                batch_idx + 1, total_batches, attempt + 1, retries, e,
            )
            if attempt < retries - 1:
                jitter = random.uniform(0.5, 1.5)
                await asyncio.sleep(backoff ** attempt * jitter)
            else:
                raise RuntimeError(f"批量翻译失败，已重试 {retries} 次: {e}")

    return []  # unreachable


async def _fallback_sequential_async(
    *,
    client: AsyncOpenAI,
    model_name: str,
    prompt_template: str,
    meta_str: str,
    batch: list[str],
    batch_idx: int,
    total_batches: int,
    cfg: dict,
    retries: int,
    backoff: float,
    cancel_event: Optional[asyncio.Event] = None,
) -> list[str]:
    """异步逐段翻译回退路径。"""
    results: list[str] = []
    for i, seg in enumerate(batch):
        result = await _translate_single_async(
            client=client, model_name=model_name, prompt_template=prompt_template,
            meta_str=meta_str, seg=seg, idx=i, total=len(batch),
            cfg=cfg, retries=retries, backoff=backoff, cancel_event=cancel_event,
        )
        results.append(result)
    return results


def _parse_json_response(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        chunks = text.split("```")
        text = chunks[1] if len(chunks) > 1 else text
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    data = json.loads(text.strip())
    if not isinstance(data, dict):
        raise ValueError("语义审计响应不是 JSON 对象")
    return data







async def translate_async(
    text: str,
    mode: str = "podcast",
    metadata: Optional[dict] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    llm_config: Optional[dict] = None,
    batch_size: int = 1,
    max_concurrency: int = 10,
    rate_limit_rpm: int = 120,
    cancel_event: Optional[asyncio.Event] = None,
    on_audit: Optional[Callable[[dict], None]] = None,
    source_language: str = "en",
) -> str:
    """异步并发翻译/重写入口。

    所有翻译批次通过 asyncio.Semaphore + TokenBucket 并发调度。
    失败批次独立重试，成功批次保留不浪费。

    Args:
        text: 清洗后的原文文本（英文或中文）
        mode: 输出模式 (faithful | podcast | condensed)
        metadata: 视频元数据 dict
        on_progress: 进度回调（在 async 上下文中同步调用）
        llm_config: 动态 LLM 配置
        batch_size: 每批合并的 segment 数
        max_concurrency: 最大并发 API 调用数
        rate_limit_rpm: token-bucket 速率限制（每分钟请求数）
        cancel_event: 取消信号
        on_audit: 审计回调
        source_language: 源语言代码（如 'en', 'zh'）

    Returns:
        中文播客稿文本
    """
    cfg = get_config()
    client, model_name, api_key = _resolve_llm_async(llm_config)
    if llm_config and "max_tokens" in llm_config:
        cfg["llm"]["max_tokens"] = llm_config["max_tokens"]

    if not api_key or api_key == "placeholder":
        logger.error("No LLM API key configured — refusing to create an English fallback artifact")
        if on_progress:
            on_progress("❌ 未配置翻译 API Key，已停止，避免把英文原文误标为中文成品")
        raise RuntimeError(
            "未配置翻译 API Key。请在页面填写并保存配置后重试；"
            "系统不会再把英文原文当作中文音频生成。"
        )

    if source_language == "zh":
        if mode == "condensed":
            prompt_file = "rewrite_chinese_condensed.txt"
        else:
            prompt_file = "rewrite_chinese_podcast.txt"
    else:
        prompt_file = MODE_PROMPTS.get(mode, MODE_PROMPTS["podcast"])
    prompt_template = _load_prompt(prompt_file)

    role_str = (cfg.get("llm") or {}).get("translator_role", "")
    if role_str:
        prompt_template = prompt_template.replace("{{role}}", role_str)

    meta_str = ""
    if metadata:
        meta_str = f"\n视频标题: {metadata.get('title', '')}\n频道: {metadata.get('channel', '')}\n"

    glossary_str = _load_glossary()
    if glossary_str:
        meta_str = meta_str + "\n" + glossary_str + "\n"

    # 忠实版和播客版使用短片段并发处理；浓缩版需要看到全局上下文，
    # 在常见视频长度下合并为单次请求，避免每个批次重复开场和总结。
    translation_cfg = cfg.get("translation", {})
    faithful_max_words = int(
        translation_cfg.get("faithful_max_words", TRANSLATION_MAX_WORDS)
    )
    segment_words = 6000 if mode == "condensed" else faithful_max_words
    segments = _split_translation_segments(text, max_words=segment_words)
    total = len(segments)

    def emit_phase(phase: str, label: str, current: int = 0, phase_total: int = 0) -> None:
        if on_progress:
            on_progress({
                "type": "translation_phase", "phase": phase, "label": label,
                "current": current, "total": phase_total,
            })

    retries = cfg["processing"]["retry_times"]
    backoff = cfg["processing"]["retry_backoff_base"]

    # 使用 config 中的值覆盖默认值（如果存在）
    t_cfg = translation_cfg
    if t_cfg:
        max_concurrency = t_cfg.get("max_concurrency", max_concurrency)
        rate_limit_rpm = t_cfg.get("rate_limit_rpm", rate_limit_rpm)

    if on_progress:
        on_progress(f"分段翻译中（共 {total} 段，并发 {max_concurrency}）...")
    emit_phase("translate", "分段翻译", 0, total)

    if batch_size > 1:
        batches = _make_batches(segments, batch_size)
    else:
        batches = [[s] for s in segments]

    sem = asyncio.Semaphore(max_concurrency)
    limiter = TokenBucket(rate=rate_limit_rpm / 60.0)

    completed_batches = 0
    async def translate_one(batch: list[str], idx: int) -> list[str]:
        nonlocal completed_batches
        async with sem:
            await limiter.acquire()
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("翻译被用户取消")
            result = await _translate_batch_async(
                client=client, model_name=model_name, prompt_template=prompt_template,
                meta_str=meta_str, batch=batch, batch_idx=idx, total_batches=len(batches),
                cfg=cfg, retries=retries, backoff=backoff, cancel_event=cancel_event,
            )
            if on_progress:
                on_progress(f"翻译中... (batch {idx + 1}/{len(batches)}, {len(batch)} 段)")
            completed_batches += 1
            emit_phase("translate", "分段翻译", completed_batches, len(batches))
            return result

    tasks = [translate_one(batch, i) for i, batch in enumerate(batches)]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    # 收集结果。完整性优先：任一批次失败都必须让整个模式失败，禁止把缺段稿
    # 写入数据库并继续 TTS。
    translated_segments: list[str] = []
    failures: list[str] = []
    for i, result in enumerate(gathered):
        if isinstance(result, Exception):
            failures.append(f"{i + 1}/{len(batches)}: {result}")
            logger.error("Batch %d/%d failed (isolated): %s", i + 1, len(batches), result)
        elif isinstance(result, list):
            translated_segments.extend(result)
        elif result is None:
            failures.append(f"{i + 1}/{len(batches)}: 返回空结果")
            logger.error("Batch %d/%d returned None", i + 1, len(batches))

    if failures:
        raise RuntimeError(
            f"翻译不完整：{len(failures)}/{len(batches)} 个批次失败；"
            f"未生成音频。首个错误: {failures[0]}"
        )

    if not translated_segments:
        raise RuntimeError("翻译未返回任何有效内容")

    if mode == "faithful":
        emit_phase("deterministic_gate", "完整性检查", 0, len(segments))
        if len(segments) != len(translated_segments):
            raise TranslationQualityError(
                f"忠实翻译片段数量不一致: 原文 {len(segments)}，译文 {len(translated_segments)}"
            )
        emit_phase("deterministic_gate", "完整性检查", len(segments), len(segments))

    translated_text = "\n\n".join(translated_segments)
    _validate_translation_output(text, translated_text, mode)
    audit = _build_translation_audit(text, mode, segments, translated_segments)
    if on_audit:
        on_audit(audit)
    return translated_text


def translate(
    text: str,
    mode: str = "podcast",
    metadata: Optional[dict] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    llm_config: Optional[dict] = None,
    batch_size: int = 1,
    source_language: str = "en",
) -> str:
    """同步兼容包装——内部调用 translate_async。"""
    return asyncio.run(translate_async(
        text=text, mode=mode, metadata=metadata,
        on_progress=on_progress, llm_config=llm_config,
        batch_size=batch_size, source_language=source_language,
    ))


def summarize(
    script_zh: str,
    metadata: Optional[dict] = None,
    llm_config: Optional[dict] = None,
) -> dict:
    """生成节目摘要和关键观点。

    Returns:
        {"title_zh": str, "summary": str, "key_points": [str, ...]}
    """
    cfg = get_config()
    client, model_name, api_key = _resolve_llm(llm_config)

    if not api_key or api_key == "placeholder":
        logger.warning("No LLM API key configured — skipping summary generation")
        return {"title_zh": metadata.get("title", "") if metadata else "", "summary": "(未翻译)", "key_points": []}

    prompt_template = _load_prompt("summary.txt")

    meta_str = ""
    if metadata:
        meta_str = f"\n原标题: {metadata.get('title', '')}\n频道: {metadata.get('channel', '')}\n"

    excerpt = script_zh[:3000]
    prompt = prompt_template.replace("{{content}}", excerpt).replace("{{metadata}}", meta_str)

    retries = cfg["processing"]["retry_times"]
    backoff = cfg["processing"]["retry_backoff_base"]

    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "你是一个专业的中文播客编辑。请只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=1024,
            )
            result_text = response.choices[0].message.content or "{}"
            result_text = result_text.strip()
            if result_text.startswith("```"):
                result_text = result_text.split("```")[1]
                if result_text.startswith("json"):
                    result_text = result_text[4:]
            return json.loads(result_text.strip())
        except json.JSONDecodeError:
            logger.warning("Summarize output not valid JSON, retrying...")
            if attempt < retries - 1:
                time.sleep(backoff ** attempt)
            else:
                return {"title_zh": metadata.get("title", "") if metadata else "", "summary": "", "key_points": []}
        except Exception as e:
            logger.warning("Summarize attempt %d/%d failed: %s", attempt + 1, retries, e)
            if attempt < retries - 1:
                time.sleep(backoff ** attempt)
            else:
                raise RuntimeError(f"摘要生成失败，已重试 {retries} 次: {e}")

    return {"title_zh": "", "summary": "", "key_points": []}


def _load_glossary() -> str:
    """加载术语表并格式化为 prompt 注入字符串。
    返回 "术语表:\\n- term_en → term_zh\\n- ..." 或空字符串。
    """
    from src.storage import db

    db.init_db()
    terms = db.get_glossary(limit=30)
    if not terms:
        return ""
    lines = ["术语表（请优先使用以下译法）:"]
    for t in terms:
        zh = t.get("term_zh", "")
        if zh:
            lines.append(f"- {t['term_en']} → {zh}")
        else:
            lines.append(f"- {t['term_en']}")
    return "\n".join(lines)


def _extract_terms(script_zh_path: str) -> None:
    """从中文译文中启发式提取术语并写入 glossary。

    扫描中文文本中的英文词汇（专有名词、技术缩写、混合大小写词），
    统计词频，出现 ≥3 次的术语自动 upsert 到 glossary 表。
    """
    from src.storage import db

    path = Path(script_zh_path)
    if not path.exists():
        logger.warning("_extract_terms: file not found %s", script_zh_path)
        return

    text = path.read_text(encoding="utf-8")

    # 提取英文术语候选
    # Pattern 1: 多词大写短语 (如 "Machine Learning", "Retrieval-Augmented Generation")
    pattern_cap_phrase = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
    # Pattern 2: 技术缩写 (如 RAG, LLM, API, GPU)
    pattern_abbrev = re.compile(r"\b([A-Z]{2,}(?:s)?)\b")
    # Pattern 3: 混合大小写词 (如 arXiv, iOS, ChatGPT)
    pattern_mixed = re.compile(r"\b([A-Z][a-zA-Z]*[a-z][A-Z][a-zA-Z]*)\b")
    # Pattern 4: 英文词后跟中文括号注释: "word（中文）" → term_en=word, term_zh=中文
    pattern_zh_gloss = re.compile(r"([A-Za-z][A-Za-z0-9\s\-+]{1,40}?)[（(]([^）)]+)[）)]")

    candidates: list[str] = []
    candidates.extend(pattern_cap_phrase.findall(text))
    candidates.extend(pattern_abbrev.findall(text))
    candidates.extend(pattern_mixed.findall(text))

    # 从括号注释中提取明确的术语-译文对，直接 upsert
    source_video_id = path.parent.name
    db.init_db()
    for match in pattern_zh_gloss.finditer(text):
        en_term = match.group(1).strip()
        zh_term = match.group(2).strip()
        if len(en_term) > 1 and len(zh_term) > 0:
            db.upsert_glossary_term(en_term, zh_term, source_video_id)
            logger.info("Glossary (explicit): '%s' -> '%s'", en_term, zh_term)
            candidates.append(en_term)

    # 过滤常见非术语词
    stop_words = {
        "The", "This", "That", "But", "And", "However", "Therefore",
        "There", "These", "Those", "Then", "Also", "Here", "Just",
        "Very", "Really", "Still", "Already", "Always", "Never",
    }
    candidates = [c for c in candidates if c not in stop_words and len(c) > 1]

    # 统计词频
    freq = Counter(candidates)

    # Upsert 出现 ≥3 次的术语
    for term, count in freq.items():
        if count >= 3:
            term_zh = _find_chinese_context(text, term)
            db.upsert_glossary_term(term, term_zh, source_video_id)
            logger.info("Glossary (auto): '%s' -> '%s' (%d occurrences)", term, term_zh, count)


def _quality_check(
    source_text: str,
    translated_text: str,
    metadata: Optional[dict] = None,
    llm_config: Optional[dict] = None,
) -> Optional[dict]:
    """翻译后质量自检：对比原文与译文中的关键信息点保留情况。

    非阻塞：任何失败都只记录 warning，永不抛出异常。

    Args:
        source_text: 原始英文清洗文本
        translated_text: 翻译后的中文文本
        metadata: 视频元数据 dict（title, channel 等）
        llm_config: 动态 LLM 配置，为 None 时使用 config.yaml 默认值

    Returns:
        {"passed": int, "total": int, "points": [...]} 或 None（检查失败时）
    """
    try:
        client, model_name, api_key = _resolve_llm(llm_config)

        if not api_key or api_key == "placeholder":
            logger.warning("Quality check skipped: no LLM API key configured")
            return None

        # 截断文本以控制 token 用量（质量检查只需采样）
        source_sample = source_text[:6000]
        translated_sample = translated_text[:6000]

        prompt = (
            "列出原文中的 5-10 个关键信息点（人名、数字、观点、案例），"
            "逐条检查译文中是否保留。以 JSON 对象格式输出：\n"
            '{"points": [{"point": "信息点描述", "present": true, "note": "备注"}]}'
            f"\n\n原文:\n{source_sample}\n\n译文:\n{translated_sample}"
        )

        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "你是一个专业的中文翻译质量审核员。请只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=512,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content or "{}"
        raw = raw.strip()

        # 解析 JSON 响应
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Quality check: unparseable JSON response: %s", raw[:200])
            return None

        # 提取 points 列表
        if isinstance(data, list):
            points = data
        elif isinstance(data, dict):
            points = data.get("points", [])
            if not points:
                # 尝试其他常见键名或 dict 值中的列表
                for v in data.values():
                    if isinstance(v, list):
                        points = v
                        break
        else:
            points = []

        if not points:
            logger.warning("Quality check: no points found in response")
            return None

        passed = sum(1 for p in points if p.get("present", False))
        total = len(points)

        if total == 0:
            return None

        if passed < total:
            for p in points:
                if not p.get("present", False):
                    point_text = p.get("point", "?")
                    note = p.get("note", "")
                    logger.warning(
                        "⚠ 质量告警: 缺少关键信息 '%s'%s",
                        point_text,
                        f" — {note}" if note else "",
                    )
            logger.warning("⚠ 质量告警: %d/%d 项通过", passed, total)
        else:
            logger.info("质量检查通过 (%d/%d)", passed, total)

        return {"passed": passed, "total": total, "points": points}

    except Exception as e:
        logger.warning("Quality check failed (non-fatal): %s", e)
        return None


def _find_chinese_context(text: str, term_en: str) -> str:
    """在文本中查找英文术语附近的汉语上下文，作为翻译候选。"""
    idx = text.find(term_en)
    if idx == -1:
        return ""

    # 取术语前后各 20 个字符作为上下文窗口
    start = max(0, idx - 20)
    end = min(len(text), idx + len(term_en) + 20)
    context = text[start:end]

    # 提取窗口中的中文字符序列
    chinese_chars = re.findall(r"[一-鿿]+", context)
    if chinese_chars:
        # 返回最长的中文片段作为翻译候选
        return max(chinese_chars, key=len)

    return ""
