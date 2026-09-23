"""测试中文内容重构与双轨管线。"""

import asyncio
from unittest.mock import MagicMock, patch
import pytest

from src.translation.client import (
    _split_translation_segments,
    translate,
    translate_async,
)


def test_split_translation_segments_chinese():
    """验证包含超长无空格中文段落时能按字符上限切分。"""
    chinese_text = "这是测试段落。" * 200  # 1600 字
    segments = _split_translation_segments(chinese_text, max_words=100)
    assert len(segments) > 1
    # 拼回去包含所有内容
    recombined = "".join("".join(s.split()) for s in segments)
    assert recombined == "".join(chinese_text.split())


@pytest.mark.asyncio
async def test_translate_async_chinese_podcast_uses_correct_prompt(monkeypatch):
    """验证 source_language='zh' 且 mode='podcast' 时使用中文播客重构 Prompt。"""
    captured_prompts = []

    class FakeChoice:
        def __init__(self, text):
            self.message = MagicMock(content=text)
            self.finish_reason = "stop"

    class FakeResponse:
        def __init__(self, text):
            self.choices = [FakeChoice(text)]

    class FakeCompletions:
        async def create(self, **kwargs):
            messages = kwargs.get("messages", [])
            captured_prompts.append(messages[-1]["content"])
            return FakeResponse("这是重写后的中文播客内容。")

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeAsyncOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = FakeChat()

    monkeypatch.setattr("src.translation.client.AsyncOpenAI", FakeAsyncOpenAI)

    fake_llm_cfg = {
        "api_key": "test_key",
        "model": "deepseek-chat",
    }

    result = await translate_async(
        text="今天我们来聊聊人工智能的发展历程。",
        mode="podcast",
        llm_config=fake_llm_cfg,
        source_language="zh",
    )

    assert result == "这是重写后的中文播客内容。"
    assert len(captured_prompts) == 1
    # 检查是否包含中文重写 Prompt 的指令
    assert "目标：将输入的中文视频转写稿或字幕，重写为一档生动、自然、好听的中文知识播客音频稿" in captured_prompts[0]


@pytest.mark.asyncio
async def test_translate_async_chinese_condensed_uses_correct_prompt(monkeypatch):
    """验证 source_language='zh' 且 mode='condensed' 时使用中文浓缩 Prompt。"""
    captured_prompts = []

    class FakeChoice:
        def __init__(self, text):
            self.message = MagicMock(content=text)
            self.finish_reason = "stop"

    class FakeResponse:
        def __init__(self, text):
            self.choices = [FakeChoice(text)]

    class FakeCompletions:
        async def create(self, **kwargs):
            messages = kwargs.get("messages", [])
            captured_prompts.append(messages[-1]["content"])
            return FakeResponse("这是浓缩后的中文内容。")

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeAsyncOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = FakeChat()

    monkeypatch.setattr("src.translation.client.AsyncOpenAI", FakeAsyncOpenAI)

    fake_llm_cfg = {
        "api_key": "test_key",
        "model": "deepseek-chat",
    }

    result = await translate_async(
        text="今天我们来聊聊人工智能的发展历程，第一部分介绍神经网络。",
        mode="condensed",
        llm_config=fake_llm_cfg,
        source_language="zh",
    )

    assert result == "这是浓缩后的中文内容。"
    assert len(captured_prompts) == 1
    assert "目标：将输入的中文视频转写内容精炼压缩为原内容量约 30–50% 的中文精华音频文稿" in captured_prompts[0]
