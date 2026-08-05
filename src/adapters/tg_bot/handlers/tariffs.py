from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from loguru import logger

async def tariffs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    logger.info(f"User {update.effective_user.id} requested tariffs")
    
    keyboard = [
        [InlineKeyboardButton("50 ГБ - 130 руб", callback_data="tariff_50")],
        [InlineKeyboardButton("150 ГБ - 250 руб", callback_data="tariff_150")],
        [InlineKeyboardButton("450 ГБ - 449 руб", callback_data="tariff_450")],
        [InlineKeyboardButton("🔙 Назад", callback_data="start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = (
        "Выберите подходящий пакет трафика:\n"
        "(Трафик не сгорает со временем)"
    )
    
    await query.message.edit_text(text, reply_markup=reply_markup)
