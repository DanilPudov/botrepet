"""Офлайн-тесты модуля students.py: колонка «Имя отправителя», senders_of, find_by_sender.

Google Sheets подменяется MagicMock'ом: get_values/update_values/clear_values.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pkgs"))

import pytest

import sheets_util
import students

TUTOR = "Анна"
HISTORY_ID = "hist"

HEADER = list(students.COLUMNS)


def row(tutor="Анна", student="Ваня", parent="Мария", rate="1700",
        added="01.11.2026", comment="", student_link="", parent_link="",
        sender="", sid="S1"):
    return [tutor, student, parent, rate, added, comment,
            student_link, parent_link, sender, sid]


class FakeSheet:
    """Мини-эмуляция листа: хранит A1-таблицу и отвечает на get/update/clear."""

    def __init__(self, rows=None):
        self.rows: list[list[str]] = rows or []

    # ---- диапазоны
    @staticmethod
    def _col_idx(letters: str) -> int:
        idx = 0
        for ch in letters:
            idx = idx * 26 + (ord(ch.upper()) - ord("A") + 1)
        return idx - 1

    @classmethod
    def _parse(cls, rng: str) -> tuple[int, int, int]:
        """«'Ученики'!A1:I5» -> (first_row, first_col, last_col)."""
        cells = rng.split("!")[-1]
        start, _, end = cells.partition(":")
        first_row = int("".join(ch for ch in start if ch.isdigit()) or 1)
        first_col = cls._col_idx("".join(ch for ch in start if ch.isalpha()) or "A")
        end_col = cls._col_idx("".join(ch for ch in end if ch.isalpha()) or "A")
        return first_row, first_col, end_col

    # ---- API, используемый students.py
    def get(self, _sh_id, rng, *, fresh=False):
        first_row, first_col, last_col = self._parse(rng)
        out = []
        for line in self.rows[first_row - 1:]:
            out.append([line[i] if i < len(line) else ""
                        for i in range(first_col, last_col + 1)])
        # как в Sheets: хвостовые пустые ячейки отбрасываются
        trimmed = []
        for vals in out:
            while vals and vals[-1] == "":
                vals = vals[:-1]
            trimmed.append(vals)
        while trimmed and not trimmed[-1]:
            trimmed.pop()
        return trimmed

    def update(self, _sh_id, rng, values):
        first_row, first_col, last_col = self._parse(rng)
        for offset, vals in enumerate(values):
            line_no = first_row - 1 + offset
            while len(self.rows) <= line_no:
                self.rows.append([])
            line = self.rows[line_no]
            width = max(last_col + 1, first_col + len(vals))
            while len(line) < width:
                line.append("")
            for i, val in enumerate(vals):
                line[first_col + i] = val
        return {}

    def clear(self, _sh_id, rng):
        first_row, _first_col, _last_col = self._parse(rng)
        line_no = first_row - 1
        if 0 <= line_no < len(self.rows):
            self.rows[line_no] = []
        return None


class Env:
    def __init__(self, home, sheet):
        self.home = home
        self.sheet = sheet
        self.updates: list[tuple[str, list]] = []


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    cfg = {
        "telegram_token": "",
        "service_account_key": "test-key.json",
        "sheets": {"recept": "r", "recept_checks_sheet": "Чеки",
                   "recept_req_sheet": "Реквизиты",
                   "history": HISTORY_ID, "history_sheet": "История",
                   "students_sheet": "Ученики"},
        "write": {"target": "history", "history_sheet": "Занятия"},
        "owner_ids": [], "access": "ids", "notification_chat_id": None,
        "reminder": {"enabled": False},
    }
    monkeypatch.setattr(sheets_util, "CONFIG_PATH", home / "config.json")
    monkeypatch.setattr(sheets_util, "STATE_PATH", home / "state.json")
    monkeypatch.setattr(sheets_util, "LOG_PATH", home / "bot.log")
    (home / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    sheet = FakeSheet()
    monkeypatch.setattr(sheets_util, "get_values", MagicMock(side_effect=sheet.get))
    update = MagicMock(side_effect=sheet.update)
    clear = MagicMock(side_effect=sheet.clear)
    monkeypatch.setattr(sheets_util, "update_values", update)
    monkeypatch.setattr(sheets_util, "clear_values", clear)

    # «лист уже существует» — без обращения к Google API
    fake_sheets = MagicMock()
    (fake_sheets.spreadsheets.return_value.get.return_value.execute
     .return_value) = {"sheets": [{"properties": {"title": "Ученики"}}]}
    monkeypatch.setattr(sheets_util, "sheets", lambda: fake_sheets)
    sheets_util._SHEETS_TITLES.clear()

    e = Env(home, sheet)
    e.update = update
    e.clear = clear
    e.fake_sheets = fake_sheets
    return e


# ---------------------------------------------------------------- схема
def test_columns_have_sender_as_ninth():
    assert len(students.COLUMNS) == 10
    assert students.COLUMNS[8] == "Имя отправителя"


def test_ensure_sheet_writes_full_header(env):
    students.ensure_sheet()
    assert env.sheet.rows[0] == HEADER
    rng = env.update.call_args.args[1]
    assert rng.endswith("A1:J1")


def test_ensure_sheet_completes_short_header(env):
    """Старая шапка A..H дотягивается до I."""
    env.sheet.rows = [row()[:8]]
    students.ensure_sheet()
    assert env.sheet.rows[0] == HEADER


def test_reads_only_sender_column_forward_compatible(env):
    """Строки без колонки I читаются с пустым «отправителем»."""
    env.sheet.rows = [HEADER, row()[:8]]
    stud = students.students_of(TUTOR)[0]
    assert stud["отправитель"] == ""
    assert stud["ученик"] == "Ваня"


def test_rows_range_is_a_i(env):
    """Чтение идёт диапазоном A1:I, а лист создаётся заранее (с шапкой)."""
    students.students_of(TUTOR)
    got = env.sheet.get("hist", "'Ученики'!A1:I")
    assert got, "лист должен быть создан перед чтением"
    assert got[0][:3] == ["Репетитор", "Ученик", "Родитель"]


# ---------------------------------------------------------------- запись/чтение
def test_add_student_writes_sender(env):
    env.sheet.rows = [HEADER]
    n = students.add_student(TUTOR, "Ваня", "Мария", "1700", sender="Иванов Иван")
    assert n == 2
    rng = env.update.call_args.args[1]
    assert rng.endswith("A2:J2")
    assert env.sheet.rows[1][8] == "Иванов Иван"
    assert students.students_of(TUTOR)[0]["отправитель"] == "Иванов Иван"


def test_add_student_without_sender_keeps_empty(env):
    env.sheet.rows = [HEADER]
    students.add_student(TUTOR, "Петя", "Ольга")
    assert env.sheet.rows[1][8] == ""
    assert len(env.sheet.rows[1]) == 10


def test_add_student_positional_old_signature(env):
    """Старый стиль вызова (позиционно) продолжает работать."""
    env.sheet.rows = [HEADER]
    students.add_student(TUTOR, "Ваня", "Мария", "1700", "коммент",
                         "ссылка1", "ссылка2")
    saved = env.sheet.rows[1]
    assert saved[5] == "коммент" and saved[6] == "ссылка1"
    assert saved[7] == "ссылка2" and saved[8] == ""


def test_update_student_sender(env):
    env.sheet.rows = [HEADER, row(sender="старое имя")]
    assert students.update_student(TUTOR, "Ваня", sender="Петров Пётр")
    assert env.sheet.rows[1][8] == "Петров Пётр"
    assert env.update.call_args.args[1].endswith("I2")


def test_update_student_keeps_sender_when_none(env):
    env.sheet.rows = [HEADER, row(sender="Иванов Иван")]
    assert students.update_student(TUTOR, "Ваня", rate="2000")
    assert env.sheet.rows[1][3] == "2000"
    assert env.sheet.rows[1][8] == "Иванов Иван"


def test_set_rate_does_not_touch_sender(env):
    env.sheet.rows = [HEADER, row(sender="Иванов Иван")]
    assert students.set_rate(TUTOR, "Ваня", "2500")
    assert env.sheet.rows[1][3] == "2500"
    assert env.sheet.rows[1][8] == "Иванов Иван"


def test_update_missing_student_returns_false(env):
    env.sheet.rows = [HEADER, row()]
    assert students.update_student(TUTOR, "Нет такого", sender="X") is False
    assert env.update.call_count == 0


def test_delete_clears_a_to_i(env):
    env.sheet.rows = [HEADER, row(sender="Иванов Иван"), row(student="Петя")]
    assert students.delete_student(TUTOR, "Ваня")
    assert env.clear.call_args.args[1].endswith("A2:J2")
    assert all(c == "" for c in env.sheet.rows[1])
    # вторая строка не сдвинулась
    assert env.sheet.rows[2][1] == "Петя"
    assert [s["ученик"] for s in students.students_of(TUTOR)] == ["Петя"]


@pytest.mark.parametrize("method", ["add", "update", "delete"])
def test_student_operations_use_expected_ranges(env, method):
    env.sheet.rows = [HEADER, row(sender="Иванов Иван")]
    if method == "add":
        students.add_student(TUTOR, "Сеня")
        rng = env.update.call_args.args[1]
        tail = "A3:J3"
    elif method == "update":
        students.update_student(TUTOR, "Ваня", rate="1")
        rng = env.update.call_args.args[1]
        tail = "D2"
    else:
        students.delete_student(TUTOR, "Ваня")
        rng = env.clear.call_args.args[1]
        tail = "A2:J2"
    assert rng.endswith(tail), rng


# ---------------------------------------------------------------- senders_of
def test_senders_of_single_names(env):
    env.sheet.rows = [HEADER, row(student="Ваня", sender="Иванов Иван"),
                      row(student="Петя", parent="Ольга", sender="Петрова Мария")]
    assert students.senders_of(TUTOR) == {"Иванов Иван": "Ваня",
                                          "Петрова Мария": "Петя"}


def test_senders_of_comma_separated(env):
    env.sheet.rows = [HEADER,
                      row(student="Ваня", sender="Иванов Иван, Иванов В.И."),
                      row(student="Петя", parent="Ольга", sender=" Семёнов , ,Персидская Ирина")]
    got = students.senders_of(TUTOR)
    assert got["Иванов Иван"] == "Ваня"
    assert got["Иванов В.И."] == "Ваня"
    assert got["Семёнов"] == "Петя"
    assert got["Персидская Ирина"] == "Петя"
    assert "" not in got            # пустые куски пропущены


def test_senders_of_skips_other_tutors(env):
    env.sheet.rows = [HEADER,
                      row(tutor="Анна", student="Ваня", sender="Иванов Иван"),
                      row(tutor="Борис", student="Сеня", sender="Петров Пётр")]
    assert students.senders_of(TUTOR) == {"Иванов Иван": "Ваня"}
    assert students.senders_of("Борис") == {"Петров Пётр": "Сеня"}


def test_senders_of_empty(env):
    env.sheet.rows = [HEADER, row(sender="")]
    assert students.senders_of(TUTOR) == {}


# ---------------------------------------------------------------- find_by_sender
def roster(env):
    env.sheet.rows = [
        HEADER,
        row(student="Ваня", parent="Мария", sender="Иванов Иван"),
        row(student="Петя", parent="Ольга",
            sender="Персидская Ирина, Семёнов Алексей"),
        row(student="Сеня", parent="Лена", sender=""),
    ]


def test_find_by_sender_exact(env):
    roster(env)
    assert students.find_by_sender(TUTOR, "Иванов Иван")["ученик"] == "Ваня"


def test_find_by_sender_exact_normalized(env):
    roster(env)
    got = students.find_by_sender(TUTOR, "  Иванов   Иван ")
    assert got is not None and got["ученик"] == "Ваня"


def test_find_by_sender_second_name_in_list(env):
    roster(env)
    assert students.find_by_sender(TUTOR, "Семёнов Алексей")["ученик"] == "Петя"


def test_find_by_sender_partial(env):
    """Вхождение одной строки в другую (длина >= 3)."""
    roster(env)
    assert students.find_by_sender(TUTOR, "Иванов")["ученик"] == "Ваня"
    assert students.find_by_sender(TUTOR, "Персидская")["ученик"] == "Петя"


def test_find_by_sender_short_partial_ignored(env):
    roster(env)
    assert students.find_by_sender(TUTOR, "Се") is None


def test_find_by_sender_unknown(env):
    roster(env)
    assert students.find_by_sender(TUTOR, "Сидоров") is None
    assert students.find_by_sender(TUTOR, "") is None
    assert students.find_by_sender(TUTOR, "   ") is None


def test_find_by_sender_other_tutor(env):
    roster(env)
    assert students.find_by_sender("Борис", "Иванов Иван") is None


def test_find_by_sender_prefers_exact_over_partial(env):
    env.sheet.rows = [
        HEADER,
        row(student="Ваня", sender="Иванов Иван"),
        row(student="Петя", parent="Ольга", sender="Иванов Иван Петрович"),
    ]
    assert students.find_by_sender(TUTOR, "Иванов Иван")["ученик"] == "Ваня"


# ---------------------------------------------------------------- совместимость
def test_existing_api_still_works(env):
    env.sheet.rows = [HEADER]
    students.add_student(TUTOR, "Ваня", "Мария", "1700", sender="Иванов Иван")
    found = students.find_student(TUTOR, "  Ваня ")
    assert found["тариф"] == "1700" and found["row"] == 2
    assert "отправитель" in found
    assert students.set_rate(TUTOR, "Ваня", "1800") is True
    assert students.find_student(TUTOR, "Ваня")["тариф"] == "1800"
    assert students.delete_student(TUTOR, "Ваня") is True
    assert students.students_of(TUTOR) == []


def test_rate_updates_only_matching_active_lessons_in_one_request(env, monkeypatch):
    import datetime as dt
    import schedule

    env.sheet.rows = [HEADER, row(), row(parent="Жанна", sid="S2")]
    get = sheets_util.get_values
    future = (dt.date.today() + dt.timedelta(days=1)).strftime("%d.%m.%Y")
    past = (dt.date.today() - dt.timedelta(days=1)).strftime("%d.%m.%Y")
    slots = [
        schedule.COLUMNS,
        ["1", TUTOR, "Ваня", "Мария", "Пт", "18:00", "1700", "да"],
        ["2", TUTOR, "Ваня", "Жанна", "Вс", "10:00", "1700", "да"],
        ["3", TUTOR, "Ваня", "Мария", "Вс", "12:00", "1300", "да"],
        ["4", TUTOR, "Ваня", "Мария", past, "12:00", "1700", "да"],
        ["5", TUTOR, "Ваня", "Мария", "Сб", "10:00", "1700", "нет"],
        ["6", TUTOR, "Ваня", "Мария", future, "12:00", "1700", "да"],
        ["7", TUTOR, "Ваня", "Мария", future, "14:00", "900", "да"],
        ["8", "Олег", "Ваня", "Мария", "Пт", "18:00", "1700", "да"],
    ]
    monkeypatch.setattr(sheets_util, "sheet_titles", lambda *args, **kw: ["Ученики", schedule.SHEET])
    monkeypatch.setattr(sheets_util, "get_values", lambda book, rng, **kw:
                        slots if rng.startswith("'Расписание'!") else get(book, rng, **kw))
    batch = MagicMock()
    monkeypatch.setattr(sheets_util, "update_values_batch", batch)

    assert students.set_rate(TUTOR, "Ваня", "1500", parent="Мария", where_row=2)
    batch.assert_called_once_with(HISTORY_ID, [
        {"range": "'Ученики'!D2", "values": [["1500"]]},
        {"range": "'Расписание'!G2", "values": [["1500"]]},
        {"range": "'Расписание'!G4", "values": [["1500"]]},
        {"range": "'Расписание'!G7", "values": [["1500"]]},
    ])
    env.update.assert_not_called()

    batch.reset_mock()
    assert not students.set_rate(TUTOR, "Ваня", "2000", parent="Мария", where_row=3)
    batch.assert_not_called()
    env.update.assert_not_called()

    batch.side_effect = TimeoutError("simulated")
    with pytest.raises(TimeoutError):
        students.set_rate(TUTOR, "Ваня", "1500", parent="Мария", where_row=2)
    env.update.assert_not_called()


def test_students_of_dict_keys(env):
    env.sheet.rows = [HEADER, row(sender="Иванов Иван")]
    stud = students.students_of(TUTOR)[0]
    for key in ("ученик", "родитель", "тариф", "добавлен", "комментарий",
                "ссылка_ученика", "ссылка_родителя", "отправитель", "row"):
        assert key in stud


def test_duplicate_rows_deduped_by_student_and_parent(env):
    env.sheet.rows = [HEADER, row(sender="A"), row(sender="B")]
    assert len(students.students_of(TUTOR)) == 1


def test_sender_names_helper():
    assert students.sender_names("А, Б,,В ") == ["А", "Б", "В"]
    assert students.sender_names("") == []
    assert students.sender_names(None) == []
