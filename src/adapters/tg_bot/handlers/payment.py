import html
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler, filters
from loguru import logger
from src.use_cases.order_use_cases import CreateOrderUseCase
from src.domain.tariffs import get_tariff
from src.adapters.tg_bot.support import support_button

WAITING_FOR_RECEIPT = 1

CARD_NUMBER = "2200 7008 5236 6417"


async def payment_details_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    # Example callback_data: tariff_50
    tariff_gb = int(query.data.split("_")[1])
    tariff = get_tariff(tariff_gb)

    # Save selected tariff in user_data
    context.user_data['selected_tariff'] = tariff_gb

    text = (
        f"<b>{tariff.gb} ГБ - {tariff.price_rub} ₽</b>\n\n"
        "Переведите сумму на карту Т-Банка:\n"
        f"<code>{CARD_NUMBER}</code>\n\n"
        "После перевода отправьте сюда скриншот чека для подтверждения оплаты."
    )

    keyboard = [
        [InlineKeyboardButton("Отмена", callback_data="cancel_payment")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
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
    order_type_label = "Продление (перенос остатка)" if order.order_type == "topup" else "Новая покупка"
    client_display = f"@{html.escape(user.username)}" if user.username else f"ID {user.id}"
    admin_text = (
        f"<b>{order_type_label}</b>\n"
        "────────────\n"
        f"Клиент: {client_display} (ID: <code>{user.id}</code>)\n"
        f"Пакет: <b>{tariff_gb} ГБ</b>\n"
        f"Заявка: <code>#{order.id}</code>"
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
        reply_markup=admin_markup,
        parse_mode="HTML",
    )

    # Notify User
    await update.message.reply_text(
        "Заявка принята. Ждём подтверждения от администратора.",
        reply_markup=InlineKeyboardMarkup([[support_button()]]),
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
