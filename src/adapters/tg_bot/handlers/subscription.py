from datetime import datetime, timezone
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from loguru import logger
from src.use_cases.traffic_use_cases import CheckTrafficUseCase
from src.adapters.tg_bot.support import support_button

STATUS_LABELS = {
    "active": "🟢 Активна",
    "limited": "🟠 Лимит трафика исчерпан",
    "expired": "🔴 Истекла",
    "disabled": "⚪ Отключена",
    "on_hold": "🟡 На паузе",
}

PROGRESS_BAR_WIDTH = 10


def _progress_bar(used_gb: float, limit_gb: float) -> str:
    ratio = min(1.0, used_gb / limit_gb) if limit_gb else 0.0
    filled = round(ratio * PROGRESS_BAR_WIDTH)
    return "▰" * filled + "▱" * (PROGRESS_BAR_WIDTH - filled)


def _format_expire(expire_at: int | None) -> str:
    if not expire_at:
        return "-"
    expire_dt = datetime.fromtimestamp(expire_at, tz=timezone.utc)
    days_left = (expire_dt - datetime.now(timezone.utc)).days
    date_str = expire_dt.strftime("%d.%m.%Y")
    if days_left > 0:
        return f"{date_str} (осталось {days_left} дн.)"
    return f"{date_str} (срок истёк)"


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
                "У вас нет активной подписки.\n"
                "Чтобы приобрести доступ, нажмите «Купить VPN»."
            )
            keyboard = [
                [InlineKeyboardButton("Купить VPN", callback_data="buy_vpn")],
                [InlineKeyboardButton("🔙 Назад", callback_data="start")],
                [support_button()],
            ]
        else:
            status_label = STATUS_LABELS.get(data["status"], data["status"])

            if data["is_unlimited"]:
                usage_block = f"Использовано: <b>{data['used_gb']} ГБ</b> · Лимит: <b>безлимит</b>\n"
            else:
                bar = _progress_bar(data["used_gb"], data["limit_gb"])
                usage_block = (
                    f"<code>{bar}</code>\n\n"
                    f"Использовано:  <b>{data['used_gb']} ГБ</b>\n"
                    f"Осталось:      <b>{data['remaining_gb']} ГБ</b>\n"
                    f"Лимит:         <b>{data['limit_gb']} ГБ</b>\n"
                )

            text = (
                "<b>Ваша подписка</b>\n"
                f"Статус: {status_label}\n\n"
                f"{usage_block}"
                f"Действует до:  <b>{_format_expire(data['expire_at'])}</b>\n\n"
                "Ссылка для подключения:\n"
                f"<code>{data['sub_url']}</code>"
            )
            keyboard = [
                [InlineKeyboardButton("Продлить / докупить", callback_data="buy_vpn")],
                [InlineKeyboardButton("🔙 Назад", callback_data="start")],
                [support_button()],
            ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Failed to fetch subscription for {user_id}: {e}")
        keyboard = [
            [InlineKeyboardButton("🔙 Назад", callback_data="start")],
            [support_button()],
        ]
        await query.message.edit_text(
            "Произошла ошибка при получении данных о подписке. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
