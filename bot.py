import os
import json
import logging
import socket
import time
from datetime import datetime

import flask

logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

app = flask.Flask(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "")
PC_MAC = os.environ.get("PC_MAC", "B4:2E:99:ED:26:3F")
ALLOW_EVERYONE = os.environ.get("ALLOW_EVERYONE", "true").lower() == "true"

pending_commands = {}
agent_last_seen = 0
agent_ip = ""

TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"


def send_wol(mac: str) -> bool:
    try:
        mac_clean = mac.replace(":", "").replace("-", "")
        if len(mac_clean) != 12:
            return False
        mac_bytes = bytes.fromhex(mac_clean)
        magic = b"\xff" * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic, ("255.255.255.255", 9))
        return True
    except Exception as e:
        logger.error("WOL error: %s", e)
        return False


def tg_send(chat_id: int, text: str):
    try:
        import requests
        requests.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML"
        }, timeout=10)
    except ImportError:
        logger.error("requests not installed")
    except Exception as e:
        logger.error("TG send error: %s", e)


def handle_command(chat_id: int, text: str):
    parts = text.strip().split()
    cmd = parts[0].lower()

    if cmd == "/start" or cmd == "/help":
        tg_send(chat_id, (
            "Управление ПК через Telegram\n\n"
            "/wake — включить ПК (WOL)\n"
            "/off [сек] — выключить\n"
            "/reboot — перезагрузить\n"
            "/sleep — сон\n"
            "/lock — заблокировать\n"
            "/cancel — отменить выключение\n"
            "/status — статус ПК"
        ))
    elif cmd == "/wake":
        tg_send(chat_id, f"Открой Mocha WOL на iPhone и отправь пакет на MAC: {PC_MAC}\nИли нажми кнопку в приложении.")
    elif cmd == "/off":
        delay = 30
        if len(parts) > 1:
            try:
                delay = int(parts[1])
            except ValueError:
                pass
        pending_commands["shutdown"] = {"delay": delay}
        tg_send(chat_id, f"⏳ Выключение через {delay} сек.")
    elif cmd == "/reboot":
        pending_commands["reboot"] = {}
        tg_send(chat_id, "⏳ Перезагрузка...")
    elif cmd == "/sleep":
        pending_commands["sleep"] = {}
        tg_send(chat_id, "⏳ Сон...")
    elif cmd == "/lock":
        pending_commands["lock"] = {}
        tg_send(chat_id, "🔒 Блокировка...")
    elif cmd == "/cancel":
        pending_commands["cancel_shutdown"] = {}
        tg_send(chat_id, "❌ Выключение отменено.")
    elif cmd == "/status":
        now = time.time()
        if now - agent_last_seen < 30:
            tg_send(chat_id, f"✅ ПК ВКЛЮЧЁН\nIP: {agent_ip}")
        else:
            tg_send(chat_id, "❌ ПК ВЫКЛЮЧЕН\n/wake чтобы включить.")
    else:
        tg_send(chat_id, "Неизвестная команда. /help")


@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    data = flask.request.get_json(silent=True)
    if not data:
        return "ok", 200
    msg = data.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "")
    if chat_id:
        handle_command(chat_id, text)
    return "ok", 200


@app.route("/api/ping", methods=["POST"])
def api_ping():
    global agent_ip, agent_last_seen
    data = flask.request.get_json(silent=True) or {}
    agent_last_seen = time.time()
    agent_ip = data.get("ip", flask.request.remote_addr or "")
    return flask.jsonify({"ok": True})


@app.route("/api/command", methods=["GET"])
def api_get_command():
    if pending_commands:
        cmd_name, cmd_data = next(iter(pending_commands.items()))
        cmd_data["command"] = cmd_name
        return flask.jsonify(cmd_data)
    return flask.jsonify({"command": "none"})


@app.route("/api/command/done", methods=["POST"])
def api_command_done():
    data = flask.request.get_json(silent=True) or {}
    cmd = data.get("command", "")
    if cmd in pending_commands:
        del pending_commands[cmd]
    return flask.jsonify({"ok": True})


@app.route("/health")
def health():
    return flask.jsonify({"status": "ok"})


@app.route("/")
def index():
    online = (time.time() - agent_last_seen < 30)
    return flask.jsonify({
        "service": "PC Remote Bot",
        "pc_status": "online" if online else "offline",
    })
