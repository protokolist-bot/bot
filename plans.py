"""
plans.py
========
Тарифы и параметры мониторинга себестоимости — в одном месте.

Модель:
- Free — лимитный (60 мин/мес), страховка от абузы бесплатным.
- week — безлимит на 7 дней (249 ₽).
- month — безлимит на 30 дней (749 ₽).

"Безлимит" реальный для пользователя, но на стороне админа считается
себестоимость каждого аккаунта. Если она превышает порог от дохода —
аккаунт помечается и админ получает сигнал.
"""

from dataclasses import dataclass


@dataclass
class Plan:
    code: str
    title: str
    price_rub: int
    days: int              # срок действия (0 для Free — помесячный сброс)
    minutes: int           # лимит минут (0 = безлимит)
    speakers: bool         # доступна ли диаризация
    all_summaries: bool
    max_file_minutes: int  # лимит длины файла (0 = без лимита)
    free_speaker_trials: int


PLANS: dict[str, Plan] = {
    "free": Plan(
        code="free",
        title="Free",
        price_rub=0,
        days=0,
        minutes=60,            # лимитный
        speakers=False,
        all_summaries=False,
        max_file_minutes=30,
        free_speaker_trials=2,
    ),
    "week": Plan(
        code="week",
        title="Неделя (безлимит)",
        price_rub=249,
        days=7,
        minutes=0,             # безлимит
        speakers=True,
        all_summaries=True,
        max_file_minutes=0,
        free_speaker_trials=0,
    ),
    "month": Plan(
        code="month",
        title="Месяц (безлимит)",
        price_rub=749,
        days=30,
        minutes=0,             # безлимит
        speakers=True,
        all_summaries=True,
        max_file_minutes=0,
        free_speaker_trials=0,
    ),
}

DEFAULT_PLAN = "free"

# Жёсткий потолок длины файла для ВСЕХ тарифов (включая безлимитные) — в минутах.
# Защита от гигантских файлов, которые долго качать и обрабатывать.
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


def format_plan_card(plan: Plan) -> str:
    lines = [f"<b>{plan.title}</b>"]
    if plan.price_rub == 0:
        lines.append("Бесплатно")
    else:
        lines.append(f"{plan.price_rub} ₽ / {plan.days} дн.")

    if plan.minutes == 0:
        lines.append("• Безлимитная транскрибация")
    else:
        lines.append(f"• {plan.minutes} минут аудио в месяц")

    if plan.speakers:
        lines.append("• Разметка по спикерам")
    elif plan.free_speaker_trials > 0:
        lines.append(f"• Спикеры: {plan.free_speaker_trials} пробных раза")

    if plan.all_summaries:
        lines.append("• Все типы саммари")
    else:
        lines.append("• Базовое саммари")

    if plan.max_file_minutes > 0:
        lines.append(f"• Длина файла: до {plan.max_file_minutes} мин")
    else:
        lines.append("• Длина файла: без лимита")

    return "\n".join(lines)
