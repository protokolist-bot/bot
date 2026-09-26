"""
doc_builder.py
==============
Сборка .docx-документов.

Две функции:
- build_transcript_docx: транскрипт с таймкодами (как было).
- build_summary_docx: отдельный документ с саммари (3-5 тезисов).
"""

import os
import uuid
from docx import Document
from docx.shared import Pt

from config import TEMP_DIR


def _format_timecode(seconds: float) -> str:
    """Секунды (83.5) -> строка [00:01:23]."""
    total = int(seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def build_transcript_docx(title: str, segments: list[dict]) -> str:
    """
    Создаёт .docx с транскриптом.
    Каждый сегмент: [ЧЧ:ММ:СС] Текст.
    Возвращает путь к файлу.
    """
    os.makedirs(TEMP_DIR, exist_ok=True)

    document = Document()
    document.add_heading(title, level=1)
    subtitle = document.add_paragraph("Транскрипция с таймкодами")
    subtitle.runs[0].italic = True
    document.add_paragraph("")

    for seg in segments:
        timecode = _format_timecode(seg["start"])
        paragraph = document.add_paragraph()

        run_time = paragraph.add_run(f"[{timecode}] ")
        run_time.bold = True
        run_time.font.size = Pt(11)

        run_text = paragraph.add_run(seg["text"])
        run_text.font.size = Pt(11)

    output_path = os.path.join(TEMP_DIR, f"transcript_{uuid.uuid4().hex}.docx")
    document.save(output_path)
    return output_path


def build_speaker_transcript_docx(title: str, segments: list[dict]) -> str:
    """
    Создаёт .docx с транскриптом, размеченным ПО СПИКЕРАМ (диаризация).
    Каждая реплика: строка "Спикер A  [00:01:23]" жирным, ниже — текст реплики.

    segments — список {"start", "end", "text", "speaker"}.
    Возвращает путь к файлу.
    """
    os.makedirs(TEMP_DIR, exist_ok=True)

    document = Document()
    document.add_heading(title, level=1)
    subtitle = document.add_paragraph("Транскрипция с разметкой по спикерам")
    subtitle.runs[0].italic = True
    document.add_paragraph("")

    for seg in segments:
        timecode = _format_timecode(seg["start"])
        speaker = seg.get("speaker", "?")

        # Строка-заголовок реплики: "Спикер A  [00:01:23]".
        header = document.add_paragraph()
        run_speaker = header.add_run(f"Спикер {speaker}")
        run_speaker.bold = True
        run_speaker.font.size = Pt(11)
        run_time = header.add_run(f"  [{timecode}]")
        run_time.font.size = Pt(10)

        # Текст реплики отдельным абзацем.
        body = document.add_paragraph(seg["text"])
        body.runs[0].font.size = Pt(11)

    output_path = os.path.join(TEMP_DIR, f"transcript_spk_{uuid.uuid4().hex}.docx")
    document.save(output_path)
    return output_path


def build_summary_docx(title: str, summary_text: str) -> str:
    """
    Создаёт отдельный .docx с саммари.
    summary_text — строка с тезисами (каждый с новой строки, начинается с '• ').
    Возвращает путь к файлу.
    """
    os.makedirs(TEMP_DIR, exist_ok=True)

    document = Document()
    document.add_heading(f"Саммари: {title}", level=1)
    subtitle = document.add_paragraph("Краткое содержание")
    subtitle.runs[0].italic = True
    document.add_paragraph("")

    # Каждую строку-тезис добавляем как отдельный абзац-буллет.
    for line in summary_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Убираем ведущий маркер, если модель его уже поставила,
        # чтобы не задваивать (список сам добавит буллет через стиль).
        clean = line.lstrip("•").lstrip("-").strip()
        paragraph = document.add_paragraph(clean, style="List Bullet")
        paragraph.runs[0].font.size = Pt(12)

    output_path = os.path.join(TEMP_DIR, f"summary_{uuid.uuid4().hex}.docx")
    document.save(output_path)
    return output_path
