"""Интерактивные сценарии через реальные обработчики с изолированными данными."""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import sys
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pkgs"))

import pytest
import bot


def text_update(uid, text):
    user = SimpleNamespace(id=uid, first_name="Тест", username=f"test_{uid}")
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(effective_user=user, effective_message=message,
                           message=message, callback_query=None)


def cb_update(uid, data):
    update = text_update(uid, "")
    update.callback_query = SimpleNamespace(
        data=data, from_user=update.effective_user, message=update.message,
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    update.message = None
    return update


def make_context():
    return SimpleNamespace(args=[], bot=SimpleNamespace(send_message=AsyncMock()))


def save_button(uid, *, force=False):
    prefix = "save:force" if force else "save"
    return f"{prefix}:{bot.get_pending(uid).get('confirm_token', '')}"


ACTORS = {
    "owner": (101, "Данил Пудов"),
    "tutor": (102, "Анна Волкова"),
    "max": (993211158, "Максим Пудов"),
    "vika": (5992545540, "Виктория Пудова"),
    "test-view": (bot._ws.TEST_ACCOUNT_ID, "Максим Пудов"),
}
DAYS = ("today", "yesterday", "dated-button", "calendar")
AMOUNTS = {
    "preset-1000": ("amount:1000", None, 1000),
    "preset-1500": ("amount:1500", None, 1500),
    "preset-2500": ("amount:2500", None, 2500),
    "card-rate": ("rate:use", None, 1900),
    "plain": ("amount:custom", "1750", 1750),
    "grouped": ("amount:custom", "1 700,50 ₽", 1700),
    "mixed-separators": ("amount:custom", "2.450,75", 2451),
}
TIMES = {
    "early-slot": (("time:slot:08:00",), None, "08:00"),
    "late-slot": (("time:slot:19:00",), None, "19:00"),
    "now": (("time:now",), None, "12:34"),
    "skip-confirmed": (("time:skip", "time:skip_confirm"), None, ""),
    "typed-colon": ((), "17:35", "17:35"),
    "manual-compact": (("time:manual",), "945", "09:45"),
    "manual-dot": (("time:manual",), "7.05", "07:05"),
}
JOURNEYS = [
    case for case in product(ACTORS, DAYS, AMOUNTS, TIMES)
    if case[3] != "now" or case[1] == "today"
]


class Sandbox:
    def __init__(self, monkeypatch):
        self.state = {
            "tutors": [{"id": uid, "name": name, "call_name": name.split()[0]}
                       for actor, (uid, name) in ACTORS.items() if actor != "test-view"],
            "view_as": {str(ACTORS["test-view"][0]): ACTORS["max"][1]},
            "revoked_tutor_ids": [104],
        }
        self.roster = [
            {"id": "AP201101", "ученик": "Алекс", "родитель": "Рита",
             "тариф": "", "row": 2},
            {"id": "AP201102", "ученик": "Алекс", "родитель": "Лина",
             "тариф": "", "row": 3},
        ]
        self.append = Mock(return_value=9)
        self.read_lessons = Mock(return_value=[])
        self.delete_schedule = Mock(return_value=True)
        self.delete_student = Mock(return_value=True)
        self.set_payment = Mock(return_value=True)
        self.set_suggestion = Mock(return_value=True)
        self.update_schedule = Mock(return_value=True)
        self.send = AsyncMock()
        self.context = make_context()
        monkeypatch.setattr(bot, "load_state", lambda: copy.deepcopy(self.state))
        monkeypatch.setattr(bot, "save_state", self.save)
        monkeypatch.setattr(bot, "load_config", lambda: {"owner_ids": [101], "access": "list"})
        monkeypatch.setattr(bot, "_cleanup_extras", AsyncMock())
        monkeypatch.setattr(bot, "_cleanup_tracked", AsyncMock())
        monkeypatch.setattr(bot, "_delete_prev", AsyncMock())
        monkeypatch.setattr(bot, "_delete_user_msg", AsyncMock())
        monkeypatch.setattr(bot, "_maybe_send_note", AsyncMock())
        monkeypatch.setattr(bot, "_send", self.send)
        monkeypatch.setattr(bot, "notify_owner", AsyncMock())
        monkeypatch.setattr(bot, "log", Mock())
        monkeypatch.setattr(bot, "log_action", Mock())
        monkeypatch.setattr(bot, "_touch_activity", Mock())
        monkeypatch.setattr(bot, "_NAV_PENDING", {})
        monkeypatch.setattr(bot.checks_writer, "append_check", self.append)
        monkeypatch.setattr(bot.checks_writer, "read_lessons", self.read_lessons)
        monkeypatch.setattr(bot.checks_writer, "read_lessons_for_save", self.read_lessons)
        monkeypatch.setattr(bot.checks_writer, "set_lesson_paid", self.set_payment)
        monkeypatch.setattr(bot.checks_writer, "target_name", lambda tutor: "Test")
        monkeypatch.setattr(bot.checks_writer, "now_hhmm", lambda: "12:34")
        monkeypatch.setattr(bot.students_module, "students_of", lambda tutor: copy.deepcopy(self.roster))
        monkeypatch.setattr(bot.students_module, "find_student", lambda *a, **kw: None)
        monkeypatch.setattr(bot.students_module, "set_rate", Mock(return_value=True))
        monkeypatch.setattr(bot.students_module, "delete_student", self.delete_student)
        monkeypatch.setattr(bot.schedule_module, "get_entry", Mock(return_value=None))
        monkeypatch.setattr(bot.schedule_module, "delete_entry", self.delete_schedule)
        monkeypatch.setattr(bot.schedule_module, "update_entry", self.update_schedule)
        monkeypatch.setattr(bot.schedule_module, "entries_of", Mock(return_value=[]))
        monkeypatch.setattr(bot.schedule_module, "cancels_for", Mock(return_value=[]))
        monkeypatch.setattr(bot.suggestions_module, "set_status", self.set_suggestion)

    def save(self, state):
        self.state = copy.deepcopy(state)

    async def click(self, uid, data):
        update = cb_update(uid, data)
        await bot.handle_callback(update, self.context)
        for call in update.callback_query.edit_message_text.await_args_list:
            markup = call.kwargs.get("reply_markup")
            if markup:
                assert all(1 <= len(button.callback_data.encode()) <= 64
                           for row in markup.inline_keyboard for button in row
                           if button.callback_data)
        return update.callback_query

    async def type(self, uid, text):
        await bot.handle_text(text_update(uid, text), self.context)

    def no_write(self):
        for mutation in (self.append, self.delete_schedule, self.delete_student,
                         self.set_payment, self.set_suggestion, self.update_schedule):
            mutation.assert_not_called()


@pytest.fixture
def aud(monkeypatch):
    network = Mock(side_effect=AssertionError("Тест не должен обращаться к Google"))
    monkeypatch.setattr(bot.sheets_util, "sheets", network)
    env = Sandbox(monkeypatch)
    yield env
    network.assert_not_called()


def lesson_draft(tutor):
    return {"шаг": "подтверждение", "репетитор": tutor, "confirm_token": "qa-current",
            "ученик": "Алекс", "родитель": "Рита",
            "дата": dt.date.today().strftime("%d.%m.%Y"),
            "сумма": "1500", "время": "17:00"}


@pytest.mark.parametrize("actor,date_kind,amount_kind,time_kind", JOURNEYS,
                         ids=["/".join(case) for case in JOURNEYS])
def test_lesson_journey(aud, actor, date_kind, amount_kind, time_kind):
    uid, tutor = ACTORS[actor]
    amount_button, amount_text, expected_amount = AMOUNTS[amount_kind]
    time_buttons, time_text, expected_time = TIMES[time_kind]
    if amount_kind == "card-rate":
        aud.roster[0]["тариф"] = str(expected_amount)
    today = dt.date.today()
    expected_day = today - dt.timedelta(days=DAYS.index(date_kind))

    async def journey():
        await aud.click(uid, "new")
        await aud.click(uid, "stu:AP201101")
        if date_kind in ("today", "yesterday"):
            await aud.click(uid, f"date:{date_kind}")
        elif date_kind == "dated-button":
            await aud.click(uid, f"date:{expected_day:%d.%m.%Y}")
        else:
            await aud.click(uid, "date:cal")
            await aud.click(uid, f"dd:{expected_day:%Y-%m-%d}")
        await aud.click(uid, amount_button)
        if amount_text is not None:
            await aud.type(uid, amount_text)
        for button in time_buttons:
            await aud.click(uid, button)
        if time_text is not None:
            await aud.type(uid, time_text)
            await aud.click(uid, "first:yes")
            await aud.click(uid, "commission:0")
        await aud.click(uid, "edit:with")
        await aud.click(uid, "with:Нормально")
        await aud.click(uid, "edit:will")
        await aud.click(uid, "will:yes")
        await aud.click(uid, "edit:comment")
        await aud.type(uid, "Записка: <сверка> & сумма")
        button = save_button(uid)
        saved = await aud.click(uid, button)
        assert "Записано" in saved.edit_message_text.await_args.args[0]
        await aud.click(uid, button)

    asyncio.run(journey())
    aud.append.assert_called_once()
    record = aud.append.call_args.args[0]
    assert record["репетитор"] == tutor
    assert (record["ученик"], record["родитель"]) == ("Алекс", "Рита")
    assert record["дата"] == expected_day.strftime("%d.%m.%Y")
    assert record["сумма"] == str(expected_amount)
    assert record["время"] == expected_time
    assert record["как_прошло"] == "Нормально"
    assert record["будут"] == "Да"
    assert record["комментарий"] == "Записка: <сверка> & сумма"
    assert record["комиссия"] == ("0" if time_text is not None else "")
    assert not bot.get_pending(uid)


INVALID_AMOUNTS = ("", "нет", "-1500", "0", "17:30", "2+2", "1-2",
                   "1 70", "1/500", "500$","500€", "15,,00", "1.2.3", "1e6",
                   "9"*400, "🎈", "1_500", "1*500")
INVALID_TIMES = ("", "25:00", "15:60", "12:30:00", "1::20", "-05:00",
                 "²:00", "㊵", "999999", "abc", "17_30", "1:007", "99",
                 "27.11", "13:ab", "12e3", "1/7")
FIELD_CASES = (
    [("сумма_текст", text) for text in INVALID_AMOUNTS]
    + [("время", text) for text in INVALID_TIMES]
    + [("время_текст", text) for text in INVALID_TIMES]
    + [("комиссия", text) for text in INVALID_AMOUNTS if text != "0"]
)


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("step,text", FIELD_CASES, ids=[f"{i:02d}-{s}" for i,(s,_) in enumerate(FIELD_CASES)])
def test_invalid_input_keeps_draft(aud, actor, step, text):
    uid, tutor = ACTORS[actor]
    draft = {**lesson_draft(tutor), "шаг": step}
    bot.set_pending(uid, draft)
    asyncio.run(aud.type(uid, text))
    aud.no_write()
    assert bot.get_pending(uid) == draft
    assert aud.send.await_count == 1


BAD_BUTTONS = (
    "schday:", "schday:7", "schday:-1", "schday:²", "addw:10", "addw:",
    "addw:вт", "student:-1", "student:1.5", "review:stu:-1", "review:stu:",
    "dd:2026-02-30", "dd:2026-13-05", "d:2026-0", "d:0000-1", "d:2026",
    "date:31.11.2026", "cxd:2026-11-31", "cx:2026-11-20:25:00",
    "uncancel:2026-11-20:", "uncancel:broken:10:00", "remind:was",
    "remind:no::20.11.2026", "remind:ok:M10:31.11.2026", "remind:unset:M10:20.11.2026",
)


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("button", BAD_BUTTONS)
def test_invalid_button_is_acknowledged_without_edit(aud, actor, button):
    uid, tutor = ACTORS[actor]
    draft = lesson_draft(tutor)
    bot.set_pending(uid, draft)
    q = asyncio.run(aud.click(uid, button))
    assert q.answer.await_count == 1
    assert q.answer.await_args.kwargs.get("show_alert")
    aud.no_write()
    assert bot.get_pending(uid) == draft


PRIVATE_BUTTONS = (
    "menu", "new", "mine", "pastweek", "students", "student:0", "student:AP201101",
    "sched:today", "sched:week", "sched:month", "schday:5", "addw:5", "sch:list",
    "sch:open:DS201101", "addonce", "cancel_lesson", "notes", "notes:edit",
    "reviews", "review:add", "referrals", "ref:add", "suggest", "admin", "logs",
    "view_mode", "view:0", "tcolor", "sedit:rate", "del:yes", "save", "save:force",
    "mdel:yes:AL201101", "pay:AL201101", "unpay:AL201101", "sdel:AS201101",
    "sch:del:AS201101", "sch:edit:AS201101", "sug:ok:2", "cleanall",
)


@pytest.mark.parametrize("uid", [103, 104], ids=["unknown", "revoked"])
@pytest.mark.parametrize("button", PRIVATE_BUTTONS)
def test_inaccessible_account_cannot_open_or_change(aud, uid, button):
    q = asyncio.run(aud.click(uid, button))
    assert q.answer.await_args.args == ("Нет доступа",)
    q.edit_message_text.assert_not_awaited()
    aud.no_write()


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("case", ("empty", "wrong-step", "wrong-tutor", "incomplete",
                                  "unconfirmed-duplicate", "wrong-duplicate-token"))
def test_stale_save_cannot_write(aud, actor, case):
    uid, tutor = ACTORS[actor]
    draft = lesson_draft(tutor)
    button = "save:qa-current"
    if case == "empty":
        draft = {}
    elif case == "wrong-step":
        draft["шаг"] = "время"
    elif case == "wrong-tutor":
        draft["репетитор"] = "Иной репетитор"
    elif case == "incomplete":
        draft["сумма"] = ""
    else:
        button = "save:force:qa-current"
        if case == "wrong-duplicate-token":
            draft["duplicate_confirmation"] = "старое подтверждение"
    bot.set_pending(uid, draft)
    q = asyncio.run(aud.click(uid, button))
    assert q.answer.await_args.kwargs.get("show_alert")
    aud.no_write()


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("kind", ("same-lesson", "different-parent", "different-time", "read-failure"))
def test_duplicate_checks_do_not_confuse_namesakes(aud, actor, kind):
    uid, tutor = ACTORS[actor]
    draft = lesson_draft(tutor)
    bot.set_pending(uid, draft)
    existing = {**draft, "дата": dt.date.today(), "сумма": 1500}
    if kind == "different-parent":
        existing["родитель"] = "Лина"
    elif kind == "different-time":
        existing["время"] = "18:00"
    if kind == "read-failure":
        aud.read_lessons.side_effect = TimeoutError()
    else:
        aud.read_lessons.return_value = [existing]
    q = asyncio.run(aud.click(uid, save_button(uid)))
    if kind == "same-lesson":
        aud.append.assert_not_called()
        assert "уже записано" in q.edit_message_text.await_args.args[0]
        asyncio.run(aud.click(uid, save_button(uid, force=True)))
        aud.append.assert_called_once()
    elif kind == "read-failure":
        aud.append.assert_not_called()
        assert "проверить" in q.edit_message_text.await_args.args[0]
    else:
        aud.append.assert_called_once()


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("button", ("sdel:AS201101", "sch:del:AS201101", "sch:edit:AS201101"))
def test_unavailable_schedule_record_is_not_mutated(aud, actor, button):
    uid, tutor = ACTORS[actor]
    asyncio.run(aud.click(uid, button))
    bot.schedule_module.get_entry.assert_called_once_with("AS201101", tutor)
    aud.no_write()


@pytest.mark.parametrize("actor", ACTORS)
def test_stale_student_delete_is_not_mutated(aud, actor):
    uid, tutor = ACTORS[actor]
    bot.set_pending(uid, lesson_draft(tutor))
    asyncio.run(aud.click(uid, "del:yes"))
    aud.no_write()


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("where", ("student:", "review:stu:", "stu:"))
def test_reordered_roster_uses_stable_id(aud, actor, where):
    uid, tutor = ACTORS[actor]
    aud.roster.reverse()
    bot.set_pending(uid, {"репетитор": tutor, "шаг": "ученик"})
    asyncio.run(aud.click(uid, f"{where}AP201101"))
    current = bot.get_pending(uid)
    if where == "review:stu:":
        assert current["отзыв_ученик"] == "Алекс"
    else:
        assert current["родитель"] == "Рита"


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("message", ("Задать задачу", "A_B [тест](ссылка)", "<проверка>&",
                                     "Первая строка\nВторая строка", "x" * 2001))
def test_notes_edit_and_render(aud, monkeypatch, actor, message):
    uid, tutor = ACTORS[actor]
    stored = {}
    def save(name, text):
        stored[name] = text
        return True
    monkeypatch.setattr(bot.notes_module, "set_note", save)
    monkeypatch.setattr(bot.notes_module, "get_note", lambda name: stored.get(name, ""))
    async def journey():
        await aud.click(uid, "notes:edit")
        await aud.type(uid, message)
        return await aud.click(uid, "notes")
    q = asyncio.run(journey())
    import html
    assert stored == {tutor: message[:2000]}
    screen = q.edit_message_text.await_args
    assert screen.kwargs["parse_mode"] == "HTML"
    assert html.escape(message[:2000]) in screen.args[0]
    assert not bot.get_pending(uid)


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("result", ("deleted", "absent", "timeout"))
def test_note_clear_reports_real_result(aud, monkeypatch, actor, result):
    uid, tutor = ACTORS[actor]
    clear = Mock(return_value=result == "deleted")
    if result == "timeout":
        clear.side_effect = TimeoutError()
    monkeypatch.setattr(bot.notes_module, "clear_note", clear)
    q = asyncio.run(aud.click(uid, "notes:clear"))
    clear.assert_called_once_with(tutor)
    assert (q.edit_message_text.await_args.args[0].endswith("Заметка очищена.")) == (result == "deleted")


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("kind", ("fresh", "repeat", "self", "too-short"))
def test_referral_recording(aud, monkeypatch, actor, kind):
    uid, tutor = ACTORS[actor]
    add = Mock()
    monkeypatch.setattr(bot.referrals_module, "add_referral", add)
    monkeypatch.setattr(bot.referrals_module, "has_referral", Mock(return_value=kind == "repeat"))
    value = {"self": tutor, "too-short": "А"}.get(kind, "Новый Репетитор")
    async def journey():
        await aud.click(uid, "ref:add")
        await aud.type(uid, value)
    asyncio.run(journey())
    if kind == "fresh":
        add.assert_called_once_with(tutor, value)
    else:
        add.assert_not_called()
        assert bot.get_pending(uid)["шаг"] == "реферал_имя"


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("student", ("AP201101", "AP201102", "none"))
@pytest.mark.parametrize("review", ("", "Пришёл отзыв", "https://example.com/review"))
def test_review_from_correct_student(aud, monkeypatch, actor, student, review):
    uid, tutor = ACTORS[actor]
    aud.roster[1]["ученик"] = "Эрик"
    add = Mock(return_value=7)
    monkeypatch.setattr(bot.reviews_module, "add_review", add)
    async def journey():
        await aud.click(uid, "review:add")
        await aud.click(uid, f"review:stu:{student}")
        await aud.type(uid, review)
    asyncio.run(journey())
    if review:
        assert add.call_args.args == (tutor, {"AP201101": "Алекс", "AP201102": "Эрик", "none": ""}[student], review)
    else:
        add.assert_not_called()
        assert bot.get_pending(uid)["шаг"] == "отзыв_текст"


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("behavior", ("confirm", "cancel", "another-token", "repeat"))
def test_schedule_deletion_requires_current_confirmation(aud, monkeypatch, actor, behavior):
    uid, tutor = ACTORS[actor]
    record = {"id": "DS201101", "репетитор": tutor, "sh_id": "book"}
    monkeypatch.setattr(bot.schedule_module, "get_entry", Mock(return_value=record))
    monkeypatch.setattr(bot, "show_student_schedule", AsyncMock())
    async def journey():
        ask = await aud.click(uid, "sch:del:DS201101")
        assert "Удалить" in ask.edit_message_text.await_args.args[0]
        aud.delete_schedule.assert_not_called()
        token = bot.get_pending(uid)["schedule_delete"]["token"]
        if behavior == "another-token":
            await aud.click(uid, "sch:del:yes:expired")
        else:
            if behavior == "cancel":
                await aud.click(uid, "cancel")
            await aud.click(uid, f"sch:del:yes:{token}")
            if behavior == "repeat":
                await aud.click(uid, f"sch:del:yes:{token}")
    asyncio.run(journey())
    if behavior in ("confirm", "repeat"):
        aud.delete_schedule.assert_called_once_with("DS201101", tutor, "book")
    else:
        aud.delete_schedule.assert_not_called()


@pytest.mark.parametrize("actor", ACTORS)
def test_old_save_confirmation_does_not_save_new_draft(aud, actor):
    uid, tutor = ACTORS[actor]
    draft = lesson_draft(tutor)
    bot.set_pending(uid, draft)
    stale = save_button(uid)
    draft["confirm_token"] = "qa-replacement"
    draft["сумма"] = "2200"
    bot.set_pending(uid, draft)
    alert = asyncio.run(aud.click(uid, stale))
    assert alert.answer.await_args.kwargs.get("show_alert")
    aud.append.assert_not_called()
    asyncio.run(aud.click(uid, save_button(uid)))
    assert aud.append.call_args.args[0]["сумма"] == "2200"


@pytest.mark.parametrize("raw,expected", [
    ("1,700.50", 1700), ("1 000 000 ₽", 1000000), ("1.000.000", 1000000),
    ("1 000 001", None), ("1.000.01", None), ("1,700,000", None),
    ("1\u202f700", 1700), ("1500.51", 1501), ("руб 500", None),
])
def test_money_boundaries(raw, expected):
    assert bot.sheets_util.parse_amount(raw) == expected


@pytest.mark.parametrize("distinct", [True, False], ids=["distinct-id", "same-id"])
@pytest.mark.parametrize("amount_column", [4, 5], ids=["legacy-E", "current-F"])
def test_reconciliation_recognizes_moved_lesson_by_id(monkeypatch, distinct, amount_column):
    import reconcile
    day = dt.date(2026, 11, 21)
    stored_id = "AL211101"
    other_id = "AL211102" if distinct else stored_id
    header = [""] * 12
    header[amount_column] = "Сумма"
    history = ["17:00", "21.11.2026", "Репетитор", "Ученик"] + [""] * 8
    history[amount_column] = "1500"
    history[11] = stored_id
    pending = {"репетитор": "Репетитор", "дата": day, "ученик": "Ученик",
               "сумма": 1500, "id": other_id}
    monkeypatch.setattr(reconcile, "load_config", lambda: {"sheets": {"history": "book", "history_sheet": "History"}})
    monkeypatch.setattr(reconcile, "get_values", lambda *a: [header, history])
    fresh, dups = reconcile.drop_duplicates([pending])
    assert fresh == ([pending] if distinct else [])
    assert dups == ([] if distinct else [pending])


@pytest.mark.parametrize("count", [0, 1, 2, 4])
def test_restore_clears_all_existing_duplicate_cancellations(monkeypatch, count):
    schedule = bot.schedule_module
    day = dt.date(2026, 11, 21)
    monkeypatch.setattr(schedule, "sheet_for", lambda tutor: "book")
    monkeypatch.setattr(schedule, "cancels_for", lambda tutor, **kw:
                        [{"row": i + 2, "id": str(i), "дата": day, "время": "17:00"}
                         for i in range(count)]
                        + [{"row": 20, "id": "20", "дата": day, "время": "18:00"}])
    batch = Mock()
    monkeypatch.setattr(schedule.sheets_util, "update_values_batch", batch)
    assert schedule.remove_cancel("Репетитор", day, "17:00") is bool(count)
    if count:
        batch.assert_called_once()
        assert len(batch.call_args.args[1]) == count
        assert all("A20:" not in row["range"] for row in batch.call_args.args[1])
    else:
        batch.assert_not_called()


@pytest.mark.parametrize("owner", "ADMV")
@pytest.mark.parametrize("record", "LPSC")
@pytest.mark.parametrize("day", [dt.date(2024, 2, 29), dt.date(2026, 1, 1), dt.date(2026, 12, 31)])
def test_id_uniqueness_for_each_record_type_and_date(owner, record, day):
    stem = f"{owner}{record}{day:%d%m}"
    used = [stem + "01", stem + "2", stem + "03"]
    assert bot.sheets_util.next_record_id(owner + record, day, used) == stem + "04"
    with pytest.raises(ValueError):
        bot.sheets_util.next_record_id(owner + record, day, [stem + f"{i:02d}" for i in range(1,100)])


@pytest.mark.parametrize("uid", [103, 104], ids=["outsider", "revoked"])
@pytest.mark.parametrize("command", [bot.cmd_help, bot.cmd_mine, bot.cmd_new],
                         ids=lambda fn: fn.__name__)
def test_private_commands_decline_without_read(aud, uid, command):
    update = text_update(uid, "")
    asyncio.run(command(update, aud.context))
    assert update.message.reply_text.await_args.args == ("Нет доступа.",)
    aud.no_write()
    aud.read_lessons.assert_not_called()


@pytest.mark.parametrize("missing", ["current", "archive", None])
def test_pre_save_lookup_does_not_suppress_read_errors(monkeypatch, missing):
    writer = bot.checks_writer
    monkeypatch.setattr(writer, "load_config", lambda: {"sheets": {
        "recept": "current", "history": "archive", "history_sheet": "История"}})
    monkeypatch.setattr(writer, "_target", lambda tutor: ("current", "Чеки"))
    checked = []
    def read(book, range_, *, fresh):
        assert fresh
        checked.append((book, range_))
        if book == missing:
            raise TimeoutError()
        return [["Время"], ["17:00", "21.11.2026", "Репетитор", "Ученик", "",
                            "1500", "", "", "", "", "", "AL211101"]]
    monkeypatch.setattr(writer, "get_values", read)
    if missing:
        with pytest.raises(TimeoutError):
            writer.read_lessons_for_save("Репетитор")
    else:
        assert [item["id"] for item in writer.read_lessons_for_save("Репетитор")] == ["AL211101"] * 2
        assert [book for book, _ in checked] == ["current", "archive"]


@pytest.mark.parametrize("who", [103, 104])
def test_unauthorized_media_does_not_send_or_delete(aud, who):
    aud.context.bot.delete_message = AsyncMock()
    asyncio.run(bot.handle_photo(text_update(who, ""), aud.context))
    aud.context.bot.send_message.assert_not_awaited()
    aud.context.bot.delete_message.assert_not_awaited()


@pytest.mark.parametrize("start,end", product(range(7), repeat=2))
def test_weekday_ranges_include_every_day_in_order(start, end):
    schedule = bot.schedule_module
    first, last = schedule.DAY_NAMES[start], schedule.DAY_NAMES[end]
    actual = schedule.parse_days(f"{first} – {last}")
    assert actual == sorted((start + i) % 7 for i in range((end - start) % 7 + 1))


@pytest.mark.parametrize("text", ["иногда", "Срочно", "Пятилетка", "²", "1" * 200,
                                 "Пн-неизвестно", "20.11.2026", ""])
def test_invalid_schedule_days_do_not_turn_into_weekdays(text):
    assert bot.schedule_module.parse_days(text) == []


@pytest.mark.parametrize("field", ["inf", "-inf", "NaN", float("inf"), float("nan"), "1e999"])
def test_nonfinite_sheets_amount_does_not_crash(field):
    import reconcile
    assert reconcile.parse_int(field) is None
    assert bot.checks_writer._parse_sum(field) == 0


@pytest.mark.parametrize("actor", ACTORS)
def test_yesterdays_now_button_returns_to_time_selection(aud, actor):
    uid, tutor = ACTORS[actor]
    draft = {**lesson_draft(tutor), "шаг": "время", "время": ""}
    draft["дата"] = (dt.date.today() - dt.timedelta(days=1)).strftime("%d.%m.%Y")
    bot.set_pending(uid, draft)
    q = asyncio.run(aud.click(uid, "time:now"))
    assert bot.get_pending(uid)["время"] == ""
    options = [button.callback_data
               for row in q.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
               for button in row]
    assert "time:now" not in options


@pytest.mark.parametrize("student_id,parent,own_slots,other_slots", [
    ("AP201101", "Рита", ("Чт 18:00", "Вс 10:00"), ("Вс 13:00", "Пн 19:00")),
    ("AP201102", "Лина", ("Вс 13:00", "Пн 19:00"), ("Чт 18:00", "Вс 10:00")),
])
def test_namesakes_have_separate_schedule_screens(aud, monkeypatch, student_id, parent,
                                                   own_slots, other_slots):
    uid, tutor = ACTORS["owner"]
    slots = [("Рита", "Чт", "18:00"), ("Рита", "Вс", "10:00"),
             ("Лина", "Вс", "13:00"), ("Лина", "Пн", "19:00")]
    entries = [{"id": str(i), "репетитор": tutor, "ученик": "Алекс", "родитель": p,
                "дни": day, "время": time}
               for i, (p, day, time) in enumerate(slots, 1)]
    bot.schedule_module.entries_of.return_value = entries
    bot.schedule_module.get_entry.side_effect = lambda rid, tutor: next(
        (e for e in entries if e["id"] == rid), None)
    monkeypatch.setattr(bot.students_module, "find_student", lambda tutor, name, parent: next(
        (s for s in aud.roster if s["ученик"] == name and s["родитель"] == parent), None))
    say = AsyncMock()
    monkeypatch.setattr(bot, "_say", say)

    async def run():
        card = await aud.click(uid, f"student:{student_id}")
        card_text = card.edit_message_text.await_args.args[0]
        assert all(slot in card_text for slot in own_slots)
        assert all(slot not in card_text for slot in other_slots)
        await aud.click(uid, "sch:list")
        markup = say.await_args.kwargs["reply_markup"]
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert all(slot in labels for slot in own_slots)
        assert all(slot not in labels for slot in other_slots)

        other = next(e for e in entries if e["родитель"] != parent)
        await aud.click(uid, f"sch:card:{other['id']}")
        opened = say.await_args.args[1]
        assert other["родитель"] in opened
        assert all(slot in opened for slot in other_slots)
        assert bot.get_pending(uid)["родитель"] == other["родитель"]

    asyncio.run(run())


def test_expired_single_lesson_is_hidden_in_card_and_week(aud):
    uid, tutor = ACTORS["owner"]
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    tomorrow = today + dt.timedelta(days=1)
    base = {"репетитор": tutor, "ученик": "Алекс", "родитель": "Рита", "цена": "1300"}
    bot.schedule_module.entries_of.return_value = [
        {**base, "id": "1", "дни": f"{yesterday:%d.%m.%Y}", "время": "11:00"},
        {**base, "id": "2", "дни": "Вт", "время": "16:00"},
        {**base, "id": "3", "дни": f"{today:%d.%m.%Y}", "время": "12:00"},
        {**base, "id": "4", "дни": f"{tomorrow:%d.%m.%Y}", "время": "13:00"},
    ]
    card = bot._schedule_summary(tutor, "Алекс", "Рита")
    assert "11:00" not in card and all(time in card for time in ["16:00", "12:00", "13:00"])
    week, _ = asyncio.run(bot.schedule_view(uid, 7))
    assert "11:00" not in week and "16:00" in week and "12:00" in week


@pytest.mark.parametrize("failure", [None, False, TimeoutError("simulated")])
def test_rate_editor_reports_only_successful_write(aud, failure):
    uid, tutor = ACTORS["owner"]
    bot.set_pending(uid, {"шаг": "правка_rate", "репетитор": tutor, "ученик": "Алекс",
                          "родитель": "Лина", "row": 3})
    save_rate = bot.students_module.set_rate
    if isinstance(failure, Exception):
        save_rate.side_effect = failure
    else:
        save_rate.return_value = failure is None
    asyncio.run(aud.type(uid, "1500"))
    save_rate.assert_called_once_with(tutor, "Алекс", "1500", parent="Лина", where_row=3)
    messages = [args.args[1] for args in aud.send.await_args_list if len(args.args) > 1]
    assert any("✅ Сохранено" in message for message in messages) == (failure is None)
    assert any("Не удалось обновить цену" in message for message in messages) == (failure is not None)


def test_reminder_reports_success_when_only_schedule_marker_fails(aud, monkeypatch):
    uid, tutor = ACTORS["tutor"]
    day = dt.date.today()
    entry = {"id": "AS201101", "репетитор": tutor, "ученик": "Алекс",
             "родитель": "Лина", "дни": f"{day:%d.%m.%Y}", "время": "18:00",
             "цена": "1500", "telegram_id": uid}
    monkeypatch.setattr(bot.schedule_module, "all_entries", lambda: [entry])
    monkeypatch.setattr(bot.schedule_module, "is_cancelled", lambda *a, **kw: False)
    monkeypatch.setattr(bot.schedule_module, "update_last_date",
                        Mock(side_effect=TimeoutError("simulated")))
    q = asyncio.run(aud.click(uid, f"remind:was:{entry['id']}:{day:%d.%m.%Y}"))
    aud.append.assert_called_once()
    assert aud.append.call_args.args[0]["сумма"] == "1500"
    assert "Занятие записано" in q.edit_message_text.await_args.args[0]
