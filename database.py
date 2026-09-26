"""
database.py
===========
База данных SQLite — «память» бота между запросами.

Хранит по каждому пользователю: тариф, срок его действия, раздельные счётчики
минут (быстрый режим и спикеры), флаг рисковости и уведомления.

Для безлимитных тарифов лимита минут нет — но копится себестоимость,
чтобы админ видел убыточные аккаунты.

Для Free-тарифа лимит минут есть (страховка от абузы).
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from plans import (
    DEFAULT_PLAN, get_plan, account_cost, net_revenue, RISK_THRESHOLD,
)

DB_PATH = "bot.db"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Создаёт таблицу, если её нет. Вызывается при старте."""
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id        INTEGER PRIMARY KEY,
            username       TEXT,
            plan           TEXT NOT NULL,
            minutes_used   REAL NOT NULL DEFAULT 0,   -- для Free-лимита (общие минуты периода)
            minutes_fast   REAL NOT NULL DEFAULT 0,   -- минут в быстром режиме (за период тарифа)
            minutes_spk    REAL NOT NULL DEFAULT 0,   -- минут со спикерами (за период тарифа)
            reset_at       TEXT NOT NULL,             -- сброс Free-лимита (ISO)
            plan_until     TEXT,                      -- до когда действует платный тариф (ISO)
            speaker_trials_used INTEGER NOT NULL DEFAULT 0,
            risky          INTEGER NOT NULL DEFAULT 0,  -- флаг: себестоимость превысила порог
            risk_notified  INTEGER NOT NULL DEFAULT 0,  -- уже уведомили админа об этом аккаунте
            created_at     TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def _month_later(from_dt: datetime) -> datetime:
    return from_dt + timedelta(days=30)


