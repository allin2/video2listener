"""U4 测试。翻译和摘要模块。"""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from src.translation.client import (
    MODE_PROMPTS,
    TranslationQualityError,
    TranslationTruncatedError,
    _build_translation_audit,
    _ensure_response_complete,
    _format_batch_source,
    _load_prompt,
    _split_translation_segments,
    _translate_single,
    _validate_translated_part,
    _validate_translation_output,
    translate_async,
)


def test_all_prompt_files_exist():
    """验证所有三种模式的 prompt 文件存在。"""
    for mode, filename in MODE_PROMPTS.items():
        prompt = _load_prompt(filename)
        assert len(prompt) > 0, f"Prompt for {mode} is empty"
        assert "{{content}}" in prompt, f"Prompt for {mode} missing content placeholder"


def test_summary_prompt_exists():
    """验证摘要 prompt 文件存在。"""
    prompt = _load_prompt("summary.txt")
    assert len(prompt) > 0
    assert "{{content}}" in prompt


def test_prompts_place_examples_before_content():
    """验证翻译提示词中示例位于正文之前，避免模型把结尾示例当成待续写内容。"""
    for mode, filename in MODE_PROMPTS.items():
        prompt = _load_prompt(filename)
        if "## 参考示例" in prompt:
            ex_pos = prompt.index("## 参考示例")
            content_pos = prompt.index("{{content}}")
            assert ex_pos < content_pos, f"In {filename}, examples should precede target content"


def test_split_truncated_segment_prefers_paragraphs():
    from src.translation.client import _split_truncated_segment
    text = "Paragraph 1\n\nParagraph 2\n\nParagraph 3\n\nParagraph 4"
    parts = _split_truncated_segment(text)
    assert len(parts) == 2
    assert parts[0] == "Paragraph 1\n\nParagraph 2"
    assert parts[1] == "Paragraph 3\n\nParagraph 4"



def test_prompt_contains_keywords():
    """忠实模式以核心语义和关键信息为目标，不要求逐字逐句复刻。"""
    prompt = _load_prompt("translate_faithful.txt")
    assert "核心语义" in prompt
    assert "关键信息" in prompt
    assert "逐句覆盖输入中的全部信息" not in prompt


def test_single_oversized_paragraph_is_split_without_losing_words():
    words = [f"word{i}" for i in range(3501)]
    segments = _split_translation_segments(" ".join(words), max_words=1600)

    assert [len(segment.split()) for segment in segments] == [1600, 1600, 301]
    assert " ".join(segments).split() == words


def test_translation_rejects_length_truncated_response():
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length")])

    with pytest.raises(TranslationTruncatedError):
        _ensure_response_complete(response)


def test_truncated_translation_is_automatically_split_and_retried():
    class FakeCompletions:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            finish_reason = "length" if self.calls == 1 else "stop"
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=f"译文{self.calls}"),
            )])

    completions = FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    translated = []

    _translate_single(
        client=client,
        model_name="test-model",
        prompt_template="{{metadata}}\n{{content}}",
        meta_str="",
        seg="one two three four",
        idx=0,
        total=1,
        cfg={"llm": {"temperature": 0, "max_tokens": 16}},
        retries=1,
        backoff=1,
        translated_segments=translated,
    )

    assert completions.calls == 3
    assert translated == ["译文2", "译文3"]


def test_batch_source_has_explicit_numbered_boundaries():
    formatted = _format_batch_source(["first", "second"])

    assert "<SOURCE_SEGMENT_1>\nfirst\n</SOURCE_SEGMENT_1>" in formatted
    assert "<SOURCE_SEGMENT_2>\nsecond\n</SOURCE_SEGMENT_2>" in formatted


def test_translation_rejects_long_untranslated_english_run():
    untranslated = " ".join(f"english{i}" for i in range(25))

    with pytest.raises(TranslationQualityError, match="未翻译英文"):
        _validate_translated_part(f"开头中文。{untranslated}结尾中文。")


def test_faithful_translation_allows_incidental_number_omission():
    _validate_translation_output(
        "It made this 11 second animation to illustrate the main idea.",
        "它用一段动画说明了核心观点。",
        "faithful",
    )


def test_faithful_translation_accepts_equivalent_chinese_numbers():
    _validate_translation_output(
        "The animation lasts 11 seconds and has nearly 50,000 stars.",
        "动画持续十一秒，并且已经获得近五万颗星。",
        "faithful",
    )


