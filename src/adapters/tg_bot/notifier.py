import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import ExtBot

from src.adapters.tg_bot.support import support_button
from src.domain.tariffs import TARIFFS
from src.domain.trial_rules import (
    BYTES_PER_GB,
    ENDED_BY_TRAFFIC,
    SendOutcome,
    TrialNotificationKind,
)

SEND_DELAY_SECONDS = 0.05  # <= ~20 сообщений в секунду при лимите Telegram ~30/с
MAX_RETRIES = 3


def tariff_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{tariff.gb} ГБ - {tariff.price_rub} ₽", callback_data=f"tariff_{tariff.gb}")]
        for tariff in TARIFFS
    ]
    rows.append([support_button()])
    return InlineKeyboardMarkup(rows)


def build_notification_text(
    kind: TrialNotificationKind,
    used_bytes: int | None,
    limit_bytes: int,
    ended_reason: str | None,
) -> str:
    if kind == TrialNotificationKind.LOW:
        limit_gb = round(limit_bytes / BYTES_PER_GB, 2)
        used_gb = round((used_bytes or 0) / BYTES_PER_GB, 2)
        return (
            "<b>Пробный трафик почти закончился</b>\n"
            f"Использовано {used_gb} из {limit_gb} ГБ.\n\n"
            "Чтобы не потерять доступ, выберите пакет:"
        )
    if kind == TrialNotificationKind.ENDED:
        reason = "исчерпан трафик" if ended_reason == ENDED_BY_TRAFFIC else "истёк срок"
        return (
            f"<b>Пробный период закончился</b> ({reason}).\n\n"
            "Чтобы продолжить пользоваться VPN, выберите пакет:"
        )
    return (
        "<b>Напоминание</b>\n"
        "Пробный период закончился вчера. Выберите пакет, чтобы вернуть доступ:"
    )


class TelegramTrialNotifier:
    def __init__(
        self,
        bot: ExtBot,
        send_delay: float = SEND_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._send_delay = send_delay
        self._sleep = sleep

    async def send(
        self,
        telegram_id: int,
        kind: TrialNotificationKind,
        *,
        used_bytes: int | None,
        limit_bytes: int,
        ended_reason: str | None,
    ) -> SendOutcome:
        text = build_notification_text(kind, used_bytes, limit_bytes, ended_reason)
        markup = tariff_keyboard()

        for attempt in range(MAX_RETRIES + 1):
            try:
                await self._bot.send_message(
                    chat_id=telegram_id, text=text, parse_mode="HTML", reply_markup=markup
                )
                await self._sleep(self._send_delay)
                return SendOutcome.SENT
            except RetryAfter as exc:
                delay = exc.retry_after
                seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
                logger.warning(f"Telegram flood control for {telegram_id}: waiting {seconds}s (attempt {attempt + 1})")
                await self._sleep(seconds + 1)
            except Forbidden:
                logger.warning(f"Cannot message {telegram_id}: bot was blocked")
                return SendOutcome.BLOCKED
            except BadRequest as exc:
                if "chat not found" in str(exc).lower():
                    logger.warning(f"Cannot message {telegram_id}: chat not found")
                    return SendOutcome.BLOCKED
                logger.error(f"Bad request sending trial notification to {telegram_id}: {exc}")
                return SendOutcome.FAILED
            except TelegramError as exc:
                logger.error(f"Failed to send trial notification to {telegram_id}: {exc}")
                return SendOutcome.FAILED

        logger.error(f"Gave up sending trial notification to {telegram_id} after flood-control retries")
        return SendOutcome.FAILED
