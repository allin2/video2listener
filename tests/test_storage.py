"""SQLite 共享视频与模式变体存储回归测试。"""

from datetime import datetime, timezone

import pytest

from src.storage import db


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "video2listener.db"
    monkeypatch.setattr(db, "_get_db_path", lambda: db_path)
    db.init_db()
    return db_path


def test_variant_crud_keeps_modes_separate(isolated_db):
    db.create_episode("video123456", "https://youtu.be/video123456", mode="faithful")

    db.upsert_variant("video123456", "faithful", status="done", audio_zh_path="faithful.mp3")
    db.upsert_variant("video123456", "podcast", status="translated", script_zh_path="podcast.txt")

    variants = db.list_variants("video123456")
    assert [item["mode"] for item in variants] == ["faithful", "podcast"]
    assert db.get_variant("video123456", "faithful")["audio_zh_path"] == "faithful.mp3"
    assert db.get_variant("video123456", "podcast")["script_zh_path"] == "podcast.txt"

    db.update_variant_status("video123456", "podcast", "done", audio_zh_path="podcast.mp3")
    assert db.get_variant("video123456", "podcast")["audio_zh_path"] == "podcast.mp3"


def test_background_artifact_update_does_not_regress_variant_status(isolated_db):
    db.create_episode("video123456", "https://youtu.be/video123456", mode="faithful")
    db.upsert_variant("video123456", "faithful", status="done")

    db.update_variant_artifacts(
        "video123456", "faithful", summary_path="/tmp/summary.json",
    )

    variant = db.get_variant("video123456", "faithful")
    assert variant["status"] == "done"
    assert variant["summary_path"] == "/tmp/summary.json"


def test_variant_mode_is_validated(isolated_db):
    db.create_episode("video123456", "https://youtu.be/video123456")

    with pytest.raises(ValueError, match="不支持的模式"):
        db.upsert_variant("video123456", "unknown")


def test_legacy_episode_is_backfilled_once_without_moving_paths(tmp_path, monkeypatch):
    db_path = tmp_path / "video2listener.db"
    monkeypatch.setattr(db, "_get_db_path", lambda: db_path)
    db.init_db()
    db.create_episode(
        "video123456",
        "https://youtu.be/video123456",
        mode="faithful",
        data_dir=str(tmp_path / "legacy"),
    )
    db.update_status(
        "video123456",
        "done",
        script_zh_path=str(tmp_path / "legacy" / "script_zh.txt"),
        audio_zh_path=str(tmp_path / "legacy" / "output.mp3"),
    )

    # 模拟升级前数据库：移除迁移标记，再执行新版本初始化。
    conn = db._get_conn()
    conn.execute("DELETE FROM schema_migration WHERE name = ?", (db.VARIANT_MIGRATION,))
    conn.execute("DELETE FROM episode_variant")
    conn.commit()
    conn.close()

    db.init_db()
    variant = db.get_variant("video123456", "faithful")
    assert variant["status"] == "done"
    assert variant["variant_dir"] is None
    assert variant["audio_zh_path"].endswith("legacy/output.mp3")

    assert db.delete_variant("video123456", "faithful") is True
    db.init_db()
    assert db.get_variant("video123456", "faithful") is None


def test_delete_episode_cascades_variants(isolated_db):
    db.create_episode("video123456", "https://youtu.be/video123456")
    db.upsert_variant("video123456", "podcast", status="done")

    assert db.delete_episode("video123456") is True
    assert db.list_variants("video123456") == []


def test_upsert_variant_is_idempotent(isolated_db):
    db.create_episode("video123456", "https://youtu.be/video123456")
    db.upsert_variant("video123456", "condensed", status="translated", script_zh_path="one.txt")
    db.upsert_variant("video123456", "condensed", status="done", audio_zh_path="two.mp3")

    variants = db.list_variants("video123456")
    assert len(variants) == 1
    assert variants[0]["script_zh_path"] == "one.txt"
    assert variants[0]["audio_zh_path"] == "two.mp3"
    assert variants[0]["status"] == "done"


def test_completed_variant_requires_nonempty_audio_file(isolated_db, tmp_path):
    db.create_episode("video123456", "https://youtu.be/video123456")
    missing = tmp_path / "missing.mp3"
    db.upsert_variant(
        "video123456", "faithful", status="done", audio_zh_path=str(missing)
    )
    assert db.get_completed_variant("video123456", "faithful") is None

    missing.write_bytes(b"audio")
    assert db.get_completed_variant("video123456", "faithful")["status"] == "done"


def test_variant_quality_status_is_persisted_and_returned(isolated_db):
    db.create_episode("video123456", "https://youtu.be/video123456")
    db.upsert_variant(
        "video123456", "faithful", status="done",
        audit_status="degraded",
        audit_message="语义审计服务暂时不可用",
        translation_audit_path="/tmp/translation_audit.json",
    )

    variant = db.get_variant("video123456", "faithful")
    assert variant["audit_status"] == "degraded"
    assert variant["audit_message"] == "语义审计服务暂时不可用"
    assert variant["translation_audit_path"].endswith("translation_audit.json")
