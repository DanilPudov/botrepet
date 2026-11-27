"""Тесты: одинаковые имена учеников не должны приводить к правке чужой строки."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


ROSTER = [
    {"row": 2, "ученик": "София", "родитель": "Жанна",
     "ссылка_ученика": "https://t.me/+70000000001"},
    {"row": 12, "ученик": "София", "родитель": "Мария",
     "ссылка_ученика": "https://t.me/+70000000002"},
    {"row": 5, "ученик": "Леон", "родитель": "Ирина",
     "ссылка_ученика": "https://t.me/leon"},
]


def _patch_roster(monkeypatch, roster=ROSTER):
    import students
    monkeypatch.setattr(students, "students_of", lambda tutor: list(roster))
    return students


def test_ambiguous_name_returns_none(monkeypatch):
    students = _patch_roster(monkeypatch)
    assert students.find_student("Данил Пудов", "София") is None


def test_ambiguous_name_resolved_by_parent(monkeypatch):
    students = _patch_roster(monkeypatch)
    assert students.find_student("Данил Пудов", "София", parent="Жанна")["row"] == 2
    assert students.find_student("Данил Пудов", "София", parent="Мария")["row"] == 12


def test_unique_name_still_found(monkeypatch):
    students = _patch_roster(monkeypatch)
    found = students.find_student("Данил Пудов", "Леон")
    assert found and found["row"] == 5


def test_update_student_uses_explicit_row(monkeypatch):
    """where_row важнее поиска по имени: пишем в указанную строку."""
    students = _patch_roster(monkeypatch)
    written = {}

    def fake_update(sh, rng, values, **kw):
        written["rng"] = rng
        written["values"] = values
        return {}

    monkeypatch.setattr(students.sheets_util, "update_values", fake_update)
    monkeypatch.setattr(students, "load_config",
                        lambda: {"sheets": {"history": "sid"}})
    monkeypatch.setattr(students, "sheet_title", lambda: "Ученики")
    monkeypatch.setattr(students, "_rows", lambda tutor, *, fresh: [
        [], *([[]] * 10), ["Данил Пудов", "София", "Мария"]
    ])

    ok = students.update_student("Данил Пудов", "София",
                                 student_link="https://t.me/right",
                                 where_row=12)
    assert ok is True
    assert written["rng"].endswith("G12")
    assert written["values"] == [["https://t.me/right"]]
