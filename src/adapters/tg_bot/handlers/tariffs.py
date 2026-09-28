from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from loguru import logger
from src.domain.tariffs import TARIFFS


async def tariffs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    logger.info(f"User {update.effective_user.id} requested tariffs")

    keyboard = []
    for tariff in TARIFFS:
        label = f"📦 {tariff.gb} ГБ — {tariff.price_rub} ₽"
        if tariff.badge:
            label += f"  {tariff.badge}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"tariff_{tariff.gb}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="start")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🛒 <b>Выберите пакет трафика</b>\n\n"
        "Доступ действует 30 дней с момента активации.\n"
        "Купите новый пакет заранее — остаток трафика не сгорит, "
        "а перенесётся в новый пакет."
    )

    await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
