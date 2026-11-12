"""
Сверка выписки Озон-банка с листом «Чеки» и перенос в лист «История».

Это сценарий «посмотри репетиторов» в автоматическом виде:
  1. Бот берёт записи из «Чеки» начиная с даты (или все с прошлого переноса).
  2. Берёт операции из выписки (CSV или список, полученный из PDF-парсера).
  3. Сопоставляет по правилу: та же дата + та же сумма.
  4. Пишет блок-зеркало в «Историю» (Лист1) копией строк из «Чеки»,
     сумму красит зелёным (пришло) / красным (не нашлось).
  5. Отправляет отчёт владельцу в Телеграм.

Запуск:
  python reconcile.py --start 08.11.2026            # сверка с этой даты
  python reconcile.py --start 08.11.2026 --dry-run  # только показать, ничего не писать

Выписку можно передать файлом CSV: --ops D:/.../ops.csv
  Формат строки: 2026-11-08 21:29;1700;Вадим
или без файла — будет взят data/ops.json (список операций, который собирает бот).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

try:  # эмодзи в консоли Windows
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

BASE_DIR = Path(__file__).resolve().parent
if (BASE_DIR / "pkgs").exists():
    sys.path.insert(0, str(BASE_DIR / "pkgs"))

from sheets_util import (  # noqa: E402
    BASE_DIR,
    load_config,
    load_state,
    save_state,
    get_values,
    update_values,
    clear_values,
    sheets,
    log,
    norm_name,
)

OPS_PATH = BASE_DIR / "data" / "ops.json"
GREEN = {"red": 0.0, "green": 0.72, "blue": 0.25}
RED = {"red": 0.9, "green": 0.15, "blue": 0.15}
WHITE = {"red": 1, "green": 1, "blue": 1}


def norm_key_name(name) -> str:
    """Имя для ключа сверки: схлопнутые пробелы и единый вид скобок.

    «Варя ( Оксана)», «Варя(Оксана )» и «Варя (Оксана)» дают один ключ.
    """
    s = norm_name(str(name))
    s = re.sub(r"\s*\(\s*", " (", s)
    s = re.sub(r"\s*\)\s*", ")", s)
    return s.strip()


def parse_int(value) -> int | None:
    """'1 700', '1\\u00a0700', '1700,00', '1700 ₽', 1700 -> int, иначе None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    s = str(value)
    for ch in ("\u00a0", " ", "₽", "\t"):
        s = s.replace(ch, "")
    s = s.strip()
    if not s:
        return None
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    elif "," in s and "." in s:
        s = s.replace(",", "")
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def cell_date(row) -> dt.date | None:
    """Дата из колонки B строки «Истории»/«Чеков» (ДД.ММ.ГГГГ), или None."""
    try:
        return dt.datetime.strptime(str(row[1]).strip(), "%d.%m.%Y").date()
    except (ValueError, IndexError):
        return None


# --------------------------------------------------------------------------- #
# Данные
# --------------------------------------------------------------------------- #
def _parse_dt(value) -> dt.datetime:
    """Дата-время операции из JSON: datetime, date или строка разных форматов."""
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time())
    s = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"не понимаю дату операции: {value!r}")


def load_ops(path: Path | None) -> list[dict]:
    """Список операций: [{"dt": datetime, "sum": int, "who": str}]"""
    if path is None:
        path = OPS_PATH
    if not path.exists():
        return []
    if path.suffix.lower() == ".csv":
        out = []
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            parts = [p.strip() for p in line.split(";")]
            if len(parts) < 2:
                continue
            try:
                d = dt.datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
                s = int(float(parts[1].replace(" ", "").replace(",", ".")))
            except ValueError:
                continue
            out.append({"dt": d, "sum": s, "who": parts[2] if len(parts) > 2 else ""})
        return out
    # JSON: bot/pdf_parser пишут дату строкой — приводим к datetime,
    # иначе match() падает на o["dt"].date().
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for item in raw:
        try:
            d = _parse_dt(item["dt"])
            s = int(round(float(item["sum"])))
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"dt": d, "sum": s, "who": str(item.get("who") or "")})
    return out


