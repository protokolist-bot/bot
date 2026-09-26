"""
transcriber_assemblyai.py
=========================
Транскрибация с ДИАРИЗАЦИЕЙ (разметкой по спикерам) через AssemblyAI.

Отличие от Groq-версии:
- AssemblyAI сам определяет, сколько говорящих, и размечает реплики
  метками Speaker A, Speaker B и т.д.
- Не нужно резать файл на куски — AssemblyAI принимает большие файлы целиком.
- Язык определяется автоматически (multilingual).

Результат приводим к тому же формату сегментов, что и у Groq,
плюс добавляем поле "speaker" — чтобы дальше переиспользовать сборку документа.

Формат сегмента на выходе:
{"start": сек, "end": сек, "text": строка, "speaker": "A"/"B"/...}
"""

import assemblyai as aai

from config import ASSEMBLYAI_API_KEY, ASSEMBLYAI_MODEL

# Устанавливаем ключ один раз при импорте модуля.
aai.settings.api_key = ASSEMBLYAI_API_KEY


def transcribe_with_speakers(audio_path: str) -> list[dict]:
    """
    Транскрибирует аудио с диаризацией.
    Принимает путь к локальному аудиофайлу (SDK сам его загрузит).
    Возвращает список сегментов-реплик со спикерами и сквозными таймкодами.

    Бросает исключение при ошибке — вызывающий код (бот) её поймает.
    """
    # speaker_labels=True включает диаризацию.
    # speech_models=[ASSEMBLYAI_MODEL] жёстко фиксирует модель (universal-2),
    # чтобы не платить за более дорогой дефолт и не зависеть от его смены.
    config = aai.TranscriptionConfig(
        speaker_labels=True,
        speech_models=[ASSEMBLYAI_MODEL],
    )

    transcriber = aai.Transcriber(config=config)
    transcript = transcriber.transcribe(audio_path)

    # Проверяем, что транскрипция не завершилась ошибкой на стороне сервиса.
    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI вернул ошибку: {transcript.error}")

    segments = []
    # utterances — список реплик, каждая с полями speaker/text/start/end.
    # start/end приходят в МИЛЛИсекундах — переводим в секунды.
    for utt in (transcript.utterances or []):
        segments.append(
            {
                "start": utt.start / 1000.0,
                "end": utt.end / 1000.0,
                "text": utt.text.strip(),
                "speaker": utt.speaker,  # "A", "B", "C", ...
            }
        )

    return segments
