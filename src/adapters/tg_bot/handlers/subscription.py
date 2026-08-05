from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from loguru import logger
from src.use_cases.traffic_use_cases import CheckTrafficUseCase

async def my_subscription_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    logger.info(f"User {user_id} requested subscription info")
    
    traffic_uc: CheckTrafficUseCase = context.bot_data["traffic_uc"]
    
    try:
        data = await traffic_uc.execute(user_id)
        
        if not data:
            text = (
                "У вас нет активной подписки VPN.\n"
                "Чтобы приобрести доступ, нажмите 'Купить VPN'."
            )
            keyboard = [
                [InlineKeyboardButton("Купить VPN", callback_data="buy_vpn")],
                [InlineKeyboardButton("🔙 Назад", callback_data="start")]
            ]
        else:
            text = (
                "📊 **Информация о подписке**\n\n"
                f"Использовано:  `{data['used_gb']} ГБ`\n"
                f"Лимит:  `{data['limit_gb']} ГБ`\n"
                f"Остаток:  `{data['remaining_gb']} ГБ`\n\n"
                "Ваша ссылка (ключ) для подключения:\n"
                f"`{data['sub_url']}`"
            )
            keyboard = [
                [InlineKeyboardButton("🔙 Назад", callback_data="start")]
            ]
            
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Failed to fetch subscription for {user_id}: {e}")
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="start")]]
        await query.message.edit_text(
            "Произошла ошибка при получении данных о подписке. Пожалуйста, попробуйте позже.", 
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
