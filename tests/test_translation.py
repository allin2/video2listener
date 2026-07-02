"""U4 测试。翻译和摘要模块。"""
import json
from src.translation.client import _load_prompt, MODE_PROMPTS


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
    """验证忠实翻译 prompt 包含必要关键词。"""
    prompt = _load_prompt("translate_faithful.txt")
    assert "翻译" in prompt or "translate" in prompt.lower()
