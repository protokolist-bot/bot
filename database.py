"""
database.py
===========
База данных SQLite — «память» бота между запросами.

Таблицы:
- users          — пользователь: тариф, лимит минут, срок пакета, счётчики,
                   доход за период (для контроля себестоимости), email.
- subscriptions  — заказы/подписки Lava (по одной строке на оформление).
- payments       — журнал успешных списаний. Уникальный ключ (заказ + время
                   списания) не даёт начислить один и тот же платёж дважды.
- usage_log      — журнал обработок (минуты и режим) для статистики админа.

Миграция: при старте init_db() добавляет недостающие колонки и таблицы
в существующий bot.db — старые пользователи и данные сохраняются.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from plans import (
    DEFAULT_PLAN, get_plan, account_cost, net_revenue, RISK_THRESHOLD,
    NET_REVENUE_SHARE, RENEWAL_GRACE_DAYS, RENEWAL_CARRY_OVER,
    COST_FAST_PER_MIN, COST_SPEAKERS_PER_MIN,
)

DB_PATH = "bot.db"

# Статусы подписки в нашей базе.
SUB_CREATED = "created"          # ссылка выдана, ждём первую оплату
SUB_ACTIVE = "activated"         # подписка действует, списания идут
SUB_SUSPENDED = "suspended"      # Lava не смогла списать, подписка приостановлена
SUB_DEACTIVATED = "deactivated"  # отключена на стороне Lava
SUB_CANCELLED = "cancelled"      # отменена пользователем или админом через бота
SUB_REPLACED = "replaced"        # заменена другой подпиской (смена тарифа)
SUB_EXPIRED = "expired"          # так и не оплачена

LIVE_STATUSES = (SUB_ACTIVE, SUB_SUSPENDED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return _now().isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db() -> None:
    """Создаёт таблицы и добавляет недостающие колонки. Вызывается при старте."""
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id        INTEGER PRIMARY KEY,
            username       TEXT,
            plan           TEXT NOT NULL,
            minutes_used   REAL NOT NULL DEFAULT 0,   -- использовано из текущего лимита
            minutes_fast   REAL NOT NULL DEFAULT 0,   -- минут в быстром режиме (за период)
            minutes_spk    REAL NOT NULL DEFAULT 0,   -- минут со спикерами (за период)
            reset_at       TEXT NOT NULL,             -- сброс Free-лимита (ISO)
            plan_until     TEXT,                      -- до когда действует платный пакет (ISO)
            speaker_trials_used INTEGER NOT NULL DEFAULT 0,
            risky          INTEGER NOT NULL DEFAULT 0,
            risk_notified  INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL
        )
        """
    )
    # --- Миграция users: новые колонки для оплат ---
    existing = _columns(conn, "users")
    new_cols = {
        "minutes_limit": "REAL NOT NULL DEFAULT 0",   # доступно минут в платном пакете (с переносом остатка)
        "revenue_rub": "REAL NOT NULL DEFAULT 0",     # чистый доход за текущий период (после комиссии)
        "email": "TEXT",
        "lava_consumer": "INTEGER NOT NULL DEFAULT 0",  # создан ли подписчик в Lava
    }
    for name, ddl in new_cols.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")

    # Платные пользователи, выданные до миграции: лимит = пакет тарифа.
    for r in conn.execute(
        "SELECT user_id, plan FROM users WHERE plan_until IS NOT NULL AND minutes_limit = 0"
    ).fetchall():
        conn.execute("UPDATE users SET minutes_limit = ? WHERE user_id = ?",
                     (get_plan(r["plan"]).minutes, r["user_id"]))

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            order_id         TEXT PRIMARY KEY,   -- наш ID заказа (передаём в Lava)
            user_id          INTEGER NOT NULL,
            plan             TEXT NOT NULL,
            product_id       TEXT,
            lava_sub_id      TEXT,               -- subscriptionId в Lava
            status           TEXT NOT NULL,
            pay_url          TEXT,
            amount           REAL,
            last_pay_time    TEXT,               -- последнее списание (UTC ISO)
            next_pay_time    TEXT,               -- следующее списание (UTC ISO)
            notified_status  TEXT,               -- о каком статусе уже уведомили пользователя
            cancel_reason    TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            last_checked_at  TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_status ON subscriptions(status)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id    TEXT NOT NULL,
            user_id     INTEGER NOT NULL,
            plan        TEXT NOT NULL,
            amount      REAL NOT NULL,          -- сумма списания, ₽
            kind        TEXT NOT NULL,          -- first / renewal
            pay_key     TEXT NOT NULL,          -- метка списания от Lava (защита от дублей)
            paid_at     TEXT NOT NULL,          -- время списания (UTC ISO)
            created_at  TEXT NOT NULL,
            UNIQUE(order_id, pay_key)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pay_user ON payments(user_id)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            plan        TEXT NOT NULL,
            mode        TEXT NOT NULL,          -- single / multi
            minutes     REAL NOT NULL,
            created_at  TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_time ON usage_log(created_at)")
    conn.commit()
    conn.close()


