#!/usr/bin/env python3
"""
wwe.py — HOMIES WWE BOT (Flask webhook version + short bold dashed restriction DM)
Requirements:
  - Flask
  - requests
  - Pillow (optional) for images
Environment variables:
  - TELEGRAM_BOT_TOKEN or BOT_TOKEN (required)
  - WEBHOOK_BASE_URL (optional, used by /set-webhook)
  - PERSISTENT_DIR (optional)
Notes:
  - This is a webhook-style server (Flask). Suitable for Vercel / any HTTP server.
  - Serverless filesystems are often ephemeral — use an external DB for persistent state if needed.
"""
import os
import json
import logging
import random
import io
import time
from typing import Dict, List, Tuple

from flask import Flask, request, jsonify

import requests

# Pillow (optional)
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN or BOT_TOKEN environment variable required")

WEBHOOK_BASE = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")
PERSISTENT_DIR = os.getenv("PERSISTENT_DIR", "")
if PERSISTENT_DIR:
    os.makedirs(PERSISTENT_DIR, exist_ok=True)
STATS_FILE = os.path.join(PERSISTENT_DIR or ".", "user_stats.json")
PARSE_MODE = "HTML"

# ---------------- LOGGING ----------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("wwe-webhook")

# ---------------- GAME CONSTANTS ----------------
MAX_HP = 200
MAX_SPECIALS_PER_MATCH = 4    # suplex & rko total per player per match
MAX_REVERSALS_PER_MATCH = 3   # reversal total per player per match
MAX_NAME_LENGTH = 16

MOVES = {
    "punch":    {"dmg": 5},
    "kick":     {"dmg": 15},
    "slam":     {"dmg": 25},
    "dropkick": {"dmg": 30},
    "suplex":   {"dmg": 45},
    "rko":      {"dmg": 55},
    "reversal": {"dmg": 0},
}

# ---------------- PERSISTENT STATS ----------------
def load_stats_file(path: str) -> Dict[str, Dict]:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # ensure fields
                for v in data.values():
                    v.setdefault("draws", 0)
                    v.setdefault("specials_used", 0)
                    v.setdefault("specials_successful", 0)
                return data
    except Exception:
        logger.exception("Failed to load stats file; starting empty")
    return {}

def save_stats_file(path: str, data: Dict[str, Dict]):
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Failed to save stats file")

user_stats: Dict[str, Dict] = load_stats_file(STATS_FILE)

# ---------------- IN-MEM STATE ----------------
lobbies: Dict[str, Dict] = {}   # group_id (str) -> lobby info
games: Dict[str, Dict] = {}     # group_id (str) -> game state

# ---------------- HELPERS ----------------
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def tg_post(method: str, payload: dict = None, files=None, timeout=10):
    url = f"{API_BASE}/{method}"
    try:
        if files:
            resp = requests.post(url, data=payload, files=files, timeout=timeout)
        else:
            resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code != 200:
            logger.warning("Telegram %s returned %s: %s", method, resp.status_code, resp.text[:400])
        try:
            return resp.json()
        except Exception:
            return {}
    except Exception:
        logger.exception("tg_post error")
        return {}

def send_message(chat_id, text, reply_markup=None, parse_mode=PARSE_MODE):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg_post("sendMessage", payload)

def edit_message_text(chat_id, message_id, text, reply_markup=None, parse_mode=PARSE_MODE):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg_post("editMessageText", payload)

