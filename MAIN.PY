#!/usr/bin/env python3
"""
wwe.py — HOMIES WWE BOT (short bold dashed restriction DM)
Requires:
  - python-telegram-bot >= 20
  - Pillow (optional) for images
Env:
  - TELEGRAM_BOT_TOKEN (optional)
  - PERSISTENT_DIR (optional)
"""
import os
import json
import logging
import random
import io
import asyncio
from typing import Dict, List, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.error import TimedOut, TelegramError
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

# Pillow (optional)
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8454875282:AAE5inNKIPnxYMX8WSZXirEU2FOCMCN_lOkS")
PERSISTENT_DIR = os.getenv("PERSISTENT_DIR", None)
if PERSISTENT_DIR:
    os.makedirs(PERSISTENT_DIR, exist_ok=True)
STATS_FILE = os.path.join(PERSISTENT_DIR, "user_stats.json") if PERSISTENT_DIR else "user_stats.json"
PARSE_MODE = "HTML"

# ---------------- LOGGING ----------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

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
try:
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            user_stats: Dict[str, Dict] = json.load(f)
    else:
        user_stats = {}
except Exception:
    logger.exception("Failed to load stats file; starting with empty stats.")
    user_stats = {}

def save_stats():
    try:
        parent = os.path.dirname(STATS_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_stats, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Failed to save stats")

for k, v in list(user_stats.items()):
    if "draws" not in v:
        user_stats[k].setdefault("draws", 0)

# ---------------- IN-MEM STATE ----------------
lobbies: Dict[int, Dict] = {}
games: Dict[int, Dict] = {}

# ---------------- HELPERS ----------------
def crowd_hype() -> str:
    return random.choice(["🔥 The crowd goes wild!", "📣 Fans erupt!", "😱 What a sequence!", "🎉 Arena is electric!"])

async def safe_send(func, *args, **kwargs):
    try:
        return await func(*args, **kwargs)
    except TimedOut:
        logger.warning("Telegram request timed out for args=%s kwargs=%s", args, kwargs)
    except TelegramError as e:
        logger.exception("Telegram error while sending: %s", e)
    except Exception:
        logger.exception("Unexpected error sending message")

# ---------------- PIL helpers (unchanged) ----------------
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

# ---------------- SHORT RESTRICTION DM (VERY SHORT, BOLD, DASHED) ----------------
async def send_short_restriction_dm(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """
    Send the short single-line bold dashed DM requested by the user:
    — <b>Use another move — you can't use this move or reversal continuously</b>
    """
    try:
        msg = "— <b>Use another move — you can't use this move or reversal continuously</b>"
        await safe_send(context.bot.send_message, chat_id=user_id, text=msg, parse_mode=PARSE_MODE)
    except Exception:
        logger.exception("Failed to send short restriction DM to %s", user_id)

# ---------------- HANDLERS (registration/stats/help/lobby) ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await safe_send(update.message.reply_text, "Please DM me /start to register your wrestler name.")
        return
    uid = update.effective_user.id; uid_s = str(uid)
    if uid_s in user_stats and user_stats[uid_s].get("name"):
        await safe_send(update.message.reply_text, f"You're already registered as <b>{user_stats[uid_s]['name']}</b>.", parse_mode=PARSE_MODE)
        return
    user_stats.setdefault(uid_s, {"name": None, "wins":0, "losses":0, "draws":0, "specials_used":0, "specials_successful":0})
    save_stats()
    context.user_data["awaiting_name"] = True
    await safe_send(update.message.reply_text, f"🎉 Welcome! Reply with your wrestler name (max {MAX_NAME_LENGTH} characters).")

async def cmd_startcareer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await safe_send(update.message.reply_text, "Use /startcareer in DM to create/change your character name.")
        return
    uid = update.effective_user.id; uid_s = str(uid)
    user_stats.setdefault(uid_s, {"name": None, "wins":0, "losses":0, "draws":0, "specials_used":0, "specials_successful":0})
    save_stats()
    context.user_data["awaiting_name"] = True
    await safe_send(update.message.reply_text, f"Reply with your wrestler name (max {MAX_NAME_LENGTH} characters).")

async def private_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    uid = update.effective_user.id; uid_s = str(uid)
    text = (update.message.text or "").strip()
    if context.user_data.get("awaiting_name"):
        name = text.strip()
        if not name:
            await safe_send(update.message.reply_text, "Name cannot be empty. Try again.")
            return
        if len(name) > MAX_NAME_LENGTH:
            await safe_send(update.message.reply_text, f"Name too long — max {MAX_NAME_LENGTH} characters.")
            return
        taken = any(info.get("name") and info["name"].lower() == name.lower() for k,info in user_stats.items() if k != uid_s)
        if taken:
            await safe_send(update.message.reply_text, "That name is already taken — pick another.")
            return
        user_stats.setdefault(uid_s, {})
        user_stats[uid_s]["name"] = name
        user_stats[uid_s].setdefault("wins",0); user_stats[uid_s].setdefault("losses",0)
        user_stats[uid_s].setdefault("draws",0)
        user_stats[uid_s].setdefault("specials_used",0); user_stats[uid_s].setdefault("specials_successful",0)
        save_stats()
        context.user_data["awaiting_name"] = False
        await safe_send(update.message.reply_text, f"🔥 Registered as <b>{name}</b>! Use /help to see commands.", parse_mode=PARSE_MODE)
        return
    in_match = any(update.effective_user.id in g.get("players",[]) for g in games.values())
    if in_match:
        await safe_send(update.message.reply_text, "You're in a match. Use /help or wait for group commentary.")
    else:
        await safe_send(update.message.reply_text, "DM commands: /start, /startcareer, /stats, /leaderboard, /help")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "💥 <b>WWE Text Brawl — Commands</b>\n\n"
        "<b>Registration & profile</b>:\n"
        "/start — register (DM)\n"
        "/startcareer — change character name (DM)\n\n"
        "<b>Match & flow</b>:\n"
        "/startgame — open a 1v1 lobby in a group\n"
        "/endmatch — ask to end the active match in this group (players only)\n"
        "/forfeit — forfeit a match (DM)\n\n"
        "<b>Moves (group buttons only, during matches)</b>:\n"
        "Punch 5 | Kick 15 | Slam 25 | Dropkick 30 | Suplex 45 | RKO 55 | Reversal (reflect)\n\n"
        "Rules:\n• Specials: 4 uses per match, cannot be used consecutively.\n• Reversal: 3 uses per match, cannot be used consecutively.\n• Reversal reflects damage back to attacker; defender takes none.\n• First to 0 HP loses. Double KO = draw (tracked).\n\n"
        "If you try a blocked move you will get a short bold dashed DM and a one-line notice in the group."
    )
    await safe_send(update.message.reply_text, help_text, parse_mode=PARSE_MODE)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; uid_s = str(uid)
    if uid_s not in user_stats or not user_stats[uid_s].get("name"):
        await safe_send(update.message.reply_text, "You are not registered. DM /start or /startcareer to register.")
        return
    info = user_stats[uid_s]
    if PIL_AVAILABLE:
        try:
            png = create_stats_image(info.get("name","Unknown"), info)
            bio = io.BytesIO(png); bio.name="stats.png"; bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=uid, photo=InputFile(bio, filename="stats.png"))
            return
        except Exception:
            logger.exception("Failed to create/send stats image; falling back to text")
    wins = info.get("wins",0); losses = info.get("losses",0); draws = info.get("draws",0)
    total = wins + losses + draws; win_pct = round((wins/total)*100,1) if total else 0.0
    sp_used = info.get("specials_used",0); sp_succ = info.get("specials_successful",0)
    sp_rate = round((sp_succ/sp_used)*100,1) if sp_used else 0.0
    hint = "Install Pillow for images: python -m pip install Pillow"
    txt = (f"<b>{info.get('name')}</b>\nWins: {wins}  Losses: {losses}  Draws: {draws}\nWin%: {win_pct}%\n"
           f"Specials used: {sp_used}  Successful: {sp_succ} ({sp_rate}%)\n\n{hint}")
    await safe_send(update.message.reply_text, txt, parse_mode=PARSE_MODE)

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    players = [(info.get("name"), info.get("wins",0), info.get("losses",0), info.get("draws",0)) for info in user_stats.values() if info.get("name")]
    if not players:
        await safe_send(update.message.reply_text, "No registered wrestlers yet.")
        return
    sorted_players = sorted(players, key=lambda x: x[1], reverse=True)[:10]
    if PIL_AVAILABLE:
        try:
            png = create_leaderboard_image(sorted_players)
            bio = io.BytesIO(png); bio.name="leaderboard.png"; bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=update.effective_chat.id, photo=InputFile(bio, filename="leaderboard.png"))
            return
        except Exception:
            logger.exception("Failed to create/send leaderboard image; falling back to text")
    lines = ["🏆 Leaderboard:"]
    for i,(n,wins,losses,draws) in enumerate(sorted_players, start=1):
        lines.append(f"{i}. {n} — {wins}W / {losses}L / {draws}D")
    lines.append("\nInstall Pillow to get leaderboard images.")
    await safe_send(update.message.reply_text, "\n".join(lines))

