"""
lava.py
=======
Клиент API Lava Business (https://api.lava.ru) — только то, что нужно боту:
рекуррентные подписки.

Формат запросов взят из официального SDK Lava (lava-payment/lava, v2.1):
- все запросы — POST с JSON-телом;
- из тела убираются поля со значением null;
- ключи сортируются по алфавиту;
- подпись = HMAC-SHA256(JSON, секретный ключ) и кладётся в тело полем "signature".

ВАЖНО про JSON: SDK написан на PHP, а PHP при сериализации экранирует "/"
как "\\/" и не-латиницу как \\uXXXX. Python по умолчанию "/" не экранирует,
поэтому подпись не совпала бы. Функция _php_json повторяет поведение PHP.
"""

import hashlib
import hmac
import json
import logging
from typing import Any

import aiohttp

import config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.lava.ru"
TIMEOUT = aiohttp.ClientTimeout(total=20)


class LavaError(Exception):
    """Ошибка API Lava (неверные ключи, неверные данные, сбой сети)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _php_json(data: Any) -> str:
    """JSON в том же виде, что выдаёт PHP json_encode() по умолчанию."""
    return json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace("/", "\\/")


def _sign(data: dict, key: str) -> str:
    """Подпись: отсортированные ключи верхнего уровня -> JSON -> HMAC-SHA256 (hex)."""
    ordered = {k: data[k] for k in sorted(data)}
    return hmac.new(key.encode(), _php_json(ordered).encode(), hashlib.sha256).hexdigest()


def check_webhook_signature(raw_body: bytes | str, signature: str) -> bool:
    """
    Проверка подписи вебхука Lava (заголовок Authorization, дополнительный ключ).
    Сейчас бот работает без вебхука, функция — задел на будущее.
    """
    if not config.LAVA_ADDITIONAL_KEY or not signature:
        return False
    try:
        data = json.loads(raw_body)
    except (ValueError, TypeError):
        return False
    expected = _sign(data, config.LAVA_ADDITIONAL_KEY)
    return hmac.compare_digest(expected, signature.strip())


async def _request(path: str, payload: dict) -> Any:
    """Подписанный POST к Lava. Возвращает поле data ответа."""
    if not config.lava_configured():
        raise LavaError("Ключи Lava не заданы в .env (LAVA_SHOP_ID, LAVA_SECRET_KEY)")

    data = {k: v for k, v in payload.items() if v is not None}
    data["shopId"] = config.LAVA_SHOP_ID
    data = {k: data[k] for k in sorted(data)}
    data["signature"] = _sign(data, config.LAVA_SECRET_KEY)
    body = _php_json(data)

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.post(BASE_URL + path, data=body.encode(), headers=headers) as resp:
                text = await resp.text()
                http_status = resp.status
    except (aiohttp.ClientError, TimeoutError) as e:
        raise LavaError(f"Нет связи с Lava: {e}") from e

    try:
        result = json.loads(text)
    except ValueError:
        raise LavaError(f"Lava вернула не JSON (HTTP {http_status}): {text[:300]}", http_status)

    status = result.get("status", http_status) if isinstance(result, dict) else http_status
    error = result.get("error") if isinstance(result, dict) else None
    if error or status != 200:
        msg = json.dumps(error, ensure_ascii=False) if isinstance(error, (dict, list)) else str(error)
        logger.warning("Lava %s -> %s: %s", path, status, msg)
        raise LavaError(msg or f"HTTP {status}", status)

    return result.get("data")


# ---------- Рекуррентные подписки ----------

async def get_products() -> list[dict]:
    """Список продуктов подписки (создаются в кабинете Lava)."""
    data = await _request("/business/recurrent/product/list", {})
    if not data:
        return []
    return data if isinstance(data, list) else [data]


async def create_consumer(consumer_id: str, email: str | None) -> dict:
    """Создать подписчика. consumer_id — наш идентификатор пользователя."""
    return await _request(
        "/business/recurrent/consumer/create",
        {"consumerId": consumer_id, "email": email},
    )


async def subscribe(product_id: str, consumer_id: str, order_id: str) -> dict:
    """
    Оформить подписку. Возвращает subscriptionId, amount, expired, url
    (url — ссылка на первую оплату).
    """
    return await _request(
        "/business/recurrent/subscription/subscribe",
        {"productId": product_id, "consumerId": consumer_id, "orderId": order_id},
    )


async def subscription_status(order_id: str | None = None,
                              subscription_id: str | None = None) -> dict:
    """
    Статус подписки: status (created/activated/suspended/deactivated),
    last_pay_time, next_pay_time, amount, deactivated_reason и др.
    """
    return await _request(
        "/business/recurrent/subscription/status",
        {"orderId": order_id, "subscriptionId": subscription_id},
    )


async def unsubscribe(order_id: str | None = None,
                      subscription_id: str | None = None) -> bool:
    """Отменить подписку (дальнейших списаний не будет)."""
    data = await _request(
        "/business/recurrent/subscription/unsubscribe",
        {"orderId": order_id, "subscriptionId": subscription_id},
    )
    return bool(isinstance(data, dict) and data.get("unsubscribed", True))
