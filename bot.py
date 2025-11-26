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
    filters,
)

# ========= CONFIG =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DATABASE_URL")

_channel_ids_str = os.getenv("CHANNEL_IDS", "")
CHANNEL_IDS = [int(x.strip()) for x in _channel_ids_str.split(",") if x.strip()]

_admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in _admin_ids_str.split(",") if x.strip()]

TITLE = "Accesso canale premium"
DESC = "Accesso ai contenuti esclusivi per 30 giorni."
PRICE_STARS = 300  # prezzo in Telegram Stars
SUB_DAYS = 30      # durata abbonamento in giorni

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ========= UTILS =========
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ========= DB (PostgreSQL via psycopg 3) =========
def get_conn():
    if not DB_URL:
        raise RuntimeError("DATABASE_URL non impostata!")
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

    # Se nel frattempo ha rinnovato ed è ancora valido, NON kicchiamo
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
            logger.warning("Errore ban utente %s da %s: %s", user_id, ch_id, e)


def schedule_all_kicks(application: Application):
    """
    All'avvio del bot:
    - legge tutte le scadenze dal DB
    - programma i job di kick (anche quelli già scaduti da eseguire subito)
    """
    jq = application.job_queue
    if jq is None:
        logger.warning("JobQueue non disponibile: nessun kick schedulato all'avvio.")
        return

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

        jq.run_once(
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
        provider_token="",  # per Telegram Stars deve essere stringa vuota
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

    # Se aveva ancora abbonamento attivo, estendo da lì; altrimenti parto da adesso
    base = old_expires if (old_expires and old_expires > now) else now
    new_expires = base + timedelta(days=SUB_DAYS)

    set_subscription(user.id, user.username, new_expires)

    # Proviamo a schedulare il kick (se c'è la JobQueue)
    jq = context.application.job_queue
    if jq is not None:
        jq.run_once(
            kick_user_from_all_chats,
            when=new_expires,
            data={"user_id": user.id},
            name=f"kick_{user.id}",
        )
    else:
        logger.warning(
            "JobQueue non disponibile: nessun kick schedulato per user %s.", user.id
        )

    # Crea link di invito con scadenza breve, ma l'accesso reale è controllato dai kick
    invite_expire = now + timedelta(hours=1)
    links = []

    for ch_id in CHANNEL_IDS:
        try:
            # 1) Unban preventivo (in caso fosse stato kiccato in passato)
            try:
                await context.bot.unban_chat_member(chat_id=ch_id, user_id=user.id)
                logger.info("Unban utente %s da chat %s", user.id, ch_id)
            except Exception as e:
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


# ========= COMANDI ADMIN / UTENTE =========
async def subinfo(update: Update, context: CallbackContext):
    """ /subinfo - mostra la propria scadenza. """
    user = update.effective_user
    now = datetime.now(timezone.utc)
    expires = get_expires_at(user.id)

    if not expires:
        await update.message.reply_text("Non risulti avere un abbonamento registrato.")
        return

    if expires <= now:
        await update.message.reply_text(
            f"Il tuo abbonamento è SCADUTO il {expires.strftime('%d/%m/%Y %H:%M UTC')}."
        )
    else:
        await update.message.reply_text(
            f"Il tuo abbonamento è ATTIVO fino al {expires.strftime('%d/%m/%Y %H:%M UTC')}."
        )


async def forcekick(update: Update, context: CallbackContext):
    """
    /forcekick <user_id>
    - Solo admin (ADMIN_IDS)
    - imposta scadenza "now" e programma un kick immediato
    """
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Comando riservato agli admin.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /forcekick <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id non valido. Deve essere un numero.")
        return

    now = datetime.now(timezone.utc)

    # Porta la sua scadenza a "now" per coerenza con la logica del bot
    set_subscription(target_id, None, now)

    jq = context.application.job_queue
    if jq is not None:
        jq.run_once(
            kick_user_from_all_chats,
            when=now + timedelta(seconds=2),
            data={"user_id": target_id},
            name=f"forcekick_{target_id}_{int(now.timestamp())}",
        )
        await update.message.reply_text(
            f"✅ Kick forzato schedulato per user_id {target_id}."
        )
    else:
        await update.message.reply_text(
            "⚠️ JobQueue non disponibile: non posso schedulare il kick automatico."
        )
        logger.warning(
            "JobQueue non disponibile durante forcekick per user %s.", target_id
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

    # Schedula i kick in base a ciò che è nel DB
    schedule_all_kicks(app)

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("subinfo", subinfo))
    app.add_handler(CommandHandler("forcekick", forcekick))

    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    logger.info("Bot avviato e in ascolto...")
    app.run_polling()


if __name__ == "__main__":
    main()