def test_translation_audit_accepts_equivalent_chinese_numbers():
    audit = _build_translation_audit(
        "It has 11 users and 50,000 stars.",
        "faithful",
        ["It has 11 users and 50,000 stars."],
        ["它有十一位用户和近五万颗星。"],
    )

    assert audit["segments"][0]["missing_numbers"] == []


def test_translation_audit_records_missing_numbers_as_evidence_only():
    audit = _build_translation_audit(
        "It made this 11 second animation to illustrate the main idea.",
        "faithful",
        ["It made this 11 second animation to illustrate the main idea."],
        ["它用一段动画说明了核心观点。"],
    )

    assert audit["segments"][0]["missing_numbers"] == ["11"]


def test_translate_without_key_fails_instead_of_returning_english(monkeypatch):
    monkeypatch.setattr(
        "src.translation.client._resolve_llm_async",
        lambda _config: (object(), "test-model", ""),
    )

    with pytest.raises(RuntimeError, match="未配置翻译 API Key"):
        asyncio.run(translate_async("English source", mode="faithful"))


def test_translate_fails_when_any_batch_is_missing(monkeypatch):
    monkeypatch.setattr(
        "src.translation.client._resolve_llm_async",
        lambda _config: (object(), "test-model", "test-key"),
    )
    monkeypatch.setattr("src.translation.client._load_prompt", lambda _filename: "{{content}}")
    monkeypatch.setattr("src.translation.client._load_glossary", lambda: "")
    monkeypatch.setattr(
        "src.translation.client._split_translation_segments",
        lambda _text, max_words: ["first batch", "second batch"],
    )

    async def fake_batch(**kwargs):
        if kwargs["batch_idx"] == 1:
            raise RuntimeError("simulated outage")
        return ["第一批译文"]

    monkeypatch.setattr("src.translation.client._translate_batch_async", fake_batch)

    with pytest.raises(RuntimeError, match="翻译不完整：1/2 个批次失败"):
        asyncio.run(translate_async("source", mode="podcast", batch_size=1))


def test_translation_audit_maps_every_source_segment_to_output():
    audit = _build_translation_audit(
        "first 23\n\nsecond 8",
        "faithful",
        ["first 23", "second 8"],
        ["第一段 23", "第二段 8"],
    )

    assert audit["all_segments_present"] is True
    assert audit["source_segment_count"] == 2
    assert audit["translation_segment_count"] == 2
    assert audit["segments"][0]["missing_numbers"] == []
    assert audit["segments"][1]["missing_numbers"] == []


def test_translation_audit_rejects_segment_count_mismatch():
    with pytest.raises(TranslationQualityError, match="翻译片段数量不一致"):
        _build_translation_audit("source", "faithful", ["one", "two"], ["一个"])




def test_faithful_audit_reports_deterministic_quality_status():
    audit = _build_translation_audit(
        "It has 11 users.\n\nIt has 50,000 stars.",
        "faithful",
        ["It has 11 users.", "It has 50,000 stars."],
        ["它有十一位用户。", "它有五万颗星。"],
    )

    assert audit["quality_status"] == "passed"
    assert audit["numeric_recall"] == 1.0
    assert "全部 2 段均已翻译" in audit["quality_message"]
    assert audit["translation_sha256"] == hashlib.sha256(
        "它有十一位用户。\n\n它有五万颗星。".encode("utf-8")
    ).hexdigest()


def test_faithful_audit_degrades_when_many_numbers_missing():
    audit = _build_translation_audit(
        "Numbers 11, 22 and 33.",
        "faithful",
        ["Numbers 11, 22 and 33."],
        ["一些数字。"],
    )

    assert audit["quality_status"] == "degraded"
    assert "11" in audit["quality_message"]


def test_non_faithful_audit_quality_is_not_applicable():
    audit = _build_translation_audit("Numbers 11.", "condensed", ["Numbers 11."], ["数字。"])

    assert audit["quality_status"] == "not_applicable"


def test_chinese_units_count_han_chars_not_space_separated_phrases():
    from src.translation.client import _segment_unit_count

    # Whisper 中文转写在短语间加空格：3 个短语 12 个汉字应折算为 6，而非 3
    assert _segment_unit_count("今天我们 聊一聊笔记 工具") == 5 + 1  # 11 汉字 → 6
    assert _segment_unit_count("用 GPT4 写代码") == 1 + 1 + 2
    assert _segment_unit_count("don't stop 50,000 times") == 4


