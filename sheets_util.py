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
        import base64 as _b64
        import json as _json

        env_json = (_os.environ.get("GOOGLE_KEY_JSON") or "").strip()
        env_b64 = (_os.environ.get("GOOGLE_KEY_B64") or "").strip()
        if env_json or env_b64:
            try:
                raw = env_json or _b64.b64decode(env_b64).decode("utf-8")
                info = _json.loads(raw)
                creds = service_account.Credentials.from_service_account_info(
                    info,
                    scopes=["https://www.googleapis.com/auth/spreadsheets"])
                _sheets_client = build("sheets", "v4", credentials=creds,
                                       cache_discovery=False,
                                       static_discovery=False)
                return _sheets_client
            except Exception as e:  # noqa: BLE001
                log(f"Ключ Google из переменной окружения не подошёл: {e}")

        key_path = str(cfg.get("service_account_key") or "")
        if key_path and not Path(key_path).is_absolute():
            for base in (DATA_DIR, BASE_DIR):
                cand = base / key_path
                if cand.exists():
                    key_path = str(cand)
                    break
        if not Path(key_path or "").exists():
            raise FileNotFoundError(
                f"Не найден ключ Google: {key_path or 'service_account.json'}. "
                "Загрузите service_account.json в /app/data "
                "или задайте переменную GOOGLE_KEY_JSON / GOOGLE_KEY_B64.")
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
    # короткие паузы: синхронный sleep блокирует весь бот (event loop)
    for attempt in range(3):
        try:
            return func(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "429" not in msg and "quota" not in msg.lower():
                raise
            last = e
            if attempt < 2:
                _t.sleep(0.4 * (attempt + 1))
    raise last


# Кэш прочитанных диапазонов: один экран бота делает 5-9 одинаковых
# чтений одних и тех же листов. TTL короткий, при записи кэш сбрасывается.
_VALUES_CACHE: dict = {}
_VALUES_TTL = 20.0


def invalidate_values_cache() -> None:
    _VALUES_CACHE.clear()


def get_values(spreadsheet_id: str, range_: str) -> list[list[str]]:
    import time as _t
    key = (spreadsheet_id, range_)
    hit = _VALUES_CACHE.get(key)
    if hit is not None and _t.time() - hit[0] < _VALUES_TTL:
        return hit[1]
    resp = _retry_429(
        lambda: (sheets()
                 .spreadsheets()
                 .values()
                 .get(spreadsheetId=spreadsheet_id, range=range_)
                 .execute()))
    out = resp.get("values", [])
    _VALUES_CACHE[key] = (_t.time(), out)
    return out


_BATCH_CACHE: dict = {}


def get_values_batch(spreadsheet_id: str,
                     ranges: list[str]) -> list[list[list[str]]]:
    """Несколько диапазонов одной таблицы за ОДИН запрос к Google."""
    import time as _t
    key = (spreadsheet_id, tuple(ranges))
    hit = _BATCH_CACHE.get(key)
    if hit is not None and _t.time() - hit[0] < _VALUES_TTL:
        return hit[1]
    resp = _retry_429(
        lambda: (sheets()
                 .spreadsheets()
                 .values()
                 .batchGet(spreadsheetId=spreadsheet_id,
                           ranges=list(ranges))
                 .execute()))
    out = [vr.get("values", []) for vr in resp.get("valueRanges", [])]
    _BATCH_CACHE[key] = (_t.time(), out)
    return out


def update_values(spreadsheet_id: str, range_: str, values: list[list], *,
                  value_input_option: str = "USER_ENTERED") -> dict:
    """Точная запись диапазона. НИКОГДА не используем append (см. памятку)."""
    body = {"values": values, "majorDimension": "ROWS"}
    invalidate_values_cache()
    _BATCH_CACHE.clear()
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
    invalidate_values_cache()
    _BATCH_CACHE.clear()
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
    """Сумма из текста: '1 700' / '1700₽' / '1500.0' / '1 700,50' -> int.

    Раньше вырезались все нецифры: «1500.0» → 15000 (в 10 раз больше!).
    Теперь дробная часть отбрасывается корректно.
    """
    s = str(text or "")
    for ch in ("\u00a0", " ", "\t", "₽", "р", "Р", "$", "€"):
        s = s.replace(ch, "")
    if not s:
        return None
    # «1.700,50»: точка — разделитель тысяч, запятая — десятичная
    if "," in s and "." in s:
        last_dot, last_comma = s.rfind("."), s.rfind(",")
        if last_comma > last_dot:
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")          # «1,700.50»
    elif "," in s:
        s = s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")              # «1.700.500» — разряды
    elif "." in s:
        head, _, tail = s.partition(".")
        # «1.700» — это 1700, а «1500.0» — 1500
        if len(tail) == 3 and tail.isdigit() and head.isdigit():
            s = head + tail
    # оставить только цифры, точку и минус
    s = "".join(ch for ch in s if ch.isdigit() or ch in ".-")
    if not s or s in (".", "-", "-."):
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    val = int(round(val))           # дробную часть отбрасываем
    if not (0 < val <= 1_000_000):
        return None
    return val



def _name_words(name: str) -> list[str]:
    """Слова имени без пунктуации/регистра, только буквы."""
    s = norm_name(name)
    s = s.replace(".", " ").replace(",", " ").replace("-", " ")
    return [w for w in s.split() if len(w) >= 2]


def _fold(name: str) -> str:
    """Имя для сравнения: нижний регистр, ё → е."""
    return norm_name(name).lower().replace("\u0451", "\u0435")


def names_match(a: str, b: str) -> bool:
    """Совпадают ли имена людей, даже если порядок слов разный.

    «Рома Жлобич» == «Жлобич Рома», «Пётр» == «Петр».
    """
    na, nb = _fold(a), _fold(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    wa, wb = _name_words(_fold(a)), _name_words(_fold(b))
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
