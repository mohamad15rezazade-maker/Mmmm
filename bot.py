import os
import re
import uuid
import sqlite3
import logging
import asyncio
from datetime import datetime
from enum import Enum

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

# ================= Config =================
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().lstrip("-").isdigit()]

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN تنظیم نشده.")
if not ADMIN_IDS:
    raise SystemExit("❌ ADMIN_IDS تنظیم نشده.")

CHANNEL_USERNAME = "@BET_1XZX"
MIN_WITHDRAW = 2000
MIN_BET = 70
MAX_BET = 3000
MAX_ROLLS = 3
START_BALANCE = 500
UNIT = "داگز"
CHALLENGE_TIMEOUT = 120  # ثانیه

FORCE_CHANNELS = [
    {"name": "📢 کانال اصلی", "username": "@BET_1XZX", "url": "https://t.me/BET_1XZX"},
    {"name": "💬 گپ بازی", "username": "@GAP_BAZIN1", "url": "https://t.me/GAP_BAZIN1"},
]

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "dicex.db")

# ================= Database =================
def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db(); k = c.cursor()
    k.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        balance INTEGER DEFAULT 0, total_bets INTEGER DEFAULT 0,
        total_wins INTEGER DEFAULT 0, referrer_id INTEGER, joined_at TEXT)""")
    k.execute("""CREATE TABLE IF NOT EXISTS games(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, game_type TEXT,
        opponent_type TEXT, bet_amount INTEGER, user_score INTEGER, bot_score INTEGER,
        result TEXT, reward INTEGER, created_at TEXT)""")
    k.execute("""CREATE TABLE IF NOT EXISTS referrals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER,
        referred_id INTEGER UNIQUE, joined_at TEXT)""")
    k.execute("""CREATE TABLE IF NOT EXISTS withdrawals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER,
        status TEXT DEFAULT 'pending', created_at TEXT)""")
    c.commit(); c.close()

def get_user(uid):
    c = db(); k = c.cursor()
    k.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    r = k.fetchone(); c.close(); return r

def create_user(uid, username, first_name, referrer_id=None):
    c = db(); k = c.cursor()
    now = datetime.now().isoformat()
    k.execute("""INSERT OR IGNORE INTO users(user_id,username,first_name,balance,joined_at,referrer_id)
        VALUES(?,?,?,?,?,?)""", (uid, username or "", first_name or "", START_BALANCE, now, referrer_id))
    if k.rowcount > 0 and referrer_id:
        k.execute("INSERT OR IGNORE INTO referrals(referrer_id,referred_id,joined_at) VALUES(?,?,?)",
                  (referrer_id, uid, now))
        k.execute("UPDATE users SET balance=balance+100 WHERE user_id=?", (referrer_id,))
    c.commit(); c.close()

def add_balance(uid, amount):
    c = db(); k = c.cursor()
    k.execute("UPDATE users SET balance=MAX(0,balance+?) WHERE user_id=?", (amount, uid))
    c.commit(); c.close()

def get_balance(uid):
    u = get_user(uid); return u["balance"] if u else 0

def record_game(uid, gtype, otype, bet, us, bs, result, reward):
    c = db(); k = c.cursor()
    now = datetime.now().isoformat()
    k.execute("""INSERT INTO games(user_id,game_type,opponent_type,bet_amount,user_score,bot_score,result,reward,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""", (uid, gtype, otype, bet, us, bs, result, reward, now))
    k.execute("UPDATE users SET total_bets=total_bets+1 WHERE user_id=?", (uid,))
    if result == "win":
        k.execute("UPDATE users SET total_wins=total_wins+1 WHERE user_id=?", (uid,))
    c.commit(); c.close()

def get_all_users():
    c = db(); k = c.cursor()
    k.execute("SELECT * FROM users ORDER BY joined_at DESC")
    r = k.fetchall(); c.close(); return r

def get_stats():
    c = db(); k = c.cursor(); s = {}
    k.execute("SELECT COUNT(*) c FROM users"); s["users"] = k.fetchone()["c"]
    k.execute("SELECT COUNT(*) c FROM games"); s["games"] = k.fetchone()["c"]
    k.execute("SELECT COALESCE(SUM(bet_amount),0) s FROM games"); s["bets"] = k.fetchone()["s"]
    k.execute("SELECT COALESCE(SUM(reward),0) s FROM games WHERE result='win'"); s["rewards"] = k.fetchone()["s"]
    k.execute("SELECT COUNT(*) c FROM referrals"); s["refs"] = k.fetchone()["c"]
    k.execute("SELECT COALESCE(SUM(balance),0) s FROM users"); s["bal_total"] = k.fetchone()["s"]
    c.close(); return s

def is_admin(uid): return uid in ADMIN_IDS

# ================= Games =================
class G(Enum):
    DICE = "dice"
    BOWLING = "bowling"
    DART = "dart"

EMOJI = {G.DICE: "🎲", G.BOWLING: "🎳", G.DART: "🎯"}
NAME_FA = {G.DICE: "تاس", G.BOWLING: "بولینگ", G.DART: "دارت"}
GAME_ALIASES = {
    "تاس": G.DICE, "dice": G.DICE,
    "بولینگ": G.BOWLING, "bowling": G.BOWLING,
    "دارت": G.DART, "dart": G.DART,
}

PERSIAN_TO_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
def normalize_digits(s): return s.translate(PERSIAN_TO_EN)

def parse_game_cmd(text: str):
    text = normalize_digits(text.strip())
    parts = text.split()
    if len(parts) == 2:
        count = 1
        game_name, bet_str = parts
    elif len(parts) == 3:
        if not parts[0].isdigit(): return None
        count = int(parts[0])
        game_name, bet_str = parts[1], parts[2]
    else:
        return None

    game = GAME_ALIASES.get(game_name.lower())
    if not game or not bet_str.isdigit(): return None

    bet = int(bet_str)
    if not (1 <= count <= MAX_ROLLS): return None
    if not (MIN_BET <= bet <= MAX_BET): return None
    return game, count, bet

# ================= Helpers =================
def fmt(n): return f"{n:,}"

def safe(s):
    """اسم کاربر رو برای Markdown امن می‌کنه."""
    if not s:
        return ""
    for ch in ["_", "*", "`", "[", "]"]:
        s = s.replace(ch, f"\\{ch}")
    return s

async def ensure_user(update: Update):
    u = update.effective_user
    if u:
        create_user(u.id, u.username, u.first_name)
    return u

async def roll_dice(context: ContextTypes.DEFAULT_TYPE, chat_id: int, game: G):
    msg = await context.bot.send_dice(chat_id=chat_id, emoji=EMOJI[game])
    return msg.dice.value

# ================= Keyboards =================
def private_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 موجودی", callback_data="u:balance"),
         InlineKeyboardButton("👤 پروفایل", callback_data="u:profile")],
        [InlineKeyboardButton("🎁 زیرمجموعه", callback_data="u:ref"),
         InlineKeyboardButton("🏆 برترین‌ها", callback_data="u:top")],
        [InlineKeyboardButton("💸 برداشت", callback_data="u:withdraw")],
    ])

def mode_kb(game: G, count: int, bet: int):
    gk = game.value
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 بازی با ربات", callback_data=f"init:bot:{gk}:{count}:{bet}")],
        [InlineKeyboardButton("👥 بازی با دوستان", callback_data=f"init:fr:{gk}:{count}:{bet}")],
        [InlineKeyboardButton("❌ انصراف", callback_data="init:cancel")],
    ])

def roll_kb(gid: str, rolled: int, total: int):
    label = f"🎲 شروع کن (0/{total})" if rolled == 0 else f"🎲 رول بعدی ({rolled}/{total})"
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"roll:{gid}")]])

def join_kb(gid: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤝 پیوستن و رول کردن", callback_data=f"join:{gid}")],
        [InlineKeyboardButton("❌ لغو", callback_data=f"cancel:{gid}")],
    ])

def join_channels_kb():
    rows = [[InlineKeyboardButton(ch["name"], url=ch["url"])] for ch in FORCE_CHANNELS]
    rows.append([InlineKeyboardButton("✅ عضو شدم، بررسی کن", callback_data="check_join")])
    return InlineKeyboardMarkup(rows)

def admin_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 آمار ربات", callback_data="adm:stats")],
        [InlineKeyboardButton("👥 لیست کاربران", callback_data="adm:users")],
        [InlineKeyboardButton("➕ افزایش امتیاز", callback_data="adm:add")],
        [InlineKeyboardButton("➖ کاهش امتیاز", callback_data="adm:rem")],
        [InlineKeyboardButton("📢 پیام همگانی", callback_data="adm:bc")],
        [InlineKeyboardButton("❌ بستن", callback_data="adm:close")],
    ])

def admin_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="adm:panel")]])

# ================= Membership =================
async def check_membership(context, user_id: int) -> bool:
    for ch in FORCE_CHANNELS:
        try:
            m = await context.bot.get_chat_member(chat_id=ch["username"], user_id=user_id)
            if m.status in ("left", "kicked"):
                return False
        except Exception as e:
            log.warning(f"check_membership {ch['username']}: {e}")
            return False
    return True

async def require_membership(update: Update, context) -> bool:
    u = update.effective_user
    if not u: return False
    if await check_membership(context, u.id): return True

    txt = (
        "🔒 **عضویت اجباری**\n\n"
        "برای استفاده از **داگز موج بات**، اول باید عضو هر دو کانال زیر بشی:\n\n"
        + "\n".join(f"📢 [{ch['name']}]({ch['url']})" for ch in FORCE_CHANNELS) +
        "\n\nبعد از عضویت، دکمه «✅ عضو شدم» رو بزن."
    )
    try:
        await context.bot.send_message(
            chat_id=u.id, text=txt,
            reply_markup=join_channels_kb(),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True)
        if update.effective_chat.type != ChatType.PRIVATE:
            try:
                await update.effective_message.reply_text(
                    f"🔒 {safe(u.first_name)} جان، برای بازی اول توی پیوی ربات رو چک کن!")
            except: pass
        return False
    except Exception:
        try:
            await update.effective_message.reply_text(
                txt, reply_markup=join_channels_kb(),
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True)
        except: pass
        return False

async def check_join_cb(update: Update, context):
    q = update.callback_query
    ok = await check_membership(context, q.from_user.id)
    if ok:
        await q.answer("✅ عضویتت تأیید شد! حالا می‌تونی بازی کنی 🎉", show_alert=True)
        try:
            await q.edit_message_text(
                "✅ **عضویتت تأیید شد!**\n\nحالا برو تو گروه بنویس:\n`1 تاس 100`",
                parse_mode=ParseMode.MARKDOWN)
        except: pass
    else:
        await q.answer("❌ هنوز عضو هر دو کانال نشدی!\nاول عضو شو بعد دکمه رو بزن.", show_alert=True)

# ================= Text builders =================
def build_roll_text(st):
    lines = [f"🎮 **{NAME_FA[st['game']]}** {EMOJI[st['game']]} — " +
             ("بازی دوستانه" if st["mode"] == "fr" else "بازی با ربات"), ""]
    lines.append(f"💰 شرط: **{fmt(st['bet'])}** {UNIT}  |  🎲 پرتاب: **{st['rolls_count']}**")
    lines.append("")

    if st["creator_rolls"]:
        lines.append(f"🎲 {st['creator_name']}: " + " + ".join(map(str, st["creator_rolls"])) +
                     f" = **{sum(st['creator_rolls'])}**")
    else:
        lines.append(f"🎲 {st['creator_name']}: در انتظار...")

    if st["mode"] == "fr":
        if st.get("opponent_id"):
            if st["opponent_rolls"]:
                lines.append(f"🎲 {st['opponent_name']}: " + " + ".join(map(str, st["opponent_rolls"])) +
                             f" = **{sum(st['opponent_rolls'])}**")
            else:
                lines.append(f"🎲 {st['opponent_name']}: در انتظار...")
        else:
            lines.append("🎲 حریف: در انتظار...")
    else:
        if st["opponent_rolls"]:
            lines.append("🤖 ربات: " + " + ".join(map(str, st["opponent_rolls"])) +
                         f" = **{sum(st['opponent_rolls'])}**")
        else:
            lines.append("🤖 ربات: در انتظار...")

    lines.append("")
    if st["phase"] == "creator_rolling":
        lines.append(f"👉 نوبت **{st['creator_name']}**")
    elif st["phase"] == "opponent_rolling":
        lines.append(f"👉 نوبت **{st['opponent_name']}**")
    return "\n".join(lines)

def build_waiting_text(st):
    return (
        f"👥 **{NAME_FA[st['game']]}** {EMOJI[st['game']]} — بازی دوستانه\n\n"
        f"🎯 **{st['creator_name']}** رول کرد و امتیازش **{sum(st['creator_rolls'])}** شد!\n"
        f"💰 شرط: **{fmt(st['bet'])}** {UNIT}  |  🎲 پرتاب: **{st['rolls_count']}**\n\n"
        f"کی حاضره به چالش بکشه؟ 🤝\n"
        f"(امتیاز بیشتر بیار تا **{fmt(st['bet']*2)}** {UNIT} ببری!)\n\n"
        f"⏰ این چالش ۲ دقیقه دیگه منقضی می‌شه."
    )

# ================= Auto cancel =================
async def auto_cancel_after(context, gid: str, seconds: int):
    await asyncio.sleep(seconds)
    st = context.bot_data.get("games", {}).get(gid)
    if not st or st["phase"] != "waiting_join":
        return
    if st.get("bet_paid"):
        add_balance(st["creator_id"], st["bet"])
    try:
        await context.bot.edit_message_text(
            chat_id=st["chat_id"],
            message_id=st.get("message_id"),
            text="⏰ زمان چالش تموم شد و لغو شد. شرط برگشت داده شد.")
    except Exception as e:
        log.warning(f"auto cancel edit failed: {e}")
    context.bot_data["games"].pop(gid, None)

# ================= Commands =================
async def cmd_start(update: Update, context):
    u = await ensure_user(update)
    chat = update.effective_chat

    if chat.type == ChatType.PRIVATE:
        ref_id = None
        if context.args and context.args[0].startswith("ref_"):
            try: ref_id = int(context.args[0][4:])
            except: pass
        if ref_id:
            create_user(u.id, u.username, u.first_name, ref_id)

        if not await require_membership(update, context): return

        bal = get_balance(u.id)
        text = (
            f"👋 سلام **{safe(u.first_name)}**!\n\n"
            "به **«داگز موج بات»** خوش اومدی 🎉\n\n"
            f"💰 موجودی فعلی: **{fmt(bal)} {UNIT}**\n\n"
            "⚠️ بازی‌ها فقط توی **گروه** انجام می‌شن.\n"
            "ربات رو به گروهت اضافه کن و بنویس:\n"
            "`1 تاس 100`\n`2 بولینگ 500`\n`3 دارت 1000`\n\n"
            "📋 /help"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=private_menu())
    else:
        await update.message.reply_text(
            "👋 سلام! برای بازی بنویس:\n`1 تاس 100`",
            parse_mode=ParseMode.MARKDOWN)

async def cmd_help(update: Update, context):
    text = (
        "📖 **راهنمای داگز موج بات**\n\n"
        "🎲 **بازی‌ها:** تاس / بولینگ / دارت\n"
        f"🎯 سقف پرتاب هر بازی: **{MAX_ROLLS}**\n\n"
        "✍️ **برای ساخت بازی اینطوری بنویس:**\n"
        "`1 تاس 100`  — یک پرتاب تاس به شرط ۱۰۰\n"
        "`2 بولینگ 500` — دو پرتاب بولینگ به شرط ۵۰۰\n"
        "`3 دارت 1000` — سه پرتاب دارت به شرط ۱۰۰۰\n\n"
        f"💰 حداقل شرط: **{fmt(MIN_BET)} {UNIT}**\n"
        f"💰 حداکثر شرط: **{fmt(MAX_BET)} {UNIT}**\n\n"
        "🎮 بعد از ساخت بازی:\n🤖 بازی با ربات\n👥 بازی با دوستان\n\n"
        "💠 **دستورات پیوی:**\n/start /help /balance /profile /referral /language\n\n"
        "👥 **در گروه بنویس:**\n`موجودی` / `م` / `بازی‌ها`\n\n"
        f"💸 حداقل برداشت: **{fmt(MIN_WITHDRAW)} {UNIT}**\n"
        f"📢 کانال: {CHANNEL_USERNAME}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_balance(update: Update, context):
    u = await ensure_user(update)
    if not await require_membership(update, context): return
    await update.message.reply_text(f"💰 موجودی: **{fmt(get_balance(u.id))}** {UNIT}", parse_mode=ParseMode.MARKDOWN)

async def cmd_profile(update: Update, context):
    u = await ensure_user(update)
    if not await require_membership(update, context): return
    row = get_user(u.id)
    wr = round((row["total_wins"] / row["total_bets"]) * 100, 1) if row["total_bets"] else 0
    text = (
        f"👤 **پروفایل {safe(u.first_name)}**\n\n"
        f"🆔 آیدی: `{u.id}`\n"
        f"💰 موجودی: **{fmt(row['balance'])}** {UNIT}\n"
        f"🎮 تعداد بازی: **{row['total_bets']}**\n"
        f"🏆 بردها: **{row['total_wins']}**\n"
        f"📈 نرخ برد: **{wr}%**"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_referral(update: Update, context):
    u = await ensure_user(update)
    if not await require_membership(update, context): return
    bot = await context.bot.get_me()
    link = f"https://t.me/{bot.username}?start=ref_{u.id}"
    c = db(); k = c.cursor()
    k.execute("SELECT COUNT(*) c FROM referrals WHERE referrer_id=?", (u.id,))
    cnt = k.fetchone()["c"]; c.close()
    await update.message.reply_text(
        f"🎁 **زیرمجموعه‌های تو:** {cnt} نفر\n\n🔗 لینک دعوت:\n`{link}`\n\n"
        f"به‌ازای هر نفر **۱۰۰ {UNIT}** می‌گیری 🎉",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_language(update: Update, context):
    await update.message.reply_text("🌐 زبان فعلی: **فارسی**", parse_mode=ParseMode.MARKDOWN)

# ================= Group router =================
async def group_text_router(update: Update, context):
    txt = (update.message.text or "").strip()
    u = await ensure_user(update)
    norm = normalize_digits(txt)

    if norm == "موجودی":
        await update.message.reply_text(f"💰 موجودی: **{fmt(get_balance(u.id))}** {UNIT}", parse_mode=ParseMode.MARKDOWN)
        return
    if norm == "م":
        await update.message.reply_text(f"💰 **{fmt(get_balance(u.id))}** {UNIT}", parse_mode=ParseMode.MARKDOWN)
        return
    if norm in ("بازی‌ها", "بازیها", "بازیا", "بازی ها"):
        await update.message.reply_text(
            "🎮 برای ساخت بازی اینطوری بنویس:\n\n"
            "`1 تاس 100`\n`2 بولینگ 500`\n`3 دارت 1000`\n\n"
            f"🎲 تعداد پرتاب: 1 تا {MAX_ROLLS}\n"
            f"💰 حداقل شرط: **{fmt(MIN_BET)}** | حداکثر: **{fmt(MAX_BET)}**",
            parse_mode=ParseMode.MARKDOWN)
        return

    parsed = parse_game_cmd(txt)
    if parsed:
        if not await require_membership(update, context): return
        await start_game_from_command(update, context, parsed, u)
        return

    looks_like = bool(re.match(r"^\s*\d*\s*(تاس|بولینگ|دارت|dice|bowling|dart)\b", norm, re.IGNORECASE))
    if looks_like:
        await update.message.reply_text(
            f"❌ فرمت اشتباه یا خارج از محدوده.\n"
            f"مثال: `1 تاس 100`\n"
            f"حداقل: **{fmt(MIN_BET)}** | حداکثر: **{fmt(MAX_BET)}** | تعداد پرتاب: 1-{MAX_ROLLS}",
            parse_mode=ParseMode.MARKDOWN)

async def start_game_from_command(update, context, parsed, u):
    game, count, bet = parsed
    bal = get_balance(u.id)
    if bal < bet:
        await update.message.reply_text(
            f"❌ موجودی کافی نداری!\n💰 موجودی: **{fmt(bal)}** {UNIT}\n💰 نیاز: **{fmt(bet)}** {UNIT}",
            parse_mode=ParseMode.MARKDOWN)
        return

    text = (
        f"🎮 بازی **{NAME_FA[game]}** {EMOJI[game]}\n\n"
        f"💰 شرط: **{fmt(bet)}** {UNIT}\n"
        f"🎲 تعداد پرتاب: **{count}**\n"
        f"👤 سازنده: **{safe(u.first_name)}**\n"
        f"💼 موجودی تو: **{fmt(bal)}** {UNIT}\n\n"
        f"👇 حالت بازی رو انتخاب کن:"
    )
    await update.message.reply_text(
        text,
        reply_markup=mode_kb(game, count, bet),
        parse_mode=ParseMode.MARKDOWN)

# ================= Callbacks =================
async def init_cb(update, context):
    q = update.callback_query
    u = await ensure_user(update)

    if q.data == "init:cancel":
        await q.answer()
        try: await q.edit_message_text("❌ لغو شد.")
        except: pass
        return

    if not await check_membership(context, q.from_user.id):
        await q.answer("🔒 اول عضو کانال‌ها شو!", show_alert=True)
        return

    parts = q.data.split(":")
    if len(parts) < 5: return
    _, mode, gk, count_s, bet_s = parts
    game = G(gk); count = int(count_s); bet = int(bet_s)

    bal = get_balance(u.id)
    if bal < bet:
        await q.answer("❌ موجودی کافی نداری!", show_alert=True)
        return

    gid = uuid.uuid4().hex[:8]
    st = {
        "id": gid, "chat_id": q.message.chat_id,
        "message_id": q.message.message_id,
        "game": game, "rolls_count": count, "bet": bet, "mode": mode,
        "phase": "creator_rolling",
        "creator_id": u.id, "creator_name": safe(u.first_name), "creator_rolls": [],
        "opponent_id": None, "opponent_name": None, "opponent_rolls": [],
        "bet_paid": False,
    }
    context.bot_data.setdefault("games", {})[gid] = st

    await q.answer()
    try:
        await q.edit_message_text(
            build_roll_text(st) + "\n\n💡 دکمه رول که زدی، شرط کسر می‌شه.",
            reply_markup=roll_kb(gid, 0, count),
            parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.warning(f"init edit failed: {e}")

async def roll_cb(update, context):
    q = update.callback_query
    u = await ensure_user(update)

    if not await check_membership(context, q.from_user.id):
        await q.answer("🔒 اول عضو کانال‌ها شو!", show_alert=True)
        return

    gid = q.data.split(":", 1)[1]
    st = context.bot_data.get("games", {}).get(gid)
    if not st:
        await q.answer("❌ بازی پیدا نشد.", show_alert=True)
        return

    if st["phase"] == "creator_rolling":
        if u.id != st["creator_id"]:
            await q.answer(f"نوبت {st['creator_name']} هست!", show_alert=True)
            return
        who = "creator"
    elif st["phase"] == "opponent_rolling":
        if u.id != st["opponent_id"]:
            await q.answer(f"نوبت {st['opponent_name']} هست!", show_alert=True)
            return
        who = "opponent"
    else:
        await q.answer("این بازی در حال انجام نیست.", show_alert=True)
        return

    # ✅ کسر شرط فقط برای سازنده و فقط یکبار
    if who == "creator" and not st.get("bet_paid"):
        bal = get_balance(u.id)
        if bal < st["bet"]:
            await q.answer("❌ موجودی کافی نداری!", show_alert=True)
            context.bot_data["games"].pop(gid, None)
            try:
                await q.edit_message_text(
                    f"❌ بازی لغو شد چون موجودی {st['creator_name']} کافی نبود.")
            except: pass
            return
        add_balance(u.id, -st["bet"])
        st["bet_paid"] = True

    await q.answer()
    value = await roll_dice(context, st["chat_id"], st["game"])

    if who == "creator":
        st["creator_rolls"].append(value)
        rolls = st["creator_rolls"]
    else:
        st["opponent_rolls"].append(value)
        rolls = st["opponent_rolls"]

    need = st["rolls_count"]

    if len(rolls) < need:
        try:
            await q.edit_message_text(
                build_roll_text(st),
                reply_markup=roll_kb(gid, len(rolls), need),
                parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            log.warning(f"roll edit failed: {e}")
        return

    if st["phase"] == "creator_rolling":
        if st["mode"] == "bot":
            st["phase"] = "bot_turn"
            try:
                await q.edit_message_text(
                    build_roll_text(st) + "\n\n🤖 ربات داره می‌ریزه...",
                    parse_mode=ParseMode.MARKDOWN)
            except: pass
            for _ in range(need):
                v = await roll_dice(context, st["chat_id"], st["game"])
                st["opponent_rolls"].append(v)
                await asyncio.sleep(0.6)
            await finish_game(context, q, st, gid)
        else:
            st["phase"] = "waiting_join"
            try:
                await q.edit_message_text(
                    build_waiting_text(st),
                    reply_markup=join_kb(gid),
                    parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                log.warning(f"waiting_join edit failed: {e}")
            asyncio.create_task(auto_cancel_after(context, gid, CHALLENGE_TIMEOUT))
        return

    if st["phase"] == "opponent_rolling":
        await finish_game(context, q, st, gid)

async def join_cb(update, context):
    q = update.callback_query
    u = await ensure_user(update)

    if not await check_membership(context, q.from_user.id):
        await q.answer("🔒 اول عضو کانال‌ها شو!", show_alert=True)
        return

    gid = q.data.split(":", 1)[1]
    st = context.bot_data.get("games", {}).get(gid)
    if not st or st["phase"] != "waiting_join":
        await q.answer("❌ این بازی دیگه در دسترس نیست.", show_alert=True)
        return
    if u.id == st["creator_id"]:
        await q.answer("😅 با خودت نمی‌تونی بازی کنی!", show_alert=True)
        return

    bal = get_balance(u.id)
    if bal < st["bet"]:
        await q.answer(f"❌ موجودی کافی نداری!\nنیاز: {fmt(st['bet'])} {UNIT}", show_alert=True)
        return

    add_balance(u.id, -st["bet"])
    st["opponent_id"] = u.id
    st["opponent_name"] = safe(u.first_name)
    st["phase"] = "opponent_rolling"
    st["bet_paid"] = True

    await q.answer()
    try:
        await q.edit_message_text(
            build_roll_text(st),
            reply_markup=roll_kb(gid, 0, st["rolls_count"]),
            parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.warning(f"join edit failed: {e}")

async def cancel_cb(update, context):
    q = update.callback_query
    u = await ensure_user(update)
    gid = q.data.split(":", 1)[1]
    st = context.bot_data.get("games", {}).get(gid)
    if not st:
        await q.answer("بازی پیدا نشد.")
        return
    if st["creator_id"] != u.id:
        await q.answer("فقط سازنده می‌تونه لغو کنه.", show_alert=True)
        return
    if st.get("bet_paid"):
        add_balance(st["creator_id"], st["bet"])
    context.bot_data["games"].pop(gid, None)
    await q.answer("لغو شد و شرطت برگشت.")
    try: await q.edit_message_text("❌ بازی لغو شد و شرط برگشت داده شد.")
    except: pass

async def finish_game(context, q, st, gid):
    gs = sum(st["creator_rolls"])
    os_ = sum(st["opponent_rolls"])

    if gs > os_:
        result = "win"; reward = st["bet"] * 2
        add_balance(st["creator_id"], reward)
        winner_name = st["creator_name"]
    elif os_ > gs:
        result = "loss"; reward = 0
        if st["mode"] == "fr" and st["opponent_id"]:
            add_balance(st["opponent_id"], st["bet"] * 2)
        winner_name = st["opponent_name"] if st["mode"] == "fr" else "ربات 🤖"
    else:
        result = "draw"; reward = 0
        add_balance(st["creator_id"], st["bet"])
        if st["mode"] == "fr" and st["opponent_id"]:
            add_balance(st["opponent_id"], st["bet"])
        winner_name = None

    record_game(st["creator_id"], st["game"].value, st["mode"], st["bet"], gs, os_, result, reward)
    if st["mode"] == "fr" and st["opponent_id"]:
        opp_res = "win" if result == "loss" else ("loss" if result == "win" else "draw")
        record_game(st["opponent_id"], st["game"].value, "fr", st["bet"], os_, gs, opp_res,
                    st["bet"] * 2 if opp_res == "win" else 0)

    lines = [f"🎮 **{NAME_FA[st['game']]}** {EMOJI[st['game']]} — نتیجه", ""]
    lines.append(f"💰 جایزه: **{fmt(st['bet']*2)}** {UNIT}")
    lines.append("")
    lines.append(f"🎲 {st['creator_name']}: " + " + ".join(map(str, st["creator_rolls"])) + f" = **{gs}**")
    opp_label = st["opponent_name"] if st["mode"] == "fr" else "🤖 ربات"
    lines.append(f"🎲 {opp_label}: " + " + ".join(map(str, st["opponent_rolls"])) + f" = **{os_}**")
    lines.append("")

    if result == "win":
        lines.append(f"🏆 برنده: **{st['creator_name']}** 🎉")
        lines.append(f"💰 **{fmt(st['bet']*2)}** {UNIT} به موجودیت اضافه شد!")
    elif result == "loss":
        lines.append(f"🏆 برنده: **{winner_name}** 🎉")
        lines.append(f"😢 **{st['creator_name']}** این بار باختی، دفعه بعد می‌بری!")
    else:
        lines.append("🤝 مساوی! شرط هر دو طرف برگشت داده شد.")

    try:
        await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.warning(f"finish edit failed: {e}")
    context.bot_data["games"].pop(gid, None)

async def user_info_cb(update, context):
    q = update.callback_query
    u = await ensure_user(update)
    data = q.data

    if data == "u:balance":
        await q.answer(f"💰 موجودی: {fmt(get_balance(u.id))} {UNIT}", show_alert=True)
    elif data == "u:profile":
        row = get_user(u.id)
        wr = round((row["total_wins"] / row["total_bets"]) * 100, 1) if row["total_bets"] else 0
        await q.answer(
            f"👤 {u.first_name}\n💰 {fmt(row['balance'])} {UNIT}\n🎮 {row['total_bets']} بازی\n🏆 {row['total_wins']} برد ({wr}%)",
            show_alert=True)
    elif data == "u:ref":
        bot = await context.bot.get_me()
        link = f"https://t.me/{bot.username}?start=ref_{u.id}"
        await q.answer(f"🔗 لینک دعوت:\n{link}", show_alert=True)
    elif data == "u:top":
        c = db(); k = c.cursor()
        k.execute("SELECT first_name, balance FROM users ORDER BY balance DESC LIMIT 10")
        rows = k.fetchall(); c.close()
        txt = "🏆 برترین‌ها\n\n"
        for i, r in enumerate(rows, 1):
            txt += f"{i}. {r['first_name']} — {fmt(r['balance'])} {UNIT}\n"
        await q.answer(txt, show_alert=True)
    elif data == "u:withdraw":
        bal = get_balance(u.id)
        if bal < MIN_WITHDRAW:
            await q.answer(
                f"❌ حداقل برداشت {fmt(MIN_WITHDRAW)} {UNIT} است.\nموجودی تو: {fmt(bal)} {UNIT}",
                show_alert=True)
            return
        c = db(); k = c.cursor()
        k.execute("INSERT INTO withdrawals(user_id,amount,created_at) VALUES(?,?,?)",
                  (u.id, bal, datetime.now().isoformat()))
        c.commit(); c.close()
        add_balance(u.id, -bal)
        await q.answer(f"✅ درخواست برداشت {fmt(bal)} {UNIT} ثبت شد!", show_alert=True)
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    aid,
                    f"💸 درخواست برداشت جدید:\n👤 {u.first_name} (`{u.id}`)\n💰 {fmt(bal)} {UNIT}",
                    parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                log.warning(f"notify admin failed: {e}")

# ================= Admin =================
async def cmd_admin(update, context):
    u = await ensure_user(update)
    if not is_admin(u.id):
        await update.message.reply_text("⛔️ دسترسی نداری.")
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("پنل ادمین فقط در پیوی.")
        return
    await update.message.reply_text("🛠 **پنل مدیریت داگز موج بات**",
                                    reply_markup=admin_panel(), parse_mode=ParseMode.MARKDOWN)

async def admin_cb(update, context):
    q = update.callback_query
    u = await ensure_user(update)
    if not is_admin(u.id):
        await q.answer("⛔️", show_alert=True)
        return
    data = q.data

    if data == "adm:panel":
        await q.answer()
        await q.edit_message_text("🛠 **پنل مدیریت داگز موج بات**",
                                  reply_markup=admin_panel(), parse_mode=ParseMode.MARKDOWN)
        return
    if data == "adm:close":
        await q.answer()
        await q.edit_message_text("✅ بسته شد.")
        return
    if data == "adm:stats":
        s = get_stats()
        txt = (
            "📊 **آمار ربات**\n\n"
            f"👥 کاربران: **{fmt(s['users'])}**\n"
            f"🎮 بازی‌ها: **{fmt(s['games'])}**\n"
            f"💠 مجموع شرط: **{fmt(s['bets'])} {UNIT}**\n"
            f"🏆 مجموع جوایز: **{fmt(s['rewards'])} {UNIT}**\n"
            f"🎁 زیرمجموعه: **{fmt(s['refs'])}**\n"
            f"💰 مجموع موجودی: **{fmt(s['bal_total'])} {UNIT}**"
        )
        await q.answer()
        await q.edit_message_text(txt, reply_markup=admin_back(), parse_mode=ParseMode.MARKDOWN)
        return
    if data == "adm:users":
        users = get_all_users()[:30]
        txt = "👥 **آخرین کاربران**\n\n"
        for r in users:
            txt += f"• {r['first_name']} | `{r['user_id']}` | {fmt(r['balance'])} {UNIT}\n"
        await q.answer()
        await q.edit_message_text(txt, reply_markup=admin_back(), parse_mode=ParseMode.MARKDOWN)
        return
    if data == "adm:add":
        context.user_data["admin_action"] = "add"
        await q.answer()
        await q.edit_message_text("➕ آیدی عددی و مقدار رو بفرست:\n`123456789 500`",
                                  reply_markup=admin_back(), parse_mode=ParseMode.MARKDOWN)
        return
    if data == "adm:rem":
        context.user_data["admin_action"] = "rem"
        await q.answer()
        await q.edit_message_text("➖ آیدی عددی و مقدار رو بفرست:\n`123456789 200`",
                                  reply_markup=admin_back(), parse_mode=ParseMode.MARKDOWN)
        return
    if data == "adm:bc":
        context.user_data["admin_action"] = "bc"
        await q.answer()
        await q.edit_message_text("📢 متن پیام همگانی رو بفرست:",
                                  reply_markup=admin_back(), parse_mode=ParseMode.MARKDOWN)
        return

async def admin_text(update, context):
    u = update.effective_user
    if not is_admin(u.id): return
    if update.effective_chat.type != ChatType.PRIVATE: return
    action = context.user_data.get("admin_action")
    if not action: return

    txt = update.message.text.strip()
    context.user_data.pop("admin_action", None)

    if action in ("add", "rem"):
        parts = normalize_digits(txt).split()
        if len(parts) != 2 or not parts[0].lstrip("-").isdigit() or not parts[1].isdigit():
            await update.message.reply_text("❌ فرمت اشتباه. مثال:\n`123456789 500`", parse_mode=ParseMode.MARKDOWN)
            return
        tid, amt = int(parts[0]), int(parts[1])
        if not get_user(tid):
            await update.message.reply_text("❌ کاربر پیدا نشد.")
            return
        if action == "add":
            add_balance(tid, amt)
            await update.message.reply_text(f"✅ {fmt(amt)} {UNIT} به `{tid}` اضافه شد.", parse_mode=ParseMode.MARKDOWN)
            try: await context.bot.send_message(tid, f"🎁 {fmt(amt)} {UNIT} به موجودی‌ات اضافه شد!")
            except: pass
        else:
            add_balance(tid, -amt)
            await update.message.reply_text(f"✅ {fmt(amt)} {UNIT} از `{tid}` کم شد.", parse_mode=ParseMode.MARKDOWN)
        return

    if action == "bc":
        users = get_all_users()
        sent, failed = 0, 0
        await update.message.reply_text(f"📢 در حال ارسال به {len(users)} کاربر...")
        for r in users:
            try:
                await context.bot.send_message(r["user_id"], f"📢 **اطلاعیه**\n\n{txt}", parse_mode=ParseMode.MARKDOWN)
                sent += 1
            except: failed += 1
            await asyncio.sleep(0.05)
        await update.message.reply_text(f"✅ ارسال: {sent} | ❌ ناموفق: {failed}")
        return

# ================= Router =================
async def callback_router(update, context):
    data = update.callback_query.data
    if data == "check_join":
        await check_join_cb(update, context)
    elif data.startswith("u:"):
        await user_info_cb(update, context)
    elif data.startswith("init:"):
        await init_cb(update, context)
    elif data.startswith("roll:"):
        await roll_cb(update, context)
    elif data.startswith("join:"):
        await join_cb(update, context)
    elif data.startswith("cancel:"):
        await cancel_cb(update, context)
    elif data.startswith("adm:"):
        await admin_cb(update, context)

async def post_init(app: Application):
    await app.bot.set_my_commands([
        ("start", "شروع"),
        ("help", "راهنما"),
        ("balance", "موجودی"),
        ("profile", "پروفایل"),
        ("referral", "زیرمجموعه"),
        ("language", "زبان"),
    ])

# ================= Main =================
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("referral", cmd_referral))
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(CommandHandler("admin", cmd_admin))

    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
        group_text_router))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        admin_text))

    app.add_handler(CallbackQueryHandler(callback_router))

    log.info("🚀 داگز موج بات روشن شد.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