# ---------------- LOBBY & STARTGAME ----------------
async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await safe_send(update.message.reply_text, "Use /startgame in a group to open a lobby.")
        return
    group_id = update.effective_chat.id
    user = update.effective_user; uid = user.id
    if str(uid) not in user_stats or not user_stats[str(uid)].get("name"):
        await safe_send(update.message.reply_text, "You must register (DM /start) before opening a lobby.")
        return
    if group_id in games:
        await safe_send(update.message.reply_text, "A match is already active here. Wait for it to finish.")
        return
    lobbies[group_id] = {"host": uid, "players": [uid], "message_id": None}
    host_name = user_stats[str(uid)]["name"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔵 Join", callback_data=f"join|{group_id}|{uid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_lobby|{group_id}|{uid}")]
    ])
    msg = await safe_send(context.bot.send_message, chat_id=group_id,
                          text=f"🎫 <b>Lobby opened</b> by <b>{host_name}</b>\nTap <b>Join</b> to accept and start a 1v1 match.",
                          parse_mode=PARSE_MODE, reply_markup=keyboard)
    if msg:
        lobbies[group_id]["message_id"] = msg.message_id

async def lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data or ""; parts = data.split("|")
    if len(parts) < 3:
        await safe_send(query.edit_message_text, "Invalid action.")
        return
    action = parts[0]; group_id = int(parts[1]); host_id = int(parts[2])
    user_id = query.from_user.id
    lobby = lobbies.get(group_id)
    if not lobby or lobby.get("host") != host_id:
        await safe_send(query.edit_message_text, "This lobby no longer exists.")
        lobbies.pop(group_id, None)
        return
    if action == "cancel_lobby":
        if user_id != host_id:
            await query.answer("Only the lobby host can cancel.", show_alert=True)
            return
        await safe_send(query.edit_message_text, "Lobby cancelled by host.")
        lobbies.pop(group_id, None)
        return
    if action == "join":
        if user_id == host_id:
            await query.answer("You created the lobby.", show_alert=True)
            return
        if str(user_id) not in user_stats or not user_stats[str(user_id)].get("name"):
            await query.answer("You must register (DM /start) before joining.", show_alert=True)
            return
        lobbies[group_id]["players"].append(user_id)
        host_name = user_stats[str(host_id)]["name"]; joiner_name = user_stats[str(user_id)]["name"]
        await safe_send(query.edit_message_text, f"✅ {joiner_name} joined {host_name}'s lobby! Starting match...")
        lobbies.pop(group_id, None)
        await start_match(group_id, host_id, user_id, context)
        return

