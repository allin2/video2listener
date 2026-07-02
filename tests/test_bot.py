"""U6 测试。飞书消息处理逻辑。"""
from src.bot.handler import parse_message, extract_youtube_id


def test_parse_default_mode():
    mode, remaining = parse_message("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert mode is None
    assert "dQw4w9WgXcQ" in remaining


def test_parse_condensed_prefix():
    mode, remaining = parse_message("浓缩 https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert mode == "condensed"


def test_parse_faithful_prefix():
    mode, remaining = parse_message("忠实 https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert mode == "faithful"


def test_parse_podcast_prefix():
    mode, remaining = parse_message("播客 https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert mode == "podcast"


def test_extract_video_id_from_url():
    vid = extract_youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert vid == "dQw4w9WgXcQ"


def test_extract_video_id_from_short():
    vid = extract_youtube_id("https://youtu.be/dQw4w9WgXcQ")
    assert vid == "dQw4w9WgXcQ"


def test_extract_video_id_raw():
    vid = extract_youtube_id("dQw4w9WgXcQ")
    assert vid == "dQw4w9WgXcQ"


def test_extract_no_video_id():
    vid = extract_youtube_id("hello world")
    assert vid is None
