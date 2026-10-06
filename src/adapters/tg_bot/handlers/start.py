from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from loguru import logger
from src.adapters.tg_bot.handlers.trial import trial_button
from src.adapters.tg_bot.support import support_button

WELCOME_TEXT = (
    "🌐 <b>GrooveVPN</b>\n"
    "<i>Стабильный интернет без блокировок</i>"
)


async def _trial_offered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    offer_uc = context.bot_data.get("trial_offer_uc")
    if offer_uc is None:
        return False
    try:
        return await offer_uc.is_offered(update.effective_user.id)
    except Exception as exc:
        logger.error(f"Failed to check trial offer for {update.effective_user.id}: {exc}")
        return False


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"User {update.effective_user.id} started the bot")

    keyboard = []
    if await _trial_offered(update, context):
        keyboard.append([trial_button()])
    keyboard += [
        [InlineKeyboardButton("Купить VPN", callback_data="buy_vpn")],
        [InlineKeyboardButton("Моя подписка", callback_data="my_subscription")],
        [support_button()],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(WELCOME_TEXT, reply_markup=reply_markup, parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.message.edit_text(WELCOME_TEXT, reply_markup=reply_markup, parse_mode="HTML")
