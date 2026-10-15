import html
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler, filters
from loguru import logger
from src.adapters.db.order_settlement import BALANCE_PHOTO, CancelOutcome, InsufficientBalance
from src.use_cases.order_use_cases import CreateOrderUseCase, UnknownTariffError
from src.domain.models import OrderPayment
from src.domain.tariffs import get_tariff
from src.adapters.tg_bot.support import support_button
from src.use_cases.referral_use_cases import PaymentQuote, PaymentQuoteUseCase

WAITING_FOR_RECEIPT = 1

CARD_NUMBER = "+7 960 992 33 29"

PAY_BALANCE = "pay_balance"
CANCEL_ORDER_PREFIX = "cancel_order_"


def cancel_order_button(order_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton("Отменить заказ", callback_data=f"{CANCEL_ORDER_PREFIX}{order_id}")


def render_payment_screen(tariff_gb: int, price_rub: int, quote: PaymentQuote | None) -> tuple[str, InlineKeyboardMarkup]:
    """Экран оплаты. Без баланса - прежний текст; с балансом - сколько списывается и сколько остаётся оплатить."""
    header = f"<b>{tariff_gb} ГБ - {price_rub} ₽</b>\n\n"
    bonus = f"Бонус по приглашению: +{quote.bonus_gb} ГБ к этому пакету\n\n" if quote and quote.bonus_gb else ""

    if quote is None or quote.balance_rub <= 0:
        text = (
            f"{header}{bonus}"
            "Переведите сумму на карту Т-Банка:\n"
            f"<code>{CARD_NUMBER}</code>\n\n"
            "После перевода отправьте сюда скриншот чека для подтверждения оплаты."
        )
        return text, InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="cancel_payment")]])

    balance_block = (
        f"Баланс: {quote.balance_total} ₽\n"
        f"Списывается с баланса: {quote.balance_rub} ₽\n"
        f"К оплате: <b>{quote.cash_rub} ₽</b>\n\n"
    )
    if quote.covers_fully:
        text = f"{header}{bonus}{balance_block}Перевод не нужен: пакет оплачивается балансом."
        keyboard = [
            [InlineKeyboardButton("Оплатить балансом", callback_data=PAY_BALANCE)],
            [InlineKeyboardButton("Отмена", callback_data="cancel_payment")],
        ]
        return text, InlineKeyboardMarkup(keyboard)

    text = (
        f"{header}{bonus}{balance_block}"
        f"Переведите {quote.cash_rub} ₽ на карту Т-Банка:\n"
        f"<code>{CARD_NUMBER}</code>\n\n"
        "После перевода отправьте сюда скриншот чека для подтверждения оплаты."
    )
    return text, InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="cancel_payment")]])


async def payment_details_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    # Example callback_data: tariff_50
    tariff_gb = int(query.data.split("_")[1])
    tariff = get_tariff(tariff_gb)

    # Save selected tariff in user_data
    context.user_data['selected_tariff'] = tariff_gb

    quote: PaymentQuote | None = None
    quote_uc: PaymentQuoteUseCase | None = context.bot_data.get("quote_uc")
    if quote_uc is not None:
        try:
            quote = await quote_uc.quote(update.effective_user.id, tariff_gb)
        except Exception as exc:
            logger.error(f"Failed to build the payment quote for {update.effective_user.id} ({type(exc).__name__})")
    # Сколько баланса показано на экране: ровно столько удержим при создании заказа
    context.user_data['quoted_balance'] = quote.balance_rub if quote else 0
    context.user_data['quoted_cash'] = quote.cash_rub if quote else tariff.price_rub

    text, reply_markup = render_payment_screen(tariff.gb, tariff.price_rub, quote)
    await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    return WAITING_FOR_RECEIPT


def _payment_lines(payment: OrderPayment | None) -> str:
    if payment is None:
        return ""
    return (
        f"Цена: {payment.price_rub} ₽\n"
        f"Списано с баланса: {payment.balance_rub} ₽\n"
        f"К получению: <b>{payment.cash_rub} ₽</b>\n"
    )


def _admin_markup(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_{order_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{order_id}"),
    ]])


