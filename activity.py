"""Журнал действий: кто, что и когда сделал в боте.

Хранится в DATA_DIR/activity.jsonl — по одному действию на строку.
Показывается владельцу в разделе «Посмотреть логи» с постраничным
просмотром, чтобы можно было посмотреть хотя бы последний день.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import sheets_util

LOG_PATH: Path = sheets_util.DATA_DIR / "activity.jsonl"

# сколько записей хранить (примерно 3-5 дней активной работы)
MAX_ENTRIES = 4000


def _now() -> dt.datetime:
    return dt.datetime.now()


def record(user: str, action: str, details: str = "") -> None:
    """Записать действие. Ошибки записи не должны ломать бота."""
    entry = {
        "ts": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "user": (user or "").strip()[:60],
        "action": (action or "").strip()[:80],
        "details": (details or "").strip()[:300],
    }
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _rotate()
    except Exception:  # noqa: BLE001
        pass


def _rotate() -> None:
    """Не дать журналу расти бесконечно."""
    try:
        if not LOG_PATH.exists():
            return
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_ENTRIES:
            keep = lines[-MAX_ENTRIES:]
            LOG_PATH.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def entries(day_offset: int = 0) -> list[dict]:
    """Записи за день: 0 — сегодня, 1 — вчера и т.д. (новые сверху)."""
    target = (_now() - dt.timedelta(days=max(0, int(day_offset)))).date()
    out: list[dict] = []
    try:
        if not LOG_PATH.exists():
            return []
        for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ts = str(e.get("ts", ""))
            try:
                d = dt.datetime.strptime(ts[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if d == target:
                out.append(e)
    except Exception:  # noqa: BLE001
        return []
    out.reverse()          # свежие сверху
    return out


def day_label(day_offset: int) -> str:
    """«сегодня 13.11» / «вчера 12.11» / «11.11»."""
    d = (_now() - dt.timedelta(days=max(0, int(day_offset)))).date()
    today = _now().date()
    if d == today:
        return f"сегодня {d:%d.%m}"
    if d == today - dt.timedelta(days=1):
        return f"вчера {d:%d.%m}"
    return f"{d:%d.%m}"


def has_day(day_offset: int) -> bool:
    return bool(entries(day_offset))


def record_system(action: str, details: str = "") -> None:
    """Записать автоматическое действие бота — от имени DSH."""
    record("DSH", action, details)
