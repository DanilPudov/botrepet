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
from sheets_util import load_config, log, norm_name

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
        log(f"Не смог подготовить лист «{SHEET}» для {tutor}: {e}")
    try:
        return sheets_util.get_values(sheet_for(tutor), f"'{SHEET}'!A1:I")
    except Exception as e:  # noqa: BLE001
        log(f"Не прочитал расписание {tutor}: {e}")
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


def _rows(tutor: str = "") -> list[list[str]]:
    try:
        ensure_sheet(tutor)
    except Exception as e:  # noqa: BLE001
        log(f"Не смог подготовить лист «{SHEET}»: {e}")
    return sheets_util.get_values(sheet_for(tutor), f"'{SHEET}'!A1:I")


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
                log(f"Не прочитал расписание {_tutor}: {e}")
    except Exception as e:  # noqa: BLE001
        log(f"Личные расписания не прочитаны: {e}")
    return out


def ensure_personal_ids() -> int:
    """Проставить буквенные ID строкам расписания в личных таблицах.

    Репетитор может добавить занятие в таблицу руками — тогда ID пустой
    или числовой. Здесь он становится M… / V…, чтобы не совпадать
    с ID общей таблицы.
    """
    added = 0
    try:
        for tutor, sid in workspaces.WORKSPACES.items():
            letter = id_letter(tutor)
            if not letter:
                continue
            try:
                rows = sheets_util.get_values(sid, f"'{SHEET}'!A1:I")
            except Exception as e:  # noqa: BLE001
                log(f"ID расписания: не прочитал {tutor}: {e}")
                continue
            if not rows:
                continue
            used = {str(r[0]).strip() for r in rows[1:]
                    if r and str(r[0]).strip().startswith(letter)}
            mx = 0
            for u in used:
                if u[len(letter):].isdigit():
                    mx = max(mx, int(u[len(letter):]))
            for n, row in enumerate(rows, start=1):
                if n == 1 or not row:
                    continue
                if not any(str(x).strip() for x in row):
                    continue
                raw = str(row[0]).strip()
                if raw.startswith(letter) and raw[len(letter):].isdigit():
                    continue
                mx += 1
                cand = f"{letter}{mx}"
                while cand in used:
                    mx += 1
                    cand = f"{letter}{mx}"
                try:
                    sheets_util.update_values(
                        sid, f"'{SHEET}'!A{n}", [[cand]],
                        value_input_option="USER_ENTERED")
                    used.add(cand)
                    added += 1
                except Exception as e:  # noqa: BLE001
                    log(f"ID расписания: не записал {tutor} стр.{n}: {e}")
    except Exception as e:  # noqa: BLE001
        log(f"ID расписания личных таблиц: {e}")
    if added:
        log(f"ID расписания добавлены: {added}")
    return added


def _next_id(tutor: str = "") -> str:
    """Следующий ID расписания: M1/V1 для личных таблиц, число для общей."""
    letter = id_letter(tutor)
    mx = 0
    for n, row in enumerate(_rows_for(tutor), start=1):
        if n == 1 or not row:
            continue
        raw = str(row[0]).strip()
        if letter:
            if raw.startswith(letter) and raw[len(letter):].isdigit():
                mx = max(mx, int(raw[len(letter):]))
        elif raw.isdigit():
            mx = max(mx, int(raw))
    return f"{letter}{mx + 1}"


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


def _cancel_rows(tutor: str = "") -> list[list[str]]:
    """Отмены из таблицы репетитора (или текущей)."""
    sh_id = sheet_for(tutor)
    return sheets_util.get_values(sh_id, f"'{CANCEL_SHEET}'!A1:E")


def cancels_for(tutor: str) -> list[dict]:
    out = []
    try:
        rows = _cancel_rows(tutor)
    except Exception:
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


