from telegram import InlineKeyboardButton

SUPPORT_URL = "https://t.me/starArsonist"


def support_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("Поддержка", url=SUPPORT_URL)
