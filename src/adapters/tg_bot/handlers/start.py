from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from loguru import logger
from src.adapters.tg_bot.support import support_button

WELCOME_TEXT = "<b>GrooveVPN</b>"


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"User {update.effective_user.id} started the bot")

    keyboard = [
        [InlineKeyboardButton("Купить VPN", callback_data="buy_vpn")],
        [InlineKeyboardButton("Моя подписка", callback_data="my_subscription")],
        [support_button()],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(WELCOME_TEXT, reply_markup=reply_markup, parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.message.edit_text(WELCOME_TEXT, reply_markup=reply_markup, parse_mode="HTML")
