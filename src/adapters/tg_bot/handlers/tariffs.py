from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from loguru import logger
from src.domain.tariffs import TARIFFS


async def tariffs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    logger.info(f"User {update.effective_user.id} requested tariffs")

    keyboard = [
        [InlineKeyboardButton(f"{tariff.gb} ГБ - {tariff.price_rub} ₽", callback_data=f"tariff_{tariff.gb}")]
        for tariff in TARIFFS
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="start")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "<b>Выберите пакет трафика</b>\n\n"
        "Пакет действует 30 дней с момента активации.\n"
        "Если купить новый пакет до окончания текущего срока - остаток "
        "трафика перенесётся в новый пакет, а срок действия отсчитается "
        "заново: 30 дней с момента покупки."
    )

    await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
