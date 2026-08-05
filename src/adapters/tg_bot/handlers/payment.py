from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler, filters
from loguru import logger
from src.use_cases.order_use_cases import CreateOrderUseCase

WAITING_FOR_RECEIPT = 1

async def payment_details_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    # Example callback_data: tariff_50
    tariff_str = query.data.split("_")[1]
    tariff_gb = int(tariff_str)
    
    # Save selected tariff in user_data
    context.user_data['selected_tariff'] = tariff_gb
    
    prices = {50: 130, 150: 250, 450: 449}
    price = prices.get(tariff_gb, 0)
    
    text = (
        f"Вы выбрали пакет на {tariff_gb} ГБ.\n"
        f"К оплате: {price} руб.\n\n"
        "Реквизиты для перевода:\n"
        "💳 Карта банка: 2200 7008 5236 6417\n\n"
        "После перевода, пожалуйста, отправьте скриншот чека или перевода в этот чат."
    )
    
    keyboard = [
        [InlineKeyboardButton("Отмена", callback_data="cancel_payment")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.message.edit_text(text, reply_markup=reply_markup)
    return WAITING_FOR_RECEIPT

async def receipt_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    photo_file_id = update.message.photo[-1].file_id
    tariff_gb = context.user_data.get('selected_tariff', 0)
    
    logger.info(f"Received receipt photo from user {user.id} for tariff {tariff_gb}GB")
    
    # Use Case injection
    create_order_uc: CreateOrderUseCase = context.bot_data["create_order_uc"]
    admin_id = context.bot_data["config"].telegram_admin_id
    
    # Create order in DB
    order = await create_order_uc.execute(
        user_id=user.id,
        username=user.username,
        tariff_gb=tariff_gb,
        photo_file_id=photo_file_id
    )
    
    # Notify Admin
    admin_text = (
        f"🆕 Новая заявка на покупку VPN\n"
        f"Пользователь: @{user.username} (ID: {user.id})\n"
        f"Тариф: {tariff_gb} ГБ\n"
        f"Order ID: {order.id}"
    )
    
    admin_keyboard = [
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_{order.id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{order.id}")
        ]
    ]
    admin_markup = InlineKeyboardMarkup(admin_keyboard)
    
    await context.bot.send_photo(
        chat_id=admin_id,
        photo=photo_file_id,
        caption=admin_text,
        reply_markup=admin_markup
    )
    
    # Notify User
    await update.message.reply_text(
        "Спасибо! После подтверждения оплаты вы получите ссылку для подключения."
    )
    
    # Clear user data
    context.user_data.pop('selected_tariff', None)
    return ConversationHandler.END

async def cancel_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    context.user_data.pop('selected_tariff', None)
    
    keyboard = [
        [InlineKeyboardButton("Купить VPN", callback_data="buy_vpn")],
        [InlineKeyboardButton("Моя подписка", callback_data="my_subscription")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.message.edit_text("Оплата отменена.", reply_markup=reply_markup)
    return ConversationHandler.END

payment_conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(payment_details_handler, pattern="^tariff_")],
    states={
        WAITING_FOR_RECEIPT: [MessageHandler(filters.PHOTO, receipt_photo_handler)]
    },
    fallbacks=[CallbackQueryHandler(cancel_payment_handler, pattern="^cancel_payment$")],
)
