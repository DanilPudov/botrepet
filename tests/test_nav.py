"""Тесты навигации: кнопка «Назад» должна вести на предыдущий экран."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class Env:
    def __init__(self, tmp_path: Path, monkeypatch):
        import sheets_util
        import students
        self.home = tmp_path
        self.mod = sheets_util
        self.monkeypatch = monkeypatch
        cfg = {"owner_ids": [], "access": "ids", "sheets": {
            "history": "history", "recept": "recept", "history_sheet": "История",
            "recept_checks_sheet": "Чеки", "students_sheet": "Ученики"}}
        monkeypatch.setattr(students, "students_of", lambda *args, **kwargs: [])
        (tmp_path / "config.json").write_text(json.dumps(cfg, ensure_ascii=False),
                                              encoding="utf-8")
        (tmp_path / "state.json").write_text(json.dumps({
            "version": 1,
            "tutors": [{"id": 1, "name": "Анна"}, {"id": 2, "name": "Пётр"}],
            "pending": {}, "last_seen_tutor": {},
        }, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(sheets_util, "CONFIG_PATH", tmp_path / "config.json")
        monkeypatch.setattr(sheets_util, "STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(sheets_util, "LOG_PATH", tmp_path / "bot.log")

    def nav(self, uid: int) -> dict:
        st = json.loads((self.home / "state.json").read_text(encoding="utf-8"))
        return (st.get("nav") or {}).get(str(uid), {})


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def _ctx():
    return SimpleNamespace(args=[], bot=SimpleNamespace(
        send_message=AsyncMock(), delete_message=AsyncMock()))


async def _click(bot, uid: int, data: str) -> None:
    q = SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid),
                        answer=AsyncMock(), edit_message_text=AsyncMock())
    q.message = SimpleNamespace(chat_id=uid, message_id=5, delete=AsyncMock())
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=uid),
                          callback_query=q, message=None, effective_message=None)
    await bot.handle_callback(upd, _ctx())


def run(coro):
    return asyncio.run(coro)


def test_back_returns_previous_screen(env):
    import bot
    run(_click(bot, 1, "more"))
    run(_click(bot, 1, "students"))
    assert env.nav(1)["current"] == "students"
    run(_click(bot, 1, "nav:back"))
    assert env.nav(1)["current"] == "more"
    run(_click(bot, 1, "nav:back"))
    assert env.nav(1)["current"] == "menu"


def test_back_from_root_goes_menu(env):
    import bot
    run(_click(bot, 1, "menu"))
    run(_click(bot, 1, "nav:back"))
    assert env.nav(1)["current"] == "menu"


def test_home_resets_history(env):
    import bot
    run(_click(bot, 1, "more"))
    run(_click(bot, 1, "students"))
    run(_click(bot, 1, "menu"))
    nav = env.nav(1)
    assert nav["current"] == "menu"
    assert nav.get("stack") == []


def test_nav_isolated_between_users(env):
    import bot
    run(_click(bot, 1, "more"))
    run(_click(bot, 2, "mine"))
    assert env.nav(1)["current"] == "more"
    assert env.nav(2)["current"] == "mine"
    run(_click(bot, 2, "nav:back"))
    assert env.nav(2)["current"] == "menu"
    assert env.nav(1)["current"] == "more"


def test_many_back_clicks_safe(env):
    import bot
    run(_click(bot, 1, "more"))
    for _ in range(10):
        run(_click(bot, 1, "nav:back"))
    assert env.nav(1)["current"] in ("menu", "more")


def test_schedule_navigation_chain(env):
    import bot
    for d in ("menu", "sched:today", "sched:week", "schday:1"):
        run(_click(bot, 1, d))
    assert env.nav(1)["current"] == "schday:1"
    run(_click(bot, 1, "nav:back"))
    assert env.nav(1)["current"] == "sched:week"
    run(_click(bot, 1, "nav:back"))
    assert env.nav(1)["current"] == "sched:today"
