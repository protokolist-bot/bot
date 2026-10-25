"""
bot.py
======
Главный файл Telegram-бота (aiogram 3.x).

Этап "Подписки": добавлены тарифы, учёт минут, меню, 8 типов саммари, админка.

Поток обработки ссылки:
1. Пользователь присылает ссылку.
2. Бот показывает выбор: один спикер / несколько.
3. Затем выбор типа саммари (или "без саммари"); для "своего запроса" —
   просит ввести текст запроса.
4. Проверяет лимиты (тариф, остаток минут, длина файла, доступ к спикерам).
5. Обрабатывает и списывает минуты.

Состояние выбора между шагами хранится в памяти по короткому ключу (sessions).
"""

import os
import re
import uuid
import asyncio
import logging
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    Message,
    FSInputFile,
    BotCommand,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

import config
import database as db
from plans import get_plan, PLANS, format_plan_card, GLOBAL_MAX_FILE_MINUTES
from audio_extractor import extract_audio, probe_duration
from media import convert_to_mp3, TELEGRAM_FILE_LIMIT
from transcriber import transcribe
from transcriber_assemblyai import transcribe_with_speakers
from summarizer import summarize, SUMMARY_TYPES
from doc_builder import (
    build_transcript_docx,
    build_speaker_transcript_docx,
    build_summary_docx,
)
import admin
import payments
from payments import PurchaseError, fmt_date, fmt_hours
from lava import LavaError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

config.validate_config()
db.init_db()

if not config.assemblyai_available():
    logger.warning("ASSEMBLYAI_API_KEY не задан — режим спикеров недоступен.")
if config.ADMIN_ID is None:
    logger.warning("ADMIN_ID не задан — админ-команды работать не будут.")

bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

URL_PATTERN = re.compile(r"https?://\S+")

# Хранилище сессий обработки: ключ -> {url, mode, summary_type, awaiting_custom, user_id}
sessions: dict[str, dict] = {}

# Ждём от пользователя email перед оплатой: user_id -> код тарифа.
awaiting_email: dict[int, str] = {}

EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Кнопки постоянной клавиатуры.
BTN_SUB = "💎 Подписка"
BTN_LIMIT = "📊 Мой лимит"
BTN_HELP = "❓ Помощь"
BTN_ABOUT = "ℹ️ О боте"

TEXT_START = (
    "👋 <b>Протоколист</b> — расшифровка аудио и видео в текст.\n\n"
    "<b>Что умеет:</b>\n"
    "• Расшифровка файлов и голосовых\n"
    "• Ссылки: YouTube, Instagram, TikTok, VK и др.\n"
    "• Таймкоды и разделение по спикерам\n"
    "• Краткая выжимка с главными мыслями\n"
    "• Разные типы саммари (встреча, интервью, лекция и др.)\n\n"
    "🎁 <b>60 минут бесплатно</b> на пробном тарифе.\n\n"
    "Пришли ссылку или файл, чтобы начать 👇"
)

TEXT_HELP = (
    "❓ <b>Как пользоваться</b>\n\n"
    "1. Пришли ссылку на видео или пост.\n"
    "2. Выбери: один голос или несколько (разметка по спикерам).\n"
    "3. Выбери тип саммари (или без него).\n"
    "4. Получи документы: транскрипцию и саммари.\n\n"
    "<b>Режимы:</b>\n"
    "👤 Один спикер — быстро, с таймкодами.\n"
    "👥 Несколько — разметка «кто говорит» (для интервью, встреч).\n\n"
    "<b>Кнопки внизу:</b>\n"
    "💎 Подписка — тарифы и лимиты.\n"
    "📊 Мой лимит — сколько минут осталось.\n\n"
    "Поддерживаются YouTube, Instagram, TikTok, VK, X, Vimeo и другие платформы."
)

# Юридические документы (требуются платёжной системой).
URL_OFERTA = "https://telegra.ph/PUBLICHNAYA-OFERTA-10-02-17"
URL_PRIVACY = "https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-10-02-93"
URL_REFUND = "https://telegra.ph/POLITIKA-VOZVRATA-10-02"

# Аккаунт техподдержки для связи.
SUPPORT_USERNAME = "emil_pay"
SUPPORT_URL = f"https://t.me/{SUPPORT_USERNAME}"

TEXT_ABOUT = (
    "ℹ️ <b>О боте</b>\n\n"
    "Протоколист превращает аудио и видео в текст и конспект.\n"
    "Один голос — быстрое распознавание, несколько — разметка по спикерам.\n"
    "Языки определяются автоматически.\n\n"
    "<b>Документы</b>\n"
    f"• <a href=\"{URL_OFERTA}\">Публичная оферта</a>\n"
    f"• <a href=\"{URL_PRIVACY}\">Политика конфиденциальности</a>\n"
    f"• <a href=\"{URL_REFUND}\">Политика возврата</a>"
)

