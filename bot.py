import os
import logging
from datetime import datetime, timedelta, timezone

import psycopg
from telegram import (
    Update,
    LabeledPrice,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackContext,
    PreCheckoutQueryHandler,
    MessageHandler,
    CallbackQueryHandler,
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
PRICE_STARS = 300
SUB_DAYS = 30

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
    """Crea le tabelle se non esistono."""
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

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS manual_requests (
            id           BIGSERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL,
            username     TEXT,
            code         TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'PENDING', -- PENDING/APPROVED/REJECTED
            requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            decided_at   TIMESTAMPTZ
        )
        """
    )

    # Un solo PENDING per utente (parziale, PostgreSQL)
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_manual_requests_pending_user
        ON manual_requests (user_id)
        WHERE status = 'PENDING'
        """
    )

    conn.commit()
    cur.close()
    conn.close()
    logger.info("Tabelle pronte: members, manual_requests.")


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


# ========= MANUAL REQUESTS =========
def upsert_pending_manual_request(user_id: int, username: str | None, code: str) -> int:
    """
    Garantisce 1 sola richiesta PENDING per utente.
    Se esiste già PENDING, aggiorna code/username/requested_at.
    Ritorna req_id.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO manual_requests (user_id, username, code, status, requested_at)
        VALUES (%s, %s, %s, 'PENDING', NOW())
        ON CONFLICT ON CONSTRAINT uq_manual_requests_pending_user
        DO UPDATE SET
            username     = EXCLUDED.username,
            code         = EXCLUDED.code,
            requested_at = NOW()
        RETURNING id
        """,
        (user_id, username, code),
    )
    req_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return req_id


