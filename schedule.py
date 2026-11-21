"""
Постоянное расписание: лист «Расписание» в таблице «История».

Колонки (A..I):
  A ID | B Репетитор | C Ученик | D Родитель | E Дни | F Время |
  G Цена | H Активно | I Последняя дата

Запись = «ученик занимается по дням E в время F». Один ученик может иметь
несколько записей. По ним бот присылает напоминания через час после начала.
"""
from __future__ import annotations

import datetime as dt

import workspaces
import sheets_util
from sheets_util import load_config, log, norm_name, names_match

SHEET = "Расписание"
COLUMNS = ["ID", "Репетитор", "Ученик", "Родитель", "Дни", "Время",
           "Цена", "Активно", "Последняя дата"]

DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

FULL_DAY_NAMES = ["понедельник", "вторник", "среда", "четверг",
                  "пятница", "суббота", "воскресенье"]


def sheet_for(tutor: str) -> str:
    """Таблица, где лежит расписание этого репетитора."""
    try:
        sid = workspaces.workspace_id(tutor)
        if sid:
            return sid
    except Exception:  # noqa: BLE001
        pass
    return workspaces.load_config()["sheets"]["history"]


def id_letter(tutor: str) -> str:
    """Буква перед ID расписания: V — Вика, M — Максим, иначе пусто."""
    try:
        t = workspaces.canon_tutor(tutor)
        if t == "Виктория Пудова":
            return "V"
        if t == "Максим Пудов":
            return "M"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _rows_for(tutor: str) -> list[list[str]]:
    """Расписание из таблицы конкретного репетитора."""
    try:
        ensure_sheet(tutor)
    except Exception as e:  # noqa: BLE001
        log(f"Не смог подготовить лист расписания: {type(e).__name__}")
    try:
        return sheets_util.get_values(sheet_for(tutor), f"'{SHEET}'!A1:I")
    except Exception as e:  # noqa: BLE001
        log(f"Не прочитал расписание: {type(e).__name__}")
        return []


def ensure_sheet(tutor: str = "") -> str:
    from sheets_util import sheets
    sh_id = sheet_for(tutor)
    if SHEET in sheets_util.sheet_titles(sh_id):
        return SHEET
    sheets().spreadsheets().batchUpdate(
        spreadsheetId=sh_id,
        body={"requests": [{"addSheet": {"properties": {"title": SHEET}}}]}).execute()
    sheets_util.update_values(sh_id, f"'{SHEET}'!A1:I1", [COLUMNS])
    sheets_util.forget_sheet_titles(sh_id)
    log(f"Создан лист «{SHEET}»")
    return SHEET


def _rows(tutor: str = "", *, fresh: bool = False) -> list[list[str]]:
    try:
        ensure_sheet(tutor)
    except Exception as e:  # noqa: BLE001
        log(f"Не смог подготовить лист «{SHEET}»: {e}")
    return sheets_util.get_values(sheet_for(tutor), f"'{SHEET}'!A1:I", fresh=fresh)


def _rows_all() -> list[list[str]]:
    """Расписание из ВСЕХ таблиц: главная + личные (Максим, Вика).

    Без этого напоминания и общий список не видели расписание
    репетиторов с собственной книгой.
    """
    out: list[tuple[str, list[str]]] = []
    seen_sh: set[str] = set()
    # главная таблица
    try:
        main_id = workspaces.spreadsheet_for("")
        for row in sheets_util.get_values(main_id, f"'{SHEET}'!A1:I"):
            out.append((main_id, row))
        seen_sh.add(main_id)
    except Exception as e:  # noqa: BLE001
        log(f"Не прочитал расписание главной: {e}")
    # личные таблицы
    try:
        for _tutor, sid in workspaces.WORKSPACES.items():
            if sid in seen_sh:
                continue
            try:
                for row in sheets_util.get_values(sid, f"'{SHEET}'!A1:I"):
                    out.append((sid, row))
            except Exception as e:  # noqa: BLE001
                log(f"Не прочитал личное расписание: {type(e).__name__}")
    except Exception as e:  # noqa: BLE001
        log(f"Личные расписания не прочитаны: {e}")
    return out


