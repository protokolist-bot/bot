"""
admin.py
========
Админ-панель: доступна ТОЛЬКО владельцу бота (config.ADMIN_ID).

Команды:
- /stats — сводная статистика.
- /grant <user_id> <plan> [дни] — выдать тариф вручную.
- /risky — список убыточных/рисковых аккаунтов.

Плюс notify_admin_risk(bot, user_id) — автоуведомление, когда аккаунт
впервые пересёк порог себестоимости (вызывается из bot.py после обработки).
"""

import logging

from aiogram import Bot
from aiogram.types import Message

import config
import database as db
from plans import PLANS

logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    return config.ADMIN_ID is not None and user_id == config.ADMIN_ID


async def handle_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return

    s = db.get_stats()
    plan_lines = []
    for code, plan in PLANS.items():
        count = s["by_plan"].get(code, 0)
        plan_lines.append(f"  • {plan.title}: {count}")
    plan_block = "\n".join(plan_lines)

    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: <b>{s['total_users']}</b>\n"
        f"💳 Активных платных подписок: <b>{s['active_paid']}</b>\n"
        f"⚠️ Рисковых аккаунтов: <b>{s['risky_count']}</b>\n\n"
        f"<b>По тарифам:</b>\n{plan_block}\n\n"
        f"🆕 Новых за 24ч: <b>{s['new_day']}</b>\n"
        f"🆕 Новых за 7 дней: <b>{s['new_week']}</b>\n\n"
        f"🎧 Минут быстрых: <b>{s['total_fast']:.0f}</b>\n"
        f"👥 Минут со спикерами: <b>{s['total_spk']:.0f}</b>"
    )
    await message.answer(text, parse_mode="HTML")


async def handle_grant(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Формат: <code>/grant &lt;user_id&gt; &lt;plan&gt; [дни]</code>\n"
            "Пример: <code>/grant 257513179 month</code>\n"
            f"Тарифы: {', '.join(PLANS.keys())}",
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

    days = None  # None => берётся срок из тарифа
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
        uname = f"@{a['username']}" if a["username"] else f"id{a['user_id']}"
        lines.append(
            f"<b>{uname}</b> (<code>{a['user_id']}</code>) — {PLANS[a['plan']].title}\n"
            f"  быстрых: {a['minutes_fast']:.0f} мин, спикеров: {a['minutes_spk']:.0f} мин\n"
            f"  себестоимость: {a['cost']:.0f} ₽ / доход: {a['revenue']:.0f} ₽\n"
            f"  маржа: <b>{a['margin']:.0f} ₽</b>\n"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


async def notify_admin_risk(bot: Bot, user_id: int) -> None:
    """
    Присылает админу сигнал, что аккаунт пересёк порог себестоимости.
    Вызывается из bot.py, когда check_and_flag_risk вернул True.
    """
    if config.ADMIN_ID is None:
        return

    info = db.get_account_cost_info(user_id)
    uname = f"@{info['username']}" if info["username"] else f"id{user_id}"
    ratio = (info["cost"] / info["revenue"] * 100) if info["revenue"] else 0

    text = (
        "⚠️ <b>Аккаунт в зоне риска</b>\n\n"
        f"Пользователь: <b>{uname}</b> (<code>{user_id}</code>)\n"
        f"Тариф: {PLANS[info['plan']].title}\n"
        f"Быстрых: {info['minutes_fast']:.0f} мин\n"
        f"Спикеров: {info['minutes_spk']:.0f} мин\n"
        f"Себестоимость: {info['cost']:.0f} ₽ ({ratio:.0f}% от дохода)\n"
        f"Маржа: <b>{info['margin']:.0f} ₽</b>\n\n"
        f"Проверь список: /risky"
    )
    try:
        await bot.send_message(config.ADMIN_ID, text, parse_mode="HTML")
    except Exception:
        logger.exception("Не удалось отправить уведомление админу")
