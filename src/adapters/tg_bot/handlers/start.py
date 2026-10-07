from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from loguru import logger
from src.adapters.tg_bot.handlers.trial import trial_button
from src.adapters.tg_bot.support import support_button
from src.domain.trial_rules import TrialConfig

WELCOME_BASE = (
    "🌐 <b>GrooveVPN</b>\n"
    "<i>Стабильный интернет без блокировок</i>"
)


def _days_word(days: int) -> str:
    if days % 10 == 1 and days % 100 != 11:
        return "день"
    if 2 <= days % 10 <= 4 and not 12 <= days % 100 <= 14:
        return "дня"
    return "дней"


def build_welcome_text(trial: TrialConfig | None = None) -> str:
    """Приветствие; блок про бесплатный период - только если триал доступен пользователю."""
    if trial is None:
        return WELCOME_BASE
    return (
        f"{WELCOME_BASE}\n\n"
        f"Попробуйте наш сервис <b>бесплатно</b>: {trial.data_gb} ГБ трафика на {trial.days} {_days_word(trial.days)}\n"
        "Не нужно ничего оплачивать или привязывать - нажмите на кнопку ниже, "
        "сразу выдадим подписку"
    )


async def _trial_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> TrialConfig | None:
    """Параметры триала, если он сейчас доступен этому пользователю, иначе None."""
    offer_uc = context.bot_data.get("trial_offer_uc")
    if offer_uc is None:
        return None
    try:
        if await offer_uc.is_offered(update.effective_user.id):
            return offer_uc.config
    except Exception as exc:
        logger.error(f"Failed to check trial offer for {update.effective_user.id}: {exc}")
    return None


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"User {update.effective_user.id} started the bot")

    trial = await _trial_offer(update, context)

    keyboard = []
    if trial is not None:
        keyboard.append([trial_button()])
    keyboard += [
        [InlineKeyboardButton("Купить VPN", callback_data="buy_vpn")],
        [InlineKeyboardButton("Моя подписка", callback_data="my_subscription")],
        [support_button()],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = build_welcome_text(trial)

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
