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
            "Предлагаем выбрать пакет, чтобы не потерять доступ:"
        )
    if kind == TrialNotificationKind.ENDED:
        reason = "исчерпан трафик" if ended_reason == ENDED_BY_TRAFFIC else "истёк срок"
        return (
            f"<b>Пробный период закончился</b> ({reason}).\n\n"
            "Предлагаем продолжить пользоваться GrooveVPN:"
        )
    return (
        "<b>Напоминание</b>\n"
        "Пробный период закончился вчера.\n"
        "Предлагаем выбрать пакет, чтобы доступ остался:"
    )


async def send_html_with_retry(
    bot: ExtBot,
    chat_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None,
    *,
    send_delay: float,
    sleep: Callable[[float], Awaitable[None]],
    kind: str,
) -> SendOutcome:
    """Отправка HTML-сообщения с ожиданием при flood control; блокировка бота и ошибки - в исход."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
            await sleep(send_delay)
            return SendOutcome.SENT
        except RetryAfter as exc:
            delay = exc.retry_after
            seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
            logger.warning(f"Telegram flood control for {chat_id}: waiting {seconds}s (attempt {attempt + 1})")
            await sleep(seconds + 1)
        except Forbidden:
            logger.warning(f"Cannot message {chat_id}: bot was blocked")
            return SendOutcome.BLOCKED
        except BadRequest as exc:
            if "chat not found" in str(exc).lower():
                logger.warning(f"Cannot message {chat_id}: chat not found")
                return SendOutcome.BLOCKED
            logger.error(f"Bad request sending {kind} notification to {chat_id}: {exc}")
            return SendOutcome.FAILED
        except TelegramError as exc:
            logger.error(f"Failed to send {kind} notification to {chat_id}: {exc}")
            return SendOutcome.FAILED

    logger.error(f"Gave up sending {kind} notification to {chat_id} after flood-control retries")
    return SendOutcome.FAILED


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
        return await send_html_with_retry(
            self._bot, telegram_id, text, tariff_keyboard(),
            send_delay=self._send_delay, sleep=self._sleep, kind="trial",
        )


def build_reward_text(reward_rub: int, balance_rub: int) -> str:
    return (
        "<b>Друг оплатил пакет</b>\n"
        f"Начислено {reward_rub} ₽, баланс {balance_rub} ₽.\n"
        "Баланс можно потратить только на пакеты."
    )


class TelegramReferralNotifier:
    """Уведомление пригласившему о начислении (реализует порт `ReferralNotifier`)."""

    def __init__(
        self,
        bot: ExtBot,
        send_delay: float = SEND_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._send_delay = send_delay
        self._sleep = sleep

    async def send_reward(self, telegram_id: int, reward_rub: int, balance_rub: int) -> SendOutcome:
        return await send_html_with_retry(
            self._bot, telegram_id, build_reward_text(reward_rub, balance_rub),
            InlineKeyboardMarkup([[InlineKeyboardButton("Купить VPN", callback_data="buy_vpn")]]),
            send_delay=self._send_delay, sleep=self._sleep, kind="referral",
        )
