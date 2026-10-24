"""
payments.py
===========
Логика оплаты подписок через Lava.

ГЛАВНЫЙ ПРИНЦИП: часы начисляются ТОЛЬКО по ответу API Lava.
Ни нажатие «Я оплатил», ни возврат по ссылке ?start=paid сами по себе
ничего не начисляют — они лишь запускают проверку.

Как бот узнаёт об оплатах (без вебхука):
- после выдачи ссылки бот каждые 30 сек спрашивает Lava о статусе (1 час);
- когда наступает дата следующего списания — проверяет каждые 30 мин;
- раз в 12 часов сверяет все действующие подписки.

Защита от двойного начисления: каждое списание записывается в таблицу
payments с уникальным ключом (заказ + время списания от Lava).
"""

import asyncio
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
import lava
from lava import LavaError
from plans import PLANS, PAID_PLANS, get_plan

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    MSK = ZoneInfo("Europe/Moscow")
except Exception:  # нет базы часовых поясов — используем фиксированный +3
    MSK = timezone(timedelta(hours=3))

# Между двумя списаниями по подписке минимум столько дней. Защищает от
# ошибочного «второго начисления», если Lava вернёт то же списание в другом виде.
MIN_DAYS_BETWEEN_CHARGES = 20

WORKER_INTERVAL_SEC = 20

_product_cache: dict[str, str] = {}
_order_locks: dict[str, asyncio.Lock] = {}


class PurchaseError(Exception):
    """Ошибка оформления — текст показываем пользователю."""


# ======================================================================
#  Вспомогательное: время и форматирование
# ======================================================================

def parse_lava_time(value) -> datetime | None:
    """
    Время от Lava -> datetime в UTC.
    Время без часового пояса ("2026-10-24 13:20:00") считаем московским.
    """
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        logger.warning("Не удалось разобрать время Lava: %r", value)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt.astimezone(timezone.utc)


def fmt_date(iso: str | None) -> str:
    """ISO (UTC) -> «24.11.2026» по Москве."""
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m.%Y")
    except ValueError:
        return iso[:10]


def fmt_dt(iso: str | None) -> str:
    """ISO (UTC) -> «24.11.2026 13:20» по Москве."""
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso[:16]


def fmt_hours(minutes: float) -> str:
    hours = minutes / 60
    return f"{hours:.0f} ч" if abs(hours - round(hours)) < 0.05 else f"{hours:.1f} ч"


def _lock(order_id: str) -> asyncio.Lock:
    if order_id not in _order_locks:
        _order_locks[order_id] = asyncio.Lock()
    return _order_locks[order_id]


# ======================================================================
#  Продукты Lava
# ======================================================================

async def load_products(force: bool = False) -> dict[str, str]:
    """
    Сопоставляет тарифы бота с продуктами подписки Lava.
    Приоритет: ID из .env; иначе — активный продукт с той же ценой.
    """
    if _product_cache and not force:
        return _product_cache

    overrides = {"standard": config.LAVA_PRODUCT_STANDARD, "max": config.LAVA_PRODUCT_MAX}
    mapping = {code: pid for code, pid in overrides.items() if pid}

    missing = [code for code in PAID_PLANS if code not in mapping]
    if missing:
        products = await lava.get_products()
        for code in missing:
            price = PLANS[code].price_rub
            match = [
                p for p in products
                if abs(float(p.get("price") or 0) - price) < 0.01 and p.get("isActive", True)
            ]
            if len(match) == 1:
                mapping[code] = str(match[0]["id"])
            elif len(match) > 1:
                logger.error("Несколько продуктов Lava с ценой %s ₽ — укажи ID в .env", price)
            else:
                logger.error("В Lava нет активного продукта с ценой %s ₽ (тариф %s)", price, code)

    _product_cache.clear()
    _product_cache.update(mapping)
    return _product_cache


# ======================================================================
#  Оформление
# ======================================================================

def consumer_id_for(user_id: int) -> str:
    return f"tg{user_id}"


async def _ensure_consumer(consumer_id: str, email: str, user_id: int) -> None:
    """Создать подписчика в Lava. Ошибку только логируем: он мог уже существовать."""
    try:
        await lava.create_consumer(consumer_id, email)
        db.set_lava_consumer(user_id)
    except LavaError as e:
        logger.warning("create_consumer %s: %s", consumer_id, e)