# ---------------- START MATCH ----------------
async def start_match(group_id: int, p1: int, p2: int, context: ContextTypes.DEFAULT_TYPE):
    if group_id in games:
        await safe_send(context.bot.send_message, chat_id=group_id, text="A match is already active here.")
        return
    name1 = user_stats.get(str(p1), {}).get("name", f"Player{p1}")
    name2 = user_stats.get(str(p2), {}).get("name", f"Player{p2}")
    games[group_id] = {
        "players": [p1, p2],
        "names": {str(p1): name1, str(p2): name2},
        "hp": {p1: MAX_HP, p2: MAX_HP},
        "specials_left": {p1: MAX_SPECIALS_PER_MATCH, p2: MAX_SPECIALS_PER_MATCH},
        "reversals_left": {p1: MAX_REVERSALS_PER_MATCH, p2: MAX_REVERSALS_PER_MATCH},
        "last_move": {p1: None, p2: None},
        "move_choice": {p1: None, p2: None},
        "round_prompt_msg_ids": [],
    }
    await safe_send(context.bot.send_message, chat_id=group_id,
                    text=(f"🛎️ MATCH START — <b>{name1}</b> vs <b>{name2}</b>!\n"
                          "Players: choose moves by pressing the buttons below. Your selections are private to the bot."),
                    parse_mode=PARSE_MODE)
    await send_group_move_prompt(group_id, context)