def ensure_personal_ids() -> int:
    """Заполнить пустые ID в личных расписаниях."""
    added = 0
    for tutor, book in workspaces.WORKSPACES.items():
        try:
            with sheets_util.RECORD_ID_LOCK:
                rows = _rows(tutor, fresh=True)
                used = {str(row[0]).strip() for row in rows[1:] if row}
                prefix = workspaces.record_owner_prefix(tutor) + "S"
                for n, row in enumerate(rows[1:], start=2):
                    if len(row) < 3 or str(row[0]).strip() or not str(row[2]).strip():
                        continue
                    day = one_time_date(str(row[4])) if len(row) > 4 else None
                    candidate = sheets_util.next_record_id(prefix, day or dt.date.today(), used)
                    sheets_util.update_values(book, f"'{SHEET}'!A{n}", [[candidate]])
                    used.add(candidate)
                    added += 1
        except Exception as error:  # noqa: BLE001
            log(f"Не удалось заполнить ID расписания: {type(error).__name__}")
    return added


def _next_record(rows: list[list[str]], tutor: str, kind: str,
                 day: dt.date) -> tuple[int, str]:
    """Свободная строка и ID расписания или отмены."""
    prefix = workspaces.record_owner_prefix(tutor) + kind
    last = max((n for n, row in enumerate(rows[1:], start=2)
                if any(str(value).strip() for value in row)), default=1)
    used = (row[0] for row in rows[1:] if row)
    return last + 1, sheets_util.next_record_id(prefix, day, used)


CANCEL_SHEET = "Отмены"
CANCEL_COLS = ["ID", "Репетитор", "Ученик", "Дата", "Время"]


def ensure_cancel_sheet(sh_id: str = "", tutor: str = "") -> str:
    from sheets_util import sheets
    if not sh_id:
        sh_id = sheet_for(tutor)
    if CANCEL_SHEET in sheets_util.sheet_titles(sh_id):
        return CANCEL_SHEET
    sheets().spreadsheets().batchUpdate(
        spreadsheetId=sh_id,
        body={"requests": [{"addSheet": {"properties": {"title": CANCEL_SHEET}}}]}).execute()
    sheets_util.update_values(sh_id, f"'{CANCEL_SHEET}'!A1:E1", [CANCEL_COLS])
    sheets_util.forget_sheet_titles(sh_id)
    log(f"Создан лист «{CANCEL_SHEET}»")
    return CANCEL_SHEET


def _cancel_rows(tutor: str = "", *, fresh: bool = False) -> list[list[str]]:
    """Отмены из таблицы репетитора (или текущей)."""
    sh_id = sheet_for(tutor)
    return sheets_util.get_values(sh_id, f"'{CANCEL_SHEET}'!A1:E", fresh=fresh)


def cancels_for(tutor: str, *, strict: bool = False) -> list[dict]:
    out = []
    try:
        rows = _cancel_rows(tutor, fresh=strict)
    except Exception as exc:
        # В новой книге лист отмен появляется после первой отмены.
        missing_sheet = (getattr(getattr(exc, "resp", None), "status", None) == 400
                         and "unable to parse range" in str(exc).lower())
        if strict and not missing_sheet:
            raise
        return []
    for n, row in enumerate(rows, start=1):
        if n == 1 or len(row) < 4 or not norm_name(row[1]):
            continue
        if norm_name(row[1]) != norm_name(tutor):
            continue
        try:
            d = dt.datetime.strptime(str(row[3]).strip(), "%d.%m.%Y").date()
        except (ValueError, TypeError):
            continue
        out.append({"id": row[0], "ученик": row[2] if len(row) > 2 else "",
                    "дата": d, "время": row[4] if len(row) > 4 else "", "row": n})
    return out


