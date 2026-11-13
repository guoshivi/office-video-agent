"""默认音色得是豆包真人，不是免费机械音。

背景：免费音色实测「少了点自然感」，接上火山之后新单子应该默认用真人音。
但老库里的 default_voice 早就写成 female_warm 了，光改默认值不动历史数据，
所以要有一个只做一次的迁移；同时用标记位兜住，免得以后有人主动改回免费又被换掉。
"""

from __future__ import annotations

from app.db import _migrate_default_voice_to_doubao, init_db, transaction


def _read(key: str) -> str:
    with transaction() as connection:
        row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def _write(key: str, value: str) -> None:
    with transaction() as connection:
        connection.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))


def test_fresh_install_defaults_to_doubao_voice():
    init_db()
    assert _read("default_voice") == "doubao_xiaohe"


def test_old_library_on_free_voice_gets_migrated_once():
    init_db()
    # 造一个「升级前」的库：默认还停在免费晓晓，并且没有迁移标记
    _write("default_voice", "female_warm")
    with transaction() as connection:
        connection.execute("DELETE FROM settings WHERE key='migration_default_voice_doubao'")

    init_db()

    assert _read("default_voice") == "doubao_xiaohe"


def test_migration_does_not_override_a_deliberate_free_choice():
    init_db()
    # 迁移跑过一次之后，用户自己改回免费音色，要尊重他的选择
    _write("default_voice", "female_warm")

    with transaction() as connection:
        _migrate_default_voice_to_doubao(connection)

    assert _read("default_voice") == "female_warm"


def test_migration_keeps_other_doubao_choices():
    init_db()
    _write("default_voice", "doubao_liufei")
    with transaction() as connection:
        connection.execute("DELETE FROM settings WHERE key='migration_default_voice_doubao'")

    init_db()

    assert _read("default_voice") == "doubao_liufei"
