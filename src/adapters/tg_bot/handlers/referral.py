import html

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.adapters.tg_bot.support import support_button
from src.domain.referral_rules import ReferralConfig
from src.use_cases.referral_use_cases import OverviewStatus, ReferralLinkUseCase, ReferralOverview

REF_MENU = "ref_menu"
REF_NEW = "ref_new"
REF_REVOKE_PREFIX = "ref_revoke_"

INVITE_BUTTON_TEXT = "Пригласить друга"
NOT_ELIGIBLE_TEXT = "Приглашения доступны после первой покупки."


def invite_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(INVITE_BUTTON_TEXT, callback_data=REF_MENU)


def render_overview(overview: ReferralOverview, config: ReferralConfig) -> tuple[str, InlineKeyboardMarkup]:
    back_row = [InlineKeyboardButton("🔙 Назад", callback_data="start")]

    if overview.status == OverviewStatus.DISABLED:
        return "Приглашения сейчас недоступны.", InlineKeyboardMarkup([back_row, [support_button()]])
    if overview.status == OverviewStatus.NOT_ELIGIBLE:
        buy_row = [InlineKeyboardButton("Купить VPN", callback_data="buy_vpn")]
        return NOT_ELIGIBLE_TEXT, InlineKeyboardMarkup([buy_row, back_row, [support_button()]])
    if overview.status == OverviewStatus.UNAVAILABLE:
        return (
            "Не удалось получить ссылку. Попробуйте позже.",
            InlineKeyboardMarkup([[invite_button()], back_row, [support_button()]]),
        )

    links_block = "\n".join(f"<code>{html.escape(link.url)}</code>" for link in overview.links)
    text = (
        f"<b>{INVITE_BUTTON_TEXT}</b>\n\n"
        "Ссылка одноразовая: работает для одного друга, пока не использована.\n\n"
        f"{links_block}\n\n"
        f"Приглашено друзей: {overview.invited}\n"
        f"Баланс: {overview.balance} ₽ (можно потратить только на пакеты)\n\n"
        f"Когда друг оплатит первый пакет, к нему добавится {config.invitee_bonus_gb} ГБ, "
        f"а на ваш баланс зачислится {config.reward_percent}% от внесённой им суммы."
    )

    keyboard: list[list[InlineKeyboardButton]] = []
    if overview.can_create:
        keyboard.append([InlineKeyboardButton("Новая ссылка", callback_data=REF_NEW)])
    for index, link in enumerate(overview.links, start=1):
        label = "Отозвать ссылку" if len(overview.links) == 1 else f"Отозвать ссылку {index}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"{REF_REVOKE_PREFIX}{link.id}")])
    keyboard += [back_row, [support_button()]]
    return text, InlineKeyboardMarkup(keyboard)


async def _show(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    link_uc: ReferralLinkUseCase = context.bot_data["referral_link_uc"]
    config: ReferralConfig = context.bot_data["referral_config"]
    logger.info(f"User {user.id} opened the invite screen ({action})")

    try:
        if action == "new":
            overview = await link_uc.create(user.id, user.username)
        elif action.startswith("revoke:"):
            overview = await link_uc.revoke(user.id, int(action.split(":", 1)[1]))
        else:
            overview = await link_uc.show(user.id, user.username)
        text, markup = render_overview(overview, config)
    except Exception as exc:
        logger.error(f"Invite screen failed for user {user.id} ({type(exc).__name__})")
        text = "Произошла ошибка. Попробуйте позже."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="start")], [support_button()]])

    await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


async def referral_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(update, context, "show")


async def referral_new_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(update, context, "new")


async def referral_revoke_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    link_id = update.callback_query.data[len(REF_REVOKE_PREFIX):]
    if not link_id.isdigit():
        await update.callback_query.answer()
        return
    await _show(update, context, f"revoke:{link_id}")
