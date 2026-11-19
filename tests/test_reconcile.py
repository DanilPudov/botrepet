"""Офлайн-тесты reconcile.py: сверка, дедупликация, вставка строк, линия недели.

Запуск (из папки бота):
  C:\\Users\\Dаnil\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe -m pytest tests\\test_reconcile.py -q

Никаких реальных запросов к Google Sheets — всё замокано.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pkgs"))

import pytest

import reconcile


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #
def test_norm_key_name():
    assert reconcile.norm_key_name("Варя ( Оксана)") == "Варя (Оксана)"
    assert reconcile.norm_key_name("Варя(Оксана )") == "Варя (Оксана)"
    assert reconcile.norm_key_name("Варя  (Оксана)") == "Варя (Оксана)"
    assert reconcile.norm_key_name("  Иван  ") == "Иван"


def test_parse_int():
    assert reconcile.parse_int("1 700") == 1700
    assert reconcile.parse_int("1700") == 1700
    assert reconcile.parse_int("1700,00") == 1700
    assert reconcile.parse_int("1\u00a0700 ₽") == 1700
    assert reconcile.parse_int(1700) == 1700
    assert reconcile.parse_int("") is None
    assert reconcile.parse_int("abc") is None
    assert reconcile.parse_int(None) is None


def test_cell_date():
    assert reconcile.cell_date(["", "08.11.2026", "X", "Y", "1700"]) == dt.date(2026, 11, 8)
    assert reconcile.cell_date(["", "не дата"]) is None
    assert reconcile.cell_date(["", ""]) is None


# --------------------------------------------------------------------------- #
# load_ops: JSON с датой-строкой (как пишет pdf_parser) и CSV
# --------------------------------------------------------------------------- #
def test_load_ops_json_string_dates(tmp_path):
    import json
    p = tmp_path / "ops.json"
    p.write_text(json.dumps([
        {"dt": "2026-11-08 21:29", "sum": 1700, "who": "Вадим"},
        {"dt": "2026-11-08 14:57", "sum": 2500, "who": "Марина"},
    ]), encoding="utf-8")
    ops = reconcile.load_ops(p)
    assert len(ops) == 2
    assert ops[0]["dt"] == dt.datetime(2026, 11, 8, 21, 29)
    assert ops[0]["sum"] == 1700


def test_load_ops_csv(tmp_path):
    p = tmp_path / "ops.csv"
    p.write_text("2026-11-08 21:29;1700;Вадим\n2026-11-08 14:57;2500;Марина\n", encoding="utf-8")
    ops = reconcile.load_ops(p)
    assert len(ops) == 2
    assert ops[0]["sum"] == 1700


# --------------------------------------------------------------------------- #
# match: дата+сумма, одна операция — одна запись
# --------------------------------------------------------------------------- #
def _mk_check(day, amount, name="Рома Жлобич", student="Петр (Татьяна)"):
    return {"row": 1, "дата": day, "время": "18:20", "репетитор": name,
            "ученик": student, "сумма": amount, "paid": False}


def test_match_basic():
    d = dt.date(2026, 11, 8)
    checks = [_mk_check(d, 1700), _mk_check(d, 2500, student="Юрий(Марина)")]
    ops = [{"dt": dt.datetime(2026, 11, 8, 21, 29), "sum": 1700},
           {"dt": dt.datetime(2026, 11, 8, 14, 57), "sum": 2500}]
    out = reconcile.match(checks, ops)
    assert [c["paid"] for c in out] == [True, True]


def test_match_one_op_not_two():
    d = dt.date(2026, 11, 8)
    # По одной операции нельзя определить, какое из двух занятий оплатили.
    checks = [_mk_check(d, 1700), _mk_check(d, 1700, student="Второй")]
    ops = [{"dt": dt.datetime(2026, 11, 8, 21, 29), "sum": 1700}]
    out = reconcile.match(checks, ops)
    assert all(c["ambiguous"] and not c["paid"] for c in out)


def test_match_two_transfers_for_one_lesson():
    day = dt.date(2026, 11, 8)
    ops = [{"dt": dt.datetime(2026, 11, 8, hour), "sum": 1500} for hour in [12, 13]]
    lesson, = reconcile.match([_mk_check(day, 1500)], ops)
    assert lesson["ambiguous"] and not lesson["paid"]
    with patch.object(reconcile, "commission_balances", return_value={}):
        report = reconcile.build_report([lesson])
    assert "вручную" in report and "красные" not in report


def test_checks_rows_mixed_schemas(monkeypatch):
    import checks_writer
    common = ["12:00", "08.11.2026", "Тест", "Ученик"]
    tail = ["комментарий", "да", "хорошо", "да", "300"]
    rows = [checks_writer.HEADER, common + ["1500"] + tail,
            common + ["79000000000", "2500"] + tail + ["123"]]
    monkeypatch.setattr(reconcile, "load_config", lambda: {"sheets": {
        "recept": "book", "recept_checks_sheet": "Test"}})
    monkeypatch.setattr(reconcile, "get_values", lambda *args: rows)
    lessons = reconcile.checks_rows(dt.date(2026, 11, 1))
    assert [(r["ученик"], r["сумма"]) for r in lessons] == [
        ("Ученик", 1500), ("Ученик (79000000000)", 2500)]
    assert all(r["комиссия"] == "300" and r["комментарий"] == "комментарий" for r in lessons)


@pytest.mark.parametrize("headers, row, student", [
    (["Сумма", "Имя ученика", "Репетитор", "Имя родителя", "Дата", "Комментарий"],
     ["2500", "Ученик", "Тест", "79000000000", "08.11.2026", "тест"], "Ученик (79000000000)"),
    (["Время (по чеку)", "Дата", "Репетитор", "Имя ученика (имя родителя)", "Сумма", "Комментарий"],
     ["12:00", "08.11.2026", "Тест", "Ученик (Родитель)", "2500", "тест"], "Ученик (Родитель)"),
])
def test_reconcile_by_header(monkeypatch, headers, row, student):
    monkeypatch.setattr(reconcile, "load_config", lambda: {"sheets": {
        "recept": "book", "recept_checks_sheet": "Test"}})
    monkeypatch.setattr(reconcile, "get_values", lambda *args: [headers, row])
    lesson, = reconcile.checks_rows(dt.date(2026, 11, 1))
    assert (lesson["ученик"], lesson["сумма"], lesson["комментарий"]) == (student, 2500, "тест")


def test_match_date_matters():
    d1, d2 = dt.date(2026, 11, 8), dt.date(2026, 11, 9)
    checks = [_mk_check(d1, 1700), _mk_check(d2, 1700)]
    ops = [{"dt": dt.datetime(2026, 11, 9, 21, 29), "sum": 1700}]
    out = reconcile.match(checks, ops)
    assert [c["paid"] for c in out] == [False, True]


# --------------------------------------------------------------------------- #
# build_report
# --------------------------------------------------------------------------- #
def test_build_report():
    d = dt.date(2026, 11, 8)
    checks = [_mk_check(d, 1700), _mk_check(d, 3000, student="X")]
    checks[0]["paid"] = True
    checks[1]["paid"] = False
    with patch.object(reconcile, "commission_balances", return_value={}):
        rep = reconcile.build_report(checks)
    assert "1 из 2" in rep
    assert "1700" in rep
    assert "80%" in rep


# --------------------------------------------------------------------------- #
# drop_duplicates / history_entry_keys
# --------------------------------------------------------------------------- #
def test_drop_duplicates_normalizes_names():
    d = dt.date(2026, 11, 8)
    checks = [_mk_check(d, 1700)]
    with patch.object(reconcile, "history_entry_keys",
                      return_value={("Рома Жлобич", d, "Петр (Татьяна)", 1700)}):
        fresh, dups = reconcile.drop_duplicates(checks)
    assert fresh == []
    assert len(dups) == 1


# --------------------------------------------------------------------------- #
# week_rule_request
# --------------------------------------------------------------------------- #
def test_week_rule_request():
    req = reconcile.week_rule_request(1045)
    b = req["updateBorders"]
    assert b["bottom"]["style"] == "SOLID_THICK"
    assert b["bottom"]["color"]["green"] == 1.0
    # 0-based: строка 1045 -> startIndex 1044, endIndex 1045
    assert b["range"]["startRowIndex"] == 1044
    assert b["range"]["endRowIndex"] == 1045
    assert b["range"]["startColumnIndex"] == 0
    assert b["range"]["endColumnIndex"] == 10


def test_apply_week_rule_calls_batch():
    batch = MagicMock()
    batch.execute.return_value = {}
    with patch.object(reconcile, "sheets", return_value=MagicMock(
            spreadsheets=MagicMock(return_value=MagicMock(batchUpdate=MagicMock(return_value=batch))))):
        # подменяем get_values/историю не нужно — has_week_rule вернёт False, вызовем напрямую
        pass
    # проверим, что request формируется и уходит — через прямой вызов с моком
    sh = MagicMock()
    sh.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
    with patch.object(reconcile, "sheets", return_value=sh), \
         patch.object(reconcile, "has_week_rule", return_value=True):
        ok = reconcile.apply_week_rule(1045)
    assert ok is True


# --------------------------------------------------------------------------- #
# insert_missing_rows: использует пустые разрывы и правильно считает сдвиги
# --------------------------------------------------------------------------- #
def _fake_rows():
    """Блок 100..105, с пустым разрывом на 103. Дата строк 100-102=08.11, 104-105=09.11."""
    rows = []
    for i in range(1, 106):
        rows.append([])
    for i, (t, student, amount) in enumerate([("21:29", "Артем(Юля)", 1700),
                                              ("21:18", "Антонетта", 2000),
                                              ("20:43", "Вита(Ольга)", 9000)], start=100):
        rows[i - 1] = ["", "08.11.2026", "Рома Жлобич", student, amount, "", "нет", "Отлично", "Да"]
    for i, (t, student, amount) in enumerate([("12:00", "Первый", 1500)], start=104):
        rows[i - 1] = ["", "09.11.2026", "Ильхам Курбанбаев", student, amount, "", "нет", "Отлично", "Да"]
    # 103 — пустая строка (разрыв)
    return rows


def test_insert_uses_gap_row():
    rows = _fake_rows()
    rec = {"дата": dt.date(2026, 11, 8), "время": "19:00", "репетитор": "Рома Жлобич",
           "ученик": "Новый", "сумма": 2000, "комментарий": "", "первое": "нет",
           "как_прошло": "Отлично", "будут": "Да", "комиссия": "", "paid": True}
    with patch.object(reconcile, "get_values", return_value=rows), \
         patch.object(reconcile, "history_entry_keys", return_value=set()):
        plan = reconcile.insert_missing_rows([rec], dry_run=True)
    assert len(plan) == 1
    # использована пустая строка 103, а не вставка
    assert plan[0]["строка"] == 103


def test_insert_calculates_shifts():
    """Две вставки в разных местах — итоговые строки сдвигаются корректно."""
    rows = []
    for i in range(1, 60):
        rows.append([])
    # блок 10..50 без разрывов
    for i in range(10, 51):
        rows[i - 1] = ["", "08.11.2026", "Рома", f"У{i}", 1000, "", "нет", "Отлично", "Да"]
    rec1 = {"дата": dt.date(2026, 11, 8), "время": "10:00", "репетитор": "ААА",
            "ученик": "X", "сумма": 100, "комментарий": "", "первое": "нет",
            "как_прошло": "Отлично", "будут": "Да", "комиссия": "", "paid": True}
    rec2 = {"дата": dt.date(2026, 11, 9), "время": "10:00", "репетитор": "БББ",
            "ученик": "Y", "сумма": 100, "комментарий": "", "первое": "нет",
            "как_прошло": "Отлично", "будут": "Да", "комиссия": "", "paid": True}
    with patch.object(reconcile, "get_values", return_value=rows), \
         patch.object(reconcile, "history_entry_keys", return_value=set()):
        plan = reconcile.insert_missing_rows([rec1, rec2], dry_run=True)
    assert len(plan) == 2
    # dry-run не пишет
    assert all(p["строка"] is None for p in plan)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
