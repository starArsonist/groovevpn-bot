import re
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from src.domain.connect_links import (
    SUPPORTED_APPS,
    ConnectConfig,
    build_connect_url,
    parse_connect_config,
)

# Bot API не документирует предел длины URL кнопки; на практике Telegram режет ~2048 символов
MAX_BUTTON_URL_LENGTH = 2048

CONNECT_HINT = (
    "Нажмите кнопку своего приложения - подписка добавится автоматически. "
    "Если приложение не открылось, скопируйте ссылку и добавьте её вручную."
)

Rendered = tuple[str, InlineKeyboardMarkup | None]


class ConnectKeyboard:
    """Строки кнопок подключения приложений; пустой результат = фича выключена."""

    def __init__(self, config: ConnectConfig | None) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config is not None

    def app_rows(self, sub_url: str | None, context: str = "") -> list[list[InlineKeyboardButton]]:
        if self._config is None or not sub_url:
            return []

        rows: list[list[InlineKeyboardButton]] = []
        skipped = 0
        for app in self._config.apps:
            url = build_connect_url(self._config, app, sub_url)
            if len(url) > MAX_BUTTON_URL_LENGTH:
                skipped += 1
                continue
            rows.append([InlineKeyboardButton(f"Подключить в {SUPPORTED_APPS[app]}", url=url)])

        if skipped:
            logger.warning(f"Connect buttons skipped for {skipped} app(s): URL is too long")
        if rows:
            # В лог - только факт показа, без URL подписки и страницы (там секретный токен)
            logger.info(f"Connect buttons prepared: {len(rows)} (context={context})")
        return rows


def build_connect_keyboard(page_url: str | None, apps_csv: str | None) -> ConnectKeyboard:
    """Собирает ConnectKeyboard из настроек. Никогда не падает: при проблемах кнопки выключаются."""
    try:
        config, warnings = parse_connect_config(page_url, apps_csv)
    except Exception as exc:
        logger.warning(f"Connect buttons disabled: failed to read configuration ({type(exc).__name__})")
        return ConnectKeyboard(None)

    for warning in warnings:
        logger.warning(warning)
    return ConnectKeyboard(config)


def _sanitize(message: str) -> str:
    return re.sub(r"\S*://\S*", "<url>", message)


async def send_with_fallback(
    deliver: Callable[[str, InlineKeyboardMarkup | None], Awaitable[Any]],
    primary: Rendered,
    fallback: Rendered,
) -> Any:
    """Отправляет сообщение с кнопками подключения; если Telegram их отклонил
    (например, BUTTON_URL_INVALID), повторяет без них - ссылка подписки не должна потеряться."""
    try:
        return await deliver(*primary)
    except BadRequest as exc:
        logger.warning(
            f"Telegram rejected the message with connect buttons ({_sanitize(str(exc))}); "
            "resending without them"
        )
        return await deliver(*fallback)
