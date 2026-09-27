"""
audio_extractor.py
==================
По ссылке из соцсети скачивает аудиодорожку и сохраняет как mp3.
Использует yt-dlp (YouTube, Instagram, TikTok, VK, X, Vimeo и др.).
Для конвертации в mp3 нужен установленный ffmpeg.

Две функции:
- probe_duration(url) — БЫСТРО узнать длительность видео (в минутах) БЕЗ скачивания.
- extract_audio(url) — скачать аудио. Возвращает (путь, название, длительность_мин).

ЗАЩИТА ОТ 403 Forbidden (актуально для серверов):
YouTube блокирует запросы с дата-центровых IP. Обходим это так:
1. Форсим IPv4 (часть хостов режет IPv6).
2. Пробуем разные "клиенты" YouTube по очереди (android, tv, web) —
   каждый имеет разный уровень доверия у YouTube; если один даёт 403,
   пробуем следующий.
Обновление самого yt-dlp (pip install -U yt-dlp) — тоже важно, оно делается
отдельно на сервере.
"""

import os
import uuid
import yt_dlp

from config import TEMP_DIR

# Порядок "клиентов" YouTube для перебора при 403.
# android и tv обычно надёжнее с серверных IP, web — как запасной.
YT_PLAYER_CLIENTS = ["android", "tv", "web"]


def _base_opts() -> dict:
    """Общие настройки yt-dlp с защитой от блокировок."""
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # Форсим IPv4 — часть медиа-хостов не отдаёт по IPv6.
        "source_address": "0.0.0.0",
        # Притворяемся обычным браузером.
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    }


def probe_duration(url: str) -> float | None:
    """Узнаёт длительность видео в МИНУТАХ без скачивания. None, если недоступно."""
    opts = _base_opts()
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        seconds = info.get("duration")
        if seconds:
            return seconds / 60.0
    except Exception:
        return None
    return None


def extract_audio(url: str) -> tuple[str, str, float]:
    """
    Скачивает аудио по ссылке.
    Возвращает (путь_к_mp3, название_видео, длительность_в_минутах).

    При 403 Forbidden перебирает разных YouTube-клиентов. Если все не сработали —
    бросает последнюю ошибку (вызывающий код покажет её пользователю).
    """
    os.makedirs(TEMP_DIR, exist_ok=True)

    unique_id = uuid.uuid4().hex
    output_template = os.path.join(TEMP_DIR, f"{unique_id}.%(ext)s")

    def build_opts(player_client: str | None) -> dict:
        opts = _base_opts()
        opts.update({
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "128",
                }
            ],
        })
        # Указываем конкретный клиент YouTube (для обхода 403).
        if player_client:
            opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}
        return opts

    last_error = None
    info = None

    # Пробуем клиентов по очереди. Первый успешный — выходим.
    for client in YT_PLAYER_CLIENTS:
        try:
            with yt_dlp.YoutubeDL(build_opts(client)) as ydl:
                info = ydl.extract_info(url, download=True)
            break  # успех
        except Exception as e:
            last_error = e
            # Чистим возможный частичный файл перед следующей попыткой.
            partial = os.path.join(TEMP_DIR, f"{unique_id}.mp3")
            if os.path.exists(partial):
                try:
                    os.remove(partial)
                except OSError:
                    pass
            continue

    # Если ни один клиент не сработал — пробуем без указания клиента (дефолт).
    if info is None:
        try:
            with yt_dlp.YoutubeDL(build_opts(None)) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            last_error = e

    # Все попытки провалились — бросаем последнюю ошибку.
    if info is None:
        raise last_error if last_error else RuntimeError("Не удалось скачать аудио")

    audio_path = os.path.join(TEMP_DIR, f"{unique_id}.mp3")
    title = info.get("title", "Транскрипция")

    duration_min = 0.0
    seconds = info.get("duration")
    if seconds:
        duration_min = seconds / 60.0
    else:
        try:
            from pydub import AudioSegment
            audio = AudioSegment.from_file(audio_path)
            duration_min = len(audio) / 1000.0 / 60.0
        except Exception:
            duration_min = 0.0

    return audio_path, title, duration_min
