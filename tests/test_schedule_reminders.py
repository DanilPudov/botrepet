import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import bot
import schedule


def test_rejected_one_time_lesson_preserves_original(monkeypatch, tmp_path):
    day = dt.date.today()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7),
                             message=SimpleNamespace(text=f"{day:%d.%m.%Y} 18:00 Ученик Б"))
    monkeypatch.setattr(bot, "_touch_activity", Mock())
    monkeypatch.setattr(bot, "_maybe_send_note", AsyncMock())
    monkeypatch.setattr(bot, "_delete_prev", AsyncMock())
    monkeypatch.setattr(bot, "_delete_user_msg", AsyncMock())
    monkeypatch.setattr(bot, "display_name", lambda uid: "Тест")
    monkeypatch.setattr(bot, "role_of", lambda uid: "tutor")
    monkeypatch.setattr(bot, "get_pending", lambda uid: {"шаг": "разовое", "репетитор": "Тест"})
    monkeypatch.setattr(bot.sheets_util, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bot.students_module, "find_student", lambda *a, **kw: {})
    monkeypatch.setattr(schedule, "entries_of", lambda *a, **kw: [
        {"дни": day.strftime("%d.%m.%Y"), "время": "18:00", "ученик": "Ученик А"}])
    cancel, add, send = Mock(), Mock(), AsyncMock()
    monkeypatch.setattr(schedule, "add_cancel", cancel)
    monkeypatch.setattr(schedule, "add_one_time", add)
    monkeypatch.setattr(bot, "_send", send)
    asyncio.run(bot.handle_text(update, SimpleNamespace(bot=Mock())))
    assert "уже стоит" in send.await_args.args[1]
    cancel.assert_not_called()
    add.assert_not_called()


def test_move_writes_only_dated_replacement_and_cancellation(monkeypatch):
    day = dt.date(2026, 11, 19)
    entry = {"id": "1", "репетитор": "Тест", "ученик": "А", "дни": "Чт", "время": "18:00"}
    monkeypatch.setattr(schedule, "sheet_for", lambda tutor: "book")
    monkeypatch.setattr(schedule, "id_letter", lambda tutor: "")
    monkeypatch.setattr(schedule, "ensure_cancel_sheet", Mock())
    monkeypatch.setattr(schedule, "is_cancelled", lambda *a, **kw: False)
    monkeypatch.setattr(schedule, "_rows", lambda tutor: [["ID"], ["1", "Тест"]])
    monkeypatch.setattr(schedule, "_cancel_rows", lambda tutor: [["ID"]])
    batch = Mock()
    monkeypatch.setattr(schedule.sheets_util, "update_values_batch", batch)
    assert schedule.move_occurrence(entry, day, "20:00") == "2"
    book, (cancel, replacement) = batch.call_args.args
    assert book == "book"
    assert cancel["values"][0][-2:] == ["19.11.2026", "18:00"]
    assert replacement["range"].endswith("A3:I3")
    assert replacement["values"][0][4:6] == ["19.11.2026", "20:00"]


def test_reminder_confirms_only_delivery_and_skips_cancelled(monkeypatch):
    day = dt.date.today()
    entry = {"id": "1", "репетитор": "Тест", "ученик": "А", "время": "18:00",
             "дни": day.strftime("%d.%m.%Y")}
    state = {}
    monkeypatch.setattr(bot, "load_state", lambda: state)
    monkeypatch.setattr(bot, "save_state", Mock())
    monkeypatch.setattr(bot, "load_config", lambda: {})
    monkeypatch.setattr(bot, "log", Mock())
    monkeypatch.setattr(schedule, "get_entry", lambda *a: entry)
    cancelled = Mock(side_effect=[True, False, False])
    monkeypatch.setattr(schedule, "is_cancelled", cancelled)
    send = AsyncMock(side_effect=[bot.NetworkError("временно недоступно"), None])
    queue = Mock()
    context = SimpleNamespace(bot=SimpleNamespace(send_message=send), job_queue=queue, job=SimpleNamespace(
        data={"entry": entry, "key": "k", "date": day.strftime("%d.%m.%Y"), "chat_id": 7}))
    asyncio.run(bot.send_lesson_reminder(context))
    send.assert_not_awaited()
    asyncio.run(bot.send_lesson_reminder(context))
    assert not state.get("reminded")
    assert queue.run_once.call_args.args[0] is bot.send_lesson_reminder
    assert queue.run_once.call_args.kwargs["when"] == 60
    context.job.data = queue.run_once.call_args.kwargs["data"]
    asyncio.run(bot.send_lesson_reminder(context))
    assert state["reminded"]["k"]
    asyncio.run(bot.send_lesson_reminder(context))
    assert send.await_count == 2