# ---------------- GROUP MOVE PROMPT ----------------
def build_shared_move_keyboard(group_id: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    game = games.get(group_id)
    if not game:
        rows.append([
            InlineKeyboardButton("Punch", callback_data=f"move|{group_id}|punch"),
            InlineKeyboardButton("Kick", callback_data=f"move|{group_id}|kick"),
            InlineKeyboardButton("Slam", callback_data=f"move|{group_id}|slam"),
        ])
        rows.append([
            InlineKeyboardButton("Dropkick", callback_data=f"move|{group_id}|dropkick"),
            InlineKeyboardButton("Suplex", callback_data=f"move|{group_id}|suplex"),
            InlineKeyboardButton("RKO", callback_data=f"move|{group_id}|rko"),
        ])
        rows.append([InlineKeyboardButton("Reversal", callback_data=f"move|{group_id}|reversal")])
        return InlineKeyboardMarkup(rows)

    p1, p2 = game["players"]
    specials_available = (game["specials_left"].get(p1,0) > 0) or (game["specials_left"].get(p2,0) > 0)
    reversal_available = (game["reversals_left"].get(p1,0) > 0) or (game["reversals_left"].get(p2,0) > 0)

    rows.append([
        InlineKeyboardButton("Punch", callback_data=f"move|{group_id}|punch"),
        InlineKeyboardButton("Kick", callback_data=f"move|{group_id}|kick"),
        InlineKeyboardButton("Slam", callback_data=f"move|{group_id}|slam"),
    ])
    special_buttons = []
    special_buttons.append(InlineKeyboardButton("Dropkick", callback_data=f"move|{group_id}|dropkick"))
    if specials_available:
        special_buttons.append(InlineKeyboardButton("Suplex", callback_data=f"move|{group_id}|suplex"))
        special_buttons.append(InlineKeyboardButton("RKO", callback_data=f"move|{group_id}|rko"))
    rows.append(special_buttons)
    if reversal_available:
        rows.append([InlineKeyboardButton("Reversal", callback_data=f"move|{group_id}|reversal")])
    return InlineKeyboardMarkup(rows)

async def send_group_move_prompt(group_id: int, context: ContextTypes.DEFAULT_TYPE):
    game = games.get(group_id)
    if not game:
        return
    kb = build_shared_move_keyboard(group_id)
    msg = await safe_send(context.bot.send_message, chat_id=group_id,
                          text="🎮 Round — Players, pick your move (buttons below). Selections are private; results will be posted when both have chosen.",
                          parse_mode=PARSE_MODE, reply_markup=kb)
    if msg:
        game.setdefault("round_prompt_msg_ids", []).append(msg.message_id)

# ---------------- MOVE CALLBACK (group buttons) ----------------
async def move_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data or ""; parts = data.split("|")
    if len(parts) != 3 or parts[0] != "move":
        try:
            await query.answer("Invalid action.", show_alert=True)
        except Exception:
            pass
        return
    _, group_str, move = parts
    try:
        group_id = int(group_str)
    except Exception:
        try:
            await query.answer("Invalid group.", show_alert=True)
        except Exception:
            pass
        return
    user_id = query.from_user.id
    game = games.get(group_id)
    if not game:
        try:
            await query.answer("No active match here.", show_alert=True)
        except Exception:
            pass
        return
    if user_id not in game["players"]:
        try:
            await query.answer("You are not part of this match.", show_alert=True)
        except Exception:
            pass
        return
    if game["move_choice"].get(user_id) is not None:
        try:
            await query.answer("You already chose this round.", show_alert=True)
        except Exception:
            pass
        return

    # VALIDATION — Option 2: show button but alert & DM player if blocked; also post one-line group notice
    last = game["last_move"].get(user_id)
    blocked_reason = None
    if move in ("suplex","rko"):
        if last in ("suplex","rko"):
            blocked_reason = "Can't use special back-to-back (cooldown)."
        elif game["specials_left"].get(user_id, 0) <= 0:
            blocked_reason = "No specials left."
    if move == "reversal":
        if last == "reversal":
            blocked_reason = "Can't use reversal back-to-back (cooldown)."
        elif game["reversals_left"].get(user_id, 0) <= 0:
            blocked_reason = "No reversals left."

    if blocked_reason:
        # alert the clicking user
        try:
            await query.answer(f"⚠️ {blocked_reason}", show_alert=True)
        except Exception:
            pass
        # send the extremely short bold dashed DM requested
        await send_short_restriction_dm(context, user_id)
        # post a one-line simple notice in the group (do not reveal the user's private selection)
        player_name = game["names"].get(str(user_id), query.from_user.first_name)
        try:
            await safe_send(context.bot.send_message, chat_id=group_id,
                            text=f"⚠️ {player_name} tried to use {move.capitalize()} but it was blocked.",
                            parse_mode=PARSE_MODE)
        except Exception:
            logger.exception("Failed to post blocked-move notice in group %s", group_id)
        return

    # otherwise record silently
    game["move_choice"][user_id] = move
    logger.info("Recorded move for user %s in group %s: %s", user_id, group_id, move)
    p1, p2 = game["players"]
    if game["move_choice"].get(p1) and game["move_choice"].get(p2):
        await resolve_turn(group_id, context)
    else:
        try:
            await query.answer("Move recorded. Waiting for opponent...", show_alert=False)
        except Exception:
            pass
        return

# ---------------- RESOLVE TURN ----------------
async def resolve_turn(group_id: int, context: ContextTypes.DEFAULT_TYPE):
    game = games.get(group_id)
    if not game:
        return
    # delete previous round prompt messages BEFORE posting results
    for mid in list(game.get("round_prompt_msg_ids", [])):
        try:
            await safe_send(context.bot.delete_message, chat_id=group_id, message_id=mid)
        except Exception:
            logger.debug("Could not delete prompt message %s in group %s", mid, group_id)
    game["round_prompt_msg_ids"] = []

    p1, p2 = game["players"]
    m1 = game["move_choice"].get(p1); m2 = game["move_choice"].get(p2)
    name1 = game["names"].get(str(p1)); name2 = game["names"].get(str(p2))

    def damage_of(name: str) -> int:
        return MOVES.get(name, {}).get("dmg", 0)

    dmg_to = {p1: 0, p2: 0}
    # reversal reflection logic
    if m1 == "reversal" and m2 != "reversal":
        dmg_to[p2] += damage_of(m2)
    if m2 == "reversal" and m1 != "reversal":
        dmg_to[p1] += damage_of(m1)
    # normal damage except when reversed
    if not (m2 == "reversal" and m1 != "reversal"):
        dmg_to[p2] += damage_of(m1)
    if not (m1 == "reversal" and m2 != "reversal"):
        dmg_to[p1] += damage_of(m2)

    prev_hp_p1 = game["hp"][p1]; prev_hp_p2 = game["hp"][p2]
    new_hp_p1 = max(0, prev_hp_p1 - dmg_to[p1]); new_hp_p2 = max(0, prev_hp_p2 - dmg_to[p2])
    game["hp"][p1] = new_hp_p1; game["hp"][p2] = new_hp_p2

    # stats bookkeeping
    if m1 in ("suplex","rko"):
        game["specials_left"][p1] = max(0, game["specials_left"][p1] - 1)
        user_stats.setdefault(str(p1), {}).setdefault("specials_used", 0)
        user_stats[str(p1)]["specials_used"] = user_stats[str(p1)].get("specials_used", 0) + 1
        if dmg_to[p2] > 0:
            user_stats.setdefault(str(p1), {}).setdefault("specials_successful", 0)
            user_stats[str(p1)]["specials_successful"] = user_stats[str(p1)].get("specials_successful", 0) + 1
    if m2 in ("suplex","rko"):
        game["specials_left"][p2] = max(0, game["specials_left"][p2] - 1)
        user_stats.setdefault(str(p2), {}).setdefault("specials_used", 0)
        user_stats[str(p2)]["specials_used"] = user_stats[str(p2)].get("specials_used", 0) + 1
        if dmg_to[p1] > 0:
            user_stats.setdefault(str(p2), {}).setdefault("specials_successful", 0)
            user_stats[str(p2)]["specials_successful"] = user_stats[str(p2)].get("specials_successful", 0) + 1

    if m1 == "reversal":
        game["reversals_left"][p1] = max(0, game["reversals_left"][p1] - 1)
    if m2 == "reversal":
        game["reversals_left"][p2] = max(0, game["reversals_left"][p2] - 1)

    game["last_move"][p1] = m1; game["last_move"][p2] = m2

    # Build two-line-per-player messages (action + HP)
    if m1 == "reversal" and m2 != "reversal":
        action_p1 = f"🔄 <b>{name1}</b> reversed <b>{name2}</b>'s {m2.capitalize()} — {name2} takes <b>{dmg_to[p2]}</b> damage!"
    else:
        action_p1 = f"💥 <b>{name1}</b> used <b>{m1.capitalize()}</b> and dealt <b>{dmg_to[p2]}</b> damage to <b>{name2}</b>!" if dmg_to[p2] > 0 else f"⚠️ <b>{name1}</b> used <b>{m1.capitalize()}</b> but dealt no damage."
    hp_line_p1 = f"<b>{name1}</b> — HP: <b>{game['hp'][p1]}</b>"

    if m2 == "reversal" and m1 != "reversal":
        action_p2 = f"🔄 <b>{name2}</b> reversed <b>{name1}</b>'s {m1.capitalize()} — {name1} takes <b>{dmg_to[p1]}</b> damage!"
    else:
        action_p2 = f"💥 <b>{name2}</b> used <b>{m2.capitalize()}</b> and dealt <b>{dmg_to[p1]}</b> damage to <b>{name1}</b>!" if dmg_to[p1] > 0 else f"⚠️ <b>{name2}</b> used <b>{m2.capitalize()}</b> but dealt no damage."
    hp_line_p2 = f"<b>{name2}</b> — HP: <b>{game['hp'][p2]}</b>"

    # Send the 4 messages (p1 action, p1 hp, p2 action, p2 hp)
    await safe_send(context.bot.send_message, chat_id=group_id, text=action_p1, parse_mode=PARSE_MODE)
    await safe_send(context.bot.send_message, chat_id=group_id, text=hp_line_p1, parse_mode=PARSE_MODE)
    await safe_send(context.bot.send_message, chat_id=group_id, text=action_p2, parse_mode=PARSE_MODE)
    await safe_send(context.bot.send_message, chat_id=group_id, text=hp_line_p2, parse_mode=PARSE_MODE)

    # WAIT 1 second after sending results (user requested)
    await asyncio.sleep(1.0)

    # optional hype after both
    if any(m in ("dropkick","suplex","rko") for m in (m1,m2)):
        await safe_send(context.bot.send_message, chat_id=group_id, text=f"<i>{crowd_hype()}</i>", parse_mode=PARSE_MODE)
        await asyncio.sleep(0.5)  # small extra beat after hype

    # KO / Draw logic
    p1_dead = (game["hp"][p1] == 0); p2_dead = (game["hp"][p2] == 0)
    if p1_dead and p2_dead:
        user_stats.setdefault(str(p1), {}).setdefault("draws", 0)
        user_stats.setdefault(str(p2), {}).setdefault("draws", 0)
        user_stats[str(p1)]["draws"] += 1
        user_stats[str(p2)]["draws"] += 1
        save_stats()
        await safe_send(context.bot.send_message, chat_id=group_id, text="<b>⚖️ DOUBLE KO — DRAW!</b>", parse_mode=PARSE_MODE)
        del games[group_id]; return
    if p1_dead or p2_dead:
        winner = p2 if p1_dead else p1; loser = p1 if p1_dead else p2
        winner_name = game["names"].get(str(winner)); loser_name = game["names"].get(str(loser))
        w = str(winner); l = str(loser)
        user_stats.setdefault(w, {"name": winner_name, "wins":0, "losses":0, "draws":0})
        user_stats.setdefault(l, {"name": loser_name, "wins":0, "losses":0, "draws":0})
        user_stats[w]["wins"] += 1; user_stats[l]["losses"] += 1
        save_stats()
        await safe_send(context.bot.send_message, chat_id=group_id, text=f"\n\n🏆 <b>{winner_name.upper()} WINS BY KO!</b> 🏆\n\n", parse_mode=PARSE_MODE)
        await send_winner_image(group_id, winner_name, hp=game["hp"][winner], context=context)
        del games[group_id]; return

    # No KO -> prepare next round
    game["move_choice"][p1] = None; game["move_choice"][p2] = None
    await safe_send(context.bot.send_message, chat_id=group_id, text="➡️ Next round — choose your move (use the buttons).")
    await send_group_move_prompt(group_id, context)

# ---------------- /endmatch (group) ----------------
async def cmd_endmatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await safe_send(update.message.reply_text, "Use /endmatch in the group where the match is active.")
        return
    group_id = update.effective_chat.id; user_id = update.effective_user.id
    game = games.get(group_id)
    if not game:
        await safe_send(update.message.reply_text, "No active match here.")
        return
    if user_id not in game["players"]:
        await safe_send(update.message.reply_text, "Only players in the active match may use /endmatch.", parse_mode=PARSE_MODE)
        return
    # delete outstanding prompts
    for mid in list(game.get("round_prompt_msg_ids", [])):
        try:
            await safe_send(context.bot.delete_message, chat_id=group_id, message_id=mid)
        except Exception:
            pass
    game["round_prompt_msg_ids"] = []
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Yes — End Match (I take the loss)", callback_data=f"confirm_end|{group_id}|yes")],
        [InlineKeyboardButton("No — Cancel", callback_data=f"confirm_end|{group_id}|no")]
    ])
    await safe_send(update.message.reply_text, "Are you sure you want to end the match? This will count as a loss for the player who confirms.", parse_mode=PARSE_MODE, reply_markup=kb)