async def receipt_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    photo_file_id = update.message.photo[-1].file_id
    tariff_gb = context.user_data.get('selected_tariff', 0)

    if get_tariff(tariff_gb) is None:
        logger.warning(f"Receipt photo from user {user.id} without a known tariff ({tariff_gb}GB): order not created")
        context.user_data.pop('selected_tariff', None)
        await update.message.reply_text(
            "Пакет не выбран. Нажмите «Купить VPN» и выберите пакет.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Купить VPN", callback_data="buy_vpn")]]),
        )
        return ConversationHandler.END

    if context.user_data.get('quoted_cash') == 0:
        # Баланс покрывает цену целиком: перевод не нужен, заказ оформляется кнопкой «Оплатить балансом»
        await update.message.reply_text(
            "Перевод не нужен: нажмите «Оплатить балансом» на экране оплаты.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Оплатить балансом", callback_data=PAY_BALANCE)]]),
        )
        return WAITING_FOR_RECEIPT

    logger.info(f"Received receipt photo from user {user.id} for tariff {tariff_gb}GB")

    # Use Case injection
    create_order_uc: CreateOrderUseCase = context.bot_data["create_order_uc"]
    admin_id = context.bot_data["config"].telegram_admin_id
    quoted_balance = context.user_data.get('quoted_balance', 0)

    # Create order in DB (with the balance hold, if the balance feature is wired)
    placed = await create_order_uc.place(
        user.id, user.username, tariff_gb, photo_file_id, balance_rub=quoted_balance
    )
    order = placed.order
    payment = placed.payment

    # Notify Admin
    order_type_label = "Продление (перенос остатка)" if order.order_type == "topup" else "Новая покупка"
    trial_repo = context.bot_data.get("trial_repo")
    if order.order_type == "topup" and trial_repo is not None:
        try:
            if await trial_repo.has_unconverted(user.id):
                order_type_label = "Покупка после пробного периода (остаток триала не переносится)"
        except Exception as exc:
            logger.error(f"Failed to check trial state for order {order.id}: {exc}")
    client_display = f"@{html.escape(user.username)}" if user.username else f"ID {user.id}"
    admin_text = (
        f"<b>{order_type_label}</b>\n"
        "────────────\n"
        f"Клиент: {client_display} (ID: <code>{user.id}</code>)\n"
        f"Пакет: <b>{tariff_gb} ГБ</b>\n"
        f"{_payment_lines(payment)}"
        f"Заявка: <code>#{order.id}</code>"
    )

    await context.bot.send_photo(
        chat_id=admin_id,
        photo=photo_file_id,
        caption=admin_text,
        reply_markup=_admin_markup(order.id),
        parse_mode="HTML",
    )

    # Notify User
    user_text = "Заявка принята. Дождитесь подтверждения оплаты."
    user_rows = [[support_button()]]
    if payment is not None:
        if payment.balance_rub > 0:
            user_text += f"\nСписано с баланса: {payment.balance_rub} ₽. К оплате: {payment.cash_rub} ₽."
        expected_cash = get_tariff(tariff_gb).price_rub - quoted_balance
        if payment.cash_rub > expected_cash:
            user_text += (
                f"\nБаланс изменился: сумма к оплате {payment.cash_rub} ₽. "
                "Доплатите разницу или напишите в поддержку."
            )
        user_rows.insert(0, [cancel_order_button(order.id)])
    await update.message.reply_text(user_text, reply_markup=InlineKeyboardMarkup(user_rows))

    # Clear user data
    for key in ('selected_tariff', 'quoted_balance', 'quoted_cash'):
        context.user_data.pop(key, None)
    return ConversationHandler.END