def _month_later(from_dt: datetime) -> datetime:
    return from_dt + timedelta(days=30)


# ======================================================================
#  Пользователи
# ======================================================================

def _has_live_subscription(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM subscriptions WHERE user_id = ? AND status = ? LIMIT 1",
        (user_id, SUB_ACTIVE),
    ).fetchone()
    return row is not None


def get_or_create_user(user_id: int, username: str | None = None) -> dict:
    """Возвращает пользователя (создаёт при отсутствии). Делает ленивые сбросы."""
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

    # --- Истечение платного пакета: вернуть на Free ---
    # Если подписка активна, ждём продления ещё RENEWAL_GRACE_DAYS дней:
    # списание у Lava может пройти с задержкой, и часы не должны пропасть.
    plan_until = user.get("plan_until")
    if plan_until and now >= datetime.fromisoformat(plan_until):
        in_grace = (
            now < datetime.fromisoformat(plan_until) + timedelta(days=RENEWAL_GRACE_DAYS)
            and _has_live_subscription(conn, user_id)
        )
        if not in_grace:
            conn.execute(
                """
                UPDATE users
                SET plan = ?, plan_until = NULL, minutes_used = 0, minutes_limit = 0,
                    minutes_fast = 0, minutes_spk = 0, revenue_rub = 0,
                    risky = 0, risk_notified = 0, reset_at = ?
                WHERE user_id = ?
                """,
                (DEFAULT_PLAN, _month_later(now).isoformat(), user_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            user = dict(row)

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


def get_user(user_id: int) -> dict | None:
    """Пользователь без создания (для админки)."""
    conn = _connect()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def find_user(query: str) -> dict | None:
    """Поиск по Telegram ID или @username (для админки)."""
    q = query.strip().lstrip("@")
    if not q:
        return None
    conn = _connect()
    if q.isdigit():
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (int(q),)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (q,)
        ).fetchone()
    conn.close()
    if row is None:
        return None
    return get_or_create_user(row["user_id"])


def minutes_limit(user: dict) -> float:
    """Сколько всего минут доступно пользователю в текущем периоде."""
    plan = get_plan(user["plan"])
    if plan.price_rub > 0 and user.get("minutes_limit"):
        return float(user["minutes_limit"])
    return float(plan.minutes)


def get_remaining_minutes(user_id: int) -> float:
    """Остаток минут. -1 — безлимит (сейчас таких тарифов нет)."""
    user = get_or_create_user(user_id)
    plan = get_plan(user["plan"])
    if plan.minutes == 0:
        return -1.0
    return max(0.0, minutes_limit(user) - user["minutes_used"])


def add_usage(user_id: int, minutes: float, mode: str) -> None:
    """
    Записывает использование после обработки.
    mode: "single" (быстрый) или "multi" (спикеры).
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
    plan_row = conn.execute("SELECT plan FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.execute(
        "INSERT INTO usage_log (user_id, plan, mode, minutes, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, plan_row["plan"] if plan_row else DEFAULT_PLAN, mode, minutes, now_iso()),
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
    Ручная выдача тарифа админом (/grant). Без оплаты: доход не начисляется.
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
        SET plan = ?, plan_until = ?, minutes_used = 0, minutes_limit = ?,
            minutes_fast = 0, minutes_spk = 0, revenue_rub = 0,
            risky = 0, risk_notified = 0, reset_at = ?
        WHERE user_id = ?
        """,
        (plan_code, plan_until, plan.minutes if plan_until else 0, reset_at, user_id),
    )
    conn.commit()
    conn.close()


