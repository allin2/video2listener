"""U4 测试。翻译和摘要模块。"""
import asyncio
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


