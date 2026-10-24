"""
admin.py
========
Админ-панель: доступна ТОЛЬКО владельцу бота (config.ADMIN_ID).

Команды:
- /admin                       — список команд.
- /stats                       — сводная статистика: пользователи, подписки, выручка, использование.
- /subs                        — действующие подписки (кто, тариф, израсходовано, следующее списание).
- /payments [N]                — последние N платежей (по умолчанию 20).
- /user <id или @username>     — карточка пользователя с кнопками управления.
- /cancel <id или @username>   — отменить подписку пользователя (по обращению в поддержку).
- /grant <user_id> <plan> [дни] — выдать тариф вручную (без оплаты).
- /risky                       — рисковые аккаунты (себестоимость близка к доходу).
- /lavacheck                   — проверка ключей Lava и продуктов.

Плюс notify_admin_risk(bot, user_id) — автоуведомление о рисковом аккаунте.
"""

import html
import logging

from aiogram import Bot
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import database as db
import lava
import payments
from lava import LavaError
from payments import fmt_date, fmt_dt, fmt_hours
from plans import PLANS, PAID_PLANS, NET_REVENUE_SHARE, get_plan, account_cost

logger = logging.getLogger(__name__)

STATUS_RU = {
    db.SUB_CREATED: "⏳ ждёт оплаты",
    db.SUB_ACTIVE: "✅ активна",
    db.SUB_SUSPENDED: "⚠️ списание не прошло",
    db.SUB_DEACTIVATED: "⛔ отключена в Lava",
    db.SUB_CANCELLED: "🚫 отменена",
    db.SUB_REPLACED: "🔁 заменена другой",
    db.SUB_EXPIRED: "⌛ не оплачена",
}


def is_admin(user_id: int) -> bool:
    return config.ADMIN_ID is not None and user_id == config.ADMIN_ID


def _label(user: dict | None, user_id: int) -> str:
    if user and user.get("username"):
        return f"@{html.escape(user['username'])} (<code>{user_id}</code>)"
    return f"<code>{user_id}</code>"


def _args(message: Message) -> list[str]:
    return (message.text or "").split()[1:]


# ======================================================================
#  /admin
# ======================================================================

async def handle_admin_help(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🛠 <b>Админ-команды</b>\n\n"
        "/stats — статистика: пользователи, подписки, выручка, использование\n"
        "/subs — действующие подписки\n"
        "/payments — последние платежи (<code>/payments 50</code> — больше)\n"
        "/user <code>ID</code> или <code>@username</code> — карточка пользователя\n"
        "/cancel <code>ID</code> — отменить подписку (по обращению в поддержку)\n"
        "/grant <code>ID тариф [дни]</code> — выдать тариф без оплаты\n"
        "/risky — аккаунты, где себестоимость близка к доходу\n"
        "/lavacheck — проверка связи с Lava\n\n"
        "💡 ID пользователя есть в уведомлениях о покупках и в /subs.",
        parse_mode="HTML",
    )


# ======================================================================
#  /stats
# ======================================================================