async def start_purchase(user_id: int, plan_code: str) -> dict:
    """
    Оформляет подписку и возвращает {"order_id", "url", "amount"}.
    Бросает PurchaseError с понятным текстом для пользователя.
    """
    if not config.lava_configured():
        raise PurchaseError("Оплата временно недоступна. Попробуй позже или напиши в поддержку.")
    if plan_code not in PAID_PLANS:
        raise PurchaseError("Неизвестный тариф.")

    user = db.get_or_create_user(user_id)
    if config.LAVA_REQUIRE_EMAIL and not user.get("email"):
        raise PurchaseError("Нужен email для чека.")

    try:
        products = await load_products()
    except LavaError:
        logger.exception("Не удалось получить продукты Lava")
        raise PurchaseError("Платёжная система не отвечает. Попробуй через пару минут.")
    product_id = products.get(plan_code)
    if not product_id:
        raise PurchaseError("Этот тариф сейчас недоступен для оплаты. Напиши в поддержку.")

    consumer_id = consumer_id_for(user_id)
    email = user.get("email") or f"{consumer_id}@{config.LAVA_EMAIL_DOMAIN}"
    if not user.get("lava_consumer"):
        await _ensure_consumer(consumer_id, email, user_id)

    order_id = f"sub{user_id}x{int(time.time())}x{secrets.token_hex(3)}"
    try:
        data = await lava.subscribe(product_id, consumer_id, order_id)
    except LavaError as e:
        if "подписчик" in str(e).lower():
            # Lava не знает этого подписчика (например, раньше он не создался).
            # Создаём заново и пробуем ещё раз.
            logger.warning("subscribe %s: %s — создаём подписчика заново", order_id, e)
            await _ensure_consumer(consumer_id, email, user_id)
            try:
                data = await lava.subscribe(product_id, consumer_id, order_id)
            except LavaError as e2:
                logger.error("subscribe %s (повтор): %s", order_id, e2)
                raise PurchaseError("Не получилось создать ссылку на оплату. "
                                    "Попробуй ещё раз или напиши в поддержку.")
        else:
            logger.error("subscribe %s: %s", order_id, e)
            raise PurchaseError("Не получилось создать ссылку на оплату. "
                                "Попробуй ещё раз или напиши в поддержку.")

    db.set_lava_consumer(user_id)  # подписка оформилась — подписчик точно есть
    url = (data or {}).get("url")
    if not url:
        logger.error("Lava не вернула ссылку на оплату: %s", data)
        raise PurchaseError("Не получилось создать ссылку на оплату. Попробуй ещё раз.")

    amount = float((data or {}).get("amount") or PLANS[plan_code].price_rub)
    db.create_subscription(order_id, user_id, plan_code, product_id,
                           (data or {}).get("subscriptionId"), url, amount)
    logger.info("Оформление: user=%s plan=%s order=%s", user_id, plan_code, order_id)
    return {"order_id": order_id, "url": url, "amount": amount}


# ======================================================================
#  Сверка с Lava и начисление
# ======================================================================