def test_long_chinese_transcript_is_split_for_parallel_condensing():
    phrase = "这是一个用于测试切分的中文短语"  # 15 汉字 → 8 单位
    text = " ".join([phrase] * 600)  # 4800 单位
    segments = _split_translation_segments(text, max_words=1500)

    assert len(segments) == 4
    assert "".join(segments).replace(" ", "").replace("\n", "") == phrase * 600


def test_condensed_position_note_limits_intro_and_summary():
    from src.translation.client import _condensed_position_note

    assert "可以用一两句话简短引入" in _condensed_position_note(0, 3)
    assert "不要写开场白、预告或总结" in _condensed_position_note(1, 3)
    assert "总结收束全篇" in _condensed_position_note(2, 3)


def test_condensed_batches_receive_position_notes(monkeypatch):
    monkeypatch.setattr(
        "src.translation.client._resolve_llm_async",
        lambda _config: (object(), "test-model", "test-key"),
    )
    monkeypatch.setattr("src.translation.client._load_prompt", lambda _filename: "{{content}}")
    monkeypatch.setattr("src.translation.client._load_glossary", lambda: "")
    monkeypatch.setattr(
        "src.translation.client._split_translation_segments",
        lambda _text, max_words: ["第一段原文", "第二段原文"],
    )
    monkeypatch.setattr("src.translation.client._validate_translation_output", lambda *a: None)
    seen = {}

    async def fake_batch(**kwargs):
        seen[kwargs["batch_idx"]] = kwargs["meta_str"]
        return [f"浓缩稿{kwargs['batch_idx']}"]

    monkeypatch.setattr("src.translation.client._translate_batch_async", fake_batch)

    asyncio.run(translate_async("source", mode="condensed", source_language="zh"))

    assert "当前第 1 段" in seen[0] and "当前第 2 段" in seen[1]


def test_condensed_length_note_gives_explicit_char_targets():
    from src.translation.client import _condensed_length_note

    zh = _condensed_length_note("字" * 2860, "zh")
    assert "本段原文约 2850 字" in zh and "850–1300 个汉字" in zh
    en = _condensed_length_note("word " * 1000, "en")  # 1000 词 ≈ 1850 字
    assert "按中文字数折算" in en and "550–850 个汉字" in en


def test_condensed_single_batch_still_gets_length_note(monkeypatch):
    monkeypatch.setattr(
        "src.translation.client._resolve_llm_async",
        lambda _config: (object(), "test-model", "test-key"),
    )
    monkeypatch.setattr("src.translation.client._load_prompt", lambda _filename: "{{content}}")
    monkeypatch.setattr("src.translation.client._load_glossary", lambda: "")
    monkeypatch.setattr(
        "src.translation.client._split_translation_segments",
        lambda _text, max_words: ["唯一一段原文" * 100],
    )
    monkeypatch.setattr("src.translation.client._validate_translation_output", lambda *a: None)
    seen = {}

    async def fake_batch(**kwargs):
        seen["meta"] = kwargs["meta_str"]
        return ["浓缩稿"]

    monkeypatch.setattr("src.translation.client._translate_batch_async", fake_batch)
    asyncio.run(translate_async("source", mode="condensed", source_language="zh"))

    assert "{{condensed_length:zh}}" in seen["meta"] and "【片段位置】" not in seen["meta"]


def test_length_target_follows_actual_request_content_after_split():
    from src.translation.client import _condensed_length_placeholder, _render_prompt

    meta = "标题\n" + _condensed_length_placeholder("zh")
    whole = _render_prompt("{{metadata}}\n{{content}}", "字" * 2860, meta)
    half = _render_prompt("{{metadata}}\n{{content}}", "字" * 1430, meta)

    # 截断拆半后，每半只能拿到自己那部分的目标，否则总输出翻倍
    assert "850–1300 个汉字" in whole
    assert "450–650 个汉字" in half
    assert "{{condensed_length" not in half


def test_truncated_single_request_splits_with_halved_targets(monkeypatch):
    from types import SimpleNamespace

    from src.translation import client as tc

    prompts = []

    class FakeCompletions:
        async def create(self, **kwargs):
            prompt = kwargs["messages"][1]["content"]
            prompts.append(prompt)
            finish = "length" if len(prompts) == 1 else "stop"
            message = SimpleNamespace(content="浓缩后的中文内容。" * 3, reasoning_content="")
            return SimpleNamespace(
                choices=[SimpleNamespace(finish_reason=finish, message=message)],
                usage=SimpleNamespace(completion_tokens=8192),
            )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    source = "\n\n".join(["字" * 1430, "字" * 1430])
    cfg = {"llm": {"temperature": 0.3, "max_tokens": 8192}}

    asyncio.run(tc._translate_single_async(
        client=fake_client, model_name="m", prompt_template="{{metadata}}{{content}}",
        meta_str=tc._condensed_length_placeholder("zh"), seg=source, idx=0, total=1,
        cfg=cfg, retries=1, backoff=1.0,
    ))

    assert "850–1300 个汉字" in prompts[0]
    assert all("450–650 个汉字" in p for p in prompts[1:]) and len(prompts) == 3


