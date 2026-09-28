from telegram import Update
from telegram.ext import ContextTypes
from loguru import logger
from src.use_cases.admin_use_cases import AdminUseCases

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
            await query.edit_message_caption(
                caption=f"{query.message.caption}\n\n✅ <b>Подтверждено</b>",
                reply_markup=None,
                parse_mode="HTML",
            )
        else:
            await query.message.reply_text("⚠️ Ошибка при подтверждении заказа (возможно он уже обработан).")

    elif action == "reject":
        success = await admin_uc.reject_order(order_id)
        if success:
            await query.edit_message_caption(
                caption=f"{query.message.caption}\n\n❌ <b>Отклонено</b>",
                reply_markup=None,
                parse_mode="HTML",
            )
        else:
            await query.message.reply_text("⚠️ Ошибка при отклонении заказа (возможно он уже обработан).")
