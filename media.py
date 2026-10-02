"""
media.py
========
Обработка файлов, присланных пользователем напрямую в Telegram
(аудио, голосовые, видео, видеосообщения, документы).

Telegram-боты через облачный Bot API могут скачивать файлы до 20 МБ.
Большие файлы — пока через ссылку (или позже через сайт/свой Bot API).

Функция convert_to_mp3 берёт скачанный файл любого формата и через ffmpeg
извлекает из него аудиодорожку в mp3 + меряет длительность.
"""

import os
import uuid
import subprocess

from config import TEMP_DIR

# Лимит размера файла для скачивания через облачный Bot API Telegram.
TELEGRAM_FILE_LIMIT = 20 * 1024 * 1024  # 20 МБ


def convert_to_mp3(input_path: str) -> tuple[str, float]:
    """
    Извлекает аудио из любого медиафайла (аудио/видео/голосовое) в mp3.
    Возвращает (путь_к_mp3, длительность_в_минутах).

    Использует ffmpeg (он уже установлен на сервере).
    Бросает исключение, если конвертация не удалась.
    """
    os.makedirs(TEMP_DIR, exist_ok=True)
    out_path = os.path.join(TEMP_DIR, f"{uuid.uuid4().hex}.mp3")

    # ffmpeg: -vn (без видео), извлекаем аудио в mp3 128k.
    # -y перезаписать, -loglevel error чтобы не шуметь.
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", input_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", "128k",
        out_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    # Меряем длительность полученного mp3 через ffprobe.
    duration_min = _probe_duration_min(out_path)

    return out_path, duration_min


def _probe_duration_min(path: str) -> float:
    """Длительность файла в минутах через ffprobe. 0.0 при неудаче."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            check=True, capture_output=True, text=True,
        )
        seconds = float(result.stdout.strip())
        return seconds / 60.0
    except Exception:
        # Фолбэк через pydub.
        try:
            from pydub import AudioSegment
            audio = AudioSegment.from_file(path)
            return len(audio) / 1000.0 / 60.0
        except Exception:
            return 0.0
