"""U5 测试。TTS 清洗模块。"""
import base64
import json

from src.tts.cleaner import clean_for_tts
from src.tts import synthesizer


def test_clean_removes_markdown():
    text = "**重要观点**：AI正在改变世界。*斜体内容*。"
    result = clean_for_tts(text)
    assert "**" not in result
    assert "*" not in result


def test_clean_removes_urls():
    text = "访问 https://example.com 了解更多。正常文本。"
    result = clean_for_tts(text)
    assert "http" not in result


def test_clean_removes_unspeakable_symbols():
    text = "文本`代码`和[链接]和{括号}"
    result = clean_for_tts(text)
    assert "`" not in result
    assert "[" not in result
    assert "{" not in result


def test_clean_splits_long_sentences():
    long_text = "这是一个长句。" * 15 + "这是另一个长句。" * 15
    result = clean_for_tts(long_text)
    lines = [l for l in result.split("\n") if l.strip()]
    assert len(lines) > 1


def test_clean_handles_english_names():
    text = "Sam Altman 表示 OpenAI 将发布新模型。"
    result = clean_for_tts(text)
    assert "Sam Altman" in result
    assert "OpenAI" in result


def test_clean_empty_text():
    result = clean_for_tts("")
    assert result == ""


def test_mimo_tts_uses_chat_completions_contract(tmp_path, monkeypatch):
    """MiMo TTS 必须使用官方 Chat Completions 音频协议。"""
    expected_audio = b"RIFF\x00\x00\x00\x00WAVE"
    response = json.dumps({
        "choices": [{
            "message": {
                "audio": {"data": base64.b64encode(expected_audio).decode("ascii")}
            }
        }]
    }).encode("utf-8")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return response

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(synthesizer.urllib.request, "urlopen", fake_urlopen)
    output = tmp_path / "segment.wav"

    synthesizer._mimi_tts("你好，世界。", output, {
        "api_key": "test-key",
        "base_url": "https://api.xiaomimimo.com/v1/",
        "model": "mimo-v2.5-tts",
        "voice": "苏打",
    })

    assert captured["url"] == "https://api.xiaomimimo.com/v1/chat/completions"
    assert captured["headers"]["Api-key"] == "test-key"
    assert captured["payload"] == {
        "model": "mimo-v2.5-tts",
        "messages": [{"role": "assistant", "content": "你好，世界。"}],
        "audio": {"format": "wav", "voice": "苏打"},
    }
    assert output.read_bytes() == expected_audio


def test_mimo_segments_use_wav_extension(tmp_path, monkeypatch):
    written_paths = []

    def fake_synthesize_segment(_text, output_path, _tts_config):
        output_path.write_bytes(b"audio")
        written_paths.append(output_path)

    monkeypatch.setattr(synthesizer, "_synthesize_segment", fake_synthesize_segment)

    result = synthesizer.synthesize(
        "第一句。\n第二句。",
        tmp_path,
        tts_config={"provider": "mimi"},
    )

    assert result == written_paths
    assert all(path.suffix == ".wav" for path in result)
