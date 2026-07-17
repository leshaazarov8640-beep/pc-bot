import os
import json
import logging
import socket
import struct
import threading
import time
from datetime import datetime

from flask import Flask, request, jsonify
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_IDS = os.environ.get("ALLOWED_IDS", "")
ALLOW_EVERYONE = os.environ.get("ALLOW_EVERYONE", "true").lower() == "true"

PC_MAC = os.environ.get("PC_MAC", "B4:2E:99:ED:26:3F")
PC_BROADCAST = os.environ.get("PC_BROADCAST", "255.255.255.255")
PC_BROADCAST = os.environ.get("PC_BROADCAST", "255.255.255.255")
PC_PORT = int(os.environ.get("PC_WOL_PORT", "9"))
PC_IP = os.environ.get("PC_IP", "")

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT = int(os.environ.get("PORT", 8080))

allowed_users = [int(x.strip()) for x in ALLOWED_IDS.split(",") if x.strip()]

# Хранилище команд для PC-агента
pending_commands = {}
agent_last_seen = {}
agent_ip = PC_IP

app = Flask(__name__)
bot_app = None


def is_authorized(user_id: int) -> bool:
    if ALLOW_EVERYONE:
        return True
    return user_id in allowed_users


def send_wol(mac: str, broadcast: str, port: int) -> bool:
    try:
        mac_clean = mac.replace(":", "").replace("-", "")
        if len(mac_clean) != 12:
            return False
        mac_bytes = bytes.fromhex(mac_clean)
        magic = b"\xff" * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic, (broadcast, port))
        return True
    except Exception as e:
        logger.error("WOL error: %s", e)
        return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Нет доступа.")
        return
    text = (
        "Привет! Управление ПК через Telegram.\n\n"
        "/wake — включить ПК (WOL)\n"
        "/off — выключить ПК\n"
        "/reboot — перезагрузить\n"
        "/sleep — сон\n"
        "/lock — заблокировать\n"
        "/status — статус ПК\n"
        "/cancel — отменить выключение\n"
        "/help — помощь"
    )
    await update.message.reply_text(text)


async def cmd_wake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    if send_wol(PC_MAC, PC_BROADCAST, PC_PORT):
        await update.message.reply_text(f"✅ WOL-пакет отправлен на {PC_MAC}")
    else:
        await update.message.reply_text("❌ Ошибка отправки WOL. Проверьте MAC-адрес.")


async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    delay = 30
    if context.args:
        try:
            delay = int(context.args[0])
        except ValueError:
            pass
    pending_commands["shutdown"] = {"delay": delay, "user": update.effective_user.id}
    await update.message.reply_text(f"⏳ Команда выключения отправлена (через {delay} сек).")


async def cmd_reboot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    pending_commands["reboot"] = {"user": update.effective_user.id}
    await update.message.reply_text("⏳ Команда перезагрузки отправлена.")


async def cmd_sleep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    pending_commands["sleep"] = {"user": update.effective_user.id}
    await update.message.reply_text("⏳ Команда сна отправлена.")


async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    pending_commands["lock"] = {"user": update.effective_user.id}
    await update.message.reply_text("⏳ Команда блокировки отправлена.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    pending_commands["cancel_shutdown"] = {"user": update.effective_user.id}
    await update.message.reply_text("⏳ Команда отмены выключения отправлена.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    now = datetime.now()
    last_seen = agent_last_seen.get("agent", 0)
    diff = now.timestamp() - last_seen
    if diff < 30:
        ip = agent_ip or PC_IP or "неизвестен"
        await update.message.reply_text(f"✅ ПК ВКЛЮЧЁН\nIP: {ip}\nПоследний сигнал: {diff:.0f} сек назад")
    else:
        await update.message.reply_text("❌ ПК ВЫКЛЮЧЕН или агент не отвечает (>30 сек).\nИспользуйте /wake для включения.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    text = (
        "📖 Команды:\n"
        "/wake — отправить WOL-пакет (включить ПК)\n"
        "/off [сек] — выключить ПК\n"
        "/reboot — перезагрузить\n"
        "/sleep — сон\n"
        "/lock — заблокировать\n"
        "/cancel — отменить выключение\n"
        "/status — статус ПК\n\n"
        "MAC: {}\n"
        "Статус: {}"
    ).format(
        PC_MAC,
        "ПК онлайн" if (datetime.now().timestamp() - agent_last_seen.get("agent", 0)) < 30 else "ПК офлайн",
    )
    await update.message.reply_text(text)


# --- API endpoints для PC-агента ---

@app.route("/api/ping", methods=["POST"])
def api_ping():
    """PC-агент шлёт сюда сигнал каждые 5-10 секунд."""
    data = request.get_json(silent=True) or {}
    global agent_ip
    agent_last_seen["agent"] = time.time()
    if data.get("ip"):
        agent_ip = data["ip"]
    if request.remote_addr:
        agent_ip = request.remote_addr
    return jsonify({"ok": True})


@app.route("/api/command", methods=["GET"])
def api_get_command():
    """PC-агент забирает команду."""
    if pending_commands:
        cmd_name, cmd_data = next(iter(pending_commands.items()))
        cmd_data["command"] = cmd_name
        return jsonify(cmd_data)
    return jsonify({"command": "none"})


@app.route("/api/command/done", methods=["POST"])
def api_command_done():
    """PC-агент подтверждает выполнение команды."""
    data = request.get_json(silent=True) or {}
    cmd = data.get("command", "")
    if cmd in pending_commands:
        del pending_commands[cmd]
    logger.info("Command done: %s", cmd)
    return jsonify({"ok": True})


@app.route("/api/wake", methods=["POST"])
def api_wake():
    """Ручной вызов WOL (можно дёргать с другого устройства)."""
    ok = send_wol(PC_MAC, PC_BROADCAST, PC_PORT)
    return jsonify({"wol_sent": ok, "mac": PC_MAC})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    return jsonify({
        "service": "PC Remote Bot",
        "pc_status": "online" if (time.time() - agent_last_seen.get("agent", 0)) < 30 else "offline",
        "commands_pending": list(pending_commands.keys()),
    })


def run_bot():
    """Запуск Telegram-бота в отдельном потоке."""
    global bot_app
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        bot_app = Application.builder().token(TOKEN).build()
        bot_app.add_handler(CommandHandler("start", cmd_start))
        bot_app.add_handler(CommandHandler("wake", cmd_wake))
        bot_app.add_handler(CommandHandler("off", cmd_off))
        bot_app.add_handler(CommandHandler("reboot", cmd_reboot))
        bot_app.add_handler(CommandHandler("sleep", cmd_sleep))
        bot_app.add_handler(CommandHandler("lock", cmd_lock))
        bot_app.add_handler(CommandHandler("cancel", cmd_cancel))
        bot_app.add_handler(CommandHandler("status", cmd_status))
        bot_app.add_handler(CommandHandler("help", cmd_help))

        if WEBHOOK_URL:
            logger.info("Starting webhook on port %s", PORT)
            bot_app.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=TOKEN,
                webhook_url=f"{WEBHOOK_URL}/{TOKEN}",
            )
        else:
            logger.info("Starting polling...")
            bot_app.run_polling()
    except Exception as e:
        logger.error("Bot error: %s", e)


if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    logger.info("Flask API starting on port %s...", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
