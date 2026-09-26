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