async def pay_balance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Оплата пакета балансом целиком: заказ создаётся с удержанием и подтверждается тем же approve_order."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    # Одноразово: повторное нажатие не создаст второй заказ
    tariff_gb = context.user_data.pop('selected_tariff', 0)
    quoted_balance = context.user_data.pop('quoted_balance', 0)
    context.user_data.pop('quoted_cash', None)

    buy_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Купить VPN", callback_data="buy_vpn")], [support_button()]])
    if get_tariff(tariff_gb) is None:
        await query.message.edit_text("Пакет не выбран. Нажмите «Купить VPN» и выберите пакет.", reply_markup=buy_markup)
        return ConversationHandler.END

    create_order_uc: CreateOrderUseCase = context.bot_data["create_order_uc"]
    admin_uc = context.bot_data["admin_uc"]
    try:
        placed = await create_order_uc.place(
            user.id, user.username, tariff_gb, BALANCE_PHOTO, balance_rub=quoted_balance, require_full=True
        )
    except (InsufficientBalance, UnknownTariffError):
        logger.info(f"Balance payment refused for user {user.id}: balance does not cover the price")
        await query.message.edit_text("Баланс изменился и больше не покрывает цену. Выберите пакет заново.", reply_markup=buy_markup)
        return ConversationHandler.END

    order = placed.order
    logger.info(f"Order {order.id} of user {user.id} paid by balance, approving")
    approved = False
    try:
        approved = await admin_uc.approve_order(order.id)
    except Exception as exc:
        logger.error(f"Approval of balance order {order.id} failed ({type(exc).__name__})")

    if approved:
        await query.message.edit_text("Пакет оплачен с баланса. Ссылка для подключения отправлена сообщением.", reply_markup=None)
        return ConversationHandler.END

    # Сбой выдачи: резерв остаётся, повтор идемпотентен, администратор получает аварийную карточку
    await query.message.edit_text(
        "Пакет оплачен с баланса, но выдача задерживается. Мы уже знаем об этом и скоро выдадим ссылку.",
        reply_markup=InlineKeyboardMarkup([[cancel_order_button(order.id)], [support_button()]]),
    )
    client_display = f"@{html.escape(user.username)}" if user.username else f"ID {user.id}"
    try:
        await context.bot.send_message(
            chat_id=context.bot_data["config"].telegram_admin_id,
            text=(
                "<b>Оплата балансом: выдача не удалась</b>\n"
                "────────────\n"
                f"Клиент: {client_display} (ID: <code>{user.id}</code>)\n"
                f"Пакет: <b>{tariff_gb} ГБ</b>\n"
                f"{_payment_lines(placed.payment)}"
                f"Заявка: <code>#{order.id}</code>"
            ),
            reply_markup=_admin_markup(order.id),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error(f"Failed to send the emergency card for order {order.id}: {exc}")
    return ConversationHandler.END


async def cancel_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пользователь отменяет свой неоплаченный заказ; зарезервированный баланс возвращается."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    raw_id = query.data[len(CANCEL_ORDER_PREFIX):]
    back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="start")], [support_button()]])
    if not raw_id.isdigit():
        return
    admin_uc = context.bot_data["admin_uc"]
    outcome = await admin_uc.cancel_order(int(raw_id), user.id)

    messages = {
        CancelOutcome.CANCELLED: "Заказ отменён. Списанный с баланса остаток возвращён.",
        CancelOutcome.NOT_PENDING: "Заказ уже обработан, отменить его нельзя.",
        CancelOutcome.IN_PROGRESS: "Заказ уже выдаётся, отменить его нельзя. Если что-то не так, напишите в поддержку.",
        CancelOutcome.NOT_OWNER: "Заказ не найден.",
    }
    await query.message.edit_text(messages[outcome], reply_markup=back)

async def cancel_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    for key in ('selected_tariff', 'quoted_balance', 'quoted_cash'):
        context.user_data.pop(key, None)

    keyboard = [
        [InlineKeyboardButton("Купить VPN", callback_data="buy_vpn")],
        [InlineKeyboardButton("Моя подписка", callback_data="my_subscription")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.message.edit_text("Оплата отменена.", reply_markup=reply_markup)
    return ConversationHandler.END

payment_conv_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(payment_details_handler, pattern="^tariff_"),
        CallbackQueryHandler(pay_balance_handler, pattern=f"^{PAY_BALANCE}$"),
    ],
    states={
        WAITING_FOR_RECEIPT: [MessageHandler(filters.PHOTO, receipt_photo_handler)]
    },
    fallbacks=[
        CallbackQueryHandler(cancel_payment_handler, pattern="^cancel_payment$"),
        CallbackQueryHandler(pay_balance_handler, pattern=f"^{PAY_BALANCE}$"),
    ],
)
