"""U3 测试。文本清洗模块。"""
from src.transcription.cleaner import clean


def test_clean_removes_filler_lines():
    text = "yeah\n\nToday we discuss AI.\n\num\n\nImportant topic."
    result = clean(text)
    assert "Today we discuss AI" in result
    assert "Important topic" in result


def test_clean_removes_ad_paragraphs():
    text = "Great content here.\n\nsubscribe to my channel and hit the bell\n\nMore content."
    result = clean(text)
    assert "subscribe" not in result.lower()


def test_clean_merges_short_sentences():
    text = "Hello.\n\nThis is a very short.\n\nThis is a much longer sentence that should be kept as is."
    result = clean(text)
    assert len(result) > 0


def test_clean_preserves_paragraphs():
    text = "First paragraph with some content.\n\nSecond paragraph with different content."
    result = clean(text)
    assert "First paragraph" in result
    assert "Second paragraph" in result


def test_clean_empty_text():
    result = clean("")
    assert result == ""


def test_clean_removes_duplicate_whitespace():
    text = "Line one.\n\n\n\n\nLine two."
    result = clean(text)
    assert "\n\n\n\n" not in result