def parse_date(s: str) -> dt.date:
    return dt.datetime.strptime(s.strip(), "%d.%m.%Y").date()


def checks_rows(start: dt.date) -> list[dict]:
    cfg = load_config()
    title = cfg["sheets"]["recept_checks_sheet"]
    rows = get_values(cfg["sheets"]["recept"], f"'{title}'!A1:J")
    out = []
    for n, row in enumerate(rows, start=1):
        if n < 2 or len(row) < 5:
            continue
        if not norm_name(row[2]) or not str(row[1]).strip():
            continue
        d = cell_date(row)
        if d is None or d < start:
            continue
        s = parse_int(row[4])
        if s is None:
            continue
        out.append({"row": n, "дата": d, "время": str(row[0]).strip() if row[0] else "",
                    "репетитор": norm_key_name(row[2]), "ученик": row[3], "сумма": s,
                    "комментарий": row[5] if len(row) > 5 else "",
                    "первое": row[6] if len(row) > 6 else "",
                    "как_прошло": row[7] if len(row) > 7 else "",
                    "будут": row[8] if len(row) > 8 else "",
                    "комиссия": row[9] if len(row) > 9 else ""})
    return out


def match(checks: list[dict], ops: list[dict]) -> list[dict]:
    """Сопоставление: одна операция — не больше одной записи; та же дата + сумма."""
    used = set()
    for c in checks:
        found = None
        for i, o in enumerate(ops):
            if i in used:
                continue
            if o["dt"].date() == c["дата"] and int(o["sum"]) == int(c["сумма"]):
                found = i
                break
        c["paid"] = found is not None
        if found is not None:
            used.add(found)
    return checks


# --------------------------------------------------------------------------- #
# Запись в «Историю»
# --------------------------------------------------------------------------- #
def next_history_row() -> int:
    """Первая пустая строка в «Истории» по колонке C (Репетитор)."""
    cfg = load_config()
    sh_id = cfg["sheets"]["history"]
    title = cfg["sheets"]["history_sheet"]
    rows = get_values(sh_id, f"'{title}'!A1:J")
    last = 0
    for n, row in enumerate(rows, start=1):
        if any(x.strip() for x in row):
            last = n
    return last + 1


def write_history(checks: list[dict], dry_run: bool = False) -> int:
    cfg = load_config()
    sh_id = cfg["sheets"]["history"]
    title = cfg["sheets"]["history_sheet"]
    start_row = next_history_row()
    if not checks:
        return start_row

    values = [[c["время"], c["дата"].strftime("%d.%m.%Y"), c["репетитор"], c["ученик"],
               c["сумма"], c["комментарий"], c["первое"], c["как_прошло"], c["будут"],
               c["комиссия"]] for c in checks]
    end_row = start_row + len(values) - 1
    log(f"История: пишу {len(values)} строк в {start_row}..{end_row}")
    if dry_run:
        return start_row

    update_values(sh_id, f"'{title}'!A{start_row}:J{end_row}", values)

    # оформление: белый текст + выравнивание, суммы зелёным/красным
    def fmt(r0, r1, color):
        return {"repeatCell": {
            "range": {"sheetId": 0, "startRowIndex": r0 - 1, "endRowIndex": r1,
                      "startColumnIndex": 0, "endColumnIndex": 10},
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                "textFormat": {"foregroundColor": WHITE}}},
            "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment,textFormat)"}}

    def money(r0, r1, color):
        return {"repeatCell": {
            "range": {"sheetId": 0, "startRowIndex": r0 - 1, "endRowIndex": r1,
                      "startColumnIndex": 4, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {"textFormat": {"foregroundColor": color}}},
            "fields": "userEnteredFormat.textFormat"}}

    reqs = [fmt(start_row, end_row, WHITE)]
    for i, c in enumerate(checks):
        r = start_row + i
        reqs.append(money(r, r, GREEN if c["paid"] else RED))
    sheets().spreadsheets().batchUpdate(
        spreadsheetId=sh_id, body={"requests": reqs}).execute()
    return start_row


