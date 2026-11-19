from unittest.mock import Mock

import pytest

import bot
import checks_writer as writer
import sheets_util


@pytest.mark.parametrize(("role", "allowed", "expected"), [
    (bot.TUTOR, None, None),
    (bot.TUTOR, ["Анна"], None),
    (bot.TUTOR, ["Ольга"], "Ольга"),
    (bot.OWNER, None, "Ольга"),
    (None, ["Ольга"], None),
])
def test_saved_view_respects_current_permissions(monkeypatch, role, allowed, expected):
    monkeypatch.setattr(bot, "load_state", lambda: {"view_as": {"1": "Ольга"}})
    monkeypatch.setattr(bot, "_view_allowed", lambda uid: allowed)
    monkeypatch.setattr(bot, "role_of", lambda uid: role)
    assert bot.view_as_name(1) == expected


def test_my_lesson_requires_unique_match_for_current_tutor(monkeypatch):
    monkeypatch.setattr(bot, "name_by_id", lambda uid: "Анна")
    own = {"id": "42", "репетитор": "Анна"}
    other = {"id": "42", "репетитор": "Ольга"}
    rows = [other]
    monkeypatch.setattr(writer, "read_lessons", lambda: rows)
    assert bot._my_lesson(1, "42") is None
    rows.append(own)
    assert bot._my_lesson(1, "42") == own
    rows.append(dict(own, лист="Другой лист"))
    assert bot._my_lesson(1, "42") is None


@pytest.mark.parametrize("delete", [False, True])
def test_edit_rechecks_owner_and_sheet(monkeypatch, delete):
    target = {"id": "42", "репетитор": "Анна", "spreadsheet_id": "private",
              "sheet_title": "Занятия"}
    rows = [[], ["", "", "Ольга"] + [""] * 8 + ["42"]]
    read = Mock(return_value=rows)
    write, clear = Mock(), Mock()
    monkeypatch.setattr(writer, "get_values", read)
    monkeypatch.setattr(writer, "update_values", write)
    monkeypatch.setattr(sheets_util, "clear_values", clear)
    action = {"delete": True} if delete else {"amount": 2000}
    assert not writer.change_tutor_lesson(target, "Анна", **action)
    read.assert_called_once_with("private", "'Занятия'!A1:L", fresh=True)
    write.assert_not_called()
    clear.assert_not_called()
    rows.append(["", "", "Анна"] + [""] * 8 + ["42"])
    assert writer.change_tutor_lesson(target, "Анна", **action)
    if delete:
        clear.assert_called_once_with("private", "'Занятия'!A3:L3")
    else:
        write.assert_called_once_with("private", "'Занятия'!F3", [[2000]])