async def sync_subscription(bot: Bot | None, order_id: str) -> dict:
    """
    Запрашивает статус подписки у Lava и, если появилось новое списание,
    начисляет пакет. Возвращает {"status", "credited", "kind"}.
    """
    async with _lock(order_id):
        sub = db.get_subscription(order_id)
        if sub is None:
            return {"status": None, "credited": False, "kind": None}

        try:
            data = await lava.subscription_status(order_id=order_id) or {}
        except LavaError:
            db.touch_subscription(order_id)
            raise
        db.touch_subscription(order_id)

        lava_status = str(data.get("status") or "").lower()
        last_raw = data.get("last_pay_time")
        last_dt = parse_lava_time(last_raw)
        next_dt = parse_lava_time(data.get("next_pay_time"))
        amount = float(data.get("amount") or sub["amount"] or get_plan(sub["plan"]).price_rub)

        updates = {
            "lava_sub_id": data.get("subscriptionId") or sub["lava_sub_id"],
            "amount": amount,
            "last_pay_time": last_dt.isoformat() if last_dt else sub["last_pay_time"],
            "next_pay_time": next_dt.isoformat() if next_dt else sub["next_pay_time"],
        }

        # --- Есть ли новое списание? ---
        credited, kind = False, None
        if lava_status == db.SUB_ACTIVE or (last_dt and lava_status in (db.SUB_SUSPENDED, db.SUB_DEACTIVATED)):
            prev = db.get_last_payment(order_id)
            pay_key = None
            if prev is None and (lava_status == db.SUB_ACTIVE or last_dt):
                kind = "first"
                pay_key = str(last_raw or data.get("activation_time") or "initial")
            elif prev is not None and last_dt:
                prev_dt = datetime.fromisoformat(prev["paid_at"])
                if last_dt > prev_dt + timedelta(days=MIN_DAYS_BETWEEN_CHARGES):
                    kind = "renewal"
                    pay_key = str(last_raw)

            if pay_key:
                paid_at = (last_dt or datetime.now(timezone.utc)).isoformat()
                if db.record_payment(order_id, sub["user_id"], sub["plan"], amount,
                                     kind, pay_key, paid_at):
                    plan_until = _plan_until(next_dt, last_dt)
                    try:
                        result = db.credit_package(sub["user_id"], sub["plan"], amount,
                                                   plan_until, renewal=(kind == "renewal"))
                    except Exception:
                        db.delete_payment(order_id, pay_key)
                        raise
                    credited = True
                    logger.info("Начислено: order=%s kind=%s user=%s", order_id, kind, sub["user_id"])
                    if kind == "first":
                        await _replace_other_subscriptions(sub["user_id"], order_id)
                    if bot:
                        await _notify_paid(bot, sub, kind, amount, result, plan_until, next_dt)

        # --- Статус в нашей базе ---
        # Если подписку отменили мы (cancelled/replaced), наш статус главнее.
        if sub["status"] not in (db.SUB_CANCELLED, db.SUB_REPLACED):
            if lava_status in (db.SUB_ACTIVE, db.SUB_SUSPENDED, db.SUB_DEACTIVATED):
                updates["status"] = lava_status
            elif lava_status == db.SUB_CREATED and sub["status"] == db.SUB_EXPIRED:
                updates["status"] = db.SUB_EXPIRED

        new_status = updates.get("status", sub["status"])
        if credited:
            updates["notified_status"] = new_status
        elif bot and new_status != sub["notified_status"] and new_status in (
                db.SUB_SUSPENDED, db.SUB_DEACTIVATED):
            await _notify_problem(bot, sub, new_status, amount, data.get("deactivated_reason"))
            updates["notified_status"] = new_status
        elif new_status == db.SUB_ACTIVE and sub["notified_status"] != db.SUB_ACTIVE:
            updates["notified_status"] = new_status

        db.update_subscription(order_id, **updates)
        return {"status": new_status, "credited": credited, "kind": kind}


def _plan_until(next_dt: datetime | None, last_dt: datetime | None) -> str:
    """Срок пакета = дата следующего списания от Lava (иначе +30 дней)."""
    now = datetime.now(timezone.utc)
    if next_dt and next_dt > now:
        return next_dt.isoformat()
    base = last_dt if last_dt and last_dt > now - timedelta(days=30) else now
    return (base + timedelta(days=30)).isoformat()


async def _replace_other_subscriptions(user_id: int, new_order: str) -> None:
    """Смена тарифа: отменяем старые подписки, чтобы не списывать дважды."""
    for old in db.get_other_live_subscriptions(user_id, new_order):
        try:
            await lava.unsubscribe(order_id=old["order_id"])
            db.update_subscription(old["order_id"], status=db.SUB_REPLACED,
                                   cancel_reason=f"заменена на {new_order}")
            logger.info("Старая подписка %s отменена (смена тарифа)", old["order_id"])
        except LavaError:
            logger.exception("Не удалось отменить старую подписку %s", old["order_id"])
            await notify_admin_text(
                None,
                f"⚠️ Не удалось автоматически отменить старую подписку "
                f"<code>{old['order_id']}</code> пользователя <code>{user_id}</code> "
                f"после смены тарифа. Отмени вручную: /cancel {user_id}",
            )


# ======================================================================
#  Отмена
# ======================================================================

async def cancel_subscription(order_id: str, reason: str) -> None:
    """Отменить автопродление (часы остаются до конца оплаченного периода)."""
    try:
        await lava.unsubscribe(order_id=order_id)
    except LavaError:
        # Возможно, подписка уже отключена на стороне Lava — проверим.
        data = await lava.subscription_status(order_id=order_id) or {}
        if str(data.get("status") or "").lower() != db.SUB_DEACTIVATED:
            raise
    db.update_subscription(order_id, status=db.SUB_CANCELLED, cancel_reason=reason,
                           notified_status=db.SUB_CANCELLED)
    logger.info("Подписка %s отменена: %s", order_id, reason)


# ======================================================================
#  Уведомления
# ======================================================================

_bot_ref: Bot | None = None


def _user_label(user_id: int) -> str:
    u = db.get_user(user_id)
    if u and u.get("username"):
        return f"@{u['username']} (<code>{user_id}</code>)"
    return f"<code>{user_id}</code>"


