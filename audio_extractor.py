"""
audio_extractor.py
==================
По ссылке из соцсети скачивает аудиодорожку и сохраняет как mp3.
Использует yt-dlp (YouTube, Instagram, TikTok, VK, X, Vimeo и др.).
Для конвертации в mp3 нужен установленный ffmpeg.

Две функции:
- probe_duration(url) — БЫСТРО узнать длительность видео (в минутах) БЕЗ скачивания,
  по метаданным. Нужна для проверки лимитов ДО обработки. Может вернуть None,
  если платформа не отдаёт длительность.
- extract_audio(url) — скачать аудио. Возвращает (путь, название, длительность_мин).
"""

import os
import uuid
import yt_dlp

from config import TEMP_DIR


def probe_duration(url: str) -> float | None:
    """
    Узнаёт длительность видео в МИНУТАХ без скачивания (по метаданным).
    Возвращает None, если длительность недоступна.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,  # только метаданные, без загрузки
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        seconds = info.get("duration")
        if seconds:
            return seconds / 60.0
    except Exception:
        # Не смогли узнать заранее — не страшно, вернём None,
        # длину потом всё равно замерим по скачанному файлу.
        return None
    return None


def extract_audio(url: str) -> tuple[str, str, float]:
    """
    Скачивает аудио по ссылке.
    Возвращает (путь_к_mp3, название_видео, длительность_в_минутах).
    Бросает исключение при битой ссылке / неподдерживаемой платформе.
    """
    os.makedirs(TEMP_DIR, exist_ok=True)

    unique_id = uuid.uuid4().hex
    output_template = os.path.join(TEMP_DIR, f"{unique_id}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    audio_path = os.path.join(TEMP_DIR, f"{unique_id}.mp3")
    title = info.get("title", "Транскрипция")

    # Длительность: сначала из метаданных, иначе замеряем по файлу.
    duration_min = 0.0
    seconds = info.get("duration")
    if seconds:
        duration_min = seconds / 60.0
    else:
        # Фолбэк: замеряем реально скачанный файл через pydub.
        try:
            from pydub import AudioSegment
            audio = AudioSegment.from_file(audio_path)
            duration_min = len(audio) / 1000.0 / 60.0
        except Exception:
            duration_min = 0.0

    return audio_path, title, duration_min
