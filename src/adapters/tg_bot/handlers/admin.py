import html
from telegram import Update
from telegram.ext import ContextTypes
from loguru import logger
from src.use_cases.admin_use_cases import AdminUseCases
from src.use_cases.referral_use_cases import BalanceOverview, BalanceOverviewUseCase


async def _append_to_card(query, suffix: str) -> None:
    """Дописывает решение к карточке заказа: подпись фото или текст (карточка заказа, оплаченного балансом, без фото)."""
    message = query.message
    if message.caption is not None or getattr(message, "photo", None):
        await query.edit_message_caption(
            caption=f"{message.caption or ''}\n\n{suffix}",
            reply_markup=None,
            parse_mode="HTML",
        )
    else:
        await query.edit_message_text(
            text=f"{message.text_html}\n\n{suffix}",
            reply_markup=None,
            parse_mode="HTML",
        )


async def admin_decision_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    admin_id = update.effective_user.id
    config = context.bot_data["config"]

    # Check if user is actually admin
    if admin_id != config.telegram_admin_id:
        logger.warning(f"Unauthorized admin action attempt by user {admin_id}")
        await query.message.reply_text("⛔ У вас нет прав администратора.")
        return

    # callback_data is either "approve_{order_id}" or "reject_{order_id}"
    action, order_id_str = query.data.split("_")
    order_id = int(order_id_str)

    admin_uc: AdminUseCases = context.bot_data["admin_uc"]

    if action == "approve":
        success = await admin_uc.approve_order(order_id)
        if success:
            await _append_to_card(query, "✅ <b>Подтверждено</b>")
        else:
            await query.message.reply_text("⚠️ Ошибка при подтверждении заказа (возможно он уже обработан).")

    elif action == "reject":
        success = await admin_uc.reject_order(order_id)
        if success:
            await _append_to_card(query, "❌ <b>Отклонено</b>")
        else:
            await query.message.reply_text("⚠️ Ошибка при отклонении заказа (возможно он уже обработан).")


def render_balance_overview(user_id: int, overview: BalanceOverview) -> str:
    lines = [f"<b>Баланс ID {user_id}</b>: {overview.balance} ₽"]
    if overview.reserves:
        reserved = ", ".join(f"#{order_id} - {amount} ₽" for order_id, amount in overview.reserves)
        lines.append(f"Резерв под ожидающие заказы: {reserved}")
    else:
        lines.append("Резервов под ожидающие заказы нет")
    if overview.entries:
        lines.append("\nПоследние записи:")
        for entry in overview.entries:
            created = entry.created_at.strftime("%d.%m.%Y %H:%M") if entry.created_at else "-"
            ref = f" (#{entry.ref_id})" if entry.ref_id is not None else ""
            lines.append(f"{created} | {html.escape(entry.kind)}{ref} | {entry.amount_rub:+d} ₽")
    else:
        lines.append("\nЗаписей нет")
    return "\n".join(lines)


async def balance_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/balance <telegram_id> - только просмотр: баланс, резервы и последние записи журнала."""
    admin_id = update.effective_user.id
    if admin_id != context.bot_data["config"].telegram_admin_id:
        logger.warning(f"Unauthorized /balance attempt by user {admin_id}")
        await update.message.reply_text("⛔ У вас нет прав администратора.")
        return

    args = getattr(context, "args", None) or []
    if len(args) != 1 or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Использование: /balance &lt;telegram_id&gt;", parse_mode="HTML")
        return

    user_id = int(args[0])
    overview_uc: BalanceOverviewUseCase = context.bot_data["balance_overview_uc"]
    overview = await overview_uc.overview(user_id)
    logger.info(f"Admin viewed the balance of user {user_id}")
    await update.message.reply_text(render_balance_overview(user_id, overview), parse_mode="HTML")