async def notify_admin_text(bot: Bot | None, text: str) -> None:
    bot = bot or _bot_ref
    if not bot or not config.ADMIN_ID or not config.ADMIN_PAYMENT_NOTIFY:
        return
    try:
        await bot.send_message(config.ADMIN_ID, text, parse_mode="HTML")
    except Exception:
        logger.exception("Не удалось отправить уведомление админу")


async def _notify_paid(bot: Bot, sub: dict, kind: str, amount: float, result: dict,
                       plan_until: str, next_dt: datetime | None) -> None:
    plan = get_plan(sub["plan"])
    carried = result["carried"]
    lines = [
        "✅ <b>Оплата прошла!</b>" if kind == "first" else "🔄 <b>Подписка продлена</b>",
        "",
        f"Тариф: <b>{plan.title}</b>",
        f"Доступно: <b>{fmt_hours(result['limit'])}</b>"
        + (f" (из них перенесено {fmt_hours(carried)})" if carried >= 1 else ""),
        f"Пакет действует до: <b>{fmt_date(plan_until)}</b>",
    ]
    if next_dt:
        lines.append(f"Следующее списание: {amount:.0f} ₽ — {fmt_date(next_dt.isoformat())}")
    lines += ["", "Отключить автопродление можно в «📊 Мой лимит»."]
    try:
        await bot.send_message(sub["user_id"], "\n".join(lines), parse_mode="HTML")
    except Exception:
        logger.exception("Не удалось уведомить пользователя %s об оплате", sub["user_id"])

    title = "💰 Новая подписка" if kind == "first" else "🔄 Продление"
    await notify_admin_text(
        bot,
        f"{title}\n{_user_label(sub['user_id'])}\n{plan.title} — <b>{amount:.0f} ₽</b>",
    )


async def _notify_problem(bot: Bot, sub: dict, status: str, amount: float, reason) -> None:
    plan = get_plan(sub["plan"])
    user = db.get_user(sub["user_id"]) or {}
    until = fmt_date(user.get("plan_until"))
    if status == db.SUB_SUSPENDED:
        text = (
            f"⚠️ <b>Не удалось продлить подписку</b>\n\n"
            f"Не прошло списание {amount:.0f} ₽ за «{plan.title}».\n"
            f"Проверь карту и баланс. Если оплата так и не пройдёт, "
            f"оформи подписку заново в «💎 Подписка»."
        )
        admin = f"⚠️ Не прошло списание\n{_user_label(sub['user_id'])}\n{plan.title} — {amount:.0f} ₽"
    else:
        text = (
            f"ℹ️ <b>Подписка «{plan.title}» отключена</b>\n\n"
            f"Автоматических списаний больше не будет. "
            f"Оставшиеся часы действуют до {until}."
        )
        admin = (f"ℹ️ Подписка отключена на стороне Lava\n{_user_label(sub['user_id'])}\n"
                 f"{plan.title}" + (f"\nПричина: {reason}" if reason else ""))
    try:
        await bot.send_message(sub["user_id"], text, parse_mode="HTML")
    except Exception:
        logger.exception("Не удалось уведомить пользователя %s", sub["user_id"])
    await notify_admin_text(bot, admin)


# ======================================================================
#  Фоновая задача
# ======================================================================

async def worker(bot: Bot) -> None:
    """Бесконечный цикл сверки подписок с Lava. Запускается из bot.py."""
    global _bot_ref
    _bot_ref = bot
    if not config.lava_configured():
        logger.warning("Ключи Lava не заданы — фоновая проверка оплат выключена.")
        return

    await asyncio.sleep(10)
    logger.info("Фоновая проверка оплат запущена.")
    last_expire = 0.0
    while True:
        try:
            if time.time() - last_expire > 3600:
                n = db.expire_stale_orders()
                if n:
                    logger.info("Просрочено неоплаченных заказов: %s", n)
                last_expire = time.time()

            for sub in db.subscriptions_to_check():
                try:
                    await sync_subscription(bot, sub["order_id"])
                except LavaError as e:
                    logger.warning("Сверка %s: %s", sub["order_id"], e)
                except Exception:
                    logger.exception("Сверка %s упала", sub["order_id"])
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка в фоновой проверке оплат")
        await asyncio.sleep(WORKER_INTERVAL_SEC)


def pay_keyboard(url: str, order_id: str, amount: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {amount:.0f} ₽", url=url)],
        [InlineKeyboardButton(text="🔄 Я оплатил — проверить", callback_data=f"chk:{order_id}")],
    ])
