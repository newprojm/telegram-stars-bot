import os
import logging
from datetime import datetime, timedelta, timezone

import psycopg
from telegram import Update, LabeledPrice
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackContext,
    PreCheckoutQueryHandler,
    MessageHandler,
    JobQueue,
    filters,
)

# ========= CONFIG =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DATABASE_URL")

_channel_ids_str = os.getenv("CHANNEL_IDS", "")
CHANNEL_IDS = [int(x.strip()) for x in _channel_ids_str.split(",") if x.strip()]

TITLE = "Accesso canale premium"
DESC = "Accesso ai contenuti esclusivi per 30 giorni."
PRICE_STARS = 300  # prezzo in Telegram Stars
SUB_DAYS = 30      # durata abbonamento in giorni

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ========= DB (PostgreSQL) =========
def get_conn():
    if not DB_URL:
        raise RuntimeError("DATABASE_URL non impostata!")
    # psycopg 3
    return psycopg.connect(DB_URL)


def init_db():
    """Crea la tabella members se non esiste."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS members (
            user_id    BIGINT PRIMARY KEY,
            username   TEXT,
            expires_at TIMESTAMPTZ
        )
        """
    )
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Tabella 'members' pronta.")


def set_subscription(user_id: int, username: str | None, expires_at: datetime):
    """Imposta/aggiorna la scadenza dell'abbonamento per un utente."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO members (user_id, username, expires_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            username   = EXCLUDED.username,
            expires_at = EXCLUDED.expires_at
        """,
        (user_id, username, expires_at.astimezone(timezone.utc)),
    )
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Set subscription per user_id=%s fino a %s", user_id, expires_at)


def get_expires_at(user_id: int) -> datetime | None:
    """Ritorna la scadenza abbonamento di un utente (UTC) oppure None."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT expires_at FROM members WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row or not row[0]:
        return None
    # row[0] è già un datetime con tz gestito da psycopg2
    return row[0].astimezone(timezone.utc)


def get_all_members():
    """Ritorna lista di (user_id, expires_at) per tutti gli utenti."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, expires_at FROM members")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# ========= JOB: KICK DAI GRUPPI / CANALI =========
async def kick_user_from_all_chats(context: CallbackContext):
    """Job che banna l'utente da tutti i gruppi/canali quando scade l'abbonamento."""
    job = context.job
    user_id = job.data["user_id"]

    now = datetime.now(timezone.utc)
    expires_at = get_expires_at(user_id)

    if expires_at and expires_at > now:
        logger.info(
            "Skip kick per user %s: abbonamento rinnovato (expires_at=%s)",
            user_id,
            expires_at,
        )
        return

    for ch_id in CHANNEL_IDS:
        try:
            await context.bot.ban_chat_member(chat_id=ch_id, user_id=user_id)
            logger.info("Utente %s bannato da chat %s", user_id, ch_id)
        except Exception as e:
            # Può fallire per permessi, tipo di chat, ecc.
            logger.warning("Errore ban utente %s da %s: %s", user_id, ch_id, e)


def schedule_all_kicks(job_queue: JobQueue):
    """
    All'avvio del bot:
    - legge tutte le scadenze dal DB
    - programma i job di kick (anche quelli già scaduti da eseguire subito)
    """
    now = datetime.now(timezone.utc)
    members = get_all_members()
    logger.info("Trovati %s membri in DB per scheduling kick.", len(members))

    for user_id, expires_at in members:
        if not expires_at:
            continue
        expires_at = expires_at.astimezone(timezone.utc)

        if expires_at <= now:
            when = now + timedelta(seconds=5)
        else:
            when = expires_at

        job_queue.run_once(
            kick_user_from_all_chats,
            when=when,
            data={"user_id": user_id},
            name=f"kick_{user_id}",
        )
        logger.info("Kick schedulato per user %s alle %s", user_id, when)


# ========= HANDLER BOT =========
async def start(update: Update, context: CallbackContext):
    await update.message.reply_text(
        f"Benvenuto 👋\n\n"
        f"Accesso 30 giorni → {PRICE_STARS} ⭐\n"
        f"Usa /buy per procedere."
    )


async def buy(update: Update, context: CallbackContext):
    """Avvia il pagamento in Stars."""
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
    """Accetta qualsiasi pre-checkout (controlli aggiuntivi se vuoi)."""
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment_handler(update: Update, context: CallbackContext):
    """Gestisce il pagamento andato a buon fine."""
    msg = update.message
    user = msg.from_user

    now = datetime.now(timezone.utc)
    old_expires = get_expires_at(user.id)

    base = old_expires if (old_expires and old_expires > now) else now
    new_expires = base + timedelta(days=SUB_DAYS)

    set_subscription(user.id, user.username, new_expires)

    context.job_queue.run_once(
        kick_user_from_all_chats,
        when=new_expires,
        data={"user_id": user.id},
        name=f"kick_{user.id}",
    )

    invite_expire = now + timedelta(hours=1)
    links = []

    for ch_id in CHANNEL_IDS:
        try:
            # 1) Unban preventivo (in caso fosse stato kiccato in passato)
            try:
                await context.bot.unban_chat_member(chat_id=ch_id, user_id=user.id)
                logger.info("Unban utente %s da chat %s", user.id, ch_id)
            except Exception as e:
                # Se fallisce perché non era bannato o è un canale con limitazioni, ok
                logger.debug(
                    "Unban non necessario/possibile per utente %s in %s: %s",
                    user.id,
                    ch_id,
                    e,
                )

            # 2) Crea il link di invito
            invite = await context.bot.create_chat_invite_link(
                chat_id=ch_id,
                expire_date=invite_expire,
                member_limit=1,
            )
            links.append(invite.invite_link)
        except Exception as e:
            logger.error("Errore creazione link per chat %s: %s", ch_id, e)

    text_links = "\n".join(links) if links else "Nessun link disponibile, contatta l'admin."
    await msg.reply_text(
        "Pagamento ricevuto! 🎉\n\n"
        f"Hai accesso fino al: {new_expires.strftime('%d/%m/%Y %H:%M UTC')}\n\n"
        f"{text_links}"
    )


# ========= MAIN =========
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN non impostato!")
    if not DB_URL:
        raise RuntimeError("DATABASE_URL non impostata!")
    if not CHANNEL_IDS:
        raise RuntimeError("CHANNEL_IDS non impostato o vuoto!")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    schedule_all_kicks(app.job_queue)

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    logger.info("Bot avviato e in ascolto...")
    app.run_polling()


if __name__ == "__main__":
    main()

if __name__ == "__main__":
    main()
