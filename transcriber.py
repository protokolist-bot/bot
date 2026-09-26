"""
transcriber.py
==============
Транскрибация аудио через Groq Whisper API.
1. Режет длинное аудио на куски (Groq принимает файлы до 25 МБ).
2. Отправляет каждый кусок, получает сегменты с таймкодами,
   склеивает всё со сквозным временем.
"""

import os
import math
from pydub import AudioSegment
from groq import Groq

from config import GROQ_API_KEY, GROQ_WHISPER_MODEL

client = Groq(api_key=GROQ_API_KEY)

# Длина одного куска: 10 минут mp3 @128kbps ≈ 9-10 МБ (запас под лимит 25 МБ).
CHUNK_LENGTH_MS = 10 * 60 * 1000


def _split_audio(audio_path: str) -> list[tuple[str, float]]:
    """
    Режет аудио на куски по CHUNK_LENGTH_MS.
    Возвращает [(путь_к_куску, смещение_в_секундах), ...].
    Короткое аудио => один элемент с оригиналом и смещением 0.
    """
    audio = AudioSegment.from_file(audio_path)
    total_ms = len(audio)

    if total_ms <= CHUNK_LENGTH_MS:
        return [(audio_path, 0.0)]

    chunks = []
    num_chunks = math.ceil(total_ms / CHUNK_LENGTH_MS)
    base_name = os.path.splitext(audio_path)[0]

    for i in range(num_chunks):
        start_ms = i * CHUNK_LENGTH_MS
        end_ms = min((i + 1) * CHUNK_LENGTH_MS, total_ms)
        chunk = audio[start_ms:end_ms]

        chunk_path = f"{base_name}_chunk{i}.mp3"
        chunk.export(chunk_path, format="mp3", bitrate="128k")

        chunks.append((chunk_path, start_ms / 1000.0))

    return chunks


def _transcribe_chunk(chunk_path: str) -> list[dict]:
    """Отправляет один кусок в Groq Whisper, возвращает сегменты с таймкодами."""
    with open(chunk_path, "rb") as f:
        result = client.audio.transcriptions.create(
            file=(os.path.basename(chunk_path), f.read()),
            model=GROQ_WHISPER_MODEL,
            response_format="verbose_json",
        )

    segments = []
    for seg in result.segments:
        if isinstance(seg, dict):
            segments.append(
                {"start": seg["start"], "end": seg["end"], "text": seg["text"]}
            )
        else:
            segments.append(
                {"start": seg.start, "end": seg.end, "text": seg.text}
            )
    return segments


def transcribe(audio_path: str) -> list[dict]:
    """
    Главная функция.
    Возвращает единый список сегментов со сквозными таймкодами:
    [{"start": сек, "end": сек, "text": строка}, ...]
    """
    all_segments = []
    chunk_files_to_cleanup = []

    chunks = _split_audio(audio_path)

    try:
        for chunk_path, offset in chunks:
            segments = _transcribe_chunk(chunk_path)
            for seg in segments:
                all_segments.append(
                    {
                        "start": seg["start"] + offset,
                        "end": seg["end"] + offset,
                        "text": seg["text"].strip(),
                    }
                )
            if chunk_path != audio_path:
                chunk_files_to_cleanup.append(chunk_path)
    finally:
        for cp in chunk_files_to_cleanup:
            if os.path.exists(cp):
                os.remove(cp)

    return all_segments