TEXT_DOCS = (
    "📄 <b>Документы</b>\n\n"
    f"• <a href=\"{URL_OFERTA}\">Публичная оферта</a>\n"
    f"• <a href=\"{URL_PRIVACY}\">Политика конфиденциальности</a>\n"
    f"• <a href=\"{URL_REFUND}\">Политика возврата</a>\n\n"
    "Оформляя подписку, вы принимаете условия оферты.\n\n"
    f"🛟 Техподдержка: @{SUPPORT_USERNAME}"
)


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SUB), KeyboardButton(text=BTN_LIMIT)],
            [KeyboardButton(text=BTN_HELP), KeyboardButton(text=BTN_ABOUT)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Пришли ссылку на видео или аудио…",
    )


def mode_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Один спикер (быстро)",
                                  callback_data=f"mode:single:{key}")],
            [InlineKeyboardButton(text="👥 Несколько спикеров",
                                  callback_data=f"mode:multi:{key}")],
        ]
    )


def summary_keyboard(key: str) -> InlineKeyboardMarkup:
    """Кнопки выбора типа саммари (2 в ряд) + без саммари."""
    buttons = []
    row = []
    for code, title in SUMMARY_TYPES.items():
        row.append(InlineKeyboardButton(text=title, callback_data=f"sum:{code}:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🚫 Без саммари",
                                         callback_data=f"sum:none:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def again_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обработать ещё", callback_data="again")]
        ]
    )


def subscribe_keyboard() -> InlineKeyboardMarkup:
    """Экран тарифов: только выбор тарифа."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ {PLANS['standard'].title} — {PLANS['standard'].price_rub} ₽",
                                  callback_data="buy:standard")],
            [InlineKeyboardButton(text=f"🚀 {PLANS['max'].title} — {PLANS['max'].price_rub} ₽",
                                  callback_data="buy:max")],
        ]
    )


def support_keyboard() -> InlineKeyboardMarkup:
    """Кнопка связи со службой поддержки (для раздела «Помощь»)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛟 Служба поддержки", url=SUPPORT_URL)],
        ]
    )


def cancel_email_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="email:cancel")],
    ])


# ---------- Админ-команды (регистрируем ПЕРВЫМИ, до общих хендлеров) ----------

@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    await admin.handle_stats(message)


@dp.message(Command("grant"))
async def cmd_grant(message: Message) -> None:
    await admin.handle_grant(message)


@dp.message(Command("risky"))
async def cmd_risky(message: Message) -> None:
    await admin.handle_risky(message)


@dp.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    await admin.handle_admin_help(message)


@dp.message(Command("subs"))
async def cmd_subs(message: Message) -> None:
    await admin.handle_subs(message)


@dp.message(Command("user"))
async def cmd_user(message: Message) -> None:
    await admin.handle_user(message)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    await admin.handle_cancel(message)


@dp.message(Command("payments"))
async def cmd_payments(message: Message) -> None:
    await admin.handle_payments(message)


@dp.message(Command("lavacheck"))
async def cmd_lavacheck(message: Message) -> None:
    await admin.handle_lavacheck(message)


@dp.callback_query(F.data.startswith("adm:"))
async def cb_admin(callback: CallbackQuery) -> None:
    await admin.handle_admin_callback(callback)


# ---------- Команды и кнопки меню ----------

@dp.message(CommandStart())
async def handle_start(message: Message, command: CommandObject) -> None:
    db.get_or_create_user(message.from_user.id, message.from_user.username)

    # Возврат со страницы оплаты Lava: t.me/<бот>?start=paid / ?start=payfail
    if command.args == "paid":
        await check_latest_payment(message, message.from_user.id)
        return
    if command.args == "payfail":
        await message.answer(
            "❌ Оплата не прошла или была отменена.\n\n"
            "Попробуй ещё раз в «💎 Подписка». Если деньги списались, "
            f"а часы не появились — напиши в поддержку: @{SUPPORT_USERNAME}",
            reply_markup=main_keyboard(),
        )
        return

    # Если в папке есть файл приветственной гифки — отправляем её с текстом.
    # Поддерживаем welcome.mp4 (видео-гифка) и welcome.gif.
    welcome_video = None
    for name in ("welcome.mp4", "welcome.gif"):
        if os.path.exists(name):
            welcome_video = name
            break

    if welcome_video:
        try:
            await message.answer_animation(
                FSInputFile(welcome_video),
                caption=TEXT_START,
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )
            return
        except Exception:
            # Если с гифкой что-то не так — не падаем, шлём просто текст.
            logger.exception("Не удалось отправить приветственную гифку")

    await message.answer(TEXT_START, parse_mode="HTML", reply_markup=main_keyboard())


