import html
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from loguru import logger

from src.adapters.tg_bot.connect import CONNECT_HINT, send_with_fallback
from src.adapters.tg_bot.support import support_button
from src.use_cases.trial_use_cases import (
    ActivateTrialUseCase,
    TrialActivationResult,
    TrialResultKind,
)

TRIAL_CALLBACK = "trial_start"

HAPP_HOWTO = (
    "Как добавить в приложение Happ:\n"
    "1. Скопируйте ссылку подписки\n"
    "2. Откройте приложение Happ\n"
    "3. Нажмите на «+»\n"
    "4. Вставьте из буфера обмена"
)


def trial_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("Попробовать бесплатно", callback_data=TRIAL_CALLBACK)


def _format_date(moment: datetime | None) -> str:
    return moment.strftime("%d.%m.%Y") if moment else "-"


def _format_gb(value: float | None) -> str:
    if value is None:
        return "-"
    return str(int(value)) if float(value).is_integer() else str(round(value, 2))


def render_trial_result(
    result: TrialActivationResult,
    connect_rows: list[list[InlineKeyboardButton]] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    connect_rows = connect_rows or []
    buy_row = [InlineKeyboardButton("Купить VPN", callback_data="buy_vpn")]
    subscription_row = [InlineKeyboardButton("Моя подписка", callback_data="my_subscription")]
    back_row = [InlineKeyboardButton("🔙 Назад", callback_data="start")]

    if result.kind == TrialResultKind.GRANTED:
        text = (
            "✅ <b>Пробный период активирован</b>\n\n"
            f"Доступно: <b>{_format_gb(result.data_gb)} ГБ</b>, действует до <b>{_format_date(result.expires_at)}</b>\n\n"
            "Ваша ссылка (ключ) для подключения:\n"
            f"<code>{html.escape(result.sub_url or '')}</code>\n\n"
            f"{CONNECT_HINT if connect_rows else HAPP_HOWTO}"
        )
        return text, InlineKeyboardMarkup([*connect_rows, subscription_row, buy_row, [support_button()]])

    if result.kind == TrialResultKind.ALREADY_ACTIVE:
        text = (
            "Пробный период уже выдан.\n"
            f"Действует до <b>{_format_date(result.expires_at)}</b>.\n\n"
            "Ваша ссылка (ключ) для подключения:\n"
            f"<code>{html.escape(result.sub_url or '')}</code>"
        )
        return text, InlineKeyboardMarkup([*connect_rows, subscription_row, buy_row, [support_button()]])

    if result.kind == TrialResultKind.ALREADY_USED:
        text = "Пробный период уже был использован. Вы можете выбрать пакет:"
        return text, InlineKeyboardMarkup([buy_row, back_row, [support_button()]])

    if result.kind == TrialResultKind.ERROR:
        text = "Не удалось активировать пробный период. Попробуйте ещё раз чуть позже."
        return text, InlineKeyboardMarkup([[trial_button()], back_row, [support_button()]])

    # NOT_ELIGIBLE, CAP_REACHED, DISABLED
    text = "Пробный период временно недоступен. Вы можете сразу выбрать пакет:"
    return text, InlineKeyboardMarkup([buy_row, back_row, [support_button()]])


async def trial_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    logger.info(f"User {user.id} pressed the trial button")

    activate_uc: ActivateTrialUseCase = context.bot_data["activate_trial_uc"]
    try:
        result = await activate_uc.execute(user.id, user.username)
    except Exception as exc:
        logger.error(f"Unexpected trial failure for {user.id}: {exc}")
        result = TrialActivationResult(TrialResultKind.ERROR)

    connect_keyboard = context.bot_data.get("connect_keyboard")
    connect_rows: list[list[InlineKeyboardButton]] = []
    if connect_keyboard is not None and result.kind in (TrialResultKind.GRANTED, TrialResultKind.ALREADY_ACTIVE):
        connect_rows = connect_keyboard.app_rows(result.sub_url, "trial")

    async def deliver(text: str, markup: InlineKeyboardMarkup | None) -> None:
        await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")

    await send_with_fallback(
        deliver,
        render_trial_result(result, connect_rows),
        render_trial_result(result),
    )