def set_email(user_id: int, email: str) -> None:
    conn = _connect()
    conn.execute("UPDATE users SET email = ? WHERE user_id = ?", (email, user_id))
    conn.commit()
    conn.close()


def set_lava_consumer(user_id: int) -> None:
    conn = _connect()
    conn.execute("UPDATE users SET lava_consumer = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def credit_package(user_id: int, plan_code: str, amount: float, plan_until: str,
                   renewal: bool) -> dict:
    """
    Начисляет оплаченный пакет.
    - Активный платный пакет: остаток + новый пакет (при продлении — если
      RENEWAL_CARRY_OVER), срок = plan_until.
    - Был Free или пакет истёк: бесплатные минуты сгорают, ставится пакет.
    Возвращает {"limit": минут всего, "carried": перенесено минут}.
    """
    user = get_or_create_user(user_id)
    plan = get_plan(plan_code)
    current = get_plan(user["plan"])
    paid_active = current.price_rub > 0 and bool(user.get("plan_until"))

    carried = 0.0
    if paid_active and (RENEWAL_CARRY_OVER or not renewal):
        carried = max(0.0, minutes_limit(user) - user["minutes_used"])

    net = amount * NET_REVENUE_SHARE
    conn = _connect()
    if paid_active:
        # Счётчики себестоимости и доход за период копятся вместе.
        conn.execute(
            """
            UPDATE users
            SET plan = ?, plan_until = ?, minutes_used = 0, minutes_limit = ?,
                revenue_rub = revenue_rub + ?, risky = 0, risk_notified = 0
            WHERE user_id = ?
            """,
            (plan_code, plan_until, carried + plan.minutes, net, user_id),
        )
    else:
        conn.execute(
            """
            UPDATE users
            SET plan = ?, plan_until = ?, minutes_used = 0, minutes_limit = ?,
                minutes_fast = 0, minutes_spk = 0, revenue_rub = ?,
                risky = 0, risk_notified = 0
            WHERE user_id = ?
            """,
            (plan_code, plan_until, float(plan.minutes), net, user_id),
        )
    conn.commit()
    conn.close()
    return {"limit": carried + plan.minutes, "carried": carried}


def revoke_package(user_id: int) -> None:
    """Сразу вернуть на Free и обнулить пакет (админ — при возврате денег)."""
    conn = _connect()
    conn.execute(
        """
        UPDATE users
        SET plan = ?, plan_until = NULL, minutes_used = 0, minutes_limit = 0,
            minutes_fast = 0, minutes_spk = 0, revenue_rub = 0,
            risky = 0, risk_notified = 0, reset_at = ?
        WHERE user_id = ?
        """,
        (DEFAULT_PLAN, _month_later(_now()).isoformat(), user_id),
    )
    conn.commit()
    conn.close()


# ======================================================================
#  Контроль себестоимости
# ======================================================================

def _revenue_of(user: dict) -> float:
    """Чистый доход за период: фактические оплаты, иначе цена тарифа (для /grant)."""
    if user.get("revenue_rub"):
        return float(user["revenue_rub"])
    return net_revenue(user["plan"])


