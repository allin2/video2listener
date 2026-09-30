"""测试全局装置。"""

import pytest

from src.storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """把测试用的数据库指向临时文件。

    应用启动钩子（server._lifespan）会在启动时回收中断任务并写库；不隔离的话，
    跑一次测试就会改写真实的 db/tasks.db。
    """
    monkeypatch.setattr(db, "_get_db_path", lambda: tmp_path / "tasks.db")
    db.init_db()