@dp.message(Command("help"))
async def handle_help_cmd(message: Message) -> None:
    await message.answer(TEXT_HELP, parse_mode="HTML",
                         reply_markup=support_keyboard())


@dp.message(Command("docs"))
async def handle_docs_cmd(message: Message) -> None:
    await message.answer(
        TEXT_DOCS, parse_mode="HTML", disable_web_page_preview=True
    )


@dp.message(F.text == BTN_HELP)
async def handle_help_btn(message: Message) -> None:
    await message.answer(TEXT_HELP, parse_mode="HTML",
                         reply_markup=support_keyboard())


@dp.message(F.text == BTN_ABOUT)
async def handle_about_btn(message: Message) -> None:
    await message.answer(TEXT_ABOUT, parse_mode="HTML",
                         disable_web_page_preview=True)


@dp.message(F.text == BTN_SUB)
async def handle_subscription(message: Message) -> None:
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    current = get_plan(user["plan"])

    cards = "\n\n".join(format_plan_card(p) for p in PLANS.values())
    text = (
        f"💎 <b>Тарифы</b>\n\n"
        f"Твой текущий тариф: <b>{current.title}</b>\n\n"
        f"{cards}"
    )
    await message.answer(
        text, parse_mode="HTML", reply_markup=subscribe_keyboard(),
        disable_web_page_preview=True,
    )


@dp.message(F.text == BTN_LIMIT)
async def handle_limit(message: Message) -> None:
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    plan = get_plan(user["plan"])
    remaining = db.get_remaining_minutes(message.from_user.id)
    limit = db.minutes_limit(user)

    text = f"📊 <b>Твой тариф</b>\n\nТариф: <b>{plan.title}</b>\n"

    # Остаток часов/минут.
    if limit >= 60:
        text += f"Осталось: <b>{fmt_hours(remaining)}</b> ({remaining:.0f} мин) из {fmt_hours(limit)}\n"
    else:
        text += f"Осталось: <b>{remaining:.0f} мин</b> из {limit:.0f} мин\n"

    keyboard = None
    sub = db.get_user_live_subscription(message.from_user.id)
    if plan.price_rub > 0 and user.get("plan_until"):
        text += f"Пакет действует до: <b>{fmt_date(user['plan_until'])}</b>\n"
    elif plan.price_rub == 0:
        text += f"Обновление лимита: {fmt_date(user['reset_at'])}\n"
        if not plan.speakers and plan.free_speaker_trials > 0:
            left = max(0, plan.free_speaker_trials - user["speaker_trials_used"])
            text += f"Пробных диаризаций осталось: <b>{left}</b>\n"

    if sub:
        sub_plan = get_plan(sub["plan"])
        if sub["status"] == db.SUB_SUSPENDED:
            text += (f"\n⚠️ Автопродление «{sub_plan.title}»: последнее списание не прошло. "
                     f"Проверь карту.\n")
        else:
            text += (f"\n🔄 Автопродление: <b>включено</b>\n"
                     f"Следующее списание: {sub['amount']:.0f} ₽ — {fmt_date(sub['next_pay_time'])}\n")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отключить автопродление",
                                  callback_data=f"unsub:ask:{sub['order_id']}")],
        ])
    elif plan.price_rub > 0:
        text += "\n🔄 Автопродление: выключено\n"

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("buy:"))
async def handle_buy(callback: CallbackQuery) -> None:
    """Выбран тариф — сразу создаём ссылку на оплату."""
    await callback.answer()
    plan_code = callback.data.split(":", 1)[1]
    if plan_code not in PLANS or PLANS[plan_code].price_rub == 0:
        return
    plan = get_plan(plan_code)
    user_id = callback.from_user.id

    current = db.get_user_live_subscription(user_id)
    if current and current["plan"] == plan_code and current["status"] == db.SUB_ACTIVE:
        await callback.message.answer(
            f"У тебя уже есть подписка «{plan.title}».\n"
            f"Следующее списание: {fmt_date(current['next_pay_time'])}.\n\n"
            f"Управлять ею можно в «📊 Мой лимит».",
        )
        return

    note = None
    if current:
        note = (f"После оплаты подписка «{get_plan(current['plan']).title}» отключится, "
                f"а остаток часов перенесётся.")
    await send_payment_link(callback.message, user_id, plan_code, note)


@dp.callback_query(F.data.startswith("agree:"))
async def handle_agree(callback: CallbackQuery) -> None:
    """Кнопка из старых сообщений: при необходимости спрашиваем email, затем ссылка."""
    await callback.answer()
    plan_code = callback.data.split(":", 1)[1]
    if plan_code not in PLANS:
        return
    await send_payment_link(callback.message, callback.from_user.id, plan_code)


