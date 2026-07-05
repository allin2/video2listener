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

    class FakeOpener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data)
            captured["timeout"] = timeout
            return FakeResponse()

    def fake_build_opener(*_handlers):
        return FakeOpener()

    monkeypatch.setattr(synthesizer.urllib.request, "build_opener", fake_build_opener)
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
        "messages": [
            {"role": "user", "content": "语气自然流畅，像播客主持人在娓娓道来"},
            {"role": "assistant", "content": "你好，世界。"},
        ],
        "audio": {"format": "wav", "voice": "苏打"},
    }
    assert output.read_bytes() == expected_audio


def test_mimo_segments_use_wav_extension(tmp_path, monkeypatch):
    written_paths = []

    def fake_synthesize_segment(_text, output_path, _tts_config, _use_ssml, _speed):
        output_path.write_bytes(b"audio")
        written_paths.append(output_path)

    monkeypatch.setattr(synthesizer, "_synthesize_segment", fake_synthesize_segment)

    result = synthesizer.synthesize(
        "第一句。\n第二句。",
        tmp_path,
        tts_config={"provider": "mimi"},
    )

    assert sorted(result) == sorted(written_paths)
    assert all(path.suffix == ".wav" for path in result)


def test_split_tts_segments_packs_short_lines_without_losing_order():
    text = "第一句。\n第二句。\n" + "长" * 12 + "\n最后一句。"

    segments = synthesizer._split_tts_segments(text, max_chars=16)

    assert all(len(segment) <= 16 for segment in segments)
    assert "".join(segment.replace("\n", "") for segment in segments) == text.replace("\n", "")
    assert len(segments) < len([line for line in text.splitlines() if line])


def test_tts_progress_uses_packed_request_count(tmp_path, monkeypatch):
    progress = []

    def fake_synthesize_segment(_text, output_path, _tts_config, _use_ssml, _speed):
        output_path.write_bytes(b"audio")

    monkeypatch.setattr(synthesizer, "_synthesize_segment", fake_synthesize_segment)

    result = synthesizer.synthesize(
        "第一段。\n第二段。\n第三段。",
        tmp_path,
        on_progress=progress.append,
        tts_config={"provider": "mimi"},
        concurrency=1,
    )

    assert len(result) == 1
    assert progress[0] == "TTS 合成中... (0/1)"
    assert progress[-1] == "TTS 合成中... (1/1)"


def test_tts_config_controls_packing_and_concurrency(tmp_path, monkeypatch):
    calls = []

    def fake_synthesize_segment(text, output_path, _tts_config, _use_ssml, _speed):
        calls.append(text)
        output_path.write_bytes(b"audio")

    monkeypatch.setattr(synthesizer, "_synthesize_segment", fake_synthesize_segment)

    result = synthesizer.synthesize(
        "一二三四。\n五六七八。\n九十。",
        tmp_path,
        tts_config={"provider": "mimi", "max_chars": 10, "concurrency": 1},
    )

    assert len(result) == 2
    assert calls == ["一二三四。", "五六七八。\n九十。"]


def test_tts_resume_reuses_valid_segments_with_matching_manifest(tmp_path, monkeypatch):
    calls = []

    def fake_synthesize_segment(text, output_path, _tts_config, _use_ssml, _speed):
        calls.append(text)
        output_path.write_bytes(b"valid mp3 placeholder")

    monkeypatch.setattr(synthesizer, "_synthesize_segment", fake_synthesize_segment)
    config = {"provider": "edge", "max_chars": 6, "concurrency": 1, "voice": "voice-a"}
    text = "一二三四五六七八九十"

    first = synthesizer.synthesize(text, tmp_path, tts_config=config)
    second = synthesizer.synthesize(text, tmp_path, tts_config=config)

    assert len(first) == len(second) == 2
    assert calls == ["一二三四五六", "七八九十"]


def test_tts_cache_is_invalidated_when_voice_changes(tmp_path, monkeypatch):
    calls = []

    def fake_synthesize_segment(text, output_path, _tts_config, _use_ssml, _speed):
        calls.append(text)
        output_path.write_bytes(b"valid mp3 placeholder")

    monkeypatch.setattr(synthesizer, "_synthesize_segment", fake_synthesize_segment)
    text = "需要重新合成的内容"

    synthesizer.synthesize(
        text, tmp_path,
        tts_config={"provider": "edge", "concurrency": 1, "voice": "voice-a"},
    )
    synthesizer.synthesize(
        text, tmp_path,
        tts_config={"provider": "edge", "concurrency": 1, "voice": "voice-b"},
    )

    assert calls == [text, text]