# ---------------- confirm callback (forfeit & endmatch confirmations) ----------------
async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    user_id = query.from_user.id

    if data == "forfeit_yes":
        for gid, game in list(games.items()):
            if user_id in game["players"]:
                p1,p2 = game["players"]; opponent = p2 if user_id == p1 else p1
                for mid in list(game.get("round_prompt_msg_ids", [])):
                    try:
                        await safe_send(context.bot.delete_message, chat_id=gid, message_id=mid)
                    except Exception:
                        pass
                await safe_send(context.bot.send_message, chat_id=gid, text=f"🛎️ {game['names'].get(str(user_id),'A player')} forfeited. {game['names'].get(str(opponent))} wins.")
                w = str(opponent); l = str(user_id)
                user_stats.setdefault(w, {"name": game['names'].get(str(opponent),""), "wins":0, "losses":0, "draws":0})
                user_stats.setdefault(l, {"name": game['names'].get(str(user_id),""), "wins":0, "losses":0, "draws":0})
                user_stats[w]["wins"] += 1; user_stats[l]["losses"] += 1
                save_stats(); del games[gid]
                try:
                    await query.edit_message_text("You forfeited the match. A loss has been recorded.")
                except Exception:
                    pass
                return
        try:
            await query.edit_message_text("You are not in a match.")
        except Exception:
            pass
        return

    if data == "forfeit_no":
        try:
            await query.edit_message_text("Forfeit canceled.")
        except Exception:
            pass
        return

    if data.startswith("confirm_end|"):
        parts = data.split("|")
        if len(parts) != 3:
            try:
                await query.edit_message_text("Invalid confirmation data.")
            except Exception:
                pass
            return
        _, group_str, choice = parts
        try:
            group_id = int(group_str)
        except Exception:
            try:
                await query.edit_message_text("Invalid group id.")
            except Exception:
                pass
            return
        game = games.get(group_id)
        if not game:
            try:
                await query.edit_message_text("No active match here.")
            except Exception:
                pass
            return
        if user_id not in game["players"]:
            try:
                await query.answer("Only match participants can confirm ending the match.", show_alert=True)
            except Exception:
                pass
            return
        opponent = game["players"][1] if game["players"][0] == user_id else game["players"][0]
        if choice == "yes":
            w = str(opponent); l = str(user_id)
            user_stats.setdefault(w, {"name": game['names'].get(str(opponent),""), "wins":0, "losses":0, "draws":0})
            user_stats.setdefault(l, {"name": game['names'].get(str(user_id),""), "wins":0, "losses":0, "draws":0})
            user_stats[w]["wins"] += 1; user_stats[l]["losses"] += 1
            save_stats()
            await safe_send(context.bot.send_message, chat_id=group_id, text=f"⚠️ {game['names'].get(str(user_id),'A player')} ended the match. {game['names'].get(str(opponent))} wins!")
            del games[group_id]
            try:
                await query.edit_message_text("Match ended. You were recorded as the player who ended the match and took the loss.")
            except Exception:
                pass
            return
        else:
            try:
                await query.edit_message_text("End-match canceled.")
            except Exception:
                pass
            return

