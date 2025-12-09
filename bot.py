import json
import re
import os
import sys
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

# ------------------ CONFIG ------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]  # asegúrate de crearla en Secrets

WARNINGS_FILE = "warnings.json"
MAX_WARNINGS = 3
DELETE_AFTER_SECONDS = 120  # 2 minutos
# -------------------------------------------

# Inicializar bot
app = ApplicationBuilder().token(BOT_TOKEN).build()

# <<< NUEVO: helper para detectar admin normal o anónimo
async def es_admin_o_anon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    # Intentamos traer la lista de admins del chat
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
    except Exception as e:
        print(f"Error obteniendo administradores: {e}", file=sys.stderr)
        admins = []

    admin_ids = {a.user.id for a in admins if a and a.user}

    # Caso 1: user "normal" y es admin
    if user and user.id in admin_ids:
        return True

    # Caso 2: mensaje enviado "en nombre del grupo" (admin anónimo)
    # Cuando eres admin anónimo, el mensaje viene como sender_chat = grupo.
    if msg and msg.sender_chat and msg.sender_chat.id == chat.id:
        return True

    return False
# >>> FIN NUEVO


# Cargar advertencias desde archivo
try:
    with open(WARNINGS_FILE, "r") as f:
        content = f.read().strip()
        warnings = json.loads(content) if content else {}
except FileNotFoundError:
    warnings = {}

# Registro de usuarios conocidos por chat
# key: "chat_id:user_id" -> {"full_name": str, "username": str, "user_id": str}
known_users = {}


def save_warnings():
    """Guarda el diccionario de advertencias en el archivo y muestra logs útiles."""
    try:
        with open(WARNINGS_FILE, "w") as f:
            json.dump(warnings, f)
            f.flush()
            os.fsync(f.fileno())
        print("WARNINGS GUARDADOS:", warnings)
        print("Archivo warnings.json en:", os.path.abspath(WARNINGS_FILE))
    except Exception as e:
        print(f"Error guardando warnings: {e}", file=sys.stderr)


def register_user(chat_id: str, user):
    """
    Registra/actualiza info básica de un usuario por chat.
    chat_id: str (id del grupo)
    user: objeto telegram.User
    """
    if user is None:
        return

    key = f"{chat_id}:{user.id}"
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    username = user.username or ""

    known_users[key] = {
        "full_name": full_name,
        "username": username,
        "user_id": str(user.id),
    }