@dp.callback_query(F.data == "email:cancel")
async def handle_email_cancel(callback: CallbackQuery) -> None:
    awaiting_email.pop(callback.from_user.id, None)
    await callback.answer("Отменено")
    await callback.message.edit_reply_markup(reply_markup=None)


async def send_payment_link(message: Message, user_id: int, plan_code: str,
                            note: str | None = None) -> None:
    """Создаёт подписку в Lava и присылает кнопку оплаты."""
    user = db.get_or_create_user(user_id)
    if config.LAVA_REQUIRE_EMAIL and not user.get("email"):
        awaiting_email[user_id] = plan_code
        await message.answer(
            "📧 Укажи email — на него придёт чек об оплате.\n"
            "Его нужно ввести один раз.",
            reply_markup=cancel_email_keyboard(),
        )
        return

    status = await message.answer("⏳ Создаю ссылку на оплату...")
    try:
        order = await payments.start_purchase(user_id, plan_code)
    except PurchaseError as e:
        await status.edit_text(f"⚠️ {e}")
        return
    except Exception:
        logger.exception("Ошибка оформления подписки user=%s", user_id)
        await status.edit_text(
            "⚠️ Не получилось создать ссылку на оплату. Попробуй ещё раз "
            f"или напиши в поддержку: @{SUPPORT_USERNAME}"
        )
        return

    plan = get_plan(plan_code)
    await status.edit_text(
        f"💳 <b>{plan.title}</b> — {order['amount']:.0f} ₽ в месяц\n\n"
        f"Нажми «Оплатить» — откроется защищённая страница Lava.\n"
        f"После оплаты часы начислятся автоматически в течение минуты. "
        f"Если не появились — нажми «Я оплатил».\n\n"
        f"🔄 Подписка продлевается автоматически каждый месяц. "
        f"Отключить можно в «📊 Мой лимит»."
        + (f"\n\nℹ️ {note}" if note else ""),
        parse_mode="HTML",
        reply_markup=payments.pay_keyboard(order["url"], order["order_id"], order["amount"]),
    )


async def report_sync_result(message: Message, result: dict) -> None:
    status = result.get("status")
    if result.get("credited"):
        return  # пользователь уже получил сообщение об успешной оплате
    if status == db.SUB_ACTIVE:
        await message.answer("✅ Подписка активна, часы начислены. Проверь «📊 Мой лимит».")
    elif status in (db.SUB_CREATED, db.SUB_EXPIRED):
        await message.answer(
            "⏳ Оплата пока не поступила.\n"
            "Если ты только что оплатил — подожди минуту и нажми «Я оплатил» ещё раз."
        )
    elif status == db.SUB_SUSPENDED:
        await message.answer("⚠️ Списание не прошло. Проверь карту и попробуй снова.")
    else:
        await message.answer(
            f"Статус подписки: {status or 'неизвестен'}. "
            f"Если что-то не так — напиши в поддержку: @{SUPPORT_USERNAME}"
        )