# --------------------------------------------------------------------------- #
# Остатки комиссий (правая таблица в 2027) — только отчёт
# --------------------------------------------------------------------------- #
def commission_balances() -> dict[str, int]:
    cfg = load_config()
    title = "Репетиторы и ученики 2027"
    rows = get_values(cfg["sheets"]["recept"], f"'{title}'!A1:H")
    out: dict[str, int] = {}
    cur = None
    for row in rows[2:]:
        if len(row) > 0 and row[0].strip() and len(row) > 4 and row[4].strip().isdigit():
            cur = norm_name(row[0])
        if cur and len(row) > 7 and row[7].strip().isdigit():
            out[cur] = out.get(cur, 0) + int(row[7])
    return out


# --------------------------------------------------------------------------- #
# Линия недели и защита от дублей
# --------------------------------------------------------------------------- #
RULE_COLOR = {"red": 0.0, "green": 1.0, "blue": 0.0}  # rgb(0,1,0)


def week_rule_request(row: int) -> dict:
    """Толстая нижняя граница зелёного цвета на A:J указанной строки (1-based)."""
    return {"updateBorders": {
        "range": {"sheetId": 0, "startRowIndex": row - 1, "endRowIndex": row,
                  "startColumnIndex": 0, "endColumnIndex": 10},
        "bottom": {"style": "SOLID_THICK", "color": RULE_COLOR},
    }}


def has_week_rule(row: int) -> bool:
    cfg = load_config()
    sh_id = cfg["sheets"]["history"]
    title = cfg["sheets"]["history_sheet"]
    resp = sheets().spreadsheets().get(
        spreadsheetId=sh_id,
        ranges=[f"'{title}'!A{row}:J{row}"],
        fields="sheets(data(rowData(values(userEnteredFormat/borders/bottom))))",
    ).execute()
    try:
        cell = resp["sheets"][0]["data"][0]["rowData"][0]["values"][0]
        bottom = cell["userEnteredFormat"]["borders"]["bottom"]
        return bottom.get("style") == "SOLID_THICK" and bottom.get("color", {}).get("green") == 1.0
    except (KeyError, IndexError):
        return False


def apply_week_rule(row: int) -> bool:
    """Ставит линию и проверяет, что она встала."""
    cfg = load_config()
    sh_id = cfg["sheets"]["history"]
    sheets().spreadsheets().batchUpdate(
        spreadsheetId=sh_id, body={"requests": [week_rule_request(row)]}).execute()
    ok = has_week_rule(row)
    log(f"Линия недели на строке {row}: {'ОК' if ok else 'НЕ ПОДТВЕРЖДЕНА'}")
    return ok


def history_entry_keys(start: dt.date) -> set[tuple[str, dt.date, str, int]]:
    """Ключи уже перенесённых в «Историю» записей (репетитор, дата, ученик, сумма)."""
    cfg = load_config()
    rows = get_values(cfg["sheets"]["history"],
                      f"'{cfg['sheets']['history_sheet']}'!A1:J")
    keys = set()
    for row in rows:
        if len(row) < 5:
            continue
        d = cell_date(row)
        s = parse_int(row[4])
        if d is None or s is None:
            continue
        if d < start or not norm_key_name(row[2]):
            continue
        keys.add((norm_key_name(row[2]), d, norm_key_name(row[3]), s))
    return keys


def drop_duplicates(checks: list[dict]) -> tuple[list[dict], list[dict]]:
    existing = history_entry_keys(min(c["дата"] for c in checks)) if checks else set()
    fresh, dups = [], []
    for c in checks:
        key = (norm_key_name(c["репетитор"]), c["дата"],
               norm_key_name(c["ученик"]), int(c["сумма"]))
        if key in existing:
            dups.append(c)
        else:
            fresh.append(c)
            existing.add(key)
    return fresh, dups


def _fmt_row(c: dict) -> list:
    return [c["время"], c["дата"].strftime("%d.%m.%Y"), c["репетитор"], c["ученик"],
            c["сумма"], c["комментарий"], c["первое"], c["как_прошло"], c["будут"],
            c["комиссия"]]


