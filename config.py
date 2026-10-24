"""
config.py
=========
Все настройки бота в одном месте.

ГЛАВНЫЙ ПРИНЦИП: секреты (токены, ключи) НИКОГДА не пишутся прямо в коде.
Читаем их из переменных окружения (файл .env).
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Токен Telegram-бота (получаешь у @BotFather) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# --- Ключ Groq API (console.groq.com) — быстрый режим без спикеров ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# --- Ключ AssemblyAI (assemblyai.com) — режим с диаризацией (спикеры) ---
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")

# --- Telegram ID администратора (владельца бота) ---
# Только этот пользователь имеет доступ к /stats и /grant.
# Узнать свой ID: напиши @userinfobot в Telegram.
_admin_raw = os.getenv("ADMIN_ID", "257513179")
ADMIN_ID = int(_admin_raw) if _admin_raw and _admin_raw.isdigit() else None

# --- Модель AssemblyAI для диаризации ---
# universal-2 — экономичная ($0.15/час), 99 языков, поддерживает диаризацию.
# Указываем ЯВНО, чтобы не зависеть от дефолта AssemblyAI (он дороже —
# universal-3-5-pro по $0.21/час — и может меняться без предупреждения).
ASSEMBLYAI_MODEL = os.getenv("ASSEMBLYAI_MODEL", "universal-2")

# --- Модель Whisper для транскрибации ---
GROQ_WHISPER_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3")

# --- Текстовая LLM-модель для саммари (тот же Groq, тот же ключ) ---
# llama-3.3-70b-versatile — сильная модель, хорошо сжимает текст в тезисы.
# Если упрёшься в лимиты — можно поставить llama-3.1-8b-instant (быстрее/дешевле).
GROQ_SUMMARY_MODEL = os.getenv("GROQ_SUMMARY_MODEL", "openai/gpt-oss-20b")

# --- Лимит размера одного куска аудио для Groq (25 МБ, берём 24 с запасом) ---
MAX_CHUNK_SIZE_BYTES = 24 * 1024 * 1024

# --- Папка для временных файлов ---
TEMP_DIR = os.getenv("TEMP_DIR", "temp")

# --- Платёжная система Lava (кабинет Lava -> проект -> «Секретные ключи») ---
LAVA_SHOP_ID = os.getenv("LAVA_SHOP_ID", "").strip()
LAVA_SECRET_KEY = os.getenv("LAVA_SECRET_KEY", "").strip()
# Дополнительный ключ — для проверки вебхуков (сейчас вебхук не используется).
LAVA_ADDITIONAL_KEY = os.getenv("LAVA_ADDITIONAL_KEY", "").strip()

# Спрашивать ли у пользователя email перед первой оплатой.
# По умолчанию НЕ спрашиваем. Если Lava начнёт требовать email
# при создании подписчика — поставь LAVA_REQUIRE_EMAIL=1.
LAVA_REQUIRE_EMAIL = os.getenv("LAVA_REQUIRE_EMAIL", "0").strip() not in ("0", "false", "no", "")

# ID продуктов подписки в Lava. Обычно НЕ нужны: бот сам находит продукты
# по цене (300 ₽ -> Стандарт, 600 ₽ -> Макс). Укажи, только если цены совпадут.
LAVA_PRODUCT_STANDARD = os.getenv("LAVA_PRODUCT_STANDARD", "").strip()
LAVA_PRODUCT_MAX = os.getenv("LAVA_PRODUCT_MAX", "").strip()

# Имя бота (без @) — для ссылок возврата после оплаты.
BOT_USERNAME = os.getenv("BOT_USERNAME", "Protocolist_bot").strip().lstrip("@")

# Присылать ли админу уведомления о покупках, продлениях и отменах.
ADMIN_PAYMENT_NOTIFY = os.getenv("ADMIN_PAYMENT_NOTIFY", "1").strip() not in ("0", "false", "no", "")


def lava_configured() -> bool:
    """Заданы ли ключи Lava (без них оплата недоступна)."""
    return bool(LAVA_SHOP_ID and LAVA_SECRET_KEY)


def validate_config() -> None:
    """Проверяем обязательные секреты. Нет — падаем сразу с понятной ошибкой."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")

    if missing:
        raise RuntimeError(
            "Не заданы обязательные переменные окружения: "
            + ", ".join(missing)
            + ".\nСоздай файл .env по образцу .env.example и впиши значения."
        )


def assemblyai_available() -> bool:
    """Есть ли ключ AssemblyAI (доступен ли режим со спикерами)."""
    return bool(ASSEMBLYAI_API_KEY)
