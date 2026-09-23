"""U3 测试。文本清洗模块。"""
from src.transcription.cleaner import clean


def test_clean_removes_filler_lines():
    text = "yeah\n\nToday we discuss AI.\n\num\n\nImportant topic."
    result, _ = clean(text)
    assert "Today we discuss AI" in result
    assert "Important topic" in result


def test_clean_removes_ad_paragraphs():
    text = "Great content here.\n\nsubscribe to my channel and hit the bell\n\nMore content."
    result, _ = clean(text)
    assert "subscribe" not in result.lower()


def test_clean_removes_urls():
    text = "Check the docs https://github.com/example/repo for info.\n\nMain content here."
    result, _ = clean(text)
    assert "http" not in result
    assert "Main content here" in result


def test_clean_removes_expanded_ads_and_fluff():
    text = (
        "Here is the core tutorial.\n\n"
        "You can download Genmail for free in the link in the description.\n\n"
        "Next important technique.\n\n"
        "记得一键三连支持博主哦。\n\n"
        "本期视频由某某品牌赞助播出。\n\n"
        "在评论区告诉我你的想法吧。\n\n"
        "Final knowledge conclusion."
    )
    result, _ = clean(text)
    assert "link in the description" not in result
    assert "一键三连" not in result
    assert "赞助" not in result
    assert "评论区" not in result
    assert "core tutorial" in result
    assert "Next important technique" in result
    assert "Final knowledge conclusion" in result


def test_clean_merges_short_sentences():
    text = "Hello.\n\nThis is a very short.\n\nThis is a much longer sentence that should be kept as is."
    result, _ = clean(text)
    assert len(result) > 0


def test_clean_preserves_paragraphs():
    text = "First paragraph with some content.\n\nSecond paragraph with different content."
    result, _ = clean(text)
    assert "First paragraph" in result
    assert "Second paragraph" in result


def test_clean_empty_text():
    result, _ = clean("")
    assert result == "" or len(result) == 0


def test_clean_removes_duplicate_whitespace():
    text = "Line one.\n\n\n\n\nLine two."
    result, _ = clean(text)
    assert "\n\n\n\n" not in result


def test_seconds_to_timestamp():
    from src.transcription.transcriber import _seconds_to_timestamp

    assert _seconds_to_timestamp(0) == "00:00:00"
    assert _seconds_to_timestamp(65.5) == "00:01:05"
    assert _seconds_to_timestamp(3661) == "01:01:01"


def test_transcribe_with_whisper_provider(tmp_path, monkeypatch):
    import sys
    from unittest.mock import MagicMock
    from src.transcription.transcriber import transcribe
    import src.transcription.transcriber as transcriber_module

    # Mock config
    test_cfg = {
        "asr": {
            "provider": "whisper",
            "model": "large-v3-turbo",
            "model_path": str(tmp_path / "model.pt"),
            "device": "cpu",
            "compute_type": "int8",
        }
    }
    # Create fake model.pt
    (tmp_path / "model.pt").touch()
    fake_audio = tmp_path / "test.wav"
    fake_audio.touch()

    monkeypatch.setattr(transcriber_module, "get_config", lambda: test_cfg)

    # Mock whisper module
    mock_whisper = MagicMock()
    mock_model = MagicMock()
    mock_whisper.load_model.return_value = mock_model
    mock_model.transcribe.return_value = {
        "language": "en",
        "segments": [
            {"start": 0.0, "end": 2.5, "text": " Hello world! "},
            {"start": 2.5, "end": 5.0, "text": " Welcome to the show. "},
        ],
    }
    monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

    out_dir = tmp_path / "output"
    entries = transcribe(fake_audio, output_dir=out_dir)

    assert len(entries) == 2
    assert entries[0] == {"start": "00:00:00", "end": "00:00:02", "text": "Hello world!"}
    assert entries[1] == {"start": "00:00:02", "end": "00:00:05", "text": "Welcome to the show."}

    # Verify model loaded with the resolved path
    mock_whisper.load_model.assert_called_once_with(str(tmp_path / "model.pt"), device="cpu")
    mock_model.transcribe.assert_called_once_with(str(fake_audio), language="en", verbose=False, fp16=False)

    # Verify outputs written
    assert (out_dir / "transcript_raw.json").exists()
    assert (out_dir / "captions_en.json").exists()


def test_transcribe_routes_pt_file_to_whisper(tmp_path, monkeypatch):
    import sys
    from unittest.mock import MagicMock
    from src.transcription.transcriber import transcribe
    import src.transcription.transcriber as transcriber_module

    # Even if provider says faster-whisper, a .pt path routes to whisper
    test_cfg = {
        "asr": {
            "provider": "faster-whisper",
            "model": "large-v3-turbo",
            "model_path": str(tmp_path / "custom.pt"),
            "device": "cpu",
        }
    }
    (tmp_path / "custom.pt").touch()
    fake_audio = tmp_path / "test.wav"
    fake_audio.touch()

    monkeypatch.setattr(transcriber_module, "get_config", lambda: test_cfg)

    mock_whisper = MagicMock()
    mock_model = MagicMock()
    mock_whisper.load_model.return_value = mock_model
    mock_model.transcribe.return_value = {
        "language": "en",
        "segments": [{"start": 1.0, "end": 3.0, "text": "PT model test"}],
    }
    monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

    entries = transcribe(fake_audio)
    assert len(entries) == 1
    assert entries[0]["text"] == "PT model test"
    mock_whisper.load_model.assert_called_once_with(str(tmp_path / "custom.pt"), device="cpu")


def test_transcribe_with_faster_whisper(tmp_path, monkeypatch):
    import sys
    from unittest.mock import MagicMock
    from src.transcription.transcriber import transcribe
    import src.transcription.transcriber as transcriber_module

    test_cfg = {
        "asr": {
            "provider": "faster-whisper",
            "model": "large-v3-turbo",
            "device": "cpu",
            "compute_type": "int8",
        }
    }
    fake_audio = tmp_path / "test.wav"
    fake_audio.touch()

    monkeypatch.setattr(transcriber_module, "get_config", lambda: test_cfg)

    mock_fw = MagicMock()
    mock_model = MagicMock()
    mock_fw.WhisperModel.return_value = mock_model

    class FakeSegment:
        def __init__(self, start, end, text):
            self.start = start
            self.end = end
            self.text = text

    class FakeInfo:
        language = "en"
        language_probability = 0.99

    mock_model.transcribe.return_value = (
        [FakeSegment(0.0, 3.0, " Faster whisper text ")],
        FakeInfo(),
    )
    monkeypatch.setitem(sys.modules, "faster_whisper", mock_fw)

    entries = transcribe(fake_audio)
    assert len(entries) == 1
    assert entries[0] == {"start": "00:00:00", "end": "00:00:03", "text": "Faster whisper text"}
    mock_fw.WhisperModel.assert_called_once_with("large-v3-turbo", device="cpu", compute_type="int8")