def check_and_flag_risk(user_id: int) -> bool:
    """
    Не превысила ли себестоимость аккаунта порог от дохода.
    True — нужно уведомить админа СЕЙЧАС (аккаунт впервые пересёк порог).
    """
    user = get_or_create_user(user_id)
    plan = get_plan(user["plan"])
    if plan.price_rub == 0:
        return False

    cost = account_cost(user["minutes_fast"], user["minutes_spk"])
    revenue = _revenue_of(user)
    if revenue <= 0:
        return False

    if cost / revenue >= RISK_THRESHOLD:
        need_notify = (user["risk_notified"] == 0)
        conn = _connect()
        conn.execute("UPDATE users SET risky = 1, risk_notified = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return need_notify
    return False


def get_risky_accounts() -> list[dict]:
    conn = _connect()
    rows = conn.execute("SELECT * FROM users WHERE risky = 1 ORDER BY user_id").fetchall()
    conn.close()

    result = []
    for r in rows:
        u = dict(r)
        cost = account_cost(u["minutes_fast"], u["minutes_spk"])
        revenue = _revenue_of(u)
        result.append({
            "user_id": u["user_id"], "username": u["username"], "plan": u["plan"],
            "minutes_fast": u["minutes_fast"], "minutes_spk": u["minutes_spk"],
            "cost": cost, "revenue": revenue, "margin": revenue - cost,
        })
    return result


def get_account_cost_info(user_id: int) -> dict:
    user = get_or_create_user(user_id)
    cost = account_cost(user["minutes_fast"], user["minutes_spk"])
    revenue = _revenue_of(user)
    return {
        "minutes_fast": user["minutes_fast"], "minutes_spk": user["minutes_spk"],
        "cost": cost, "revenue": revenue, "margin": revenue - cost,
        "username": user["username"], "plan": user["plan"],
    }


# ======================================================================
#  Подписки
# ======================================================================

def create_subscription(order_id: str, user_id: int, plan: str, product_id: str,
                        lava_sub_id: str | None, pay_url: str, amount: float) -> None:
    now = now_iso()
    conn = _connect()
    conn.execute(
        """
        INSERT INTO subscriptions (order_id, user_id, plan, product_id, lava_sub_id, status,
                                   pay_url, amount, created_at, updated_at, last_checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (order_id, user_id, plan, product_id, lava_sub_id, SUB_CREATED,
         pay_url, amount, now, now, now),
    )
    conn.commit()
    conn.close()


def get_subscription(order_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM subscriptions WHERE order_id = ?", (order_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


_SUB_FIELDS = {"status", "lava_sub_id", "amount", "last_pay_time", "next_pay_time",
               "notified_status", "cancel_reason", "last_checked_at"}


def update_subscription(order_id: str, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in _SUB_FIELDS}
    if not fields:
        return
    fields["updated_at"] = now_iso()
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn = _connect()
    conn.execute(f"UPDATE subscriptions SET {sets} WHERE order_id = ?",
                 (*fields.values(), order_id))
    conn.commit()
    conn.close()


def touch_subscription(order_id: str) -> None:
    """Отметить время последней проверки (без смены updated_at)."""
    conn = _connect()
    conn.execute("UPDATE subscriptions SET last_checked_at = ? WHERE order_id = ?",
                 (now_iso(), order_id))
    conn.commit()
    conn.close()


def get_user_live_subscription(user_id: int) -> dict | None:
    """Действующая подписка пользователя (activated или suspended), самая свежая."""
    conn = _connect()
    row = conn.execute(
        f"""
        SELECT * FROM subscriptions
        WHERE user_id = ? AND status IN ({",".join("?" * len(LIVE_STATUSES))})
        ORDER BY created_at DESC LIMIT 1
        """,
        (user_id, *LIVE_STATUSES),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_subscriptions(user_id: int, limit: int = 10) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_other_live_subscriptions(user_id: int, except_order: str) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        f"""
        SELECT * FROM subscriptions
        WHERE user_id = ? AND order_id != ? AND status IN ({",".join("?" * len(LIVE_STATUSES))})
        """,
        (user_id, except_order, *LIVE_STATUSES),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def subscriptions_to_check() -> list[dict]:
    """
    Какие подписки фоновая задача должна сверить с Lava сейчас:
    - новые заказы (ждём первую оплату): каждые 30 сек в течение часа;
    - наступила дата списания: каждые 30 мин в течение 7 дней;
    - приостановленные: раз в час;
    - все действующие: раз в 12 часов (на случай отмены на стороне Lava).
    """
    now = _now()
    iso = lambda dt: dt.isoformat()
    conn = _connect()
    rows = conn.execute(
        """
        SELECT * FROM subscriptions WHERE
          (status = :created AND created_at > :hour_ago AND last_checked_at < :sec30_ago)
          OR (status = :active AND next_pay_time IS NOT NULL AND next_pay_time <= :now
              AND next_pay_time > :week_ago AND last_checked_at < :min30_ago)
          OR (status = :suspended AND last_checked_at < :hour_ago)
          OR (status = :active AND last_checked_at < :h12_ago)
        ORDER BY last_checked_at
        LIMIT 50
        """,
        {
            "created": SUB_CREATED, "active": SUB_ACTIVE, "suspended": SUB_SUSPENDED,
            "now": iso(now),
            "sec30_ago": iso(now - timedelta(seconds=30)),
            "min30_ago": iso(now - timedelta(minutes=30)),
            "hour_ago": iso(now - timedelta(hours=1)),
            "h12_ago": iso(now - timedelta(hours=12)),
            "week_ago": iso(now - timedelta(days=7)),
        },
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def expire_stale_orders() -> int:
    """Неоплаченные заказы старше суток помечаем как просроченные."""
    day_ago = (_now() - timedelta(days=1)).isoformat()
    conn = _connect()
    cur = conn.execute(
        "UPDATE subscriptions SET status = ?, updated_at = ? WHERE status = ? AND created_at < ?",
        (SUB_EXPIRED, now_iso(), SUB_CREATED, day_ago),
    )
    conn.commit()
    conn.close()
    return cur.rowcount


def list_live_subscriptions(limit: int = 50) -> list[dict]:
    """Действующие подписки + данные пользователя (для /subs)."""
    conn = _connect()
    rows = conn.execute(
        f"""
        SELECT s.*, u.username, u.minutes_used, u.minutes_limit, u.plan_until
        FROM subscriptions s JOIN users u ON u.user_id = s.user_id
        WHERE s.status IN ({",".join("?" * len(LIVE_STATUSES))})
        ORDER BY s.next_pay_time
        LIMIT ?
        """,
        (*LIVE_STATUSES, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ======================================================================
#  Платежи
# ======================================================================

def record_payment(order_id: str, user_id: int, plan: str, amount: float,
                   kind: str, pay_key: str, paid_at: str) -> bool:
    """Записать списание. False — такое списание уже было записано (дубль)."""
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO payments (order_id, user_id, plan, amount, kind, pay_key, paid_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (order_id, user_id, plan, amount, kind, pay_key, paid_at, now_iso()),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def delete_payment(order_id: str, pay_key: str) -> None:
    """Откат записи о платеже (если начисление не удалось)."""
    conn = _connect()
    conn.execute("DELETE FROM payments WHERE order_id = ? AND pay_key = ?", (order_id, pay_key))
    conn.commit()
    conn.close()


def get_last_payment(order_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM payments WHERE order_id = ? ORDER BY paid_at DESC LIMIT 1", (order_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_payments(limit: int = 20, user_id: int | None = None) -> list[dict]:
    conn = _connect()
    if user_id is None:
        rows = conn.execute(
            """
            SELECT p.*, u.username FROM payments p LEFT JOIN users u ON u.user_id = p.user_id
            ORDER BY p.paid_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT p.*, u.username FROM payments p LEFT JOIN users u ON u.user_id = p.user_id
            WHERE p.user_id = ? ORDER BY p.paid_at DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def user_payment_totals(user_id: int) -> dict:
    conn = _connect()
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS s FROM payments WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return {"count": row["n"], "sum": row["s"]}


# ======================================================================
#  Статистика для админа
# ======================================================================

def get_stats() -> dict:
    conn = _connect()
    now = _now()
    now_s = now.isoformat()
    day_ago = (now - timedelta(days=1)).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()
    month_ago = (now - timedelta(days=30)).isoformat()
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]

    s: dict = {}
    # --- Пользователи ---
    s["total_users"] = one("SELECT COUNT(*) FROM users")
    s["new_day"] = one("SELECT COUNT(*) FROM users WHERE created_at > ?", day_ago)
    s["new_week"] = one("SELECT COUNT(*) FROM users WHERE created_at > ?", week_ago)
    s["new_month"] = one("SELECT COUNT(*) FROM users WHERE created_at > ?", month_ago)
    s["active_users_30d"] = one(
        "SELECT COUNT(DISTINCT user_id) FROM usage_log WHERE created_at > ?", month_ago)

    # --- Платные пакеты (включая выданные вручную) ---
    s["active_paid"] = one(
        "SELECT COUNT(*) FROM users WHERE plan_until IS NOT NULL AND plan_until > ?", now_s)
    s["paid_by_plan"] = {
        r[0]: r[1] for r in conn.execute(
            "SELECT plan, COUNT(*) FROM users WHERE plan_until IS NOT NULL AND plan_until > ? "
            "GROUP BY plan", (now_s,)).fetchall()
    }

    # --- Подписки ---
    s["subs_active_by_plan"] = {
        r[0]: r[1] for r in conn.execute(
            "SELECT plan, COUNT(*) FROM subscriptions WHERE status = ? GROUP BY plan",
            (SUB_ACTIVE,)).fetchall()
    }
    s["subs_active"] = sum(s["subs_active_by_plan"].values())
    s["mrr"] = one("SELECT COALESCE(SUM(amount), 0) FROM subscriptions WHERE status = ?", SUB_ACTIVE)
    s["subs_suspended"] = one("SELECT COUNT(*) FROM subscriptions WHERE status = ?", SUB_SUSPENDED)
    s["cancelled_30d"] = one(
        "SELECT COUNT(*) FROM subscriptions WHERE status IN (?, ?) AND updated_at > ?",
        SUB_CANCELLED, SUB_DEACTIVATED, month_ago)
    s["orders_30d"] = one("SELECT COUNT(*) FROM subscriptions WHERE created_at > ?", month_ago)
    s["unpaid_orders_30d"] = one(
        "SELECT COUNT(*) FROM subscriptions WHERE created_at > ? AND status IN (?, ?)",
        month_ago, SUB_CREATED, SUB_EXPIRED)

    # --- Покупатели и выручка ---
    s["buyers_total"] = one("SELECT COUNT(DISTINCT user_id) FROM payments")
    s["buyers_30d"] = one(
        "SELECT COUNT(DISTINCT user_id) FROM payments WHERE kind = 'first' AND paid_at > ?", month_ago)
    s["first_30d"] = one("SELECT COUNT(*) FROM payments WHERE kind = 'first' AND paid_at > ?", month_ago)
    s["renewals_30d"] = one(
        "SELECT COUNT(*) FROM payments WHERE kind = 'renewal' AND paid_at > ?", month_ago)
    s["revenue_30d"] = one("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE paid_at > ?", month_ago)
    s["revenue_total"] = one("SELECT COALESCE(SUM(amount), 0) FROM payments")
    s["net_30d"] = s["revenue_30d"] * NET_REVENUE_SHARE
    s["net_total"] = s["revenue_total"] * NET_REVENUE_SHARE

    # --- Использование за 30 дней ---
    s["min_fast_30d"] = one(
        "SELECT COALESCE(SUM(minutes), 0) FROM usage_log WHERE mode != 'multi' AND created_at > ?",
        month_ago)
    s["min_spk_30d"] = one(
        "SELECT COALESCE(SUM(minutes), 0) FROM usage_log WHERE mode = 'multi' AND created_at > ?",
        month_ago)
    s["min_free_30d"] = one(
        "SELECT COALESCE(SUM(minutes), 0) FROM usage_log WHERE plan = ? AND created_at > ?",
        DEFAULT_PLAN, month_ago)
    s["cost_30d"] = s["min_fast_30d"] * COST_FAST_PER_MIN + s["min_spk_30d"] * COST_SPEAKERS_PER_MIN
    s["jobs_30d"] = one("SELECT COUNT(*) FROM usage_log WHERE created_at > ?", month_ago)

    # Сколько платные пользователи израсходовали из своих пакетов.
    row = conn.execute(
        "SELECT COALESCE(SUM(minutes_used), 0), COALESCE(SUM(minutes_limit), 0) "
        "FROM users WHERE plan_until IS NOT NULL AND plan_until > ?", (now_s,)
    ).fetchone()
    s["paid_used_min"], s["paid_limit_min"] = row[0], row[1]
    s["risky_count"] = one("SELECT COUNT(*) FROM users WHERE risky = 1")

    # Совместимость со старой админкой.
    s["by_plan"] = {
        r[0]: r[1] for r in conn.execute("SELECT plan, COUNT(*) FROM users GROUP BY plan").fetchall()
    }
    conn.close()
    return s
