"""开工体检测试：重点是"结论对不对"和"能不能一键修"。

这块代码本身很简单，但它决定了不懂电脑的人打开页面看到什么。
错的方向只有两个：该拦的不拦（以为自己能发，结果点了没反应），
或者不该拦的瞎拦（明明能用，却一直显示红字）。
"""

from __future__ import annotations

import pytest

from app import ready
from app.db import init_db, transaction


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """每个用例都从干净状态开始：清设置、清缓存。"""
    init_db()
    with transaction() as connection:
        connection.execute("DELETE FROM settings WHERE key LIKE 'publish_%_enabled'")
    monkeypatch.setattr(ready, "_CACHE", {"at": 0.0, "data": None})
    yield


def _enable(platform: str, value: bool = True) -> None:
    ready.toggle_platform(platform, value)


# ---------- 单项判定 ----------

def test_xhs_off_is_optional_not_blocking():
    item = ready._xhs_item({"publish_xhs_enabled": "0"}, {})
    assert item.ok is False
    assert item.optional is True          # 没开不算"卡住"，只是建议打开
    assert item.action == "enable_xiaohongshu"
    assert "打开" in item.detail


def test_xhs_enabled_but_down_says_start_it():
    _enable("xiaohongshu")
    from app.db import get_settings

    item = ready._xhs_item(get_settings(), {"reachable": False, "logged_in": False})
    assert item.ok is False
    assert item.optional is False
    assert item.action == "start_xiaohongshu"
    assert item.action_label == "帮我启动"


def test_xhs_up_but_logged_out_asks_for_login():
    _enable("xiaohongshu")
    from app.db import get_settings

    item = ready._xhs_item(get_settings(), {"reachable": True, "logged_in": False})
    assert item.action == "login_xiaohongshu"


def test_xhs_ready():
    _enable("xiaohongshu")
    from app.db import get_settings

    item = ready._xhs_item(get_settings(), {"reachable": True, "logged_in": True})
    assert item.ok is True
    assert item.optional is False


def test_douyin_without_browser_offers_download_page(monkeypatch):
    """一个浏览器都没装时，不能只留一句话——得给一个能点的下载入口。"""
    _enable("douyin")
    monkeypatch.setattr(ready, "_find_browser", lambda: ("", ""))
    from app.db import get_settings

    item = ready._douyin_item(get_settings(), {"reachable": False})
    assert item.ok is False
    assert "下载页" in item.detail
    assert item.action == "install_browser"
    assert item.action_label


def test_douyin_uses_edge_when_chrome_missing(monkeypatch):
    """Windows 自带 Edge 也是 Chromium，能挂调试端口——不该逼人先去装 Chrome。"""
    _enable("douyin")
    monkeypatch.setattr(
        ready, "_find_browser", lambda: (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "Edge")
    )
    from app.db import get_settings

    item = ready._douyin_item(get_settings(), {"reachable": False})
    assert item.ok is False
    assert item.action == "start_douyin"          # 有浏览器 = 有下一步，不是死路


def test_douyin_up_but_logged_out_opens_login_page(monkeypatch):
    _enable("douyin")
    monkeypatch.setattr(ready, "_find_browser", lambda: (r"C:\chrome.exe", "Chrome"))
    from app.db import get_settings

    item = ready._douyin_item(get_settings(), {"reachable": True, "logged_in": False})
    assert item.action == "start_douyin"
    assert item.action_label == "打开登录页"


# ---------- 结论 ----------

def test_headline_when_everything_off(monkeypatch):
    monkeypatch.setattr(ready, "_render_item", lambda settings: ready.Item("render", "出片", True, "在线"))
    monkeypatch.setattr(ready, "_platform_probe", lambda settings: ({}, {}))
    report = ready.check(force=True)
    assert report["ready"] is True
    assert "可以出片" in report["headline"]
    # 两个平台没开，但都不算 blocking
    assert report["blocking"] == 0
    assert report["optional"] == 2


def test_headline_blocks_when_render_is_down(monkeypatch):
    monkeypatch.setattr(
        ready, "_render_item", lambda settings: ready.Item("render", "出片", False, "连不上", "open_settings", "去看设置")
    )
    report = ready.check(force=True)
    assert report["ready"] is False
    assert report["blocking"] == 1
    assert "还不能开工" in report["headline"]