def answer_callback(callback_query_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text
    return tg_post("answerCallbackQuery", payload)

def delete_message(chat_id, message_id):
    return tg_post("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

def send_photo(chat_id, photo_bytes, filename="image.png", caption=None):
    url = f"{API_BASE}/sendPhoto"
    files = {"photo": (filename, photo_bytes)}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    try:
        resp = requests.post(url, data=data, files=files, timeout=15)
        if resp.status_code != 200:
            logger.warning("sendPhoto returned %s %s", resp.status_code, resp.text[:400])
        return resp.json()
    except Exception:
        logger.exception("sendPhoto error")
        return {}

def crowd_hype() -> str:
    return random.choice(["🔥 The crowd goes wild!", "📣 Fans erupt!", "😱 What a sequence!", "🎉 Arena is electric!"])

# ---------------- PIL helpers (unchanged, adapted) ----------------
def find_font_pair() -> Tuple:
    if not PIL_AVAILABLE:
        return (None, None)
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for path in candidates:
        try:
            if os.path.exists(path):
                return (ImageFont.truetype(path, 64), ImageFont.truetype(path, 28))
        except Exception:
            continue
    try:
        return (ImageFont.load_default(), ImageFont.load_default())
    except Exception:
        return (None, None)

def measure_text(draw, text, font) -> Tuple[int,int]:
    try:
        bbox = draw.textbbox((0,0), text, font=font)
        return bbox[2]-bbox[0], bbox[3]-bbox[1]
    except Exception:
        try:
            return font.getsize(text)
        except Exception:
            return (len(text)*8, 16)

def create_stats_image(name: str, stats: Dict) -> bytes:
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow not available")
    title_font, body_font = find_font_pair()
    W, H = 900, 420
    bg = (18,18,30); accent=(255,140,0)
    img = Image.new("RGB", (W,H), color=bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0,0),(W,100)], fill=accent)
    title = "CAREER STATS"
    w_t, h_t = measure_text(draw, title, title_font)
    draw.text(((W-w_t)//2, 20), title, font=title_font, fill=(0,0,0))
    draw.text((40,130), name, font=body_font, fill=(255,255,255))
    wins = stats.get("wins",0); losses = stats.get("losses",0); draws = stats.get("draws",0)
    total = wins + losses + draws
    win_pct = round((wins/total)*100,1) if total else 0.0
    sp_used = stats.get("specials_used",0); sp_succ = stats.get("specials_successful",0)
    sp_rate = round((sp_succ/sp_used)*100,1) if sp_used else 0.0
    lines = [
        f"Wins: {wins}",
        f"Losses: {losses}",
        f"Draws: {draws}",
        f"Win %: {win_pct}%",
        f"Specials used: {sp_used}",
        f"Specials successful: {sp_succ} ({sp_rate}%)",
    ]
    y = 180
    for ln in lines:
        draw.text((60,y), ln, font=body_font, fill=(230,230,230))
        y += 32
    footer = "HOMIES WWE BOT"
    w_f, h_f = measure_text(draw, footer, body_font)
    draw.text(((W-w_f)//2, H-50), footer, font=body_font, fill=(160,160,160))
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return buf.getvalue()

def create_leaderboard_image(entries: List[Tuple[str,int,int,int]]) -> bytes:
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow not available")
    title_font, body_font = find_font_pair()
    W = 900
    rows = max(3, len(entries))
    H = 140 + rows*40
    bg = (12,12,24); accent = (30,144,255)
    img = Image.new("RGB", (W,H), color=bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0,0),(W,100)], fill=accent)
    title = "LEADERBOARD"
    w_t, h_t = measure_text(draw, title, title_font)
    draw.text(((W-w_t)//2,18), title, font=title_font, fill=(255,255,255))
    start_y = 120; x_rank = 60; x_name = 120; x_record = W - 320
    for i,(name,wins,losses,draws) in enumerate(entries, start=1):
        draw.text((x_rank, start_y+(i-1)*40), f"{i}.", font=body_font, fill=(255,255,255))
        draw.text((x_name, start_y+(i-1)*40), name, font=body_font, fill=(230,230,230))
        draw.text((x_record, start_y+(i-1)*40), f"{wins}W / {losses}L / {draws}D", font=body_font, fill=(200,200,200))
    footer = "HOMIES WWE BOT"
    w_f, h_f = measure_text(draw, footer, body_font)
    draw.text(((W-w_f)//2, H-36), footer, font=body_font, fill=(150,150,150))
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return buf.getvalue()

# ---------------- SHORT RESTRICTION DM ----------------
def send_short_restriction_dm(user_id: int):
    """
    Very short single-line bold dashed DM:
    — <b>Use another move — you can't use this move or reversal continuously</b>
    """
    msg = "— <b>Use another move — you can't use this move or reversal continuously</b>"
    send_message(user_id, msg)

# ---------------- LOBBY / MATCH / MOVE LOGIC (adapted) ----------------
def ensure_user(uid: str):
    if uid not in user_stats:
        user_stats[uid] = {"name": None, "wins":0, "losses":0, "draws":0, "specials_used":0, "specials_successful":0}
        save_stats_file(STATS_FILE, user_stats)

def build_move_keyboard(group_id: str):
    game = games.get(group_id)
    rows = []
    rows.append([
        {"text": "Punch", "callback_data": f"move|{group_id}|punch"},
        {"text": "Kick", "callback_data": f"move|{group_id}|kick"},
        {"text": "Slam", "callback_data": f"move|{group_id}|slam"},
    ])
    special_row = [{"text":"Dropkick","callback_data":f"move|{group_id}|dropkick"}]
    if not game:
        special_row.append({"text":"Suplex","callback_data":f"move|{group_id}|suplex"})
        special_row.append({"text":"RKO","callback_data":f"move|{group_id}|rko"})
    else:
        p1, p2 = game["players"]
        if game["specials_left"].get(str(p1),0) > 0 or game["specials_left"].get(str(p2),0) > 0:
            special_row.append({"text":"Suplex","callback_data":f"move|{group_id}|suplex"})
            special_row.append({"text":"RKO","callback_data":f"move|{group_id}|rko"})
    rows.append(special_row)
    if not game:
        rows.append([{"text":"Reversal","callback_data":f"move|{group_id}|reversal"}])
    else:
        p1, p2 = game["players"]
        if game["reversals_left"].get(str(p1),0) > 0 or game["reversals_left"].get(str(p2),0) > 0:
            rows.append([{"text":"Reversal","callback_data":f"move|{group_id}|reversal"}])
    return {"inline_keyboard": rows}

def start_match_sync(group_id: str, p1: int, p2: int):
    # synchronous start used by webhook handler
    name1 = user_stats.get(str(p1), {}).get("name", f"Player{p1}")
    name2 = user_stats.get(str(p2), {}).get("name", f"Player{p2}")
    games[group_id] = {
        "players": [p1, p2],
        "names": {str(p1): name1, str(p2): name2},
        "hp": {str(p1): MAX_HP, str(p2): MAX_HP},
        "specials_left": {str(p1): MAX_SPECIALS_PER_MATCH, str(p2): MAX_SPECIALS_PER_MATCH},
        "reversals_left": {str(p1): MAX_REVERSALS_PER_MATCH, str(p2): MAX_REVERSALS_PER_MATCH},
        "last_move": {str(p1): None, str(p2): None},
        "move_choice": {str(p1): None, str(p2): None},
        "round_prompt_msg_ids": []
    }
    send_message(group_id, f"🛎️ MATCH START — <b>{name1}</b> vs <b>{name2}</b>!\nPlayers: choose moves by tapping buttons below. Selections are private to the bot.")
    # send prompt
    send_round_prompt(group_id)

def send_round_prompt(group_id: str):
    game = games.get(group_id)
    if not game:
        return
    kb = build_move_keyboard(group_id)
    res = send_message(group_id, "🎮 Round — Players, pick your move (buttons below). Selections are private; results will be posted when both have chosen.", reply_markup=kb)
    try:
        mid = res.get("result", {}).get("message_id")
        if mid:
            game.setdefault("round_prompt_msg_ids", []).append(mid)
    except Exception:
        pass

def resolve_turn_sync(group_id: str):
    game = games.get(group_id)
    if not game:
        return
    # delete round prompts
    for mid in list(game.get("round_prompt_msg_ids", [])):
        try:
            delete_message(group_id, mid)
        except Exception:
            pass
    game["round_prompt_msg_ids"] = []

    p1, p2 = game["players"]
    p1s, p2s = str(p1), str(p2)
    m1 = game["move_choice"].get(p1s); m2 = game["move_choice"].get(p2s)
    name1 = game["names"].get(p1s); name2 = game["names"].get(p2s)

    def damage_of(move_name: str) -> int:
        return MOVES.get(move_name, {}).get("dmg", 0)

    dmg_to = {p1: 0, p2: 0}
    # reversal logic
    if m1 == "reversal" and m2 != "reversal": dmg_to[p2] += damage_of(m2)
    if m2 == "reversal" and m1 != "reversal": dmg_to[p1] += damage_of(m1)
    # normal damage unless reversed
    if not (m2 == "reversal" and m1 != "reversal"): dmg_to[p2] += damage_of(m1)
    if not (m1 == "reversal" and m2 != "reversal"): dmg_to[p1] += damage_of(m2)

    prev1 = game["hp"][p1s]; prev2 = game["hp"][p2s]
    new1 = max(0, prev1 - dmg_to[p1]); new2 = max(0, prev2 - dmg_to[p2])
    game["hp"][p1s] = new1; game["hp"][p2s] = new2

    # bookkeeping specials/reversals and stats
    if m1 in ("suplex","rko"):
        game["specials_left"][p1s] = max(0, game["specials_left"][p1s] - 1)
        user_stats.setdefault(p1s, {}).setdefault("specials_used", 0)
        user_stats[p1s]["specials_used"] = user_stats[p1s].get("specials_used", 0) + 1
        if dmg_to[p2] > 0:
            user_stats[p1s]["specials_successful"] = user_stats[p1s].get("specials_successful", 0) + 1
    if m2 in ("suplex","rko"):
        game["specials_left"][p2s] = max(0, game["specials_left"][p2s] - 1)
        user_stats.setdefault(p2s, {}).setdefault("specials_used", 0)
        user_stats[p2s]["specials_used"] = user_stats[p2s].get("specials_used", 0) + 1
        if dmg_to[p1] > 0:
            user_stats[p2s]["specials_successful"] = user_stats[p2s].get("specials_successful", 0) + 1

    if m1 == "reversal":
        game["reversals_left"][p1s] = max(0, game["reversals_left"][p1s] - 1)
    if m2 == "reversal":
        game["reversals_left"][p2s] = max(0, game["reversals_left"][p2s] - 1)

    game["last_move"][p1s] = m1; game["last_move"][p2s] = m2

    # Build messages
    if m1 == "reversal" and m2 != "reversal":
        action1 = f"🔄 <b>{name1}</b> reversed <b>{name2}</b>'s {m2.capitalize()} — {name2} takes <b>{dmg_to[p2]}</b> damage!"
    else:
        action1 = f"💥 <b>{name1}</b> used <b>{(m1 or 'unknown').capitalize()}</b> and dealt <b>{dmg_to[p2]}</b> damage to <b>{name2}</b>!" if dmg_to[p2] > 0 else f"⚠️ <b>{name1}</b> used <b>{(m1 or 'unknown').capitalize()}</b> but dealt no damage."
    hp1_line = f"<b>{name1}</b> — HP: <b>{new1}</b>"

    if m2 == "reversal" and m1 != "reversal":
        action2 = f"🔄 <b>{name2}</b> reversed <b>{name1}</b>'s {m1.capitalize()} — {name1} takes <b>{dmg_to[p1]}</b> damage!"
    else:
        action2 = f"💥 <b>{name2}</b> used <b>{(m2 or 'unknown').capitalize()}</b> and dealt <b>{dmg_to[p1]}</b> damage to <b>{name1}</b>!" if dmg_to[p1] > 0 else f"⚠️ <b>{name2}</b> used <b>{(m2 or 'unknown').capitalize()}</b> but dealt no damage."
    hp2_line = f"<b>{name2}</b> — HP: <b>{new2}</b>"

    # Send the 4 result messages
    send_message(group_id, action1)
    send_message(group_id, hp1_line)
    send_message(group_id, action2)
    send_message(group_id, hp2_line)

    # save stats quickly
    save_stats_file(STATS_FILE, user_stats)

    # WAIT 1 second after sending results (user requested)
    time.sleep(1.0)

    # optional hype
    if any(m in ("dropkick","suplex","rko") for m in (m1, m2) if m):
        send_message(group_id, f"<i>{crowd_hype()}</i>")
        time.sleep(0.5)

    # KO / draw logic
    p1_dead = (new1 == 0); p2_dead = (new2 == 0)
    if p1_dead and p2_dead:
        user_stats.setdefault(p1s, {}).setdefault("draws", 0)
        user_stats.setdefault(p2s, {}).setdefault("draws", 0)
        user_stats[p1s]["draws"] += 1
        user_stats[p2s]["draws"] += 1
        save_stats_file(STATS_FILE, user_stats)
        send_message(group_id, "<b>⚖️ DOUBLE KO — DRAW!</b>")
        games.pop(group_id, None)
        return

    if p1_dead or p2_dead:
        winner = p2 if p1_dead else p1
        loser = p1 if p1_dead else p2
        w = str(winner); l = str(loser)
        user_stats.setdefault(w, {}).setdefault("wins", 0)
        user_stats.setdefault(l, {}).setdefault("losses", 0)
        user_stats[w]["wins"] += 1; user_stats[l]["losses"] += 1
        save_stats_file(STATS_FILE, user_stats)
        send_message(group_id, f"\n\n🏆 <b>{user_stats.get(w,{}).get('name','WINNER').upper()} WINS BY KO!</b> 🏆\n\n")
        # winner image optional
        try:
            if PIL_AVAILABLE:
                png = create_winner_image(user_stats.get(w,{}).get("name","Winner"), game["hp"].get(str(winner),0))
                if png:
                    send_photo(group_id, png, filename="winner.png")
        except Exception:
            pass
        games.pop(group_id, None)
        return

    # else prepare next round
    game["move_choice"][p1s] = None; game["move_choice"][p2s] = None
    send_message(group_id, "➡️ Next round — choose your move (use the buttons).")
    send_round_prompt(group_id)

def create_winner_image(name: str, hp: int) -> bytes:
    if not PIL_AVAILABLE:
        return None
    title_font, body_font = find_font_pair()
    W, H = 1000, 400; bg=(10,10,20); accent=(255,200,0)
    img = Image.new("RGB", (W,H), color=bg); draw = ImageDraw.Draw(img)
    draw.rectangle([(0,0),(W,90)], fill=accent)
    title = "WINNER"
    w_t, h_t = measure_text(draw, title, title_font)
    draw.text(((W-w_t)//2, 18), title, font=title_font, fill=(0,0,0))
    name_text = name or "WINNER"
    w_n, h_n = measure_text(draw, name_text, body_font)
    draw.text(((W-w_n)//2, 150), name_text, font=body_font, fill=(255,255,255))
    hp_text = f"Final HP: {hp}"
    w_h, h_h = measure_text(draw, hp_text, body_font)
    draw.text(((W-w_h)//2, 210), hp_text, font=body_font, fill=(220,220,220))
    footer = "HOMIES WWE BOT"
    w_f, h_f = measure_text(draw, footer, body_font)
    draw.text(((W-w_f)//2, H-60), footer, font=body_font, fill=(180,180,180))
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return buf.read()

# ---------------- FLASK WEBHOOK ----------------
app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return "HOMIES WWE BOT (webhook) OK", 200

@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    if not WEBHOOK_BASE:
        return "Set WEBHOOK_BASE_URL env to your public base url (e.g. https://your-app.vercel.app)", 400
    webhook_url = WEBHOOK_BASE.rstrip("/") + "/webhook"
    res = tg_post("setWebhook", {"url": webhook_url})
    return jsonify(res)

@app.route("/webhook", methods=["POST"])
def webhook_receiver():
    update = request.get_json(force=True)
    try:
        # Messages
        if "message" in update:
            msg = update["message"]
            chat = msg.get("chat", {})
            text = msg.get("text", "") or ""
            from_user = msg.get("from", {})
            user_id = from_user.get("id")
            chat_type = chat.get("type")
            chat_id = str(chat.get("id"))

            # PRIVATE (DM) flows
            if chat_type == "private":
                uid = str(user_id)
                ensure_user(uid)
                # name registration: if user has no name and sends non-command text, treat as name
                if (not user_stats.get(uid, {}).get("name")) and text and not text.startswith("/"):
                    name = text.strip()
                    if not name:
                        send_message(user_id, "Name cannot be empty. Try again.")
                        return jsonify({"ok": True})
                    if len(name) > MAX_NAME_LENGTH:
                        send_message(user_id, f"Name too long — max {MAX_NAME_LENGTH} characters.")
                        return jsonify({"ok": True})
                    # uniqueness check
                    if any(info.get("name","").lower() == name.lower() for k,info in user_stats.items() if k != uid and info.get("name")):
                        send_message(user_id, "That name is already taken — pick another.")
                        return jsonify({"ok": True})
                    user_stats.setdefault(uid, {})["name"] = name
                    # ensure other fields
                    user_stats[uid].setdefault("wins",0); user_stats[uid].setdefault("losses",0)
                    user_stats[uid].setdefault("draws",0); user_stats[uid].setdefault("specials_used",0)
                    user_stats[uid].setdefault("specials_successful",0)
                    save_stats_file(STATS_FILE, user_stats)
                    send_message(user_id, f"🔥 Registered as <b>{name}</b>! Use /help to see commands.")
                    return jsonify({"ok": True})

                # DM commands
                if text.startswith("/"):
                    cmd = text.split()[0].lower()
                    if cmd == "/start":
                        send_message(user_id, f"🎉 Welcome! Reply with your wrestler name (max {MAX_NAME_LENGTH} characters).")
                        return jsonify({"ok": True})
                    if cmd == "/stats":
                        info = user_stats.get(uid)
                        if not info or not info.get("name"):
                            send_message(user_id, "You are not registered. Reply with your name or use /start.")
                            return jsonify({"ok": True})
                        wins = info.get("wins",0); losses = info.get("losses",0); draws = info.get("draws",0)
                        total = wins + losses + draws; win_pct = round((wins/total)*100,1) if total else 0.0
                        sp_used = info.get("specials_used",0); sp_succ = info.get("specials_successful",0)
                        sp_rate = round((sp_succ/sp_used)*100,1) if sp_used else 0.0
                        send_message(user_id, f"<b>{info.get('name')}</b>\nWins: {wins} Losses: {losses} Draws: {draws}\nWin%: {win_pct}%\nSpecials used: {sp_used} Successful: {sp_succ} ({sp_rate}%)")
                        return jsonify({"ok": True})
                    if cmd == "/startcareer":
                        send_message(user_id, f"Reply with your wrestler name (max {MAX_NAME_LENGTH} characters).")
                        return jsonify({"ok": True})
                    if cmd == "/forfeit":
                        kb = {"inline_keyboard":[[{"text":"Yes — Forfeit","callback_data":"forfeit_yes"}],[{"text":"No — Cancel","callback_data":"forfeit_no"}]]}
                        send_message(user_id, "Are you sure you want to forfeit this match? This will count as a loss.", reply_markup=kb)
                        return jsonify({"ok": True})
                    if cmd == "/help":
                        send_message(user_id, "DM commands: /start, reply with name to register, /stats, /forfeit, /startcareer.")
                        return jsonify({"ok": True})
                # fallback DM
                send_message(user_id, "DM commands: /start, reply with name to register, /stats, /help.")
                return jsonify({"ok": True})

            # GROUP flows
            if chat_type in ("group", "supergroup"):
                # group commands
                if text and text.startswith("/"):
                    cmd = text.split()[0].lower()
                    if cmd == "/startgame":
                        uid = str(user_id)
                        if uid not in user_stats or not user_stats[uid].get("name"):
                            send_message(chat_id, "You must register (DM /start) before opening a lobby.")
                            return jsonify({"ok": True})
                        if chat_id in games:
                            send_message(chat_id, "A match is already active here. Wait for it to finish.")
                            return jsonify({"ok": True})
                        if chat_id in lobbies:
                            send_message(chat_id, "A lobby is already open here.")
                            return jsonify({"ok": True})
                        lobbies[chat_id] = {"host": user_id, "players":[user_id], "message_id": None}
                        kb = {"inline_keyboard":[[{"text":"🔵 Join","callback_data":f"join|{chat_id}|{user_id}"}],[{"text":"❌ Cancel","callback_data":f"cancel_lobby|{chat_id}|{user_id}"}]]}
                        res = send_message(chat_id, f"🎫 <b>Lobby opened</b> by <b>{user_stats[uid].get('name')}</b>\nTap Join to accept and start a 1v1 match.", reply_markup=kb)
                        try:
                            mid = res.get("result", {}).get("message_id")
                            if mid:
                                lobbies[chat_id]["message_id"] = mid
                        except Exception:
                            pass
                        return jsonify({"ok": True})
                    if cmd == "/endmatch":
                        game = games.get(chat_id)
                        if not game:
                            send_message(chat_id, "No active match here.")
                            return jsonify({"ok": True})
                        if user_id not in game["players"]:
                            send_message(chat_id, "Only players in the active match may use /endmatch.")
                            return jsonify({"ok": True})
                        # delete prompts
                        for mid in list(game.get("round_prompt_msg_ids", [])):
                            try:
                                delete_message(chat_id, mid)
                            except Exception:
                                pass
                        kb = {"inline_keyboard":[[{"text":"Yes — End Match (I take the loss)","callback_data":f"confirm_end|{chat_id}|yes"}],[{"text":"No — Cancel","callback_data":f"confirm_end|{chat_id}|no"}]]}
                        send_message(chat_id, "Are you sure you want to end the match? This will count as a loss for the player who confirms.", reply_markup=kb)
                        return jsonify({"ok": True})
                    if cmd == "/help":
                        send_message(chat_id, "Group commands: /startgame — open lobby, /endmatch — end match (players only).")
                        return jsonify({"ok": True})
                # ignore other group text
                return jsonify({"ok": True})

        # Callback queries (buttons)
        if "callback_query" in update:
            cq = update["callback_query"]
            data = cq.get("data", "")
            from_user = cq.get("from", {})
            callback_id = cq.get("id")
            message = cq.get("message", {}) or {}
            chat = message.get("chat", {}) or {}
            chat_id = str(chat.get("id"))
            user_id = from_user.get("id")

            # lobby callbacks
            if data.startswith("join|") or data.startswith("cancel_lobby|"):
                parts = data.split("|")
                if len(parts) < 3:
                    answer_callback(callback_id, "Invalid action", show_alert=True)
                    return jsonify({"ok": True})
                action = parts[0]; gid = parts[1]; host_id = int(parts[2])
                lobby = lobbies.get(gid)
                if not lobby or lobby.get("host") != host_id:
                    answer_callback(callback_id, "This lobby no longer exists", show_alert=True)
                    lobbies.pop(gid, None)
                    save_stats_file(STATS_FILE, user_stats)
                    return jsonify({"ok": True})
                if action == "cancel_lobby":
                    if user_id != host_id:
                        answer_callback(callback_id, "Only the lobby host can cancel.", show_alert=True)
                        return jsonify({"ok": True})
                    answer_callback(callback_id, "Lobby cancelled.")
                    try:
                        edit_message_text(gid, message.get("message_id"), "Lobby cancelled by host.")
                    except Exception:
                        pass
                    lobbies.pop(gid, None)
                    save_stats_file(STATS_FILE, user_stats)
                    return jsonify({"ok": True})
                if action == "join":
                    if user_id == host_id:
                        answer_callback(callback_id, "You created the lobby.", show_alert=True)
                        return jsonify({"ok": True})
                    if str(user_id) not in user_stats or not user_stats[str(user_id)].get("name"):
                        answer_callback(callback_id, "You must register (DM /start) before joining.", show_alert=True)
                        return jsonify({"ok": True})
                    lobbies.pop(gid, None)
                    answer_callback(callback_id, "Joining... match starting.")
                    start_match_sync(gid, host_id, user_id)
                    save_stats_file(STATS_FILE, user_stats)
                    return jsonify({"ok": True})

            # move callback: move|gid|move
            if data.startswith("move|"):
                parts = data.split("|")
                if len(parts) != 3:
                    answer_callback(callback_id, "Invalid move", show_alert=True)
                    return jsonify({"ok": True})
                _, gid, move = parts
                game = games.get(gid)
                if not game:
                    answer_callback(callback_id, "No active match here.", show_alert=True)
                    return jsonify({"ok": True})
                if user_id not in game["players"]:
                    answer_callback(callback_id, "You are not part of this match.", show_alert=True)
                    return jsonify({"ok": True})
                pstr = str(user_id)
                if game["move_choice"].get(pstr) is not None:
                    answer_callback(callback_id, "You already chose this round.", show_alert=True)
                    return jsonify({"ok": True})
                # validation
                last = game["last_move"].get(pstr)
                blocked_reason = None
                if move in ("suplex","rko"):
                    if last in ("suplex","rko"):
                        blocked_reason = "Can't use special back-to-back (cooldown)."
                    elif game["specials_left"].get(pstr, 0) <= 0:
                        blocked_reason = "No specials left."
                if move == "reversal":
                    if last == "reversal":
                        blocked_reason = "Can't use reversal back-to-back (cooldown)."
                    elif game["reversals_left"].get(pstr, 0) <= 0:
                        blocked_reason = "No reversals left."

                if blocked_reason:
                    answer_callback(callback_id, f"⚠️ {blocked_reason}", show_alert=True)
                    send_short_restriction_dm(user_id)
                    player_name = game["names"].get(pstr, from_user.get("first_name", "A player"))
                    send_message(gid, f"⚠️ {player_name} tried to use {move.capitalize()} but it was blocked.")
                    return jsonify({"ok": True})

                # record move
                game["move_choice"][pstr] = move
                answer_callback(callback_id, "Move recorded. Waiting for opponent...")
                p1, p2 = game["players"]
                if game["move_choice"].get(str(p1)) and game["move_choice"].get(str(p2)):
                    resolve_turn_sync(gid)
                return jsonify({"ok": True})

            # confirm_end|gid|yes/no
            if data.startswith("confirm_end|"):
                parts = data.split("|")
                if len(parts) != 3:
                    answer_callback(callback_id, "Invalid data", show_alert=True)
                    return jsonify({"ok": True})
                _, gid, choice = parts
                game = games.get(gid)
                if not game:
                    answer_callback(callback_id, "No active match here.", show_alert=True)
                    return jsonify({"ok": True})
                if user_id not in game["players"]:
                    answer_callback(callback_id, "Only participants may confirm", show_alert=True)
                    return jsonify({"ok": True})
                opponent = game["players"][1] if game["players"][0] == user_id else game["players"][0]
                if choice == "yes":
                    w = str(opponent); l = str(user_id)
                    user_stats.setdefault(w, {}).setdefault("wins", 0)
                    user_stats.setdefault(l, {}).setdefault("losses", 0)
                    user_stats[w]["wins"] += 1; user_stats[l]["losses"] += 1
                    save_stats_file(STATS_FILE, user_stats)
                    send_message(gid, f"⚠️ {user_stats.get(str(user_id),{}).get('name','A player')} ended the match. {user_stats.get(w,{}).get('name','Opponent')} wins!")
                    games.pop(gid, None)
                    answer_callback(callback_id, "Match ended; loss recorded for confirmer.")
                    try:
                        edit_message_text(gid, message.get("message_id"), "Match ended. You took the loss.")
                    except Exception:
                        pass
                    return jsonify({"ok": True})
                else:
                    answer_callback(callback_id, "Canceled")
                    try:
                        edit_message_text(gid, message.get("message_id"), "End-match canceled.")
                    except Exception:
                        pass
                    return jsonify({"ok": True})

            # forfeit yes/no
            if data == "forfeit_yes":
                forfeited = False
                for gid, game in list(games.items()):
                    if user_id in game["players"]:
                        p1,p2 = game["players"]; opp = p2 if user_id==p1 else p1
                        for mid in list(game.get("round_prompt_msg_ids", [])):
                            try:
                                delete_message(gid, mid)
                            except Exception:
                                pass
                        send_message(gid, f"🛎️ {game['names'].get(str(user_id),'A player')} forfeited. {game['names'].get(str(opp))} wins.")
                        w = str(opp); l = str(user_id)
                        user_stats.setdefault(w, {}).setdefault("wins",0)
                        user_stats.setdefault(l, {}).setdefault("losses",0)
                        user_stats[w]["wins"] += 1; user_stats[l]["losses"] += 1
                        save_stats_file(STATS_FILE, user_stats)
                        games.pop(gid, None)
                        try:
                            edit_message_text(chat.get("id"), message.get("message_id"), "You forfeited the match. Loss recorded.")
                        except Exception:
                            pass
                        forfeited = True
                        break
                if not forfeited:
                    try:
                        edit_message_text(chat.get("id"), message.get("message_id"), "You are not in a match.")
                    except Exception:
                        pass
                answer_callback(callback_id, "Processed")
                return jsonify({"ok": True})

            if data == "forfeit_no":
                answer_callback(callback_id, "Canceled")
                try:
                    edit_message_text(chat.get("id"), message.get("message_id"), "Forfeit canceled.")
                except Exception:
                    pass
                return jsonify({"ok": True})

    except Exception:
        logger.exception("Webhook handling error")
    return jsonify({"ok": True})

# ---------------- Run (for local testing) ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info("Starting Flask webhook server on port %s", port)
    app.run(host="0.0.0.0", port=port, debug=False)
