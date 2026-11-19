import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot
import sheets_util
import workspaces


@pytest.mark.parametrize("account", ["workspace", "test", "anyone", "invited"])
def test_revoked_access_survives_restart_and_can_be_restored(tmp_path, monkeypatch, account):
    owner, tutor = 101, 102
    monkeypatch.setattr(workspaces, "WORKSPACE_TUTORS", [(tutor, "Пример")] if account == "workspace" else [])
    monkeypatch.setattr(workspaces, "TEST_ACCOUNT_ID", tutor if account == "test" else 103)
    monkeypatch.setattr(sheets_util, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(bot, "log_action", lambda *args: None)
    access = {"anyone": "anyone", "invited": "invite_code"}.get(account, "list")
    monkeypatch.setattr(bot, "load_config", lambda: {"owner_ids": [owner], "access": access})
    sheets_util.save_state({"tutors": [], "invited": [tutor] if account == "invited" else []})
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=owner), message=message)
    context = SimpleNamespace(args=[str(tutor), "Пример"])

    assert bot.role_of(tutor) == bot.TUTOR
    asyncio.run(bot.remove_tutor(update, context))
    assert bot.role_of(tutor) is None
    callback = SimpleNamespace(data="nav:back", from_user=SimpleNamespace(id=tutor), answer=AsyncMock())
    asyncio.run(bot.handle_callback(SimpleNamespace(callback_query=callback), SimpleNamespace()))
    callback.answer.assert_awaited_once_with("Нет доступа", show_alert=True)
    workspaces.ensure_tutors(sheets_util.load_state, sheets_util.save_state)
    assert sheets_util.load_state()["tutors"] == []
    assert bot.role_of(tutor) is None
    asyncio.run(bot.add_tutor(update, context))
    assert bot.role_of(tutor) == bot.TUTOR
    assert sheets_util.load_state()["tutors"] == [{"id": tutor, "name": "Пример"}]
