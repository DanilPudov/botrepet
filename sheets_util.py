"""
Общие утилиты бота репетиторов: конфиг, состояние, доступ к Google Sheets.
Всё хранится рядом со скриптом — бот можно перенести на другое место/хостинг,
поменяв только пути в config.json.
"""
from __future__ import annotations

import json
import datetime as dt
import threading
from pathlib import Path

import os as _os

BASE_DIR = Path(__file__).resolve().parent


def _data_dir() -> Path:
    """Папка для изменяемых файлов: DATA_DIR (хостинг) или папка бота."""
    env = (_os.environ.get("DATA_DIR") or "").strip()
    if env:
        try:
            p = Path(env)
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            pass
    # Bothost: постоянное хранилище по умолчанию
    if Path("/app/data").is_dir():
        return Path("/app/data")
    return BASE_DIR


DATA_DIR = _data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "bot.log"

_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# Конфиг и состояние
# --------------------------------------------------------------------------- #
def _ensure_config() -> None:
    """Если в DATA_DIR нет config.json — скопировать из папки бота."""
    if CONFIG_PATH.exists():
        return
    src = BASE_DIR / "config.json"
    if src.exists() and src != CONFIG_PATH:
        try:
            CONFIG_PATH.write_text(src.read_text(encoding="utf-8"),
                                   encoding="utf-8")
        except OSError:
            pass


def load_config() -> dict:
    with _lock:
        _ensure_config()
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    with _lock:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def load_state() -> dict:
    with _lock:
        if not STATE_PATH.exists():
            return {"version": 1, "tutors": [], "pending": {}, "last_seen_tutor": {}}
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    with _lock:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Google Sheets
# --------------------------------------------------------------------------- #
_sheets_client = None


def sheets():
    """Клиент Google Sheets API (сервисный аккаунт из config)."""
    global _sheets_client
    if _sheets_client is None:
        import sys

        # пакеты могут лежать рядом в ./pkgs (локальный запуск без установки)
        pkg_dir = BASE_DIR / "pkgs"
        if pkg_dir.exists() and str(pkg_dir) not in sys.path:
            sys.path.insert(0, str(pkg_dir))

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        cfg = load_config()
        key_path = str(cfg.get("service_account_key") or "")
        if key_path and not Path(key_path).is_absolute():
            for base in (DATA_DIR, BASE_DIR):
                cand = base / key_path
                if cand.exists():
                    key_path = str(cand)
                    break
        creds = service_account.Credentials.from_service_account_file(
            key_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        # static_discovery=False: не требует локальных discovery-документов
        # (экономит ~107 МБ диска на сервере)
        _sheets_client = build("sheets", "v4", credentials=creds,
                               cache_discovery=False,
                               static_discovery=False)
    return _sheets_client


def _retry_429(func, *args, **kwargs):
    """Повтор при превышении квоты Google (429) с паузой."""
    import time as _t
    last = None
    for attempt in range(4):
        try:
            return func(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "429" not in msg and "quota" not in msg.lower():
                raise
            last = e
            _t.sleep(1.5 * (attempt + 1))
    raise last


def get_values(spreadsheet_id: str, range_: str) -> list[list[str]]:
    resp = _retry_429(
        lambda: (sheets()
                 .spreadsheets()
                 .values()
                 .get(spreadsheetId=spreadsheet_id, range=range_)
                 .execute()))
    return resp.get("values", [])


def update_values(spreadsheet_id: str, range_: str, values: list[list], *,
                  value_input_option: str = "USER_ENTERED") -> dict:
    """Точная запись диапазона. НИКОГДА не используем append (см. памятку)."""
    body = {"values": values, "majorDimension": "ROWS"}
    return _retry_429(
        lambda: (sheets()
                 .spreadsheets()
                 .values()
                 .update(
                     spreadsheetId=spreadsheet_id,
                     range=range_,
                     valueInputOption=value_input_option,
                     body=body,
                 )
                 .execute()))


def clear_values(spreadsheet_id: str, range_: str) -> None:
    sheets().spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=range_
    ).execute()


# --------------------------------------------------------------------------- #
# Вспомогательное
# --------------------------------------------------------------------------- #
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_RU = [
    "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]


def fmt_date_ru(d: dt.date) -> str:
    return f"{WEEKDAYS_RU[d.weekday()]}, {d.day} {MONTHS_RU[d.month - 1]}"


def parse_amount(text: str) -> int | None:
    """'1 700' / '1700₽' / '1500.0' -> int, иначе None."""
    clean = "".join(ch for ch in str(text) if ch.isdigit())
    if not clean:
        return None
    val = int(clean)
    if not (0 < val <= 1_000_000):
        return None
    return val



def _name_words(name: str) -> list[str]:
    """Слова имени без пунктуации/регистра, только буквы."""
    s = norm_name(name)
    s = s.replace(".", " ").replace(",", " ").replace("-", " ")
    return [w for w in s.split() if len(w) >= 2]


def names_match(a: str, b: str) -> bool:
    """Совпадают ли имена людей, даже если порядок слов разный.

    «Рома Жлобич» == «Жлобич Рома», «Роман Жлобич» ≈ «Рома Жлобич».
    """
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    wa, wb = _name_words(a), _name_words(b)
    if not wa or not wb:
        return False
    if set(wa) == set(wb):
        return True
    # каждое слово одного имени должно найти пару в другом
    def _pair(words1, words2):
        for w1 in words1:
            ok = False
            for w2 in words2:
                if w1 == w2 or (len(w1) >= 3 and len(w2) >= 3
                                and (w1.startswith(w2) or w2.startswith(w1))):
                    ok = True
                    break
            if not ok:
                return False
        return True
    if len(wa) == len(wb) and _pair(wa, wb) and _pair(wb, wa):
        return True
    # одно имя — часть другого (например «Рома» и «Рома Жлобич»)
    short, long_ = (wa, wb) if len(wa) < len(wb) else (wb, wa)
    if len(short) == 1 and _pair(short, long_):
        return True
    return False

def norm_name(name: str) -> str:
    return " ".join(str(name).replace("\u00a0", " ").split()).strip()


def tutor_names_from_checks() -> list[str]:
    """Уникальные имена репетиторов из листа «Чеки» (в порядке первого появления)."""
    cfg = load_config()
    sh_id = cfg["sheets"]["recept"]
    title = cfg["sheets"]["recept_checks_sheet"]
    rows = get_values(sh_id, f"'{title}'!A2:C")
    seen: list[str] = []
    for row in rows:
        if len(row) < 3:
            continue
        name = norm_name(row[2])
        if name and name not in seen:
            seen.append(name)
    return seen
