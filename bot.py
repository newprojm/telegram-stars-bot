import os
import logging
from datetime import datetime, timedelta

from telegram import Update, LabeledPrice
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackContext,
    PreCheckoutQueryHandler,
    MessageHandler,
    filters,
)

# ========= CONFIG =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
_channel_ids_str = os.getenv("CHANNEL_IDS", "")
CHANNEL_IDS = [int(x.strip()) for x in _channel_ids_str.split(",") if x.strip()]

TITLE = "Accesso canale premium"
DESC = "Accesso ai contenuti esclusivi per 30 giorni."
PRICE_STARS = 300

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def start(update: Update, context: CallbackContext):
    await update.message.reply_text(
        f"Benvenuto 👋\n\n"
        f"Accesso 30 giorni → {PRICE_STARS} ⭐\n"
        f"Usa /buy per procedere."
    )


async def buy(update: Update, context: CallbackContext):
    prices = [LabeledPrice("Accesso 30 giorni", PRICE_STARS)]
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=TITLE,
        description=DESC,
        payload=f"premium_{update.effective_chat.id}",
        provider_token="",
        currency="XTR",
        prices=prices,
    )


async def precheckout_handler(update: Update, context: CallbackContext):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment_handler(update: Update, context: CallbackContext):
    msg = update.message

    expire_date = datetime.utcnow() + timedelta(hours=1)
    links = []

    for ch_id in CHANNEL_IDS:
        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=ch_id,
                expire_date=expire_date,
                member_limit=1,
            )
            links.append(invite.invite_link)
        except Exception as e:
            logger.error(e)

    await msg.reply_text("Pagamento ricevuto! 🎉\n\n" + "\n".join(links))


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN non impostato!")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    app.run_polling()


if __name__ == "__main__":
    main()