def get_pending_manual_request(user_id: int) -> tuple[int, str, str, str | None] | None:
    """
    Ritorna (id, code, status, username) dell'ultima richiesta PENDING dell'utente.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, code, status, username
        FROM manual_requests
        WHERE user_id=%s AND status='PENDING'
        ORDER BY requested_at DESC
        LIMIT 1
        """,
        (user_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row  # None oppure (id, code, status, username)


def decide_manual_request(req_id: int, status: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE manual_requests
        SET status=%s, decided_at=NOW()
        WHERE id=%s
        """,
        (status, req_id),
    )
    conn.commit()
    cur.close()
    conn.close()


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

        when = (now + timedelta(seconds=5)) if expires_at <= now else expires_at

        jq.run_once(
            kick_user_from_all_chats,
            when=when,
            data={"user_id": user_id},
            name=f"kick_{user_id}",
        )
        logger.info("Kick schedulato per user %s alle %s", user_id, when)


# ========= CORE: GRANT ACCESS (riusata da Stars e Manuale) =========
async def grant_access(user_id: int, username: str | None, context: CallbackContext) -> datetime:
    """
    Estende/attiva l'accesso, schedula kick, crea link di invito e li invia all'utente.
    Ritorna new_expires.
    """
    now = datetime.now(timezone.utc)
    old_expires = get_expires_at(user_id)

    base = old_expires if (old_expires and old_expires > now) else now
    new_expires = base + timedelta(days=SUB_DAYS)

    set_subscription(user_id, username, new_expires)

    jq = context.application.job_queue
    if jq is not None:
        jq.run_once(
            kick_user_from_all_chats,
            when=new_expires,
            data={"user_id": user_id},
            name=f"kick_{user_id}",
        )
    else:
        logger.warning("JobQueue non disponibile: nessun kick schedulato per user %s.", user_id)

    # Link invito (scadenza breve)
    invite_expire = now + timedelta(hours=1)
    links = []

    for ch_id in CHANNEL_IDS:
        try:
            # Unban preventivo
            try:
                await context.bot.unban_chat_member(chat_id=ch_id, user_id=user_id)
            except Exception:
                pass

            invite = await context.bot.create_chat_invite_link(
                chat_id=ch_id,
                expire_date=invite_expire,
                member_limit=1,
            )
            links.append(invite.invite_link)
        except Exception as e:
            logger.error("Errore creazione link per chat %s: %s", ch_id, e)

    text_links = "\n".join(links) if links else "Nessun link disponibile, contatta l'admin."

    await context.bot.send_message(
        chat_id=user_id,
        text=(
            "Accesso attivato! 🎉\n\n"
            f"Hai accesso fino al: {new_expires.strftime('%d/%m/%Y %H:%M UTC')}\n\n"
            f"{text_links}"
        ),
    )

    return new_expires


# ========= HANDLER BOT =========
async def start(update: Update, context: CallbackContext):
    await update.message.reply_text(
        f"Benvenuto 👋\n\n"
        f"Accesso 30 giorni → {PRICE_STARS} ⭐\n"
        f"Usa /buy per scegliere il metodo."
    )


async def buy(update: Update, context: CallbackContext):
    """Mostra 2 opzioni: Stars automatico e Manuale (codice + approvazione admin)."""
    keyboard = [
        [InlineKeyboardButton(f"Paga {PRICE_STARS}⭐ (Telegram Stars)", callback_data="pay_stars")],
        [InlineKeyboardButton("Pagamento manuale (inserisci codice)", callback_data="pay_manual")],
    ]
    await update.message.reply_text(
        "Scegli il metodo di pagamento:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def buy_choice_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    await q.answer()

    if q.data == "pay_stars":
        prices = [LabeledPrice("Accesso 30 giorni", PRICE_STARS)]
        await context.bot.send_invoice(
            chat_id=q.message.chat_id,
            title=TITLE,
            description=DESC,
            payload=f"premium_{q.message.chat_id}",
            provider_token="",  # Stars
            currency="XTR",
            prices=prices,
        )
        return

    if q.data == "pay_manual":
        await q.message.reply_text(
            "Ok ✅\n\n"
            "Inserisci il *codice* che ti ha dato l'admin con:\n"
            "`/redeem IL_TUO_CODICE`\n\n"
            "Esempio: `/redeem ABCD-1234`",
            parse_mode="Markdown",
        )


async def precheckout_handler(update: Update, context: CallbackContext):
    """Accetta qualsiasi pre-checkout (aggiungi controlli se vuoi)."""
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment_handler(update: Update, context: CallbackContext):
    """Gestisce il pagamento Stars andato a buon fine."""
    msg = update.message
    user = msg.from_user

    new_expires = await grant_access(user.id, user.username, context)

    await msg.reply_text(
        "Pagamento ricevuto! 🎉\n"
        f"Accesso attivo fino al: {new_expires.strftime('%d/%m/%Y %H:%M UTC')}\n"
        "(Ti ho inviato i link in chat.)"
    )


# ========= MANUALE: UTENTE /REDEEM =========
async def redeem(update: Update, context: CallbackContext):
    """
    /redeem CODICE
    - salva/aggiorna richiesta PENDING (una sola per utente)
    - notifica agli admin con pulsanti Approva/Rifiuta
    """
    if not context.args:
        await update.message.reply_text("Uso: /redeem CODICE")
        return

    code = context.args[0].strip()
    user = update.effective_user

    req_id = upsert_pending_manual_request(user.id, user.username, code)

    await update.message.reply_text(
        "Richiesta inviata ✅\n"
        "Appena l’admin approva, riceverai i link di accesso.\n\n"
        "Nota: se reinvii /redeem, aggiorni la richiesta in sospeso."
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approva", callback_data=f"man_approve:{user.id}:{req_id}"),
        InlineKeyboardButton("❌ Rifiuta", callback_data=f"man_reject:{user.id}:{req_id}"),
    ]])

    text = (
        "🧾 Richiesta pagamento manuale (PENDING)\n"
        f"• user: {user.full_name}\n"
        f"• username: @{user.username}\n" if user.username else f"• username: (none)\n"
        f"• user_id: {user.id}\n"
        f"• code: {code}\n"
        f"• req_id: {req_id}\n"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard)
        except Exception as e:
            logger.warning("Errore notifica admin %s: %s", admin_id, e)


# ========= MANUALE: CALLBACK ADMIN APPROVA/RIFIUTA =========
async def manual_admin_callback(update: Update, context: CallbackContext):
    """
    Callback data:
    - man_approve:<user_id>:<req_id>
    - man_reject:<user_id>:<req_id>
    """
    q = update.callback_query
    admin = q.from_user
    await q.answer()

    if not is_admin(admin.id):
        try:
            await q.edit_message_text("❌ Non sei autorizzato.")
        except Exception:
            pass
        return

    try:
        action, user_id_str, req_id_str = q.data.split(":", 2)
        user_id = int(user_id_str)
        req_id = int(req_id_str)
    except Exception:
        try:
            await q.edit_message_text("⚠️ Callback non valido.")
        except Exception:
            pass
        return

    # Verifica che la richiesta sia ancora PENDING
    pending = get_pending_manual_request(user_id)
    if not pending:
        try:
            await q.edit_message_text("⚠️ Nessuna richiesta PENDING trovata (forse già gestita).")
        except Exception:
            pass
        return

    pending_id, pending_code, pending_status, pending_username = pending
    if pending_id != req_id or pending_status != "PENDING":
        try:
            await q.edit_message_text("⚠️ Questa richiesta non è più valida o non è PENDING.")
        except Exception:
            pass
        return

    if action == "man_reject":
        decide_manual_request(req_id, "REJECTED")
        try:
            await q.edit_message_text(
                f"❌ RIFIUTATO\nuser_id={user_id}\nreq_id={req_id}\ncode={pending_code}"
            )
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Codice rifiutato. Contatta l’admin se pensi sia un errore."
            )
        except Exception:
            pass
        return

    if action == "man_approve":
        decide_manual_request(req_id, "APPROVED")

        try:
            new_expires = await grant_access(user_id, pending_username, context)
        except Exception as e:
            logger.exception("Errore grant_access per user %s: %s", user_id, e)
            try:
                await q.edit_message_text(
                    f"⚠️ APPROVATO ma errore nell'attivazione.\nuser_id={user_id}\nreq_id={req_id}\nDettagli log."
                )
            except Exception:
                pass
            return

        try:
            await q.edit_message_text(
                "✅ APPROVATO\n"
                f"user_id={user_id}\n"
                f"req_id={req_id}\n"
                f"code={pending_code}\n"
                f"scadenza={new_expires.strftime('%d/%m/%Y %H:%M UTC')}"
            )
        except Exception:
            pass
        return


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

    # Porta la scadenza a "now"
    set_subscription(target_id, None, now)

    jq = context.application.job_queue
    if jq is not None:
        jq.run_once(
            kick_user_from_all_chats,
            when=now + timedelta(seconds=2),
            data={"user_id": target_id},
            name=f"forcekick_{target_id}_{int(now.timestamp())}",
        )
        await update.message.reply_text(f"✅ Kick forzato schedulato per user_id {target_id}.")
    else:
        await update.message.reply_text("⚠️ JobQueue non disponibile: non posso schedulare il kick automatico.")
        logger.warning("JobQueue non disponibile durante forcekick per user %s.", target_id)


# ========= MAIN =========
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN non impostato!")
    if not DB_URL:
        raise RuntimeError("DATABASE_URL non impostata!")
    if not CHANNEL_IDS:
        raise RuntimeError("CHANNEL_IDS non impostato o vuoto!")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS non impostato o vuoto!")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Schedula i kick in base a ciò che è nel DB
    schedule_all_kicks(app)

    # Handlers base
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CallbackQueryHandler(buy_choice_callback, pattern=r"^pay_(stars|manual)$"))

    # Manuale
    app.add_handler(CommandHandler("redeem", redeem))
    app.add_handler(CallbackQueryHandler(manual_admin_callback, pattern=r"^man_(approve|reject):"))

    # Info/admin
    app.add_handler(CommandHandler("subinfo", subinfo))
    app.add_handler(CommandHandler("forcekick", forcekick))

    # Stars payment
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    logger.info("Bot avviato e in ascolto...")
    app.run_polling()


if __name__ == "__main__":
    main()