def _norm(v) -> str:
    """Нормализация имени для сравнения (пробелы + скобки)."""
    return norm_key_name(v)


def _not_empty(row: list) -> bool:
    return any(str(x).strip() for x in row)


def insert_missing_rows(checks: list[dict], dry_run: bool = False) -> list[dict]:
    """
    Вставляет недостающие строки внутрь уже существующего блока «Истории»,
    сохраняя хронологию (сортировка по дате; внутри дня — после других
    учеников того же репетитора, как в «Чеках»).

    Возвращает план: [{"запись": c, "перед_строкой": N, "позиция": "...",
                       "строка": M}]
    «перед_строкой» — номер строки ДО вставок, «строка» — итоговая строка,
    куда попала запись (None для вставок в dry-run).
    """
    cfg = load_config()
    sh_id = cfg["sheets"]["history"]
    title = cfg["sheets"]["history_sheet"]
    all_rows = get_values(sh_id, f"'{title}'!A1:J")
    if not all_rows:
        return []

    def day_of(r: list) -> dt.date | None:
        try:
            return dt.datetime.strptime(str(r[1]).strip(), "%d.%m.%Y").date()
        except (ValueError, IndexError):
            return None

    # --- 1) строки блока: только там, где есть репетитор И читаемая дата.
    # Заголовок «Репетитор» в первой строке поэтому в блок не попадает,
    # а пустые строки выше блока не считаются «разрывами» внутри него. ---
    row_dates: dict[int, dt.date] = {}
    occupied: set[int] = set()
    data_rows: list[tuple[int, dt.date]] = []
    for i, r in enumerate(all_rows, start=1):
        d = day_of(r)
        if d is not None:
            row_dates[i] = d
        if _not_empty(r):
            occupied.add(i)
        if d is not None and len(r) > 2 and _norm(r[2]):
            data_rows.append((i, d))
    if not data_rows:
        return []
    first = min(i for i, _ in data_rows)
    last = max(i for i, _ in data_rows)
    lo = min(d for _, d in data_rows)

    # --- 2) свободные места внутри блока: пустые строки рядом с нужной датой ---
    free_rows: list[int] = [i for i in range(first, last + 1) if i not in occupied]

    def free_spot_for(day: dt.date) -> int | None:
        """Пустая строка внутри блока, ближайшая сверху/снизу к строке с датой day."""
        best: int | None = None
        best_dist: int | None = None
        for i in free_rows:
            prev = max((k for k in row_dates if k < i), default=None)
            nxt = min((k for k in row_dates if k > i), default=None)
            dist = None
            if prev is not None and row_dates[prev] == day:
                dist = i - prev
            if nxt is not None and row_dates[nxt] == day:
                d2 = nxt - i
                if dist is None or d2 < dist:
                    dist = d2
            if dist is not None and (best_dist is None or dist < best_dist):
                best, best_dist = i, dist
        if best is not None:
            free_rows.remove(best)
        return best

    def spot_after_tutor(day: dt.date, tutor: str, student: str) -> int | None:
        """Пустая строка сразу под последней строкой этого репетитора за этот день."""
        cands = [i for i, r in enumerate(all_rows, start=1)
                 if row_dates.get(i) == day and len(r) > 2 and _norm(r[2]) == tutor]
        if not cands:
            return None
        same = [i for i in cands if len(all_rows[i - 1]) > 3
                and _norm(all_rows[i - 1][3]) == student]
        anchor = (same or cands)[-1]
        nxt = anchor + 1
        if nxt in free_rows and nxt <= last:
            free_rows.remove(nxt)
            return nxt
        return None

    existing = history_entry_keys(lo)
    plan: list[dict] = []
    for c in sorted(checks, key=lambda x: x["дата"]):  # stable: как в «Чеках»
        key = (norm_key_name(c["репетитор"]), c["дата"],
               norm_key_name(c["ученик"]), int(c["сумма"]))
        if key in existing:
            continue
        existing.add(key)

        # якорь: строка того же дня и того же репетитора
        day_tutor = [i for i, r in enumerate(all_rows, start=1)
                     if row_dates.get(i) == c["дата"] and len(r) > 2
                     and norm_key_name(r[2]) == key[0]]
        if day_tutor:
            same = [i for i in day_tutor if len(all_rows[i - 1]) > 3
                    and _norm(all_rows[i - 1][3]) == key[2]]
            anchor = (same or day_tutor)[-1]
            pos = anchor + 1
            where = f"после строки {anchor}"
        elif any(d == c["дата"] for d in row_dates.values()):
            pos = max(i for i, d in row_dates.items() if d == c["дата"]) + 1
            where = f"в конец {c['дата']:%d.%m}"
        else:
            pos = next((i for i, d in sorted(row_dates.items()) if d > c["дата"]), last + 1)
            where = f"новый день {c['дата']:%d.%m}"

        plan.append({"запись": c, "перед_строкой": pos, "позиция": where,
                     "строка": spot_after_tutor(c["дата"], key[0], key[2])
                     or free_spot_for(c["дата"])})

    log(f"История: недостающих строк — {len(plan)} (блок {first}..{last})")
    if dry_run or not plan:
        return plan

    # --- 3) свободных мест не хватило — вставляем строки ---
    # Все insertDimension идут одним batchUpdate, а Google применяет их
    # последовательно: startIndex каждой следующей вставки считается УЖЕ
    # с учётом предыдущих. Поэтому идём сверху вниз и к исходной позиции
    # прибавляем число уже вставленных строк (k) — иначе при 2+ вставках
    # в разных местах строки уезжают не туда.
    free_items = [item for item in plan if item["строка"]]
    to_insert = [item for item in plan if not item["строка"]]
    if to_insert:
        ordered = sorted(to_insert, key=lambda it: it["перед_строкой"])  # stable
        requests = []
        for k, item in enumerate(ordered):
            start = item["перед_строкой"] - 1 + k
            requests.append({"insertDimension": {
                "range": {"sheetId": 0, "dimension": "ROWS",
                          "startIndex": start, "endIndex": start + 1},
                "inheritFromBefore": False}})
            # итоговая строка вставленной записи
            item["строка"] = item["перед_строкой"] + k
        sheets().spreadsheets().batchUpdate(
            spreadsheetId=sh_id, body={"requests": requests}).execute()
        # заранее занятые пустые строки ниже вставок тоже сдвигаются
        insert_positions = [item["перед_строкой"] for item in ordered]
        for item in free_items:
            t = item["строка"]
            item["строка"] = t + sum(1 for p in insert_positions if p <= t)

    # --- 4) значения и оформление ---
    fmt_reqs = []
    for item in plan:
        t = item["строка"]
        update_values(sh_id, f"'{title}'!A{t}:J{t}", [_fmt_row(item["запись"])])
        fmt_reqs += [
            {"repeatCell": {
                "range": {"sheetId": 0, "startRowIndex": t - 1, "endRowIndex": t,
                          "startColumnIndex": 0, "endColumnIndex": 10},
                "cell": {"userEnteredFormat": {
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                    "textFormat": {"foregroundColor": WHITE}}},
                "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment,textFormat)"}},
            {"repeatCell": {
                "range": {"sheetId": 0, "startRowIndex": t - 1, "endRowIndex": t,
                          "startColumnIndex": 4, "endColumnIndex": 5},
                "cell": {"userEnteredFormat": {"textFormat": {
                    "foregroundColor": GREEN if item["запись"]["paid"] else RED}}},
                "fields": "userEnteredFormat.textFormat"}},
        ]
    sheets().spreadsheets().batchUpdate(
        spreadsheetId=sh_id, body={"requests": fmt_reqs}).execute()
    return plan


