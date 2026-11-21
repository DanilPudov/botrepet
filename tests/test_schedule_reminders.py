import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import TimedOut

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
    monkeypatch.setattr(schedule, "_rows", lambda tutor, **kw: [["ID"], ["1", "Тест"]])
    monkeypatch.setattr(schedule, "_cancel_rows", lambda tutor, **kw: [["ID"]])
    batch = Mock()
    monkeypatch.setattr(schedule.sheets_util, "update_values_batch", batch)
    assert schedule.move_occurrence(entry, day, "20:00") == "AS191101"
    book, (cancel, replacement) = batch.call_args.args
    assert book == "book"
    assert cancel["values"][0][0] == "AC191101"
    assert cancel["values"][0][-2:] == ["19.11.2026", "18:00"]
    assert replacement["range"].endswith("A3:I3")
    assert replacement["values"][0][4:6] == ["19.11.2026", "20:00"]


@pytest.fixture
def reminder(monkeypatch, tmp_path):
    day = dt.date.today()
    entry = {"id": "1", "репетитор": "Тест", "ученик": "А", "время": "18:00",
             "дни": day.strftime("%d.%m.%Y")}
    monkeypatch.setattr(bot.sheets_util, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bot, "load_state", lambda: {})
    monkeypatch.setattr(bot, "load_config", lambda: {})
    monkeypatch.setattr(bot, "log", Mock())
    monkeypatch.setattr(schedule, "get_entry", lambda rid, tutor: {**entry, "id": rid})
    monkeypatch.setattr(bot, "_already_recorded", lambda *a: False)
    monkeypatch.setattr(bot, "_chat_id_by_tutor", lambda tutor: 7)
    cancelled = Mock(return_value=False)
    monkeypatch.setattr(schedule, "is_cancelled", cancelled)
    return day, entry, cancelled


def _context(day, entry, send):
    queue = Mock()
    queue.get_jobs_by_name.return_value = []
    return SimpleNamespace(bot=SimpleNamespace(send_message=send), job_queue=queue,
                           job=SimpleNamespace(data={
                               "entry": entry, "date": day.strftime("%d.%m.%Y"), "chat_id": 7}))


def test_reminder_retries_explicit_rate_limit_and_skips_cancelled(reminder):
    day, entry, cancelled = reminder
    send = AsyncMock(side_effect=[bot.RetryAfter(60), None])
    context = _context(day, entry, send)
    queue = context.job_queue
    cancelled.return_value = True
    asyncio.run(bot.send_lesson_reminder(context))
    send.assert_not_awaited()
    cancelled.return_value = False
    asyncio.run(bot.send_lesson_reminder(context))
    assert not bot._claimed_reminders(day)
    assert queue.run_once.call_args.args[0] is bot.send_lesson_reminder
    assert queue.run_once.call_args.kwargs["when"] == 60
    context.job.data = queue.run_once.call_args.kwargs["data"]
    asyncio.run(bot.send_lesson_reminder(context))
    assert bot._claimed_reminders(day)
    asyncio.run(bot.send_lesson_reminder(context))
    assert send.await_count == 2


def test_reminder_timeout_does_not_retry_or_reschedule(monkeypatch, reminder):
    day, entry, _ = reminder
    send = AsyncMock(side_effect=TimedOut())
    first = _context(day, entry, send)
    asyncio.run(bot.send_lesson_reminder(first))
    first.job_queue.run_once.assert_not_called()

    duplicate = {**entry, "id": "2"}
    restarted = _context(day, duplicate, send)
    monkeypatch.setattr(schedule, "all_entries", lambda: [duplicate])
    asyncio.run(bot.reschedule_reminders(restarted))
    restarted.job_queue.run_once.assert_not_called()
    asyncio.run(bot.send_lesson_reminder(restarted))
    send.assert_awaited_once()


def test_simultaneous_jobs_send_once_even_with_different_row_ids(reminder):
    day, entry, _ = reminder

    async def run():
        started, finish = asyncio.Event(), asyncio.Event()

        async def slow_send(**kwargs):
            started.set()
            await finish.wait()

        send = AsyncMock(side_effect=slow_send)
        first = asyncio.create_task(bot.send_lesson_reminder(_context(day, entry, send)))
        await started.wait()
        duplicate = {**entry, "id": "2", "время": "18.00"}
        await bot.send_lesson_reminder(_context(day, duplicate, send))
        finish.set()
        await first
        send.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("legacy_sent", [False, True])
def test_scheduler_groups_duplicate_rows_but_keeps_another_time(monkeypatch, reminder, legacy_sent):
    day, entry, _ = reminder
    duplicate = {**entry, "id": "2", "время": "18.00", "ученик": "а "}
    another = {**entry, "id": "3", "время": "19:00"}
    monkeypatch.setattr(schedule, "all_entries", lambda: [duplicate, entry, another])
    if legacy_sent:
        monkeypatch.setattr(bot, "load_state", lambda: {"reminded": {f"Тест-1-{day:%Y%m%d}": True}})
    context = _context(day, entry, AsyncMock())
    asyncio.run(bot.reschedule_reminders(context))
    entries = [call.kwargs["data"]["entry"] for call in context.job_queue.run_once.call_args_list]
    assert entries == ([another] if legacy_sent else [duplicate, another])


def test_recorded_lesson_does_not_prompt_again(monkeypatch, reminder):
    day, entry, _ = reminder
    recorded = Mock(return_value=True)
    monkeypatch.setattr(bot, "_already_recorded", recorded)
    send = AsyncMock()
    context = _context(day, entry, send)
    asyncio.run(bot.send_lesson_reminder(context))
    send.assert_not_awaited()
    recorded.assert_called_once_with("Тест", day.strftime("%d.%m.%Y"), "18:00", "А", "")
    monkeypatch.setattr(schedule, "all_entries", lambda: [entry])
    asyncio.run(bot.reschedule_reminders(context))
    context.job_queue.run_once.assert_not_called()