@dp.callback_query(F.data.startswith("chk:"))
async def handle_check_payment(callback: CallbackQuery) -> None:
    """Кнопка «Я оплатил — проверить»."""
    order_id = callback.data.split(":", 1)[1]
    sub = db.get_subscription(order_id)
    if sub is None or sub["user_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    await callback.answer("Проверяю...")
    try:
        result = await payments.sync_subscription(bot, order_id)
    except LavaError:
        await callback.message.answer("⚠️ Платёжная система не отвечает. Попробуй через минуту.")
        return
    await report_sync_result(callback.message, result)


async def check_latest_payment(message: Message, user_id: int) -> None:
    """Возврат со страницы оплаты: проверяем последний заказ пользователя."""
    subs = db.get_user_subscriptions(user_id, limit=1)
    if not subs:
        await message.answer("Не нашёл твой заказ. Оформи подписку в «💎 Подписка».",
                             reply_markup=main_keyboard())
        return
    await message.answer("🔎 Проверяю оплату...", reply_markup=main_keyboard())
    result = {"status": subs[0]["status"], "credited": False}
    for attempt in range(3):
        try:
            result = await payments.sync_subscription(bot, subs[0]["order_id"])
        except LavaError:
            await message.answer("⚠️ Платёжная система не отвечает. Попробуй через минуту.")
            return
        if result.get("status") != db.SUB_CREATED:
            break
        await asyncio.sleep(5)  # платёж мог ещё не дойти до Lava
    await report_sync_result(message, result)


@dp.callback_query(F.data.startswith("unsub:"))
async def handle_unsubscribe(callback: CallbackQuery) -> None:
    """Отключение автопродления пользователем (с подтверждением)."""
    _, action, order_id = callback.data.split(":", 2)
    if action == "no":
        await callback.answer("Подписка сохранена")
        await callback.message.edit_text("👍 Автопродление остаётся включённым.")
        return
    sub = db.get_subscription(order_id)
    if sub is None or sub["user_id"] != callback.from_user.id:
        await callback.answer("Подписка не найдена", show_alert=True)
        return
    if sub["status"] not in db.LIVE_STATUSES:
        await callback.answer("Автопродление уже отключено", show_alert=True)
        return

    if action == "ask":
        await callback.answer()
        user = db.get_or_create_user(callback.from_user.id)
        await callback.message.answer(
            f"Отключить автопродление «{get_plan(sub['plan']).title}»?\n\n"
            f"Списаний больше не будет. Оставшиеся часы действуют до "
            f"{fmt_date(user.get('plan_until'))}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Да, отключить",
                                      callback_data=f"unsub:yes:{order_id}")],
                [InlineKeyboardButton(text="Нет, оставить",
                                      callback_data="unsub:no:-")],
            ]),
        )
        return
    if action == "yes":
        await callback.answer()
        try:
            await payments.cancel_subscription(order_id, "пользователь в боте")
        except LavaError:
            logger.exception("Не удалось отменить подписку %s", order_id)
            await callback.message.edit_text(
                "⚠️ Не получилось отключить автопродление. Попробуй позже "
                f"или напиши в поддержку: @{SUPPORT_USERNAME}"
            )
            return
        user = db.get_or_create_user(callback.from_user.id)
        await callback.message.edit_text(
            f"✅ Автопродление отключено.\n"
            f"Оставшиеся часы действуют до {fmt_date(user.get('plan_until'))}."
        )
        await payments.notify_admin_text(
            bot, f"🚫 Пользователь отключил автопродление\n"
                 f"<code>{callback.from_user.id}</code> "
                 f"{'@' + callback.from_user.username if callback.from_user.username else ''}\n"
                 f"{get_plan(sub['plan']).title}",
        )


@dp.callback_query(F.data.startswith("pay:"))
async def handle_old_pay_buttons(callback: CallbackQuery) -> None:
    """Старые кнопки «картой / СБП» из сообщений до обновления."""
    await callback.answer()
    await callback.message.answer("Выбери тариф заново в «💎 Подписка».",
                                  reply_markup=main_keyboard())


@dp.callback_query(F.data == "back:plans")
async def handle_back_to_plans(callback: CallbackQuery) -> None:
    """Вернуться к списку тарифов."""
    await callback.answer()
    cards = "\n\n".join(format_plan_card(p) for p in PLANS.values())
    text = (
        f"💎 <b>Тарифы</b>\n\n{cards}"
    )
    await callback.message.answer(
        text, parse_mode="HTML", reply_markup=subscribe_keyboard(),
        disable_web_page_preview=True,
    )


@dp.callback_query(F.data == "again")
async def handle_again(callback: CallbackQuery) -> None:
    await callback.message.answer("Пришли следующую ссылку 🎧")
    await callback.answer()


# ---------- Шаг 1: ссылка -> выбор спикеров ----------

@dp.message(F.text, ~F.text.startswith("/"))
async def handle_text(message: Message) -> None:
    # Ждём email перед оплатой?
    if message.from_user.id in awaiting_email:
        email = (message.text or "").strip()
        if not EMAIL_PATTERN.match(email):
            await message.answer("Похоже, это не email. Пример: name@mail.ru",
                                 reply_markup=cancel_email_keyboard())
            return
        plan_code = awaiting_email.pop(message.from_user.id)
        db.set_email(message.from_user.id, email)
        await send_payment_link(message, message.from_user.id, plan_code)
        return

    # Проверим, не ждём ли мы от пользователя текст "своего запроса" саммари.
    for key, sess in list(sessions.items()):
        if sess.get("awaiting_custom") and sess["user_id"] == message.from_user.id:
            sess["custom_request"] = message.text
            sess["awaiting_custom"] = False
            await start_processing(message, key)
            return

    match = URL_PATTERN.search(message.text or "")
    if not match:
        await message.answer(
            "Пришли ссылку на видео или аудио, и я его расшифрую. 🎧",
            reply_markup=main_keyboard(),
        )
        return

    url = match.group(0)
    key = uuid.uuid4().hex[:8]
    sessions[key] = {"url": url, "user_id": message.from_user.id}

    await message.answer(
        "Один в записи голос или несколько?\n\n"
        "👤 <b>Один спикер</b> — быстро, с таймкодами.\n"
        "👥 <b>Несколько</b> — с разметкой «кто говорит».",
        reply_markup=mode_keyboard(key),
        parse_mode="HTML",
    )


