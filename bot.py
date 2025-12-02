import json
import re
import os
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)
from keep_alive import keep_alive

# Levanta el mini servidor SOLO en Replit
if os.environ.get("REPL_ID"):
    keep_alive()


# Levanta el mini servidor (para Replit/UptimeRobot)
keep_alive()

# ------------------ CONFIG ------------------
# IMPORTANTE: el token va en una variable de entorno, NO en el código
BOT_TOKEN = os.environ["BOT_TOKEN"]  # asegúrate de crearla en Secrets

WARNINGS_FILE = "warnings.json"
MAX_WARNINGS = 3
DELETE_AFTER_SECONDS = 10  # 2 minutos
# -------------------------------------------

# Inicializar bot
app = ApplicationBuilder().token(BOT_TOKEN).build()

# Cargar advertencias
try:
    with open(WARNINGS_FILE, "r") as f:
        content = f.read().strip()
        warnings = json.loads(content) if content else {}
except FileNotFoundError:
    warnings = {}


def save_warnings():
    """Guarda el diccionario de advertencias en el archivo."""
    with open(WARNINGS_FILE, "w") as f:
        json.dump(warnings, f)


# Regex para enlaces prohibidos
WHATSAPP_REGEX = re.compile(r"https?://chat\.whatsapp\.com/[^\s]+", re.IGNORECASE)
TELEGRAM_REGEX = re.compile(r"(https?://)?t\.me/\+?[^\s]+", re.IGNORECASE)
URL_SHORTENERS = re.compile(
    r"https?://(bit\.ly|tinyurl\.com|goo\.gl|t\.co|rebrand\.ly)/[^\s]+",
    re.IGNORECASE,
)


def contains_link(message: str) -> bool:
    """Devuelve True si el mensaje contiene un enlace que queremos bloquear."""
    if not message:
        return False

    if (
        WHATSAPP_REGEX.search(message)
        or TELEGRAM_REGEX.search(message)
        or URL_SHORTENERS.search(message)
    ):
        return True

    return False


# --- JOB PARA BORRAR MENSAJES DEL BOT DESPUÉS DE X TIEMPO ---
async def delete_message_later(context: ContextTypes.DEFAULT_TYPE):
    """Callback del JobQueue: borra un mensaje pasado un tiempo."""
    job = context.job
    chat_id = job.data["chat_id"]
    message_id = job.data["message_id"]

    try:
        await context.bot.delete_message(chat_id, message_id)
    except Exception as e:
        # Si ya fue borrado o no hay permisos, no pasa nada
        print(f"Error al borrar mensaje programado: {e}")


# ------------------ COMANDO /warnings ------------------
async def check_user_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /warnings @usuario  |  /warnings nombre
    jq = context.application.job_queue

    if not context.args:
        msg = await update.message.reply_text(
            "Usa: /warnings @usuario o /warnings nombre"
        )
        if jq:
            jq.run_once(
                delete_message_later,
                when=DELETE_AFTER_SECONDS,
                data={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )
        return

    search = " ".join(context.args).lower()
    chat_id = str(update.effective_chat.id)

    user_found = False

    for key, value in warnings.items():
        try:
            chat_key, user_id = key.split(":")
        except ValueError:
            continue

        if chat_key != chat_id:
            continue

        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
        except Exception:
            continue

        name = f"{member.user.first_name} {member.user.last_name or ''}".strip().lower()
        username = (member.user.username or "").lower()

        # Coincidencias:
        # - search == username (sin @)
        # - search contenido en el nombre
        # - search == user_id
        if (
            search == username
            or search in name
            or search == user_id
        ):
            msg = await update.message.reply_text(
                f"{member.user.first_name} lleva {value}/{MAX_WARNINGS} advertencias."
            )
            if jq:
                jq.run_once(
                    delete_message_later,
                    when=DELETE_AFTER_SECONDS,
                    data={"chat_id": msg.chat_id, "message_id": msg.message_id},
                )
            user_found = True
            break

    if not user_found:
        msg = await update.message.reply_text(
            "Ese usuario no tiene advertencias registradas."
        )
        if jq:
            jq.run_once(
                delete_message_later,
                when=DELETE_AFTER_SECONDS,
                data={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )


# ------------------ MANEJO DE LINKS ------------------
async def check_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    # Solo actuar en grupos / supergrupos
    if update.effective_chat.type not in ("group", "supergroup"):
        return

    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    message = update.message.text
    key = f"{chat_id}:{user_id}"

    # Ignorar admins / creador
    #try:
     #   member = await context.bot.get_chat_member(chat_id, user_id)
      #  if member.status in ["administrator", "creator"]:
       #     return
    #except Exception as e:
     #   print(f"Error en get_chat_member: {e}")
      #  return

    if contains_link(message):
        # 1) Intentar borrar el mensaje del usuario
        try:
            await update.message.delete()
        except Exception as e:
            print(f"Error al borrar mensaje del usuario: {e}")

        # 2) Sumar advertencia
        warnings[key] = warnings.get(key, 0) + 1
        save_warnings()

        current_warnings = warnings[key]

        # 3) Avisar al usuario en el grupo
        warning_text = (
            f"🚫 {update.effective_user.first_name}, enviar links de otros grupos no está permitido.\n"
            f"Advertencia {current_warnings}/{MAX_WARNINGS}. "
            "A la tercera se aplica expulsión automática."
        )

        warning_msg = None
        try:
            warning_msg = await context.bot.send_message(chat_id, warning_text)
        except Exception as e:
            print(f"Error al enviar mensaje de advertencia: {e}")

        # 3.1) Programar borrado del mensaje de advertencia
        if warning_msg:
            jq = context.application.job_queue
            if jq:
                jq.run_once(
                    delete_message_later,
                    when=DELETE_AFTER_SECONDS,
                    data={
                        "chat_id": warning_msg.chat_id,
                        "message_id": warning_msg.message_id,
                    },
                )

        # 4) Si llegó al máximo, ban
        if current_warnings >= MAX_WARNINGS:
            try:
                await context.bot.ban_chat_member(chat_id, user_id)
                # Limpiar advertencias de ese usuario en ese grupo
                del warnings[key]
                save_warnings()

                kick_text = (
                    f"{update.effective_user.first_name} ha sido expulsado "
                    "por exceder el límite de advertencias por enviar enlaces "
                    "prohibidos."
                )
                kick_msg = await context.bot.send_message(chat_id, kick_text)

                jq = context.application.job_queue
                if jq:
                    jq.run_once(
                        delete_message_later,
                        when=DELETE_AFTER_SECONDS,
                        data={
                            "chat_id": kick_msg.chat_id,
                            "message_id": kick_msg.message_id,
                        },
                    )

            except Exception as e:
                print(f"Error al banear usuario: {e}")


# ------------------ HANDLERS ------------------
app.add_handler(CommandHandler("warnings", check_user_warnings))
app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), check_links))

# ---------------- RUN BOT -----------------
if __name__ == "__main__":
    print("Bot corriendo...")
    app.run_polling()