def test_headline_counts_publishing_gaps(monkeypatch):
    monkeypatch.setattr(ready, "_render_item", lambda settings: ready.Item("render", "出片", True, "在线"))
    _enable("xiaohongshu")
    monkeypatch.setattr(ready, "_platform_probe", lambda settings: ({}, {}))
    report = ready.check(force=True)
    # 出片能用 = 还能干活；但发布那一项没配好，所以不是全绿
    assert report["can_render"] is True
    assert report["ready"] is False
    assert report["blocking"] == 1
    assert "还差 1 件事" in report["headline"]


def test_check_is_cached_until_forced(monkeypatch):
    calls = {"n": 0}

    def fake_render(settings):
        calls["n"] += 1
        return ready.Item("render", "出片", True, "在线")

    monkeypatch.setattr(ready, "_render_item", fake_render)
    monkeypatch.setattr(ready, "_platform_probe", lambda settings: ({}, {}))

    ready.check(force=True)
    ready.check()
    ready.check()
    assert calls["n"] == 1
    ready.check(force=True)
    assert calls["n"] == 2


# ---------- 一键修 ----------

def test_toggle_platform_persists():
    assert ready.toggle_platform("xiaohongshu", True)["ok"] is True
    from app.db import get_settings

    assert get_settings()["publish_xhs_enabled"] == "1"
    ready.toggle_platform("xiaohongshu", False)
    assert get_settings()["publish_xhs_enabled"] == "0"


def test_toggle_rejects_unknown_platform():
    assert ready.toggle_platform("weibo", True)["ok"] is False


def test_start_xhs_reports_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(ready, "XHS_EXE", tmp_path / "missing.exe")
    monkeypatch.setattr(ready, "_port_open", lambda port, *a, **k: False)
    result = ready.start_xiaohongshu()
    assert result["ok"] is False
    assert "fetch_publish_tools" in result["message"]


def test_start_xhs_is_noop_when_already_running(monkeypatch):
    monkeypatch.setattr(ready, "_port_open", lambda port, *a, **k: True)
    result = ready.start_xiaohongshu()
    assert result["ok"] is True
    assert "本来就在跑" in result["message"]


# ---------- 诊断 ----------

def test_diagnose_text_is_pasteable(monkeypatch):
    monkeypatch.setattr(ready, "_render_item", lambda settings: ready.Item("render", "出片", True, "在线"))
    monkeypatch.setattr(ready, "_platform_probe", lambda settings: ({}, {}))
    # 诊断文本里那一段是直接问平台的，测试里不要真去连
    monkeypatch.setattr(
        "app.publish.platform_status",
        lambda settings=None: {"xiaohongshu": {"reachable": True, "logged_in": False}, "douyin": {}},
    )
    text = ready.diagnose()["text"]
    assert "开工体检" in text
    assert "队列" in text
    assert "出片" in text
    # 不能出现只有开发者才看得懂的词
    for jargon in ("MCP", "CDP", "Traceback"):
        assert jargon not in text.split("【最近的报错】")[0]


# ---------- 开机自启 ----------

def test_autostart_does_nothing_when_platforms_are_off(monkeypatch):
    calls = []
    monkeypatch.setattr(ready, "start_xiaohongshu", lambda: calls.append("xhs") or {"ok": True})
    monkeypatch.setattr(ready, "start_douyin", lambda: calls.append("dy") or {"ok": True})
    assert ready.autostart() == {}
    assert calls == []


def test_autostart_only_touches_enabled_platforms(monkeypatch):
    calls = []
    monkeypatch.setattr(ready, "start_xiaohongshu", lambda: calls.append("xhs") or {"ok": True})
    monkeypatch.setattr(ready, "start_douyin", lambda: calls.append("dy") or {"ok": True})
    _enable("xiaohongshu")
    started = ready.autostart()
    assert list(started) == ["xiaohongshu"]
    assert calls == ["xhs"]


def test_autostart_never_opens_the_douyin_browser(monkeypatch):
    """抖音要开可见窗口，没点按钮就弹出来等于吓人，一律等用户自己点。"""
    calls = []
    monkeypatch.setattr(ready, "start_xiaohongshu", lambda: calls.append("xhs") or {"ok": True})
    monkeypatch.setattr(ready, "start_douyin", lambda: calls.append("dy") or {"ok": True})
    _enable("xiaohongshu")
    _enable("douyin")

    started = ready.autostart()

    assert list(started) == ["xiaohongshu"]
    assert calls == ["xhs"]


def test_autostart_swallows_failures(monkeypatch):
    """起不来只是少个功能，不能让整个网页跟着挂。"""
    _enable("xiaohongshu")

    def boom():
        raise OSError("xhs missing")

    monkeypatch.setattr(ready, "start_xiaohongshu", boom)
    assert ready.autostart() == {}
