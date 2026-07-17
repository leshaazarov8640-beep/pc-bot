import os
import json
import logging
import socket
import time
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests

logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOW_EVERYONE = os.environ.get("ALLOW_EVERYONE", "true").lower() == "true"

PC_MAC = os.environ.get("PC_MAC", "B4:2E:99:ED:26:3F")
PC_BROADCAST = os.environ.get("PC_BROADCAST", "255.255.255.255")
PC_WOL_PORT = int(os.environ.get("PC_WOL_PORT", "9"))

PORT = int(os.environ.get("PORT", 8080))
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"
last_update_id = 0
pending_commands = {}
agent_last_seen = 0
agent_ip = ""


def send_wol(mac: str) -> bool:
    try:
        mac_clean = mac.replace(":", "").replace("-", "")
        if len(mac_clean) != 12:
            return False
        mac_bytes = bytes.fromhex(mac_clean)
        magic = b"\xff" * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic, (PC_BROADCAST, PC_WOL_PORT))
        return True
    except Exception as e:
        logger.error("WOL error: %s", e)
        return False


def tg_send(chat_id: int, text: str):
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        logger.error("TG send error: %s", e)


def handle_command(chat_id: int, text: str):
    global agent_last_seen, agent_ip

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
        if send_wol(PC_MAC):
            tg_send(chat_id, f"✅ WOL-пакет отправлен на {PC_MAC}")
        else:
            tg_send(chat_id, "❌ Ошибка WOL")
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


def poll_bot():
    global last_update_id
    while True:
        try:
            url = f"{TELEGRAM_API}/getUpdates?timeout=30"
            if last_update_id:
                url += f"&offset={last_update_id + 1}"
            r = requests.get(url, timeout=35)
            data = r.json()
            if not data.get("ok"):
                continue
            for upd in data.get("result", []):
                last_update_id = upd["update_id"]
                msg = upd.get("message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "")
                handle_command(chat_id, text)
        except requests.Timeout:
            pass
        except Exception as e:
            logger.error("Poll error: %s", e)
            time.sleep(3)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_json({"status": "ok"})
        elif self.path.startswith("/api/command"):
            self.send_json(pending_commands if pending_commands else {"command": "none"})
        elif self.path == "/":
            self.send_json({
                "status": "ok",
                "pc": "online" if (time.time() - agent_last_seen < 30) else "offline",
            })
        else:
            self.send_error(404)

    def do_POST(self):
        global agent_last_seen, agent_ip
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}

        if self.path == "/api/ping":
            agent_last_seen = time.time()
            agent_ip = data.get("ip", self.client_address[0])
            self.send_json({"ok": True})
        elif self.path == "/api/command/done":
            cmd = data.get("command", "")
            if cmd in pending_commands:
                del pending_commands[cmd]
            self.send_json({"ok": True})
        elif self.path == "/api/wake":
            ok = send_wol(PC_MAC)
            self.send_json({"wol_sent": ok})
        else:
            self.send_error(404)

    def send_json(self, obj):
        msg = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)

    def log_message(self, format, *args):
        logger.info("%s - %s", self.client_address[0], format % args)


if __name__ == "__main__":
    t = threading.Thread(target=poll_bot, daemon=True)
    t.start()
    logger.info("Bot polling started in thread")
    logger.info("Starting HTTP server on port %s...", PORT)
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
