"""U7 测试。管道编排模块。"""
import pytest
from src.pipeline.state import TaskStatus, check_stage_file, next_status


def test_status_order():
    assert next_status(TaskStatus.NEW) == TaskStatus.METADATA_FETCHED
    assert next_status(TaskStatus.METADATA_FETCHED) == TaskStatus.TEXT_READY
    assert next_status(TaskStatus.TEXT_READY) == TaskStatus.TRANSLATED
    assert next_status(TaskStatus.TRANSLATED) == TaskStatus.TTS_DONE
    assert next_status(TaskStatus.TTS_DONE) == TaskStatus.DONE
    assert next_status(TaskStatus.DONE) is None


def test_status_values_unique():
    values = [s.value for s in TaskStatus]
    assert len(values) == len(set(values))


def test_failed_not_in_order():
    """FAILED 不在正常流转顺序中。"""
    with pytest.raises(ValueError):
        from src.pipeline.state import STATUS_ORDER
        STATUS_ORDER.index(TaskStatus.FAILED)
    # FAILED 不在 STATUS_ORDER 中是一个设计决定


def test_all_statuses_have_string_values():
    for status in TaskStatus:
        assert isinstance(status.value, str)
        assert len(status.value) > 0


def test_partial_tts_segments_do_not_mark_stage_complete(tmp_path):
    segments = tmp_path / "tts_segments"
    segments.mkdir()
    (segments / "segment_0000.wav").write_bytes(b"partial")

    assert check_stage_file(tmp_path, TaskStatus.TTS_DONE) is False

    (tmp_path / "A_Video_Title.mp3").write_bytes(b"complete")
    assert check_stage_file(tmp_path, TaskStatus.TTS_DONE) is True