async def handle_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return

    s = db.get_stats()
    subs_lines = "\n".join(
        f"  • {PLANS[c].title}: <b>{s['subs_active_by_plan'].get(c, 0)}</b>" for c in PAID_PLANS
    )
    usage_pct = (s["paid_used_min"] / s["paid_limit_min"] * 100) if s["paid_limit_min"] else 0
    margin_30d = s["net_30d"] - s["cost_30d"]
    conversion = (s["orders_30d"] - s["unpaid_orders_30d"]) / s["orders_30d"] * 100 if s["orders_30d"] else 0

    text = (
        "📊 <b>Статистика</b>\n\n"
        "👥 <b>Пользователи</b>\n"
        f"  Всего: <b>{s['total_users']}</b>\n"
        f"  Новых: {s['new_day']} за 24ч · {s['new_week']} за 7д · {s['new_month']} за 30д\n"
        f"  Пользовались ботом за 30д: <b>{s['active_users_30d']}</b>\n\n"
        "💳 <b>Подписки</b>\n"
        f"  Активных: <b>{s['subs_active']}</b>\n{subs_lines}\n"
        f"  Ежемесячный доход по активным: <b>{s['mrr']:.0f} ₽</b>\n"
        f"  Проблемных (не прошло списание): {s['subs_suspended']}\n"
        f"  Отменено за 30д: {s['cancelled_30d']}\n"
        f"  С платным пакетом сейчас (вкл. выданные вручную): {s['active_paid']}\n\n"
        "💰 <b>Покупки и выручка</b>\n"
        f"  Купили хотя бы раз: <b>{s['buyers_total']}</b> чел.\n"
        f"  За 30д: новых подписок {s['first_30d']} · продлений {s['renewals_30d']}\n"
        f"  Переходов к оплате за 30д: {s['orders_30d']} (оплачено {conversion:.0f}%)\n"
        f"  Выручка 30д: <b>{s['revenue_30d']:.0f} ₽</b> → на руки {s['net_30d']:.0f} ₽\n"
        f"  Выручка всего: {s['revenue_total']:.0f} ₽ → на руки {s['net_total']:.0f} ₽\n\n"
        "⏱ <b>Использование за 30 дней</b>\n"
        f"  Обработок: {s['jobs_30d']}\n"
        f"  Быстрый режим: {fmt_hours(s['min_fast_30d'])} · спикеры: {fmt_hours(s['min_spk_30d'])}\n"
        f"  Из них на Free: {fmt_hours(s['min_free_30d'])}\n"
        f"  Платные израсходовали {fmt_hours(s['paid_used_min'])} из {fmt_hours(s['paid_limit_min'])} "
        f"({usage_pct:.0f}%)\n"
        f"  Себестоимость: <b>{s['cost_30d']:.0f} ₽</b>\n"
        f"  Маржа 30д (на руки − себестоимость): <b>{margin_30d:.0f} ₽</b>\n\n"
        f"⚠️ Рисковых аккаунтов: <b>{s['risky_count']}</b> (/risky)"
    )
    await message.answer(text, parse_mode="HTML")


# ======================================================================
#  /subs
# ======================================================================

