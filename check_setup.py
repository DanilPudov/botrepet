"""
Самопроверка настройки бота: python check_setup.py

Проверяет по шагам, что настроено, и печатает, что именно осталось сделать.
Ничего не меняет, только читает.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

BASE_DIR = Path(__file__).resolve().parent
if (BASE_DIR / "pkgs").exists():
    sys.path.insert(0, str(BASE_DIR / "pkgs"))

OK, BAD, WARN = "✅", "❌", "⚠️ "
problems: list[str] = []


def say(icon: str, text: str) -> None:
    print(f"{icon} {text}")


def main() -> int:
    print("=" * 62)
    print(" Самопроверка бота репетиторов")
    print("=" * 62)

    # 1. пакеты
    try:
        import telegram  # noqa: F401
        import googleapiclient  # noqa: F401
        say(OK, "Пакеты python-telegram-bot и google-api-python-client на месте")
    except ImportError as e:
        say(BAD, f"Не хватает пакета: {e}")
        problems.append("переустановить пакеты в pkgs/")

    # 2. config.json
    cfg_path = BASE_DIR / "config.json"
    if not cfg_path.exists():
        say(BAD, "Нет config.json")
        return 1
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        say(OK, "config.json читается")
    except json.JSONDecodeError as e:
        say(BAD, f"config.json повреждён: {e}")
        return 1

    # 3. токен
    token = (cfg.get("telegram_token") or "").strip()
    if not token:
        say(WARN, "Токен бота не задан — запускать бота рано")
        problems.append("получить токен у @BotFather и выполнить: "
                        "python bot.py --set-token=...")
    elif not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", token):
        say(WARN, "Токен задан, но не похож на настоящий (вида 123456:AAA...)")
        problems.append("перепроверить токен у @BotFather (/token)")
    else:
        say(OK, f"Токен задан (…{token[-4:]})")

    # 4. владелец
    owners = cfg.get("owner_ids") or []
    if owners:
        say(OK, f"owner_ids заполнен: {owners}")
    else:
        say(WARN, "owner_ids пуст — не будет меню «Управление»")
        problems.append("узнать свой Telegram id (бот покажет его на /start "
                        "без доступа) и вписать в config.json -> owner_ids")

    # 5. ключ сервисного аккаунта
    key_path = Path(cfg.get("service_account_key", ""))
    if not key_path.exists():
        say(BAD, f"Нет файла ключа: {key_path}")
        problems.append("проверить service_account_key в config.json")
    else:
        try:
            key = json.loads(key_path.read_text(encoding="utf-8"))
            email = key.get("client_email", "?")
            say(OK, f"Ключ сервисного аккаунта: {email}")
        except json.JSONDecodeError:
            say(BAD, "Файл ключа повреждён (не JSON)")
            problems.append("перекачать JSON-ключ сервисного аккаунта")

    # 6. доступ к таблицам
    sheets_cfg = cfg.get("sheets", {})
    try:
        from sheets_util import get_values
        tests = [
            ("Чеки Репет", sheets_cfg.get("recept"),
             f"'{sheets_cfg.get('recept_checks_sheet', 'Чеки')}'!A1:J2"),
            ("История", sheets_cfg.get("history"),
             f"'{sheets_cfg.get('history_sheet', 'Лист1')}'!A1:J2"),
        ]
        for name, sid, rng in tests:
            if not sid:
                say(BAD, f"{name}: id таблицы не задан в config.json")
                problems.append(f"вписать id таблицы «{name}» в config.json")
                continue
            try:
                rows = get_values(sid, rng)
                say(OK, f"{name}: доступ есть ({len(rows)} строк прочитано)")
            except Exception as e:  # noqa: BLE001
                say(BAD, f"{name}: нет доступа — {type(e).__name__}: {e}")
                problems.append(f"расшарить «{name}» на сервисный аккаунт (Редактор)")
    except Exception as e:  # noqa: BLE001
        say(BAD, f"Google Sheets недоступен: {e}")
        problems.append("проверить интернет/ключ/права")

    # 7. state.json
    st_path = BASE_DIR / "state.json"
    try:
        state = json.loads(st_path.read_text(encoding="utf-8"))
        n = len(state.get("tutors", []))
        say(OK, f"state.json в порядке, подключено репетиторов: {n}")
    except (OSError, json.JSONDecodeError) as e:
        say(WARN, f"state.json отсутствует/повреждён ({e}) — бот создаст заново")

    # 8. имена репетиторов для меню
    try:
        from sheets_util import tutor_names_from_checks
        names = tutor_names_from_checks()
        if names:
            say(OK, f"В «Чеках» найдено репетиторов: {len(names)}")
        else:
            say(WARN, "В «Чеках» нет имён репетиторов — меню выбора будет пустым")
    except Exception:  # noqa: BLE001
        pass

    # 9. куда пишутся занятия
    try:
        import checks_writer
        where = checks_writer.target_name()
        if (cfg.get("write") or {}).get("target", "recept") == "history":
            say(OK, f"Запись занятий: {where} (ТЕСТОВЫЙ режим, «Чеки» не трогаются)")
        else:
            say(OK, f"Запись занятий: {where} (боевой режим)")
    except Exception as e:  # noqa: BLE001
        say(WARN, f"Не удалось определить место записи: {e}")

    print("-" * 62)
    if problems:
        print("Что осталось сделать:")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
        return 1
    print("Всё настроено! Запускайте: start_bot.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