def find_users_in_chat_by_query(chat_id: str, query: str):
    """
    Busca usuarios en ESTE chat cuyo nombre/username coincida con query.
    Devuelve lista de tuplas (user_id:str, data:dict)
    """
    q = (query or "").strip().lstrip("@").lower()
    matches = []

    if not q:
        return matches

    for key, data in known_users.items():
        try:
            chat_key, user_id = key.split(":")
        except ValueError:
            continue

        if chat_key != chat_id:
            continue

        name = (data.get("full_name") or "").lower()
        username = (data.get("username") or "").lower()
        stored_user_id = data.get("user_id") or user_id

        if (
            q == username
            or q in name
            or q == stored_user_id
        ):
            matches.append((stored_user_id, data))

    return matches


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
    """
    /warnings (como reply)  -> revisa warnings del usuario del mensaje respondido
    /warnings juanito       -> busca en usuarios conocidos del chat que coincidan con 'juanito'
    """
    jq = context.application.job_queue
    chat_id = str(update.effective_chat.id)
    message = update.message

    # Borrar el mensaje del usuario inmediatamente
    try:
        await message.delete()
    except Exception:
        pass

    # Registrar al que ejecuta el comando
    if update.effective_user:
        register_user(chat_id, update.effective_user)

    # CASO A: /warnings sin argumentos pero en reply a un mensaje
    if (not context.args) and message and message.reply_to_message:
        target_user = message.reply_to_message.from_user
        register_user(chat_id, target_user)

        user_id = str(target_user.id)
        key = f"{chat_id}:{user_id}"
        current_warnings = warnings.get(key, 0)

        text = (
            f"{target_user.first_name} va "
            f"{current_warnings} de {MAX_WARNINGS}. No me tientes 😌"
        )
        msg = await context.bot.send_message(chat_id, text)

        if jq:
            jq.run_once(
                delete_message_later,
                when=DELETE_AFTER_SECONDS,
                data={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )
        return

    # CASO B: /warnings sin args y sin reply -> explicar uso
    if not context.args:
        msg = await context.bot.send_message(
            chat_id,
            "Usa /warnings respondiendo al mensaje de alguien,\n"
            "o /warnings nombre/usuario (ej. /warnings juanito)."
        )
        if jq:
            jq.run_once(
                delete_message_later,
                when=DELETE_AFTER_SECONDS,
                data={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )
        return

    # CASO C: /warnings juanito (con texto)
    search = " ".join(context.args)
    matches = find_users_in_chat_by_query(chat_id, search)

    # Nadie coincide → no existe “juanito” en el registro del bot
    if not matches:
        msg = await context.bot.send_message(
            chat_id,
            f"No encontré a nadie en este grupo que coincida con “{search}”."
        )
        if jq:
            jq.run_once(
                delete_message_later,
                when=DELETE_AFTER_SECONDS,
                data={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )
        return

    # Varias coincidencias
    if len(matches) > 1:
        if len(matches) > 5:
            msg_text = (
                f"Encontré varios usuarios que coinciden con “{search}”.\n"
                "Sé más específico o responde al mensaje de la persona y usa /warnings."
            )
        else:
            lista = []
            for uid, data in matches:
                full_name = data.get("full_name") or "(sin nombre)"
                username = data.get("username")
                if username:
                    lista.append(f"- {full_name} (@{username})")
                else:
                    lista.append(f"- {full_name}")

            msg_text = (
                "Encontré varios posibles:\n"
                + "\n".join(lista)
                + "\n\nResponde directamente al mensaje de la persona y usa /warnings "
                  "para ver sus advertencias."
            )

        msg = await context.bot.send_message(chat_id, msg_text)
        if jq:
            jq.run_once(
                delete_message_later,
                when=DELETE_AFTER_SECONDS,
                data={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )
        return

    # CASO D: exactamente 1 match → revisamos sus warnings (aunque tenga 0)
    user_id, data = matches[0]
    key = f"{chat_id}:{user_id}"
    current_warnings = warnings.get(key, 0)

    nombre = data.get("full_name") or data.get("username") or "Este usuario"
    text = f"{nombre} trae {current_warnings} de {MAX_WARNINGS}… ojo ahí 👀"

    msg = await context.bot.send_message(chat_id, text)
    if jq:
        jq.run_once(
            delete_message_later,
            when=DELETE_AFTER_SECONDS,
            data={"chat_id": msg.chat_id, "message_id": msg.message_id},
        )

# ------------------ COMANDO /unwarn ------------------
async def unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /unwarn (respondiendo a un mensaje) -> limpia warnings del usuario respondido
    /unwarn nombre_o_username         -> limpia warnings del usuario encontrado por nombre
    Solo admins/creador pueden usarlo.
    """
    jq = context.application.job_queue
    chat_id = str(update.effective_chat.id)
    message = update.message
    user_id = str(update.effective_user.id)

    # <<< NUEVO: precalcular si es admin normal o anónimo
    es_admin = await es_admin_o_anon(update, context)
    # >>> FIN NUEVO

    # Borrar el comando del chat para no ensuciar
    try:
        await message.delete()
    except Exception:
        pass

    # Validar que quien lo usa sea admin/creador
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        # <<< NUEVO: si no sale admin en get_chat_member, pero sí es admin anónimo, lo dejamos pasar
        if member.status not in ["administrator", "creator"] and not es_admin:
            msg = await context.bot.send_message(
                chat_id,
                "Solo admins pueden usar /unwarn."
            )
            if jq:
                jq.run_once(
                    delete_message_later,
                    when=DELETE_AFTER_SECONDS,
                    data={"chat_id": msg.chat_id, "message_id": msg.message_id},
                )
            return
        # >>> FIN NUEVO
    except Exception as e:
        print("Error en admin-check /unwarn:", e)
        # <<< NUEVO: fallback, si no podemos leer get_chat_member,
        # pero sí es admin/anónimo según es_admin_o_anon, continuamos.
        if not es_admin:
            return
        # >>> FIN NUEVO

    target_user_id = None
    display_name = None

    # Caso A: /unwarn como reply (sin argumentos)
    if message.reply_to_message and not context.args:
        target = message.reply_to_message.from_user
        target_user_id = str(target.id)
        display_name = target.first_name

    # Caso B: /unwarn con texto (sin reply) -> buscar por nombre/usuario
    else:
        if not context.args:
            msg = await context.bot.send_message(
                chat_id,
                "Usa /unwarn respondiendo al mensaje de alguien\n"
                "o /unwarn nombre_o_usuario (ej. /unwarn @juanito o /unwarn juan)."
            )
            if jq:
                jq.run_once(
                    delete_message_later,
                    when=DELETE_AFTER_SECONDS,
                    data={"chat_id": msg.chat_id, "message_id": msg.message_id},
                )
            return

        search = " ".join(context.args)
        matches = find_users_in_chat_by_query(chat_id, search)

        if not matches:
            msg = await context.bot.send_message(
                chat_id,
                f"No encontré a nadie en este grupo que coincida con “{search}”."
            )
            if jq:
                jq.run_once(
                    delete_message_later,
                    when=DELETE_AFTER_SECONDS,
                    data={"chat_id": msg.chat_id, "message_id": msg.message_id},
                )
            return

        if len(matches) > 1:
            if len(matches) > 5:
                msg_text = (
                    f"Encontré varios usuarios que coinciden con “{search}”.\n"
                    "Sé más específico (ej. nombre y apellido o username completo)."
                )
            else:
                lista = []
                for uid, data in matches:
                    full_name = data.get("full_name") or "(sin nombre)"
                    username = data.get("username")
                    if username:
                        lista.append(f"- {full_name} (@{username})")
                    else:
                        lista.append(f"- {full_name}")

                msg_text = (
                    "Encontré varios posibles:\n"
                    + "\n".join(lista)
                    + "\n\nPrueba con un nombre más específico o usa el username completo."
                )

            msg = await context.bot.send_message(chat_id, msg_text)
            if jq:
                jq.run_once(
                    delete_message_later,
                    when=DELETE_AFTER_SECONDS,
                    data={"chat_id": msg.chat_id, "message_id": msg.message_id},
                )
            return

        # Solo 1 match
        target_user_id, data = matches[0]
        display_name = data.get("full_name") or data.get("username") or "este usuario"

    # Ya tenemos target_user_id y display_name -> limpiamos sus warnings
    key = f"{chat_id}:{target_user_id}"

    if key in warnings:
        del warnings[key]
        save_warnings()
        result_text = f"🧹 Limpio el historial de {display_name}. Como si nada hubiera pasado 😉"
    else:
        result_text = f"{display_name} no tiene advertencias registradas."

    msg = await context.bot.send_message(chat_id, result_text)
    if jq:
        jq.run_once(
            delete_message_later,
            when=DELETE_AFTER_SECONDS,
            data={"chat_id": msg.chat_id, "message_id": msg.message_id},
        )

# ------------------ COMANDO /debugwarnings ------------------
async def debug_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Muestra el contenido actual de warnings y la ruta del archivo.
    Solo admins/creador pueden usarlo.
    """
    jq = context.application.job_queue
    chat_id = str(update.effective_chat.id)
    message = update.message
    user_id = str(update.effective_user.id)

    # Borrar el comando para no ensuciar el chat
    try:
        await message.delete()
    except:
        pass

    # Validar admin
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ["administrator", "creator"]:
            msg = await context.bot.send_message(
                chat_id,
                "Solo admins pueden usar /debugwarnings."
            )
            if jq:
                jq.run_once(
                    delete_message_later,
                    when=DELETE_AFTER_SECONDS,
                    data={"chat_id": msg.chat_id, "message_id": msg.message_id},
                )
            return
    except Exception as e:
        print("Error en admin-check /debugwarnings:", e)
        return

    # Construir texto
    from pprint import pformat
    warnings_text = pformat(warnings, width=80)
    file_path = os.path.abspath(WARNINGS_FILE)

    texto = (
        "🐛 DEBUG DE ADVERTENCIAS\n\n"
        f"Archivo donde se guardan las advertencias:\n`{file_path}`\n\n"
        f"Esto es lo que tiene guardado el bot en este momento:\n```{warnings_text}```"
    )

    # Enviar mensaje con autodestrucción
    msg = await context.bot.send_message(chat_id, texto, parse_mode="Markdown")

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

    # Registrar usuario que manda el mensaje
    register_user(chat_id, update.effective_user)

    # Si es reply, registrar también al otro
    if update.message.reply_to_message:
        register_user(chat_id, update.message.reply_to_message.from_user)

    # Ignorar admins / creador (si quieres que no reciban warnings, descomenta esto)
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status in ["administrator", "creator"]:
            return
    except Exception as e:
        print(f"Error en get_chat_member: {e}")
        return

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
            f"🚫 {update.effective_user.first_name}, aquí no se permiten links de otros grupos.\n"
            f"Llevas {current_warnings} de {MAX_WARNINGS}.\n\n"
            "A la tercera vas pa' fuera, eh 🙃"
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
                    f"{update.effective_user.first_name} llegó al límite.\n\n"
                    "Se avisó y se cumplió 😇."
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
app.add_handler(CommandHandler("unwarn", unwarn))
app.add_handler(CommandHandler("debugwarnings", debug_warnings))
app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), check_links))

# ---------------- RUN BOT -----------------
if __name__ == "__main__":
    print("Bot corriendo...")
    app.run_polling()