def add_cancel(tutor: str, student: str, day: dt.date, time_str: str) -> int:
    sh_id = sheet_for(tutor)
    ensure_cancel_sheet(sh_id, tutor)
    rows = _cancel_rows(tutor)
    last = 1
    letter = id_letter(tutor)
    max_id = 0
    for n, row in enumerate(rows, start=1):
        row = row or []
        if any(str(x).strip() for x in row):
            last = n
        raw = str(row[0]).strip() if row else ""
        if letter:
            if raw.startswith(letter) and raw[len(letter):].isdigit():
                max_id = max(max_id, int(raw[len(letter):]))
        elif raw.isdigit():
            max_id = max(max_id, int(raw))
    rid = f"{letter}{max_id + 1}"
    sheets_util.update_values(
        sh_id, f"'{CANCEL_SHEET}'!A{last + 1}:E{last + 1}",
        [[str(rid), tutor, student, day.strftime("%d.%m.%Y"), time_str]])
    log(f"ОТМЕНА: {tutor} — {student} {day:%d.%m} {time_str}")
    return rid


def is_cancelled(tutor: str, day: dt.date, time_str: str) -> bool:
    want = parse_time_slot(time_str)
    if want is None:
        return False
    for c in cancels_for(tutor):
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
        parts = str(value).replace(".", ":").split(":")
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return hh * 60 + mm
    except (ValueError, TypeError):
        return None


def remove_cancel(tutor: str, day: dt.date, time_str: str) -> bool:
    """Снять отмену занятия (вернуть в расписание на эту дату)."""
    sh_id = sheet_for(tutor)
    for c in cancels_for(tutor):
        if c["дата"] == day and parse_time_slot(c["время"]) == parse_time_slot(time_str):
            try:
                sheets_util.update_values(
                    sh_id, f"'{CANCEL_SHEET}'!A{c['row']}:E{c['row']}", [[""] * 5])
            except Exception as e:  # noqa: BLE001
                log(f"Не удалось снять отмену: {e}")
                return False
            log(f"ОТМЕНА снята: {tutor} {day:%d.%m} {time_str}")
            return True
    return False


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
    for e in all_entries():
        if str(e["id"]).strip() != want:
            continue
        if tutor and norm_name(e.get("репетитор", "")) != norm_name(tutor):
            continue
        return e
    return None


def update_entry(entry_id, day: str, time_str: str,
                 tutor: str = "", sh_id: str = "") -> bool:
    """Меняет день и время записи по ID (в таблице этого репетитора)."""
    if not sh_id:
        sh_id = sheet_for(tutor)
    rows = _rows_for(tutor)
    for n, row in enumerate(rows, start=1):
        if n == 1 or not row:
            continue
        if str(row[0]).strip() == str(entry_id):
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


def entries_of(tutor: str, only_active: bool = True) -> list[dict]:
    out = []
    for n, row in enumerate(_rows_for(tutor), start=1):
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
    sh_id = sheet_for(tutor)
    rows = _rows_for(tutor)
    last = 1
    for n, row in enumerate(rows, start=1):
        if any(str(x).strip() for x in row):
            last = n
    rid = _next_id(tutor)
    row_num = last + 1
    sheets_util.update_values(
        sh_id, f"'{SHEET}'!A{row_num}:I{row_num}",
        [[str(rid), tutor, student, parent, days, time_str, price, "да", ""]])
    log(f"РАСПИСАНИЕ добавлено id={rid}: {tutor} — {student} {days} {time_str}")
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
        if str(row[0]).strip() == str(entry_id):
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
        if str(row[0]).strip() == str(entry_id):
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
    """'Пн,Ср' / 'пн ср' / '1,3' -> [0, 2]."""
    text = (text.replace(";", ",").replace("/", ",")
             .replace(" ", ",").replace("\t", ",").lower())
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx <= 6:
                out.append(idx)
            continue
        short = part[:2].capitalize()
        if short in DAY_NAMES:
            out.append(DAY_NAMES.index(short))
            continue
        # «понедельник», «воскресенье» и т.п.
        for w, full in enumerate(FULL_DAY_NAMES):
            if part and (full.startswith(part[:3]) or part.startswith(full[:3])):
                out.append(w)
                break
    return sorted(set(out))


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