async def handle_subs(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    subs = db.list_live_subscriptions(limit=50)
    if not subs:
        await message.answer("Действующих подписок пока нет.")
        return

    lines = [f"💳 <b>Действующие подписки ({len(subs)})</b>\n"]
    for sub in subs:
        user = {"username": sub["username"]}
        limit = sub["minutes_limit"] or get_plan(sub["plan"]).minutes
        warn = " ⚠️" if sub["status"] == db.SUB_SUSPENDED else ""
        lines.append(
            f"{_label(user, sub['user_id'])}{warn}\n"
            f"  {get_plan(sub['plan']).title} · израсходовано "
            f"{fmt_hours(sub['minutes_used'] or 0)} из {fmt_hours(limit)}\n"
            f"  след. списание: {fmt_date(sub['next_pay_time'])}"
        )
    lines.append("\nПодробнее: /user ID")
    await _send_long(message, "\n".join(lines))


# ======================================================================
#  /payments
# ======================================================================

async def handle_payments(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    args = _args(message)
    limit = int(args[0]) if args and args[0].isdigit() else 20
    rows = db.list_payments(limit=min(limit, 200))
    if not rows:
        await message.answer("Платежей пока нет.")
        return
    lines = [f"💰 <b>Последние платежи ({len(rows)})</b>\n"]
    for p in rows:
        kind = "новая" if p["kind"] == "first" else "продление"
        lines.append(
            f"{fmt_dt(p['paid_at'])} · <b>{p['amount']:.0f} ₽</b> · {get_plan(p['plan']).title} "
            f"({kind})\n  {_label(p, p['user_id'])}"
        )
    await _send_long(message, "\n".join(lines))


# ======================================================================
#  /user
# ======================================================================

def _user_card(user: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    uid = user["user_id"]
    plan = get_plan(user["plan"])
    limit = db.minutes_limit(user)
    remaining = max(0.0, limit - user["minutes_used"])
    cost = account_cost(user["minutes_fast"], user["minutes_spk"])
    totals = db.user_payment_totals(uid)
    sub = db.get_user_live_subscription(uid)
    history = db.get_user_subscriptions(uid, limit=5)

    lines = [
        f"👤 <b>Пользователь</b> {_label(user, uid)}",
        f"Зарегистрирован: {fmt_date(user['created_at'])}",
        f"Email: {html.escape(user['email']) if user.get('email') else '—'}",
        "",
        f"Тариф: <b>{plan.title}</b>",
        f"Израсходовано: {fmt_hours(user['minutes_used'])} из {fmt_hours(limit)} "
        f"(осталось {fmt_hours(remaining)})",
    ]
    if user.get("plan_until"):
        lines.append(f"Пакет до: {fmt_date(user['plan_until'])}")
    if plan.price_rub > 0:
        lines.append(
            f"Себестоимость периода: {cost:.0f} ₽ из дохода {user.get('revenue_rub') or 0:.0f} ₽"
            + (" ⚠️ рисковый" if user.get("risky") else "")
        )
    lines.append(f"Оплат всего: {totals['count']} на {totals['sum']:.0f} ₽")

    if history:
        lines.append("\n<b>Подписки:</b>")
        for h in history:
            lines.append(
                f"• {get_plan(h['plan']).title} — {STATUS_RU.get(h['status'], h['status'])}"
                + (f", след. списание {fmt_date(h['next_pay_time'])}"
                   if h["status"] in db.LIVE_STATUSES else "")
                + f"\n  <code>{h['order_id']}</code>"
            )

    buttons = []
    if sub:
        buttons.append([InlineKeyboardButton(
            text="❌ Отменить автопродление", callback_data=f"adm:cancel:{sub['order_id']}")])
    if plan.price_rub > 0:
        buttons.append([InlineKeyboardButton(
            text="⛔ Отменить и обнулить пакет (возврат)", callback_data=f"adm:revoke:{uid}")])
    if history:
        buttons.append([InlineKeyboardButton(
            text="🔄 Сверить с Lava", callback_data=f"adm:sync:{history[0]['order_id']}")])
    buttons.append([InlineKeyboardButton(text="💰 Платежи", callback_data=f"adm:pays:{uid}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def handle_user(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    args = _args(message)
    if not args:
        await message.answer("Формат: <code>/user 123456789</code> или <code>/user @username</code>",
                             parse_mode="HTML")
        return
    user = db.find_user(args[0])
    if user is None:
        await message.answer("Пользователь не найден в базе.")
        return
    text, kb = _user_card(user)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ======================================================================
#  /cancel
# ======================================================================

async def handle_cancel(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    args = _args(message)
    if not args:
        await message.answer(
            "Формат: <code>/cancel 123456789</code> или <code>/cancel @username</code>\n\n"
            "Бот покажет подписку и спросит, что сделать:\n"
            "• отменить автопродление (часы остаются до конца периода);\n"
            "• отменить и обнулить пакет — если вы вернули деньги.",
            parse_mode="HTML",
        )
        return
    user = db.find_user(args[0])
    if user is None:
        await message.answer("Пользователь не найден в базе.")
        return
    sub = db.get_user_live_subscription(user["user_id"])
    plan = get_plan(user["plan"])
    if sub is None and plan.price_rub == 0:
        await message.answer(f"У {_label(user, user['user_id'])} нет подписки и платного пакета.",
                             parse_mode="HTML")
        return

    buttons = []
    if sub:
        buttons.append([InlineKeyboardButton(
            text="❌ Отменить автопродление", callback_data=f"adm:cancel:{sub['order_id']}")])
    if plan.price_rub > 0:
        buttons.append([InlineKeyboardButton(
            text="⛔ Отменить и обнулить пакет (возврат)", callback_data=f"adm:revoke:{user['user_id']}")])
    text = (
        f"{_label(user, user['user_id'])}\n"
        f"Тариф: {plan.title}, пакет до {fmt_date(user.get('plan_until'))}\n"
        + (f"Подписка: {STATUS_RU.get(sub['status'])}, след. списание {fmt_date(sub['next_pay_time'])}"
           if sub else "Автопродление уже отключено.")
        + "\n\nЧто сделать?"
    )
    await message.answer(text, parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# ======================================================================
#  Кнопки админки
# ======================================================================

async def handle_admin_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, action, arg = callback.data.split(":", 2)
    bot = callback.bot

    if action == "cancel":
        sub = db.get_subscription(arg)
        if not sub:
            await callback.answer("Подписка не найдена", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            f"Отменить автопродление «{get_plan(sub['plan']).title}» у "
            f"<code>{sub['user_id']}</code>?\nОставшиеся часы сохранятся до конца периода.",
            parse_mode="HTML",
            reply_markup=_confirm(f"adm:cancelyes:{arg}"),
        )

    elif action == "cancelyes":
        await callback.answer()
        sub = db.get_subscription(arg)
        if not sub or sub["status"] not in db.LIVE_STATUSES:
            await callback.message.edit_text("Подписка уже не активна.")
            return
        try:
            await payments.cancel_subscription(arg, "админ (обращение в поддержку)")
        except LavaError as e:
            await callback.message.edit_text(f"⚠️ Lava не отменила подписку: {html.escape(str(e))}")
            return
        user = db.get_user(sub["user_id"]) or {}
        await callback.message.edit_text(
            f"✅ Автопродление отменено. Пакет действует до {fmt_date(user.get('plan_until'))}.")
        await _tell_user(bot, sub["user_id"],
                         f"ℹ️ По твоему обращению автопродление «{get_plan(sub['plan']).title}» "
                         f"отключено. Списаний больше не будет. Оставшиеся часы действуют до "
                         f"{fmt_date(user.get('plan_until'))}.")

    elif action == "revoke":
        await callback.answer()
        await callback.message.answer(
            f"⛔ Отменить подписку и <b>обнулить пакет</b> у <code>{arg}</code>?\n\n"
            "Пользователь сразу вернётся на Free. Используйте, если вернули деньги.\n"
            "Сам возврат делается в кабинете Lava → «Транзакции».",
            parse_mode="HTML",
            reply_markup=_confirm(f"adm:revokeyes:{arg}"),
        )

    elif action == "revokeyes":
        await callback.answer()
        user_id = int(arg)
        sub = db.get_user_live_subscription(user_id)
        if sub:
            try:
                await payments.cancel_subscription(sub["order_id"], "админ: отмена с обнулением")
            except LavaError as e:
                await callback.message.edit_text(
                    f"⚠️ Lava не отменила подписку: {html.escape(str(e))}\nПакет НЕ обнулён.")
                return
        db.revoke_package(user_id)
        await callback.message.edit_text("✅ Подписка отменена, пакет обнулён, пользователь на Free.")
        await _tell_user(bot, user_id,
                         "ℹ️ По твоему обращению подписка отменена, платный пакет закрыт. "
                         "Списаний больше не будет.")

    elif action == "sync":
        await callback.answer("Сверяю...")
        try:
            result = await payments.sync_subscription(bot, arg)
        except LavaError as e:
            await callback.message.answer(f"⚠️ Lava: {html.escape(str(e))}")
            return
        sub = db.get_subscription(arg) or {}
        await callback.message.answer(
            f"Статус: {STATUS_RU.get(result['status'], result['status'])}\n"
            f"Последнее списание: {fmt_dt(sub.get('last_pay_time'))}\n"
            f"Следующее: {fmt_dt(sub.get('next_pay_time'))}"
            + ("\n💰 Найдено и начислено новое списание." if result["credited"] else ""),
        )

    elif action == "pays":
        await callback.answer()
        rows = db.list_payments(limit=30, user_id=int(arg))
        if not rows:
            await callback.message.answer("Платежей нет.")
            return
        lines = [f"💰 Платежи <code>{arg}</code>:"]
        for p in rows:
            kind = "новая" if p["kind"] == "first" else "продление"
            lines.append(f"{fmt_dt(p['paid_at'])} · {p['amount']:.0f} ₽ · "
                         f"{get_plan(p['plan']).title} ({kind})")
        await callback.message.answer("\n".join(lines), parse_mode="HTML")

    elif action == "no":
        await callback.answer("Отменено")
        await callback.message.edit_text("Действие отменено.")

    else:
        await callback.answer()


def _confirm(yes_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да", callback_data=yes_data),
         InlineKeyboardButton(text="Нет", callback_data="adm:no:-")],
    ])


async def _tell_user(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text)
    except Exception:
        logger.warning("Не удалось написать пользователю %s", user_id)


async def _send_long(message: Message, text: str) -> None:
    """Telegram ограничивает сообщение 4096 символами — режем по строкам."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3900:
            await message.answer(chunk, parse_mode="HTML")
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await message.answer(chunk, parse_mode="HTML")


# ======================================================================
#  /lavacheck
# ======================================================================

async def handle_lavacheck(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    if not config.lava_configured():
        await message.answer("❌ В .env не заданы LAVA_SHOP_ID и/или LAVA_SECRET_KEY.")
        return
    try:
        products = await lava.get_products()
        mapping = await payments.load_products(force=True)
    except LavaError as e:
        await message.answer(
            f"❌ Lava ответила ошибкой:\n<code>{html.escape(str(e))}</code>\n\n"
            "Проверь LAVA_SHOP_ID и LAVA_SECRET_KEY в .env (кабинет → проект → «Секретные ключи»).",
            parse_mode="HTML",
        )
        return

    lines = ["✅ <b>Связь с Lava работает</b>\n", "<b>Продукты подписки в Lava:</b>"]
    for p in products:
        active = "активен" if p.get("isActive", True) else "выключен"
        lines.append(f"• {html.escape(str(p.get('name')))} — {float(p.get('price') or 0):.0f} ₽, "
                     f"период {p.get('periodDays') or p.get('period')} дн. ({active})")
    lines.append("\n<b>Тарифы бота:</b>")
    for code in PAID_PLANS:
        pid = mapping.get(code)
        lines.append(f"• {PLANS[code].title}: " + ("✅ найден продукт" if pid else "❌ продукт не найден"))
    lines.append(f"\nEmail при оплате: {'спрашиваем' if config.LAVA_REQUIRE_EMAIL else 'не спрашиваем'}")
    lines.append(f"Комиссия в расчётах: {(1 - NET_REVENUE_SHARE) * 100:.0f}%")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ======================================================================
#  /grant и /risky (без изменений по смыслу)
# ======================================================================

async def handle_grant(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Формат: <code>/grant &lt;user_id&gt; &lt;plan&gt; [дни]</code>\n"
            "Пример: <code>/grant 257513179 standard 30</code>\n"
            f"Тарифы: {', '.join(PLANS.keys())}\n\n"
            "Тариф выдаётся без оплаты и без автопродления.",
            parse_mode="HTML",
        )
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("user_id должен быть числом.")
        return

    plan_code = parts[2].lower()
    if plan_code not in PLANS:
        await message.answer(f"Неизвестный тариф. Доступны: {', '.join(PLANS.keys())}")
        return

    days = None
    if len(parts) >= 4:
        try:
            days = int(parts[3])
        except ValueError:
            await message.answer("Число дней должно быть целым.")
            return

    db.get_or_create_user(target_id)
    db.set_plan(target_id, plan_code, days)

    days_str = f"{days} дн." if days else f"{PLANS[plan_code].days or 30} дн."
    await message.answer(
        f"✅ Пользователю <code>{target_id}</code> выдан тариф "
        f"<b>{PLANS[plan_code].title}</b> на {days_str}",
        parse_mode="HTML",
    )
    logger.info("Admin выдал %s тариф %s", target_id, plan_code)


async def handle_risky(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return

    accounts = db.get_risky_accounts()
    if not accounts:
        await message.answer("✅ Рисковых аккаунтов нет — все в плюсе.")
        return

    lines = ["⚠️ <b>Рисковые аккаунты</b>\n"]
    for a in accounts:
        uname = f"@{html.escape(a['username'])}" if a["username"] else f"id{a['user_id']}"
        lines.append(
            f"<b>{uname}</b> (<code>{a['user_id']}</code>) — {get_plan(a['plan']).title}\n"
            f"  быстрых: {a['minutes_fast']:.0f} мин, спикеров: {a['minutes_spk']:.0f} мин\n"
            f"  себестоимость: {a['cost']:.0f} ₽ / доход: {a['revenue']:.0f} ₽\n"
            f"  маржа: <b>{a['margin']:.0f} ₽</b>\n"
        )
    await _send_long(message, "\n".join(lines))


async def notify_admin_risk(bot: Bot, user_id: int) -> None:
    """Сигнал админу: аккаунт впервые пересёк порог себестоимости."""
    if config.ADMIN_ID is None:
        return

    info = db.get_account_cost_info(user_id)
    uname = f"@{info['username']}" if info["username"] else f"id{user_id}"
    ratio = (info["cost"] / info["revenue"] * 100) if info["revenue"] else 0

    text = (
        "⚠️ <b>Аккаунт в зоне риска</b>\n\n"
        f"Пользователь: <b>{html.escape(uname)}</b> (<code>{user_id}</code>)\n"
        f"Тариф: {get_plan(info['plan']).title}\n"
        f"Быстрых: {info['minutes_fast']:.0f} мин\n"
        f"Спикеров: {info['minutes_spk']:.0f} мин\n"
        f"Себестоимость: {info['cost']:.0f} ₽ ({ratio:.0f}% от дохода)\n"
        f"Маржа: <b>{info['margin']:.0f} ₽</b>\n\n"
        f"Проверь: /user {user_id}"
    )
    try:
        await bot.send_message(config.ADMIN_ID, text, parse_mode="HTML")
    except Exception:
        logger.exception("Не удалось отправить уведомление админу")