def _fake_client(script):
    """script: 依次返回的 (finish_reason, content, reasoning)；记录每次 max_tokens。"""
    from types import SimpleNamespace

    calls = []

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs["max_tokens"])
            finish, content, reasoning = script[min(len(calls), len(script)) - 1]
            message = SimpleNamespace(content=content, reasoning_content=reasoning)
            return SimpleNamespace(
                choices=[SimpleNamespace(finish_reason=finish, message=message)],
                usage=SimpleNamespace(completion_tokens=kwargs["max_tokens"]),
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())), calls


def _run_single(client, seg="第一段原文。\n\n第二段原文。"):
    from src.translation import client as tc

    cfg = {"llm": {"temperature": 0.3, "max_tokens": 8192}}
    return asyncio.run(tc._translate_single_async(
        client=client, model_name="m", prompt_template="{{metadata}}{{content}}",
        meta_str="", seg=seg, idx=0, total=1, cfg=cfg, retries=1, backoff=1.0,
    ))


def test_reasoning_exhaustion_raises_budget_instead_of_splitting():
    client, calls = _fake_client([
        ("length", "", "思考" * 7000),
        ("stop", "浓缩后的完整中文稿。", "思考"),
    ])

    assert _run_single(client) == "浓缩后的完整中文稿。"
    assert calls == [8192, 24576]  # 同一段提额重试，没有拆分


def test_reasoning_exhaustion_splits_only_after_reaching_cap():
    client, calls = _fake_client([
        ("length", "", "思考" * 7000),
        ("length", "", "思考" * 7000),
        ("length", "", "思考" * 7000),
        ("stop", "半段浓缩稿。", ""),
    ])

    _run_single(client)
    # 8192 → 24576 → 32768（上限）仍耗尽，才拆成两半，且两半沿用上限额度
    assert calls == [8192, 24576, 32768, 32768, 32768]


def test_long_content_truncation_still_splits():
    client, calls = _fake_client([
        ("length", "很长的正文" * 500, ""),
        ("stop", "半段译文。", ""),
    ])

    _run_single(client)
    assert calls == [8192, 8192, 8192]


def test_scaled_length_placeholder_tightens_targets():
    from src.translation.client import _condensed_length_placeholder, _render_prompt

    meta = _condensed_length_placeholder("zh", 0.65)
    rendered = _render_prompt("{{metadata}}{{content}}", "字" * 2860, meta)

    # 850–1300 × 0.65 ≈ 550–850
    assert "550–850 个汉字" in rendered
    assert _condensed_length_placeholder("zh") == "{{condensed_length:zh}}"


def test_chinese_source_uses_rewriter_role_not_translator_role(monkeypatch):
    from src.translation import client as tc

    monkeypatch.setattr(tc, "_resolve_llm_async", lambda _config: (object(), "m", "k"))
    monkeypatch.setattr(tc, "_load_prompt", lambda _f: "{{role}}|{{content}}")
    monkeypatch.setattr(tc, "_load_glossary", lambda: "")
    monkeypatch.setattr(tc, "_validate_translation_output", lambda *a: None)
    monkeypatch.setattr(tc, "get_config", lambda: {
        "llm": {"translator_role": "翻译角色", "rewriter_role": "改写角色",
                "max_tokens": 8192, "temperature": 0.3},
        "processing": {"retry_times": 1, "retry_backoff_base": 1.0},
        "translation": {},
    })
    seen = []

    async def fake_batch(**kwargs):
        seen.append(kwargs["prompt_template"])
        return ["稿子"]

    monkeypatch.setattr(tc, "_translate_batch_async", fake_batch)

    asyncio.run(tc.translate_async("原文", mode="podcast", source_language="zh"))
    asyncio.run(tc.translate_async("source", mode="podcast", source_language="en"))

    assert seen[0].startswith("改写角色|") and seen[1].startswith("翻译角色|")