# ---------------- winner image ----------------
async def send_winner_image(chat_id: int, winner_name: str, hp: int, context: ContextTypes.DEFAULT_TYPE):
    if not PIL_AVAILABLE:
        await safe_send(context.bot.send_message, chat_id=chat_id, text=f"🏆 Winner: {winner_name} (HP: {hp})\nInstall Pillow: python -m pip install Pillow")
        return
    try:
        title_font, body_font = find_font_pair()
        W, H = 1000, 400; bg=(10,10,20); accent=(255,200,0)
        img = Image.new("RGB", (W,H), color=bg); draw = ImageDraw.Draw(img)
        draw.rectangle([(0,0),(W,90)], fill=accent)
        title = "WINNER"
        w_t, h_t = measure_text(draw, title, title_font)
        draw.text(((W-w_t)//2, 18), title, font=title_font, fill=(0,0,0))
        name_text = winner_name
        w_n, h_n = measure_text(draw, name_text, body_font)
        draw.text(((W-w_n)//2, 150), name_text, font=body_font, fill=(255,255,255))
        hp_text = f"Final HP: {hp}"
        w_h, h_h = measure_text(draw, hp_text, body_font)
        draw.text(((W-w_h)//2, 210), hp_text, font=body_font, fill=(220,220,220))
        footer = "HOMIES WWE BOT"
        w_f, h_f = measure_text(draw, footer, body_font)
        draw.text(((W-w_f)//2, H-60), footer, font=body_font, fill=(180,180,180))
        buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
        await safe_send(context.bot.send_photo, chat_id=chat_id, photo=InputFile(buf, filename="winner.png"))
    except Exception:
        logger.exception("Failed to create/send winner image")
        await safe_send(context.bot.send_message, chat_id=chat_id, text=f"🏆 Winner: {winner_name} (HP: {hp})")

# ---------------- forfeit & fallbacks ----------------
async def cmd_forfeit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await safe_send(update.message.reply_text, "Forfeit from DM only.")
        return
    uid = update.effective_user.id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Yes — Forfeit", callback_data="forfeit_yes")],
        [InlineKeyboardButton("No — Cancel", callback_data="forfeit_no")]
    ])
    await safe_send(update.message.reply_text, "Are you sure you want to forfeit this match? This will count as a loss.", reply_markup=kb)

async def dm_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send(update.message.reply_text, "Use /start to register or /help for commands. If in a match, use the group buttons to pick moves.")

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled exception: %s", context.error)

# ---------------- BOT COMMANDS SETUP ----------------
async def set_bot_commands(app):
    try:
        await app.bot.set_my_commands([
            ("start","Register (DM)"),
            ("startcareer","Create/change character name (DM)"),
            ("startgame","Open 1v1 lobby (group)"),
            ("endmatch","End current match (group; players only)"),
            ("forfeit","Forfeit current match (DM)"),
            ("stats","Show your career stats (image)"),
            ("leaderboard","Show leaderboard (image)"),
            ("help","Show help & move descriptions"),
        ])
        logger.info("Bot commands set.")
    except Exception:
        logger.exception("Failed to set bot commands (this is non-fatal).")

# ---------------- MAIN ----------------
def main():
    builder = ApplicationBuilder().token(BOT_TOKEN).post_init(set_bot_commands)
    app = builder.build()

    # core commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("startcareer", cmd_startcareer))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("startgame", cmd_startgame))
    app.add_handler(CommandHandler("endmatch", cmd_endmatch))
    app.add_handler(CommandHandler("forfeit", cmd_forfeit))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))

    # callbacks
    app.add_handler(CallbackQueryHandler(lobby_callback, pattern=r"^(join|cancel_lobby)\|"))
    app.add_handler(CallbackQueryHandler(move_callback, pattern=r"^move\|"))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern=r"^(confirm_end\||forfeit_yes|forfeit_no)"))

    # DM handlers
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, private_text_handler))
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.PRIVATE, dm_fallback))

    app.add_error_handler(global_error_handler)

    logger.info("Starting HOMIES WWE BOT (polling).")
    app.run_polling()

if __name__ == "__main__":
    main()