def get_or_create_user(user_id: int, username: str | None = None) -> dict:
    """Возвращает пользователя (создаёт при отсутствии). Делает ленивый сброс."""
    conn = _connect()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    now = _now()

    if row is None:
        reset_at = _month_later(now).isoformat()
        conn.execute(
            """
            INSERT INTO users (user_id, username, plan, minutes_used, minutes_fast,
                               minutes_spk, reset_at, plan_until, speaker_trials_used,
                               risky, risk_notified, created_at)
            VALUES (?, ?, ?, 0, 0, 0, ?, NULL, 0, 0, 0, ?)
            """,
            (user_id, username, DEFAULT_PLAN, reset_at, now.isoformat()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

    user = dict(row)

    if username and username != user.get("username"):
        conn.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        conn.commit()
        user["username"] = username

    # --- Истечение платного тарифа: вернуть на Free, обнулить счётчики периода ---
    plan_until = user.get("plan_until")
    if plan_until and now >= datetime.fromisoformat(plan_until):
        conn.execute(
            """
            UPDATE users
            SET plan = ?, plan_until = NULL, minutes_used = 0,
                minutes_fast = 0, minutes_spk = 0, risky = 0, risk_notified = 0
            WHERE user_id = ?
            """,
            (DEFAULT_PLAN, user_id),
        )
        conn.commit()
        user["plan"] = DEFAULT_PLAN
        user["plan_until"] = None
        user["minutes_used"] = 0
        user["minutes_fast"] = 0
        user["minutes_spk"] = 0
        user["risky"] = 0
        user["risk_notified"] = 0

    # --- Ленивый сброс Free-лимита минут ---
    reset_at = datetime.fromisoformat(user["reset_at"])
    if now >= reset_at and user["plan"] == DEFAULT_PLAN:
        next_reset = _month_later(now).isoformat()
        conn.execute(
            "UPDATE users SET minutes_used = 0, reset_at = ? WHERE user_id = ?",
            (next_reset, user_id),
        )
        conn.commit()
        user["minutes_used"] = 0
        user["reset_at"] = next_reset

    conn.close()
    return user


def get_remaining_minutes(user_id: int) -> float:
    """
    Остаток минут для Free (лимитный тариф).
    Для безлимитных тарифов возвращает -1 (признак безлимита).
    """
    user = get_or_create_user(user_id)
    plan = get_plan(user["plan"])
    if plan.minutes == 0:
        return -1.0  # безлимит
    return max(0.0, plan.minutes - user["minutes_used"])


def add_usage(user_id: int, minutes: float, mode: str) -> None:
    """
    Записывает использование после обработки.
    mode: "single" (быстрый) или "multi" (спикеры).
    Обновляет и общий счётчик (для Free-лимита), и раздельные (для себестоимости).
    """
    conn = _connect()
    if mode == "multi":
        conn.execute(
            "UPDATE users SET minutes_used = minutes_used + ?, minutes_spk = minutes_spk + ? WHERE user_id = ?",
            (minutes, minutes, user_id),
        )
    else:
        conn.execute(
            "UPDATE users SET minutes_used = minutes_used + ?, minutes_fast = minutes_fast + ? WHERE user_id = ?",
            (minutes, minutes, user_id),
        )
    conn.commit()
    conn.close()


def use_speaker_trial(user_id: int) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE users SET speaker_trials_used = speaker_trials_used + 1 WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def set_plan(user_id: int, plan_code: str, days: int | None = None) -> None:
    """
    Назначает тариф. days=None => берётся срок из самого тарифа.
    Обнуляет счётчики периода и флаги риска.
    """
    plan = get_plan(plan_code)
    if days is None:
        days = plan.days if plan.days > 0 else 30

    conn = _connect()
    now = _now()
    plan_until = (now + timedelta(days=days)).isoformat() if plan_code != DEFAULT_PLAN else None
    reset_at = _month_later(now).isoformat()
    conn.execute(
        """
        UPDATE users
        SET plan = ?, plan_until = ?, minutes_used = 0, minutes_fast = 0,
            minutes_spk = 0, risky = 0, risk_notified = 0, reset_at = ?
        WHERE user_id = ?
        """,
        (plan_code, plan_until, reset_at, user_id),
    )
    conn.commit()
    conn.close()


def check_and_flag_risk(user_id: int) -> bool:
    """
    Проверяет, не превысила ли себестоимость аккаунта порог от дохода.
    Если да и раньше не уведомляли — ставит risky, помечает risk_notified
    и возвращает True (значит нужно уведомить админа СЕЙЧАС).
    Иначе False.
    Только для платных тарифов (у Free дохода нет).
    """
    user = get_or_create_user(user_id)
    plan = get_plan(user["plan"])

    # Free и бесплатные — не мониторим (дохода нет).
    if plan.price_rub == 0:
        return False

    cost = account_cost(user["minutes_fast"], user["minutes_spk"])
    revenue = net_revenue(user["plan"])
    if revenue <= 0:
        return False

    ratio = cost / revenue
    if ratio >= RISK_THRESHOLD:
        # Помечаем рисковым. Уведомляем только если ещё не уведомляли.
        need_notify = (user["risk_notified"] == 0)
        conn = _connect()
        conn.execute(
            "UPDATE users SET risky = 1, risk_notified = 1 WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        conn.close()
        return need_notify
    return False


def get_risky_accounts() -> list[dict]:
    """Список помеченных рисковых аккаунтов с деталями для админа."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM users WHERE risky = 1 ORDER BY user_id"
    ).fetchall()
    conn.close()

    result = []
    for r in rows:
        u = dict(r)
        cost = account_cost(u["minutes_fast"], u["minutes_spk"])
        revenue = net_revenue(u["plan"])
        result.append({
            "user_id": u["user_id"],
            "username": u["username"],
            "plan": u["plan"],
            "minutes_fast": u["minutes_fast"],
            "minutes_spk": u["minutes_spk"],
            "cost": cost,
            "revenue": revenue,
            "margin": revenue - cost,
        })
    return result


def get_account_cost_info(user_id: int) -> dict:
    """Текущая себестоимость и доход аккаунта (для уведомлений)."""
    user = get_or_create_user(user_id)
    cost = account_cost(user["minutes_fast"], user["minutes_spk"])
    revenue = net_revenue(user["plan"])
    return {
        "minutes_fast": user["minutes_fast"],
        "minutes_spk": user["minutes_spk"],
        "cost": cost,
        "revenue": revenue,
        "margin": revenue - cost,
        "username": user["username"],
        "plan": user["plan"],
    }


def get_stats() -> dict:
    conn = _connect()
    total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]

    by_plan = {}
    for r in conn.execute("SELECT plan, COUNT(*) AS c FROM users GROUP BY plan").fetchall():
        by_plan[r["plan"]] = r["c"]

    now_iso = _now().isoformat()
    active_paid = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE plan_until IS NOT NULL AND plan_until > ?",
        (now_iso,),
    ).fetchone()["c"]

    day_ago = (_now() - timedelta(days=1)).isoformat()
    week_ago = (_now() - timedelta(days=7)).isoformat()
    new_day = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE created_at > ?", (day_ago,)
    ).fetchone()["c"]
    new_week = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE created_at > ?", (week_ago,)
    ).fetchone()["c"]

    total_fast = conn.execute("SELECT COALESCE(SUM(minutes_fast),0) AS s FROM users").fetchone()["s"]
    total_spk = conn.execute("SELECT COALESCE(SUM(minutes_spk),0) AS s FROM users").fetchone()["s"]
    risky_count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE risky = 1").fetchone()["c"]

    conn.close()
    return {
        "total_users": total_users,
        "by_plan": by_plan,
        "active_paid": active_paid,
        "new_day": new_day,
        "new_week": new_week,
        "total_fast": total_fast,
        "total_spk": total_spk,
        "risky_count": risky_count,
    }