def _cancel_record(tutor: str, student: str, day: dt.date, time_str: str):
    row, rid = _next_record(_cancel_rows(tutor, fresh=True), tutor, "C", day)
    return rid, {"range": f"'{CANCEL_SHEET}'!A{row}:E{row}",
                 "values": [[rid, tutor, student, day.strftime("%d.%m.%Y"), time_str]]}


def add_cancel(tutor: str, student: str, day: dt.date, time_str: str) -> str:
    with sheets_util.RECORD_ID_LOCK:
        sh_id = sheet_for(tutor)
        ensure_cancel_sheet(sh_id, tutor)
        slot = parse_time_slot(time_str)
        if slot is None:
            raise ValueError("Некорректное время в отмене занятия.")
        previous = next((item for item in cancels_for(tutor, strict=True)
                         if item["дата"] == day and parse_time_slot(item["время"]) == slot), None)
        if previous:
            return str(previous["id"])
        rid, record = _cancel_record(tutor, student, day, time_str)
        sheets_util.update_values(sh_id, record["range"], record["values"])
        log("Отмена занятия записана")
        return rid


def is_cancelled(tutor: str, day: dt.date, time_str: str, *, strict: bool = False) -> bool:
    want = parse_time_slot(time_str)
    if want is None:
        return False
    for c in cancels_for(tutor, strict=strict):
        got = parse_time_slot(c["время"])
        if got is not None and c["дата"] == day and got == want:
            return True
    return False


def parse_time_slot(value) -> int | None:
    """Время → минуты от полуночи. None, если время не читается.

    Раньше возвращалось -1, и два «нечитаемых» времени считались
    совпадающими → занятие ложно помечалось отменённым.
    """
    try:
        parts = str(value).strip().replace(".", ":").split(":")
        if len(parts) > 2 or any(not part.isdecimal() or len(part) > 2 for part in parts):
            return None
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return hh * 60 + mm
    except (ValueError, TypeError):
        return None


def remove_cancel(tutor: str, day: dt.date, time_str: str) -> bool:
    """Снять отмену занятия (вернуть в расписание на эту дату)."""
    slot = parse_time_slot(time_str)
    if slot is None:
        return False
    sh_id = sheet_for(tutor)
    with sheets_util.RECORD_ID_LOCK:
        rows = [item["row"] for item in cancels_for(tutor, strict=True)
                if item["дата"] == day and parse_time_slot(item["время"]) == slot]
        if not rows:
            return False
        sheets_util.update_values_batch(sh_id, [
            {"range": f"'{CANCEL_SHEET}'!A{row}:E{row}", "values": [[""] * 5]}
            for row in rows
        ])
    log("Отмена занятия снята")
    return True


def next_date_for_weekday(weekday: int, after: dt.date | None = None) -> dt.date:
    base = after or dt.date.today()
    return base + dt.timedelta(days=(weekday - base.weekday()) % 7)


