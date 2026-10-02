"""
plans.py
========
Тарифы и параметры мониторинга себестоимости — в одном месте.

Модель — ПАКЕТЫ ЧАСОВ (не безлимит):
- Free     — пробный, 60 минут, помесячный сброс.
- standard — 30 часов (1800 мин) за 300 ₽, пакет действует 30 дней.
- max      — 100 часов (6000 мин) за 600 ₽, пакет действует 30 дней.

Пакет действует N дней с момента покупки; минуты в пределах пакета.
На стороне админа считается себестоимость каждого аккаунта раздельно
(быстрый режим / спикеры). Если себестоимость превышает порог от дохода —
аккаунт помечается и админ получает сигнал (/risky).
"""

from dataclasses import dataclass


@dataclass
class Plan:
    code: str
    title: str
    price_rub: int
    days: int              # срок действия пакета в днях (0 для Free — помесячный сброс)
    minutes: int           # лимит минут (0 = безлимит; у нас везде лимит)
    speakers: bool         # доступна ли диаризация
    all_summaries: bool
    max_file_minutes: int  # лимит длины файла (0 = без лимита, действует глобальный)
    free_speaker_trials: int


PLANS: dict[str, Plan] = {
    "free": Plan(
        code="free",
        title="Пробный",
        price_rub=0,
        days=0,
        minutes=60,            # 60 минут
        speakers=False,
        all_summaries=False,
        max_file_minutes=30,
        free_speaker_trials=2,
    ),
    "standard": Plan(
        code="standard",
        title="Стандарт · 30 часов",
        price_rub=300,
        days=30,
        minutes=30 * 60,       # 1800 минут = 30 часов
        speakers=True,
        all_summaries=True,
        max_file_minutes=0,    # действует глобальный лимит (3 часа)
        free_speaker_trials=0,
    ),
    "max": Plan(
        code="max",
        title="Макс · 100 часов",
        price_rub=600,
        days=30,
        minutes=100 * 60,      # 6000 минут = 100 часов
        speakers=True,
        all_summaries=True,
        max_file_minutes=0,
        free_speaker_trials=0,
    ),
}

DEFAULT_PLAN = "free"

# Жёсткий потолок длины одного файла для ВСЕХ тарифов — в минутах.
GLOBAL_MAX_FILE_MINUTES = 180  # 3 часа

# --- Параметры мониторинга себестоимости ---

# Себестоимость за минуту в рублях (курс 86 ₽/$).
COST_FAST_PER_MIN = 0.057      # быстрый режим (Groq)
COST_SPEAKERS_PER_MIN = 0.244  # со спикерами (AssemblyAI)

# Доля оплаты, доходящая до тебя после эквайринга и налога (~10% съедается).
NET_REVENUE_SHARE = 0.90

# Порог: если себестоимость превысила эту долю чистого дохода — аккаунт рисковый.
RISK_THRESHOLD = 0.70


def get_plan(code: str) -> Plan:
    return PLANS.get(code, PLANS[DEFAULT_PLAN])


def net_revenue(plan_code: str) -> float:
    """Чистый доход с пользователя за его тариф (за вычетом комиссий)."""
    plan = get_plan(plan_code)
    return plan.price_rub * NET_REVENUE_SHARE


def account_cost(minutes_fast: float, minutes_speakers: float) -> float:
    """Себестоимость аккаунта в рублях по раздельным счётчикам минут."""
    return minutes_fast * COST_FAST_PER_MIN + minutes_speakers * COST_SPEAKERS_PER_MIN


def _format_hours(minutes: int) -> str:
    """Красиво показывает лимит: часы, если кратно, иначе минуты."""
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60} часов"
    return f"{minutes} минут"


def format_plan_card(plan: Plan) -> str:
    lines = [f"<b>{plan.title}</b>"]
    if plan.price_rub == 0:
        lines.append("Бесплатно")
    else:
        lines.append(f"{plan.price_rub} ₽ / пакет на {plan.days} дн.")

    lines.append(f"• {_format_hours(plan.minutes)} расшифровки")

    if plan.speakers:
        lines.append("• Разметка по спикерам")
    elif plan.free_speaker_trials > 0:
        lines.append(f"• Спикеры: {plan.free_speaker_trials} пробных раза")

    if plan.all_summaries:
        lines.append("• Все типы саммари")
    else:
        lines.append("• Базовое саммари")

    if plan.price_rub > 0:
        lines.append(f"• Пакет действует {plan.days} дней")

    return "\n".join(lines)