def block_bounds() -> tuple[int, int] | None:
    """Первая и последняя строки блока записей в «Истории» (с учётом зазоров).

    Учитываются только строки с репетитором И датой — заголовок и мусорные
    строки без даты в границы не попадают.
    """
    cfg = load_config()
    all_rows = get_values(cfg["sheets"]["history"], f"'{cfg['sheets']['history_sheet']}'!A1:J")
    idx = [i for i, r in enumerate(all_rows, start=1)
           if len(r) > 2 and _norm(r[2]) and cell_date(r) is not None]
    if not idx:
        return None
    return min(idx), max(idx)


# --------------------------------------------------------------------------- #
# Отчёт
# --------------------------------------------------------------------------- #
def build_report(checks: list[dict]) -> str:
    ok = [c for c in checks if c["paid"]]
    bad = [c for c in checks if not c["paid"]]
    ok_sum = sum(c["сумма"] for c in ok)
    total_sum = sum(c["сумма"] for c in checks)

    per: dict[str, int] = {}
    for c in ok:
        per[c["репетитор"]] = per.get(c["репетитор"], 0) + c["сумма"]

    lines = [f"✅ Нашлись в банке: {len(ok)} из {len(checks)} записей ({ok_sum} ₽ из {total_sum} ₽)"]
    for t, s in sorted(per.items(), key=lambda x: -x[1]):
        lines.append(f"  {t}: {s} ₽")

    if bad:
        lines += ["", "⚠️ Не нашлись в выписке (красные):"]
        for c in bad:
            lines.append(f"  {c['дата']:%d.%m} {c['время']} — {c['репетитор']} — {c['ученик']} — {c['сумма']} ₽")

    bal = commission_balances()
    bal = {t: v for t, v in bal.items() if v > 0}
    if bal:
        lines += ["", "💸 Остаток комиссии:"]
        for t, v in sorted(bal.items(), key=lambda x: -x[1]):
            lines.append(f"  {t}: {v} ₽")

    lines += ["", f"Итого к выплате репетиторам 80%: {round(ok_sum * 0.8)} ₽ (без учёта комиссий)"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="дата начала в формате 08.11.2026")
    ap.add_argument("--ops", default=None, help="CSV/JSON с операциями банка")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rule", action="store_true",
                    help="поставить толстую линию недели на последней строке блока")
    ap.add_argument("--allow-dups", action="store_true",
                    help="писать записи даже если они уже есть в «Истории»")
    args = ap.parse_args()

    start = parse_date(args.start)
    ops_path = Path(args.ops) if args.ops else None
    ops = load_ops(ops_path)
    if not ops:
        print("Нет операций банка. Передайте --ops file.csv или положите data/ops.json")
        print("Формат CSV: 2026-11-08 21:29;1700;Вадим")
        sys.exit(2)

    checks = match(checks_rows(start), ops)
    print(build_report(checks))

    fresh, dups = (checks, []) if args.allow_dups else drop_duplicates(checks)
    if dups:
        print(f"\n⏭ Уже есть в «Истории», пропускаю: {len(dups)}")
        for c in dups:
            print(f"  {c['дата']:%d.%m} — {c['репетитор']} — {c['ученик']} — {c['сумма']} ₽")

    plan = insert_missing_rows(fresh, dry_run=args.dry_run)
    if plan:
        print(f"\nИстория: недостающие строки ({len(plan)}):")
        for item in plan:
            c = item["запись"]
            tgt = f"строка {item['строка']}" if item.get("строка") else "вставить строку"
            print(f"  {tgt}: {c['дата']:%d.%m} {c['время']} — {c['репетитор']} — "
                  f"{c['ученик']} — {c['сумма']} ₽  [{item['позиция']}]")
    else:
        print("\nНовых строк для «Истории» нет — всё уже на месте.")

    if args.rule and not args.dry_run:
        bounds = block_bounds()
        if not bounds:
            print("Блок в «Истории» не найден — линию ставить некуда.")
            return
        last_row = bounds[1]
        if apply_week_rule(last_row):
            print(f"\n✅ Линия недели на последней строке блока: {last_row} (A:J, SOLID_THICK).")
        else:
            print(f"\n⚠️ Линию недели на строке {last_row} поставить не удалось — "
                  "поставьте вручную.")
    elif not args.dry_run:
        print("\nГотово. Линию недели можно поставить флагом --rule.")


if __name__ == "__main__":
    main()