# ---------- Приём файлов (аудио, голосовые, видео) ----------

@dp.message(F.audio | F.voice | F.video | F.video_note | F.document)
async def handle_media(message: Message) -> None:
    """
    Ловит присланный файл, скачивает (до 20 МБ), конвертирует в mp3,
    и запускает тот же поток выбора (спикеры → саммари → обработка).
    """
    db.get_or_create_user(message.from_user.id, message.from_user.username)

    # Определяем медиа-объект и его данные в зависимости от типа.
    media = (
        message.audio or message.voice or message.video
        or message.video_note or message.document
    )
    if media is None:
        return

    file_size = getattr(media, "file_size", None) or 0
    file_id = media.file_id

    # Имя для документа-транскрипции: имя файла, если есть.
    title = getattr(media, "file_name", None) or "Аудио"
    # Убираем расширение из имени для заголовка.
    title = os.path.splitext(title)[0]

    # Проверка лимита 20 МБ (ограничение облачного Bot API Telegram).
    if file_size and file_size > TELEGRAM_FILE_LIMIT:
        await message.answer(
            "⚠️ Файл больше 20 МБ — Telegram не даёт ботам скачивать такие.\n\n"
            "Что можно сделать:\n"
            "• пришли файл поменьше или короче;\n"
            "• либо загрузи запись на YouTube/Google Drive и пришли ссылку."
        )
        return

    status = await message.answer("📥 Скачиваю файл...")

    raw_path = None
    audio_path = None
    try:
        # Скачиваем файл из Telegram на сервер.
        os.makedirs("temp", exist_ok=True)
        raw_path = os.path.join("temp", f"{uuid.uuid4().hex}_{message.from_user.id}")
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, destination=raw_path)

        # Конвертируем в mp3 (в отдельном потоке — ffmpeg синхронный).
        await status.edit_text("🎧 Извлекаю аудио...")
        audio_path, duration_min = await asyncio.to_thread(convert_to_mp3, raw_path)

        # Готовим сессию с уже извлечённым аудио (не URL).
        key = uuid.uuid4().hex[:8]
        sessions[key] = {
            "ready_audio": audio_path,
            "title": title,
            "duration_min": duration_min,
            "user_id": message.from_user.id,
        }

        await status.delete()
        await message.answer(
            "Один в записи голос или несколько?\n\n"
            "👤 <b>Один спикер</b> — быстро, с таймкодами.\n"
            "👥 <b>Несколько</b> — с разметкой «кто говорит».",
            reply_markup=mode_keyboard(key),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Ошибка приёма файла от %s", message.from_user.id)
        await status.edit_text(
            "❌ Не удалось обработать файл. "
            "Попробуй другой файл или пришли ссылку."
        )
        # Чистим временные файлы при ошибке.
        for p in (raw_path, audio_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
    finally:
        # Исходный скачанный файл больше не нужен (аудио уже извлечено).
        if raw_path and os.path.exists(raw_path):
            try:
                os.remove(raw_path)
            except OSError:
                pass

@dp.callback_query(F.data.startswith("mode:"))
async def handle_mode(callback: CallbackQuery) -> None:
    _, mode, key = callback.data.split(":", 2)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    sess = sessions.get(key)
    if sess is None:
        await callback.message.answer("Ссылка устарела. Пришли её заново 🎧")
        return

    # Проверка доступа к диаризации по тарифу.
    if mode == "multi":
        if not config.assemblyai_available():
            await callback.message.answer(
                "⚠️ Режим спикеров сейчас недоступен. Обрабатываю как один голос."
            )
            mode = "single"
        else:
            user = db.get_or_create_user(callback.from_user.id)
            plan = get_plan(user["plan"])
            if not plan.speakers:
                # Free: проверяем пробные диаризации.
                left = plan.free_speaker_trials - user["speaker_trials_used"]
                if left <= 0:
                    await callback.message.answer(
                        "👥 Разметка по спикерам доступна на платных тарифах.\n"
                        "На бесплатном тарифе пробные диаризации закончились.\n\n"
                        "Нажми «💎 Подписка», чтобы оформить тариф.",
                    )
                    return

    sess["mode"] = mode

    await callback.message.answer(
        "Какое саммари подготовить?",
        reply_markup=summary_keyboard(key),
    )


# ---------- Шаг 3: выбран тип саммари -> обработка ----------

@dp.callback_query(F.data.startswith("sum:"))
async def handle_summary_choice(callback: CallbackQuery) -> None:
    _, stype, key = callback.data.split(":", 2)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    sess = sessions.get(key)
    if sess is None:
        await callback.message.answer("Ссылка устарела. Пришли её заново 🎧")
        return

    # Проверка доступа к продвинутым саммари по тарифу.
    if stype not in ("none", "brief"):
        user = db.get_or_create_user(callback.from_user.id)
        plan = get_plan(user["plan"])
        if not plan.all_summaries:
            await callback.message.answer(
                "Этот тип саммари доступен на платных тарифах.\n"
                "На бесплатном тарифе доступен «Краткий пересказ».\n\n"
                "Делаю краткий пересказ."
            )
            stype = "brief"

    sess["summary_type"] = stype

    # "Свой запрос" — просим ввести текст.
    if stype == "custom":
        sess["awaiting_custom"] = True
        await callback.message.answer(
            "Опиши, какое саммари нужно. Например:\n"
            "«Выдели только договорённости и сроки» или "
            "«Составь список всех упомянутых цифр»."
        )
        return

    await start_processing(callback.message, key)


# ---------- Основная обработка ----------

async def start_processing(message: Message, key: str) -> None:
    sess = sessions.get(key)
    if sess is None:
        await message.answer("Сессия устарела. Пришли ссылку заново 🎧")
        return

    url = sess.get("url")
    ready_audio = sess.get("ready_audio")  # путь к готовому mp3 (если пришёл файл)
    ready_title = sess.get("title")
    ready_duration = sess.get("duration_min")
    mode = sess.get("mode", "single")
    stype = sess.get("summary_type", "brief")
    custom_request = sess.get("custom_request")
    user_id = sess["user_id"]

    # Убираем сессию — она больше не нужна.
    sessions.pop(key, None)

    user = db.get_or_create_user(user_id)
    plan = get_plan(user["plan"])
    remaining = db.get_remaining_minutes(user_id)

    # Эффективный лимит длины файла: минимум из глобального (3 часа) и тарифного.
    # Если тарифный = 0 (безлимит), действует только глобальный.
    if plan.max_file_minutes and plan.max_file_minutes > 0:
        effective_file_limit = min(GLOBAL_MAX_FILE_MINUTES, plan.max_file_minutes)
    else:
        effective_file_limit = GLOBAL_MAX_FILE_MINUTES

    audio_path = None
    transcript_path = None
    summary_path = None

    status = await message.answer("🔎 Готовлю обработку...")

    try:
        # === Получение аудио: либо из готового файла, либо скачиванием по ссылке ===
        if ready_audio:
            # Аудио уже извлечено из присланного файла.
            audio_path = ready_audio
            title = ready_title or "Аудио"
            duration_min = ready_duration or 0.0
            # Проверки лимитов по уже известной длине.
            if duration_min > effective_file_limit:
                await status.edit_text(
                    f"⚠️ Запись длиннее максимальной длины ({effective_file_limit} мин)."
                )
                return
            if remaining >= 0 and duration_min > remaining:
                await status.edit_text(
                    f"⚠️ Недостаточно минут: нужно ~{duration_min:.0f}, "
                    f"осталось {remaining:.0f}.\n\nДокупи часы в «💎 Подписка»."
                )
                return
        else:
            # Обработка ссылки: предварительная проверка длины по метаданным.
            dur = await asyncio.to_thread(probe_duration, url)
            if dur is not None:
                if dur > effective_file_limit:
                    await status.edit_text(
                        f"⚠️ Это видео длиннее максимальной длины ({effective_file_limit} мин).\n"
                        f"Длина видео: {dur:.0f} мин.\n\n"
                        f"Раздели запись на части или оформи тариф в «💎 Подписка»."
                    )
                    return
                if remaining >= 0 and dur > remaining:
                    await status.edit_text(
                        f"⚠️ Недостаточно минут: нужно ~{dur:.0f}, осталось {remaining:.0f}.\n\n"
                        + (f"Лимит обновится {fmt_date(user['reset_at'])}, "
                           if plan.price_rub == 0 else "")
                        + "или смени тариф в «💎 Подписка»."
                    )
                    return

            # Скачивание аудио по ссылке.
            await status.edit_text("🎧 Скачиваю аудио...")
            audio_path, title, duration_min = await asyncio.to_thread(extract_audio, url)
            logger.info("Аудио: %s (%.1f мин, режим=%s)", audio_path, duration_min, mode)

            # Пост-проверка по реальной длине (если заранее не знали).
            if dur is None:
                if duration_min > effective_file_limit:
                    await status.edit_text(
                        f"⚠️ Видео длиннее максимальной длины ({effective_file_limit} мин)."
                    )
                    return
                if remaining >= 0 and duration_min > remaining:
                    await status.edit_text(
                        f"⚠️ Недостаточно минут: нужно ~{duration_min:.0f}, "
                        f"осталось {remaining:.0f}."
                    )
                    return

        # Транскрибация.
        if mode == "multi":
            await status.edit_text("🎧 Распознаю речь и определяю спикеров...")
            segments = await asyncio.to_thread(transcribe_with_speakers, audio_path)
        else:
            await status.edit_text("🎧 Распознаю речь...")
            segments = await asyncio.to_thread(transcribe, audio_path)

        if not segments:
            await status.edit_text("😕 Не удалось распознать речь.")
            return

        # Документ транскрипции.
        await status.edit_text("📄 Собираю документ...")
        if mode == "multi":
            transcript_path = await asyncio.to_thread(
                build_speaker_transcript_docx, title, segments
            )
            caption = "✅ Транскрипция с разметкой по спикерам готова."
        else:
            transcript_path = await asyncio.to_thread(
                build_transcript_docx, title, segments
            )
            caption = "✅ Транскрипция с таймкодами готова."
        await message.answer_document(
            FSInputFile(transcript_path, filename=f"{title}.docx"),
            caption=caption,
        )

        # Саммари (если выбрано).
        if stype != "none":
            await status.edit_text("📝 Готовлю саммари...")
            summary_text = await asyncio.to_thread(
                summarize, segments, stype, custom_request
            )
            type_title = SUMMARY_TYPES.get(stype, "Саммари")
            summary_path = await asyncio.to_thread(
                build_summary_docx, f"{type_title}: {title}", summary_text
            )
            await message.answer_document(
                FSInputFile(summary_path, filename=f"Саммари ({type_title}) — {title}.docx"),
                caption=f"📝 Саммари готово ({type_title}).",
                reply_markup=again_inline(),
            )
        else:
            await message.answer("Готово!", reply_markup=again_inline())

        # Списание использования (с разбивкой по режиму для учёта себестоимости).
        db.add_usage(user_id, duration_min, mode)
        if mode == "multi" and not plan.speakers:
            db.use_speaker_trial(user_id)

        # Проверка порога себестоимости (только платные тарифы).
        # Если аккаунт впервые ушёл в зону риска — уведомляем админа.
        if await asyncio.to_thread(db.check_and_flag_risk, user_id):
            await admin.notify_admin_risk(bot, user_id)

        await status.delete()

    except Exception:
        # Полную ссылку в лог не пишем — только домен платформы.
        domain = urlparse(url).netloc if url else None
        logger.exception("Ошибка обработки (платформа=%s, файл=%s)", domain, bool(ready_audio))
        await status.edit_text(
            "❌ Не получилось обработать.\n"
            "Если это ссылка — возможно, платформа не поддерживается или видео "
            "приватное. Если файл — попробуй другой формат. Попробуй ещё раз."
        )
    finally:
        for path in (audio_path, transcript_path, summary_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


# ---------- Запуск ----------

async def setup_bot_commands() -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="help", description="Как пользоваться"),
        BotCommand(command="docs", description="Документы (оферта, политики)"),
    ])
    # Админу — расширенное меню команд (видно только в чате с ним).
    if config.ADMIN_ID:
        from aiogram.types import BotCommandScopeChat
        try:
            await bot.set_my_commands([
                BotCommand(command="admin", description="Админ: все команды"),
                BotCommand(command="stats", description="Статистика"),
                BotCommand(command="subs", description="Активные подписки"),
                BotCommand(command="payments", description="Последние платежи"),
                BotCommand(command="user", description="Карточка пользователя"),
                BotCommand(command="cancel", description="Отменить подписку пользователя"),
                BotCommand(command="risky", description="Рисковые аккаунты"),
                BotCommand(command="lavacheck", description="Проверка связи с Lava"),
                BotCommand(command="start", description="Запустить бота"),
            ], scope=BotCommandScopeChat(chat_id=config.ADMIN_ID))
        except Exception:
            logger.warning("Не удалось установить админ-меню команд")


async def main() -> None:
    logger.info("Бот запускается...")
    try:
        # Первое обращение к Telegram — здесь всплывёт неверный токен.
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        # Понятное сообщение вместо длинного traceback.
        print("\n" + "=" * 50)
        print("  ОШИБКА: не удалось подключиться к Telegram.")
        print("=" * 50)
        print("  Скорее всего, неверный токен бота в файле .env.")
        print("  Что проверить:")
        print("  1. Открой @BotFather в Telegram")
        print("  2. /mybots -> твой бот -> API Token")
        print("  3. Впиши актуальный токен в .env")
        print("     (строка TELEGRAM_BOT_TOKEN=...)")
        print("=" * 50)
        print(f"  Техническая деталь: {e}")
        print()
        return

    await setup_bot_commands()
    # Фоновая сверка оплат с Lava (работает параллельно с ботом).
    payments_task = asyncio.create_task(payments.worker(bot))
    try:
        await dp.start_polling(bot)
    finally:
        payments_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
