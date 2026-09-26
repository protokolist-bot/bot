"""
summarizer.py
=============
Делает саммари из транскрипта через Groq LLM — с выбором ТИПА саммари.

Тип задаёт "оптику", под которой модель смотрит на текст. Восемь типов:
- brief    — Краткий пересказ (3-5 тезисов)
- interview— Интервью/собеседование
- meeting  — Встреча/мит
- lecture  — Лекция/урок
- podcast  — Подкаст/беседа
- sales    — Продающий/созвон с клиентом
- training — Обучение/тренинг
- custom   — Свой запрос (пользователь описывает, что нужно)

Для длинных транскриптов используется map-reduce:
1. Режем текст на части, кратко пересказываем каждую.
2. Из пересказов делаем финальное саммари нужного типа.
"""

from groq import Groq

from config import GROQ_API_KEY, GROQ_SUMMARY_MODEL

client = Groq(api_key=GROQ_API_KEY)

# Порог в символах, выше которого включаем поэтапное суммирование.
MAX_CHARS_PER_PART = 12000

# Человекочитаемые названия типов (для кнопок и заголовков документов).
SUMMARY_TYPES: dict[str, str] = {
    "brief": "Краткий пересказ",
    "interview": "Интервью",
    "meeting": "Встреча",
    "lecture": "Лекция",
    "podcast": "Подкаст",
    "sales": "Продающий звонок",
    "training": "Обучение",
    "custom": "Свой запрос",
}

# Инструкции (промпты) для финального саммари каждого типа.
# {text} подставляется автоматически.
_FINAL_PROMPTS: dict[str, str] = {
    "brief": (
        "На основе текста ниже составь краткое саммари в виде 3–5 ключевых "
        "тезисов. Каждый тезис — с новой строки, начинается с '• '. "
        "Только тезисы, без вступления и заключения."
    ),
    "interview": (
        "Ниже расшифровка интервью или собеседования. Составь конспект:\n"
        "• Основные обсуждённые темы и вопросы\n"
        "• Ключевые ответы и позиции говорящих\n"
        "• Важные факты, качества, компетенции (если это собеседование)\n"
        "• Договорённости и выводы\n"
        "Каждый пункт — с новой строки, начинается с '• '."
    ),
    "meeting": (
        "Ниже расшифровка рабочей встречи. Составь протокол:\n"
        "• Принятые решения\n"
        "• Задачи (с ответственными и сроками, если названы)\n"
        "• Открытые вопросы\n"
        "• Следующие шаги\n"
        "Каждый пункт — с новой строки, начинается с '• '. "
        "Если раздел пуст — пропусти его."
    ),
    "lecture": (
        "Ниже расшифровка лекции или урока. Составь учебный конспект:\n"
        "• Основные понятия и определения\n"
        "• Ключевые мысли по темам\n"
        "• Выводы\n"
        "Каждый пункт — с новой строки, начинается с '• '."
    ),
    "podcast": (
        "Ниже расшифровка подкаста или беседы. Составь саммари:\n"
        "• Обсуждаемые темы\n"
        "• Ключевые мысли и позиции участников\n"
        "• Интересные факты и заметные высказывания\n"
        "Каждый пункт — с новой строки, начинается с '• '."
    ),
    "sales": (
        "Ниже расшифровка звонка с клиентом. Составь саммари для продаж:\n"
        "• Потребности и запросы клиента\n"
        "• Возражения и сомнения\n"
        "• Что было предложено/обещано\n"
        "• Договорённости и следующие шаги\n"
        "Каждый пункт — с новой строки, начинается с '• '."
    ),
    "training": (
        "Ниже расшифровка обучающего материала или тренинга. Составь конспект:\n"
        "• Ключевые навыки и умения, которым учат\n"
        "• Пошаговые инструкции и методики (если есть)\n"
        "• Практические выводы и рекомендации\n"
        "Каждый пункт — с новой строки, начинается с '• '."
    ),
}

# Системная подсказка для модели.
_SYSTEM = (
    "Ты — ассистент, который делает точные и полезные конспекты. "
    "Отвечай на языке исходного текста. Не выдумывай факты, "
    "опирайся только на текст."
)


def _full_text_from_segments(segments: list[dict]) -> str:
    """Склеивает сегменты в один сплошной текст (без таймкодов)."""
    return " ".join(seg["text"] for seg in segments).strip()


def _split_text(text: str, max_chars: int) -> list[str]:
    """Режет длинный текст на части не длиннее max_chars, по границам предложений."""
    if len(text) <= max_chars:
        return [text]

    parts = []
    current = ""
    for sentence in text.replace("! ", "!|").replace("? ", "?|").replace(". ", ".|").split("|"):
        if len(current) + len(sentence) + 1 > max_chars:
            if current:
                parts.append(current.strip())
            current = sentence
        else:
            current += " " + sentence
    if current.strip():
        parts.append(current.strip())
    return parts


def _ask_llm(prompt: str) -> str:
    """Отправляет запрос в Groq LLM и возвращает текстовый ответ."""
    response = client.chat.completions.create(
        model=GROQ_SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
    return response.choices[0].message.content.strip()


def _summarize_part(text: str) -> str:
    """Кратко пересказывает одну часть длинного текста (шаг map)."""
    prompt = (
        "Ниже фрагмент расшифровки аудио. Кратко перескажи его основные мысли "
        "в нескольких предложениях, только по существу:\n\n" + text
    )
    return _ask_llm(prompt)


def _build_final_prompt(summary_type: str, text: str, custom_request: str | None) -> str:
    """Собирает финальный промпт под выбранный тип саммари."""
    if summary_type == "custom":
        # Свой запрос: пользователь сам описал, что хочет.
        instruction = (
            custom_request
            or "Составь краткое саммари в виде тезисов."
        )
        return (
            f"Ниже расшифровка аудио. Выполни запрос пользователя по этому тексту.\n"
            f"Запрос: {instruction}\n\n"
            f"Текст:\n{text}"
        )

    instruction = _FINAL_PROMPTS.get(summary_type, _FINAL_PROMPTS["brief"])
    return f"{instruction}\n\nТекст:\n{text}"


def summarize(
    segments: list[dict],
    summary_type: str = "brief",
    custom_request: str | None = None,
) -> str:
    """
    Главная функция.
    segments       — сегменты транскрипта.
    summary_type   — один из ключей SUMMARY_TYPES.
    custom_request — текст запроса пользователя (только для типа "custom").

    Возвращает готовое саммари (строка).
    """
    full_text = _full_text_from_segments(segments)
    if not full_text:
        return "• Не удалось выделить содержание."

    parts = _split_text(full_text, MAX_CHARS_PER_PART)

    if len(parts) == 1:
        prompt = _build_final_prompt(summary_type, parts[0], custom_request)
        return _ask_llm(prompt)

    # Длинный текст: сначала пересказываем части, потом сводим под нужный тип.
    partial_summaries = [_summarize_part(p) for p in parts]
    combined = "\n".join(partial_summaries)
    prompt = _build_final_prompt(summary_type, combined, custom_request)
    return _ask_llm(prompt)
