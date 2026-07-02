"""LLM 翻译模块。支持动态 API 凭证和模型选择。"""

import json
import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

from openai import OpenAI

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
        )
        model = llm_config.get("model", "deepseek-chat")
        return client, model, llm_config["api_key"]
    else:
        api_key = cfg["llm"].get("api_key", "")
        client = OpenAI(
            api_key=api_key or "placeholder",
            base_url=cfg["llm"]["base_url"],
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
            result = response.choices[0].message.content or ""
            translated_segments.append(result.strip())
            if on_progress:
                on_progress(f"翻译中... ({idx + 1}/{total})")
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
    joined = "\n\n".join(batch)
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
            raw = response.choices[0].message.content or ""
            parts = [p.strip() for p in raw.split(BATCH_DELIMITER)]

            if len(parts) == n:
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
                result_text = response.choices[0].message.content or ""
                results.append(result_text.strip())
                if on_progress:
                    on_progress(
                        f"翻译中... (batch {batch_idx + 1}/{total_batches}, "
                        f"fallback {i + 1}/{len(batch)})"
                    )
                break
            except Exception as e:
                logger.warning(
                    "Batch %d/%d fallback segment %d attempt %d/%d failed: %s",
                    batch_idx + 1, total_batches, i + 1, attempt + 1, retries, e,
                )
                if attempt < retries - 1:
                    time.sleep(backoff ** attempt)
                else:
                    raise RuntimeError(f"翻译失败（回退路径），已重试 {retries} 次: {e}")
    return results


def translate(
    text: str,
    mode: str = "podcast",
    metadata: Optional[dict] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    llm_config: Optional[dict] = None,
    batch_size: int = 1,
) -> str:
    """将英文文本翻译/改写为中文播客稿。

    Args:
        text: 清洗后的英文文本
        mode: 输出模式 (faithful | podcast | condensed)
        metadata: 视频元数据 dict（title, channel 等）
        on_progress: 进度回调
        llm_config: 动态 LLM 配置 {"api_key": "...", "base_url": "...", "model": "..."}
        batch_size: 批量翻译的段数（默认 1 = 逐段翻译，>1 时合并多个 segment 到一次 API 调用）

    Returns:
        中文播客稿文本（如无 API Key 则返回英文原文 + 提示）
    """
    cfg = get_config()
    client, model_name, api_key = _resolve_llm(llm_config)

    if not api_key or api_key == "placeholder":
        logger.warning("No LLM API key configured — skipping translation, using English text directly")
        if on_progress:
            on_progress("⚠️ 未配置 API Key，跳过翻译，使用英文原文...")
        return (
            "# 注意：以下为英文原文（未翻译，因未配置 LLM API Key）\n"
            "# 请设置环境变量 VIDEO2LISTENER_DEEPSEEK_API_KEY 后重试以获取中文版\n\n"
            + text
        )

    prompt_file = MODE_PROMPTS.get(mode, MODE_PROMPTS["podcast"])
    prompt_template = _load_prompt(prompt_file)

    meta_str = ""
    if metadata:
        meta_str = f"\n视频标题: {metadata.get('title', '')}\n频道: {metadata.get('channel', '')}\n"

    # 注入术语表
    glossary_str = _load_glossary()
    if glossary_str:
        meta_str = meta_str + "\n" + glossary_str + "\n"

    # 分段：按段落边界，每段 < 4000 tokens（估算 ~3000 英文词）
    paragraphs = text.split("\n\n")
    segments: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para.split())
        if current_len + para_len > 3000 and current:
            segments.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += para_len

    if current:
        segments.append("\n\n".join(current))

    if on_progress:
        on_progress(f"分段翻译中（共 {len(segments)} 段）...")

    translated_segments: list[str] = []
    retries = cfg["processing"]["retry_times"]
    backoff = cfg["processing"]["retry_backoff_base"]

    if batch_size > 1:
        # --- 批量翻译路径 ---
        batches = _make_batches(segments, batch_size)
        for batch_idx, batch in enumerate(batches):
            batch_result = _translate_batch(
                client=client,
                model_name=model_name,
                prompt_template=prompt_template,
                meta_str=meta_str,
                batch=batch,
                batch_idx=batch_idx,
                total_batches=len(batches),
                cfg=cfg,
                retries=retries,
                backoff=backoff,
                on_progress=on_progress,
            )
            translated_segments.extend(batch_result)
    else:
        # --- 逐段翻译路径（原始行为，batch_size=1）---
        for i, seg in enumerate(segments):
            _translate_single(
                client=client,
                model_name=model_name,
                prompt_template=prompt_template,
                meta_str=meta_str,
                seg=seg,
                idx=i,
                total=len(segments),
                cfg=cfg,
                retries=retries,
                backoff=backoff,
                translated_segments=translated_segments,
                on_progress=on_progress,
            )

    return "\n\n".join(translated_segments)


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
