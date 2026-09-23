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


def test_clean_preserves_underscores_in_technical_identifiers():
    """下划线属于内容本身，不能被当作 Markdown 符号删除。

    回归：此前字符类包含 `_`，会把 VIDEO2LISTENER_DEEPSEEK_API_KEY 清洗成
    VIDEO2LISTENERDEEPSEEKAPIKEY，导致给用户的配置指引失效。
    """
    text = "请设置 VIDEO2LISTENER_DEEPSEEK_API_KEY 后重试，并检查 __init__ 方法。"
    result = clean_for_tts(text)

    assert "VIDEO2LISTENER_DEEPSEEK_API_KEY" in result
    assert "__init__" in result
    assert "VIDEO2LISTENERDEEPSEEKAPIKEY" not in result


def test_clean_preserves_snake_case_and_paths():
    text = "调用 get_config() 读取 config.yaml，字段是 api_key。"
    result = clean_for_tts(text)

    assert "get_config" in result
    assert "config.yaml" in result
    assert "api_key" in result


def test_clean_still_removes_markdown_emphasis_without_touching_identifiers():
    text = "**重点**：使用 snake_case 命名。"
    result = clean_for_tts(text)

    assert "**" not in result
    assert "重点" in result
    assert "snake_case" in result


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


def test_fish_tts_uses_official_contract(tmp_path, monkeypatch):
    """Fish Audio TTS 必须使用用户指定的 POST /v1/tts 协议，带 model 头与 reference_id。"""
    expected_audio = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 40
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return expected_audio

    class FakeOpener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(synthesizer.urllib.request, "build_opener", lambda *_args: FakeOpener())
    output = tmp_path / "segment.mp3"

    synthesizer._fish_tts("我替你们把 Obsidian 学了一遍。", output, {
        "api_key": "test-fish-key",
        "base_url": "https://api.fish.audio/v1",
        "model": "s2.1-pro-free",
        "voice": "7f92f8afb8ec43bf81429cc1c9199cb1",
    })

    assert captured["url"] == "https://api.fish.audio/v1/tts"
    assert captured["headers"]["authorization"] == "Bearer test-fish-key"
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["headers"]["model"] == "s2.1-pro-free"
    assert captured["payload"] == {
        "text": "我替你们把 Obsidian 学了一遍。",
        "reference_id": "7f92f8afb8ec43bf81429cc1c9199cb1",
        "format": "mp3",
    }
    assert output.read_bytes() == expected_audio