def one_time_date(value: str) -> dt.date | None:
    """Если «Дни» содержит дату (ДД.ММ.ГГГГ) — это разовое занятие."""
    try:
        return dt.datetime.strptime(str(value).strip(), "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return None


def entries_for(tutor: str, student: str) -> list[dict]:
    """Все записи расписания конкретного ученика."""
    want = norm_name(student)
    return [e for e in entries_of(tutor) if norm_name(e["ученик"]) == want]


def get_entry(entry_id, tutor: str = "") -> dict | None:
    """Запись расписания по ID. Если задан репетитор — только его."""
    want = str(entry_id).strip()
    matches = [entry for entry in all_entries()
               if str(entry["id"]).strip() == want
               and (not tutor or norm_name(entry.get("репетитор", "")) == norm_name(tutor))]
    return matches[0] if len(matches) == 1 else None


def update_entry(entry_id, day: str, time_str: str,
                 tutor: str = "", sh_id: str = "") -> bool:
    """Меняет день и время записи по ID (в таблице этого репетитора)."""
    if not sh_id:
        sh_id = sheet_for(tutor)
    rows = _rows_for(tutor)
    for n, row in enumerate(rows, start=1):
        if n == 1 or not row:
            continue
        if str(row[0]).strip() != str(entry_id):
            continue
        # ID может совпасть у разных репетиторов в одном листе
        if tutor and len(row) > 1 and not names_match(row[1], tutor):
            continue
        sheets_util.update_values(
            sh_id, f"'{SHEET}'!E{n}:F{n}", [[day, time_str]])
        log(f"РАСПИСАНИЕ обновлено id={entry_id}: {day} {time_str}")
        return True
    return False


def add_one_time(tutor: str, student: str, parent: str, day: dt.date,
                 time_str: str, price: str) -> str:
    """Разовое (перенесённое) занятие: одна конкретная дата."""
    return add_entry(tutor, student, parent, day.strftime("%d.%m.%Y"),
                     time_str, price)


def move_occurrence(entry: dict, day: dt.date, new_time: str) -> str:
    """Погасить исходный слот на одну дату и добавить замену одним запросом."""
    with sheets_util.RECORD_ID_LOCK:
        tutor, student = entry["репетитор"], entry["ученик"]
        sh_id = sheet_for(tutor)
        ensure_cancel_sheet(sh_id, tutor)
        if is_cancelled(tutor, day, entry["время"], strict=True):
            raise ValueError("Исходное занятие уже отменено или перенесено.")
        row, rid = _next_record(_rows(tutor, fresh=True), tutor, "S", day)
        _, cancel = _cancel_record(tutor, student, day, entry["время"])
        new_lesson = {"range": f"'{SHEET}'!A{row}:I{row}",
                      "values": [[rid, tutor, student, entry.get("родитель", ""),
                                  day.strftime("%d.%m.%Y"), new_time,
                                  entry.get("цена", ""), "да", ""]]}
        sheets_util.update_values_batch(sh_id, [cancel, new_lesson])
        return rid


def cleanup_expired() -> int:
    """Удалить разовые занятия, дата которых уже прошла."""
    today = dt.date.today()
    removed = 0
    for e in all_entries():
        d = one_time_date(str(e.get("дни", "")))
        if d and d < today:
            delete_entry(e["id"], e.get("репетитор", ""), e.get("sh_id", ""))
            removed += 1
    return removed


def entries_of(tutor: str, only_active: bool = True, *, strict: bool = False) -> list[dict]:
    out = []
    rows = _rows(tutor) if strict else _rows_for(tutor)
    for n, row in enumerate(rows, start=1):
        if n == 1 or len(row) < 2 or not norm_name(row[1]):
            continue
        if norm_name(row[1]) != norm_name(tutor):
            continue
        active = (row[7].strip().lower() not in ("0", "нет", "false") if len(row) > 7 else True)
        if only_active and not active:
            continue
        rid = str(row[0]).strip()
        if not rid:
            continue
        out.append({
            "id": rid,
            "репетитор": row[1],
            "ученик": row[2] if len(row) > 2 else "",
            "родитель": row[3] if len(row) > 3 else "",
            "дни": row[4] if len(row) > 4 else "",
            "время": row[5] if len(row) > 5 else "",
            "цена": row[6] if len(row) > 6 else "",
            "активно": active,
            "последняя": row[8] if len(row) > 8 else "",
            "row": n,
        })
    return out


def add_entry(tutor: str, student: str, parent: str, days: str, time_str: str,
              price: str) -> str:
    """Добавляет запись расписания. Возвращает ID."""
    with sheets_util.RECORD_ID_LOCK:
        sh_id = sheet_for(tutor)
        day = one_time_date(days) or dt.date.today()
        row_num, rid = _next_record(_rows(tutor, fresh=True), tutor, "S", day)
        sheets_util.update_values(
            sh_id, f"'{SHEET}'!A{row_num}:I{row_num}",
            [[str(rid), tutor, student, parent, days, time_str, price, "да", ""]])
        log("Запись добавлена в расписание")
        return rid


def update_last_date(entry_id, day: dt.date, tutor: str = "",
                     sh_id: str = "") -> None:
    """Записать «последнюю дату» занятия в таблицу его репетитора."""
    if not sh_id:
        sh_id = sheet_for(tutor)
    rows = _rows_for(tutor)
    for n, row in enumerate(rows, start=1):
        if n == 1 or not row:
            continue
        if str(row[0]).strip() != str(entry_id):
            continue
        if tutor and len(row) > 1 and not names_match(row[1], tutor):
            continue
        sheets_util.update_values(
            sh_id, f"'{SHEET}'!I{n}", [[day.strftime("%d.%m.%Y")]])
        return


def delete_entry(entry_id, tutor: str = "", sh_id: str = "") -> bool:
    """Удалить запись расписания (ищет в таблице репетитора)."""
    if not sh_id:
        sh_id = sheet_for(tutor)
    rows = _rows_for(tutor)
    for n, row in enumerate(rows, start=1):
        if n == 1 or not row:
            continue
        if str(row[0]).strip() != str(entry_id):
            continue
        if tutor and len(row) > 1 and not names_match(row[1], tutor):
            continue
        sheets_util.clear_values(sh_id, f"'{SHEET}'!A{n}:I{n}")
        log(f"РАСПИСАНИЕ удалено id={entry_id}")
        return True
    return False


def all_entries() -> list[dict]:
    """Все активные записи расписания по всем репетиторам (все таблицы)."""
    out = []
    for n, (sh_id, row) in enumerate(_rows_all(), start=1):
        if len(row) < 6 or not norm_name(row[1]):
            continue
        if len(row) > 7 and row[7].strip().lower() in ("0", "нет", "false"):
            continue
        rid = str(row[0]).strip()
        if not rid or rid.lower() == "id":
            continue
        out.append({
            "id": rid, "sh_id": sh_id, "репетитор": row[1],
            "ученик": row[2] if len(row) > 2 else "",
            "родитель": row[3] if len(row) > 3 else "",
            "дни": row[4] if len(row) > 4 else "",
            "время": row[5] if len(row) > 5 else "",
            "цена": row[6] if len(row) > 6 else "",
            "row": n,
        })
    return out


def parse_days(text: str) -> list[int]:
    """Разобрать отдельные дни недели и диапазоны, например «Пн–Ср»."""
    text = " ".join(str(text or "").casefold().split())
    if text in ("каждый день", "ежедневно"):
        return list(range(7))
    aliases = {name.casefold(): i for i, name in enumerate(DAY_NAMES)}
    aliases.update({name.casefold(): i for i, name in enumerate(FULL_DAY_NAMES)})
    aliases.update({str(i + 1): i for i in range(7)})

    def day_index(value: str) -> int | None:
        result = aliases.get(value)
        if result is not None:
            return result
        if len(value) >= 3:
            return next((i for i, name in enumerate(FULL_DAY_NAMES)
                         if name.casefold().startswith(value)), None)
        return None

    text = text.replace("–", "-").replace("—", "-").replace(" -", "-").replace("- ", "-")
    for separator in (";", "/", " "):
        text = text.replace(separator, ",")
    out: set[int] = set()
    for part in text.split(","):
        first, sep, last = part.partition("-")
        start = day_index(first)
        if start is None:
            continue
        if not sep:
            out.add(start)
            continue
        end = day_index(last)
        if end is not None:
            out.update((start + offset) % 7 for offset in range((end - start) % 7 + 1))
    return sorted(out)


def next_occurrence(entry: dict, after: dt.date | None = None) -> dt.date | None:
    """Ближайшая дата занятия по дням недели (или дата разового)."""
    base = after or dt.date.today()
    single = one_time_date(str(entry.get("дни", "")))
    if single is not None:
        return single if single >= base else None
    days = parse_days(str(entry.get("дни", "")))
    if not days:
        return None
    for offset in range(0, 15):
        d = base + dt.timedelta(days=offset)
        if d.weekday() in days:
            return d
    return None