def test_fish_tts_sanitizes_legacy_mimo_voices(tmp_path, monkeypatch):
    """当传入旧版 MiMo 预置音色（如'茉莉'）时，自动纠偏为默认 Fish Audio ID。"""
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 40

    class FakeOpener:
        def open(self, request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

    monkeypatch.setattr(synthesizer.urllib.request, "build_opener", lambda *_args: FakeOpener())
    output = tmp_path / "segment.mp3"

    synthesizer._fish_tts("测试", output, {
        "api_key": "test-key",
        "voice": "茉莉",
    })

    assert captured["payload"]["reference_id"] == "7f92f8afb8ec43bf81429cc1c9199cb1"



def test_synthesize_segment_routes_to_fish(tmp_path, monkeypatch):
    called = []

    def fake_fish(text, output_path, tts_config):
        called.append((text, output_path, tts_config))
        output_path.write_bytes(b"dummy mp3")

    monkeypatch.setattr(synthesizer, "_fish_tts", fake_fish)
    out = tmp_path / "test.mp3"
    synthesizer._synthesize_segment("测试文本", out, tts_config={"provider": "fish", "api_key": "k"})
    assert len(called) == 1
    assert called[0][0] == "测试文本"


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


def test_edge_tts_does_not_emit_raw_ssml_or_http_tags(tmp_path, monkeypatch):
    """验证 Edge TTS 不会发送 <speak> 或 <break time="..."> 等 SSML 标签。

    回归：此前 _edge_tts 自行拼接了 SSML 标签，但 edge_tts.Communicate 库会转义这些标签，
    导致微软语音服务把标签当成普通正文读出 "HTTP"、"break time" 等严重噪音。
    """
    import sys
    from unittest.mock import MagicMock

    calls = []

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            calls.append((text, voice, kwargs))

        async def save(self, path):
            pass

    mock_edge = MagicMock()
    mock_edge.Communicate = FakeCommunicate
    monkeypatch.setitem(sys.modules, "edge_tts", mock_edge)

    out_file = tmp_path / "out.mp3"
    test_text = "这是第一句话——这是破折号之后的句子。\n\n这是新段落。"

    synthesizer._edge_tts(test_text, out_file, use_ssml=True)

    assert len(calls) == 1
    passed_text, voice, _ = calls[0]

    # 绝不能包含原始 XML/SSML 标签或 URL
    assert "<speak" not in passed_text
    assert "</speak>" not in passed_text
    assert "<break" not in passed_text
    assert "xmlns" not in passed_text
    assert "http" not in passed_text
    assert "break time" not in passed_text
    # 破折号应替换为自然停顿标点
    assert "——" not in passed_text
    assert "，" in passed_text


def test_fish_tts_supports_speed_prosody(tmp_path, monkeypatch):
    """Fish Audio TTS 在语速不为 1.0 时应在请求体中带上 prosody.speed。"""
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 40

    class FakeOpener:
        def open(self, request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

    monkeypatch.setattr(synthesizer.urllib.request, "build_opener", lambda *_args: FakeOpener())
    output = tmp_path / "segment.mp3"

    # 测试通过 tts_config["speed"] = 1.25
    synthesizer._fish_tts("测试文本", output, {
        "api_key": "test-key",
        "speed": 1.25,
    })
    assert captured["payload"]["prosody"] == {"speed": 1.25}

    # 测试通过 speed 参数 = 0.8
    synthesizer._fish_tts("测试文本", output, {
        "api_key": "test-key",
    }, speed=0.8)
    assert captured["payload"]["prosody"] == {"speed": 0.8}

    # 测试默认 1.0 时不包含 prosody
    synthesizer._fish_tts("测试文本", output, {
        "api_key": "test-key",
        "speed": 1.0,
    })
    assert "prosody" not in captured["payload"]


def test_fish_tts_clamps_speed_prosody(tmp_path, monkeypatch):
    """Fish Audio TTS 超出 0.5 ~ 2.0 范围的语速应被截断限制。"""
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 40

    class FakeOpener:
        def open(self, request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

    monkeypatch.setattr(synthesizer.urllib.request, "build_opener", lambda *_args: FakeOpener())
    output = tmp_path / "segment.mp3"

    # 3.0 应被限制为 2.0
    synthesizer._fish_tts("测试文本", output, {
        "api_key": "test-key",
        "speed": 3.0,
    })
    assert captured["payload"]["prosody"] == {"speed": 2.0}

    # 0.1 应被限制为 0.5
    synthesizer._fish_tts("测试文本", output, {
        "api_key": "test-key",
        "speed": 0.1,
    })
    assert captured["payload"]["prosody"] == {"speed": 0.5}


def test_edge_tts_supports_speed(tmp_path, monkeypatch):
    """Edge-TTS 应将 speed 倍率转换为百分比 rate 字符串。"""
    import sys
    from unittest.mock import MagicMock

    calls = []

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            calls.append((text, voice, kwargs))

        async def save(self, path):
            pass

    mock_edge = MagicMock()
    mock_edge.Communicate = FakeCommunicate
    monkeypatch.setitem(sys.modules, "edge_tts", mock_edge)

    out_file = tmp_path / "out.mp3"
    synthesizer._edge_tts("测试文本", out_file, speed=1.2)
    assert calls[-1][2].get("rate") == "+20%"

    synthesizer._edge_tts("测试文本", out_file, speed=0.8)
    assert calls[-1][2].get("rate") == "-20%"

