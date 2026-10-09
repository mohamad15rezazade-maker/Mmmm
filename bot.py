# -*- coding: utf-8 -*-
import os
import re
import html
import asyncio
import random
import logging
import sqlite3
from datetime import datetime, timezone

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# =============== CONFIG ===============

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
GAME_GROUP_ID = int(os.getenv("GAME_GROUP_ID", "0"))

CHANNEL = "@BET_1XZX"
REQUIRED_GROUP = "@GAP_BAZIN1"

DB_FILE = "bot.db"
MIN_WITHDRAW = 2000
DEFAULT_REFERRAL_REWARD = 60
TRANSFER_FEE_PERCENT = 3

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("moj-bot")

games = {}
game_seq = 0
user_states = {}
db_lock = asyncio.Lock()


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def connect():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT NOT NULL DEFAULT '',
            balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS referrals(
            invitee_id INTEGER PRIMARY KEY,
            inviter_id INTEGER NOT NULL,
            reward INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transfers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            fee INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_transfers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            fee INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        INSERT OR IGNORE INTO settings(key,value)
        VALUES('referral_reward','60');
        """)


def ensure_user(user):
    if not user:
        return
    with connect() as con:
        con.execute("""
        INSERT INTO users(user_id,username,first_name,balance,created_at)
        VALUES(?,?,?,0,?)
        ON CONFLICT(user_id) DO UPDATE SET
        username=excluded.username,
        first_name=excluded.first_name
        """, (
            user.id, user.username, user.first_name or "", timestamp()
        ))


def get_user(uid):
    with connect() as con:
        return con.execute(
            "SELECT * FROM users WHERE user_id=?", (uid,)
        ).fetchone()


def get_balance(uid):
    row = get_user(uid)
    return int(row["balance"]) if row else 0


def setting(key, default):
    with connect() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else str(default)


def set_setting(key, value):
    with connect() as con:
        con.execute("""
        INSERT INTO settings(key,value) VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))


def is_admin(uid):
    return uid in ADMIN_IDS


def user_label(row):
    if row["username"]:
        return "@" + html.escape(row["username"])
    return str(row["user_id"])


async def membership_ok(context, uid):
    for chat in (CHANNEL, REQUIRED_GROUP):
        try:
            member = await context.bot.get_chat_member(chat, uid)
            if member.status in ("left", "kicked", "banned"):
                return False
        except TelegramError:
            return False
    return True


def join_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "عضویت در کانال", url="https://t.me/BET_1XZX"
        )],
        [InlineKeyboardButton(
            "عضویت در گروه", url="https://t.me/GAP_BAZIN1"
        )],
        [InlineKeyboardButton(
            "✅ عضو شدم", callback_data="check_join"
        )]
    ])


# =============== START ===============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    # Referral reward is granted once.
    if context.args and context.args[0].startswith("ref_"):
        try:
            inviter = int(context.args[0][4:])
        except ValueError:
            inviter = 0

        if inviter and inviter != user.id and get_user(inviter):
            reward = int(setting(
                "referral_reward", DEFAULT_REFERRAL_REWARD
            ))
            with connect() as con:
                exists = con.execute(
                    "SELECT 1 FROM referrals WHERE invitee_id=?",
                    (user.id,)
                ).fetchone()
                if not exists:
                    con.execute(
                        "INSERT INTO referrals VALUES(?,?,?,?)",
                        (user.id, inviter, reward, timestamp())
                    )
                    con.execute(
                        "UPDATE users SET balance=balance+? WHERE user_id=?",
                        (reward, inviter)
                    )

    # Deliver pending numeric-ID transfers.
    with connect() as con:
        pending = con.execute(
            "SELECT * FROM pending_transfers WHERE recipient_id=? ORDER BY id",
            (user.id,)
        ).fetchall()

        for item in pending:
            con.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (item["amount"], user.id)
            )
            con.execute("""
                INSERT INTO transfers
                (sender_id,recipient_id,amount,fee,status,created_at)
                VALUES(?,?,?,?,?,?)
            """, (
                item["sender_id"], user.id, item["amount"],
                item["fee"], "completed", timestamp()
            ))
            con.execute(
                "DELETE FROM pending_transfers WHERE id=?",
                (item["id"],)
            )

    await update.message.reply_text(
        "سلام رفیق 👋\n"
        "به موج بات خوش اومدی.\n\n"
        f"💰 موجودی داگز موج بات: {get_balance(user.id)}\n\n"
        "برای راهنما /help رو بزن."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📚 راهنمای موج بات\n\n"
        "/start - شروع\n"
        "/help - راهنما\n"
        "/balance - موجودی\n"
        "/profile - پروفایل\n"
        "/referral - لینک زیرمجموعه\n"
        "/language - زبان\n"
        "/withdraw 2000 - ثبت درخواست مجازی\n\n"
        "دستورات گروه:\n"
        "موجودی یا م - نمایش موجودی\n"
        "بازی‌ها - راهنمای بازی\n"
        "1 تاس 100\n"
        "2 بولینگ 500\n"
        "3 دارت 1000\n\n"
        "انتقال:\n"
        "انتقال 600 8935601841\n"
        "انتقال 600 @username\n"
        "یا روی پیام کاربر ریپلای کن:\n"
        "انتقال 600\n\n"
        "امتیازها مجازی‌اند؛ بازی‌ها شرط‌بندی واقعی ندارند."
    )


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    await update.effective_message.reply_text(
        f"💰 موجودی داگز موج بات شما: {get_balance(user.id)}"
    )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    row = get_user(user.id)
    await update.effective_message.reply_text(
        f"👤 پروفایل\nشناسه: {user.id}\n"
        f"نام کاربری: @{user.username or 'ندارد'}\n"
        f"موجودی داگز موج بات: {row['balance']}"
    )


async def language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("زبان ربات فارسی است 🇮🇷")


async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{user.id}"
    await update.effective_message.reply_text(
        f"🔗 لینک دعوت شما:\n{link}\n"
        f"پاداش فعلی: {setting('referral_reward', 60)} داگز موج بات"
    )


# =============== TRANSFERS ===============

async def transfer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    sender = update.effective_user
    ensure_user(sender)

    parts = (message.text or "").split()
    if len(parts) not in (2, 3):
        await message.reply_text(
            "فرمت:\nانتقال 600 8935601841\n"
            "انتقال 600 @username\n"
            "یا ریپلای و بنویس انتقال 600"
        )
        return

    try:
        amount = int(parts[1])
    except ValueError:
        await message.reply_text("مبلغ باید عدد صحیح باشد.")
        return

    if amount < 1:
        await message.reply_text("مبلغ باید بیشتر از صفر باشد.")
        return

    recipient_id = None
    recipient = None

    if message.reply_to_message and len(parts) == 2:
        recipient = message.reply_to_message.from_user
        if recipient:
            recipient_id = recipient.id
            ensure_user(recipient)

    elif len(parts) == 3:
        target = parts[2]
        if target.isdigit():
            recipient_id = int(target)
            recipient = get_user(recipient_id)
        elif target.startswith("@"):
            username = target[1:].lower()
            with connect() as con:
                recipient = con.execute(
                    "SELECT * FROM users WHERE lower(username)=?",
                    (username,)
                ).fetchone()
            if recipient:
                recipient_id = recipient["user_id"]

    if not recipient_id:
        await message.reply_text(
            "گیرنده شناسایی نشد. برای انتقال با یوزرنیم، "
            "گیرنده باید قبلاً توسط ربات شناسایی شده باشد. "
            "از آیدی عددی هم می‌تونی استفاده کنی."
        )
        return

    if recipient_id == sender.id:
        await message.reply_text("نمی‌تونی به خودت انتقال بدی.")
        return

    fee = math.ceil(amount * TRANSFER_FEE_PERCENT / 100)
    total = amount + fee

    async with db_lock:
        with connect() as con:
            sender_row = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (sender.id,)
            ).fetchone()

            if not sender_row or sender_row["balance"] < total:
                await message.reply_text(
                    f"موجودی کافی نیست.\n"
                    f"مبلغ انتقال: {amount}\nکارمزد: {fee}\n"
                    f"مجموع لازم: {total} داگز موج بات"
                )
                return

            # Deduct from sender first; all DB changes are atomic.
            con.execute(
                "UPDATE users SET balance=balance-? WHERE user_id=?",
                (total, sender.id)
            )

            owner_id = next(iter(ADMIN_IDS), None)
            if owner_id:
                con.execute("""
                    INSERT INTO users(user_id,username,first_name,balance,created_at)
                    VALUES(?,NULL,'مالک',0,?)
                    ON CONFLICT(user_id) DO NOTHING
                """, (owner_id, timestamp()))
                con.execute(
                    "UPDATE users SET balance=balance+? WHERE user_id=?",
                    (fee, owner_id)
                )

            recipient_row = con.execute(
                "SELECT 1 FROM users WHERE user_id=?",
                (recipient_id,)
            ).fetchone()

            if recipient_row:
                con.execute(
                    "UPDATE users SET balance=balance+? WHERE user_id=?",
                    (amount, recipient_id)
                )
                status = "completed"
                con.execute("""
                    INSERT INTO transfers
                    (sender_id,recipient_id,amount,fee,status,created_at)
                    VALUES(?,?,?,?,?,?)
                """, (
                    sender.id, recipient_id, amount, fee,
                    status, timestamp()
                ))
            else:
                # Pending transfer to a numeric ID not yet registered.
                con.execute("""
                    INSERT INTO pending_transfers
                    (sender_id,recipient_id,amount,fee,created_at)
                    VALUES(?,?,?,?,?)
                """, (
                    sender.id, recipient_id, amount, fee, timestamp()
                ))
                status = "pending"

    if status == "completed":
        await message.reply_text(
            f"✅ انتقال انجام شد.\n"
            f"گیرنده: {recipient_id}\n"
            f"مبلغ دریافتی: {amount}\n"
            f"کارمزد ۳٪: {fee}\n"
            f"موجودی شما: {get_balance(sender.id)}"
        )
    else:
        await message.reply_text(
            f"📨 انتقال {amount} داگز موج بات برای آیدی "
            f"{recipient_id} ثبت شد و تا شروع ربات توسط گیرنده معلق می‌ماند.\n"
            f"کارمزد: {fee}"
        )


# =============== VIRTUAL WITHDRAW REQUESTS ===============

async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "فرمت: /withdraw 2000\nحداقل درخواست: ۲۰۰۰ داگز موج بات"
        )
        return

    amount = int(context.args[0])
    if amount < MIN_WITHDRAW:
        await update.effective_message.reply_text(
            "حداقل درخواست ۲۰۰۰ داگز موج بات است."
        )
        return

    with connect() as con:
        con.execute("""
            INSERT INTO withdrawals(user_id,amount,status,created_at)
            VALUES(?,?,?,?)
        """, (user.id, amount, "pending", timestamp()))

    await update.effective_message.reply_text(
        "درخواست مجازی شما ثبت شد و منتظر بررسی ادمین است. "
        "این درخواست پرداخت واقعی ایجاد نمی‌کند."
    )

    for admin in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin,
                f"📥 درخواست مجازی\nکاربر: {user.id}\nمبلغ: {amount}"
            )
        except TelegramError:
            pass


# =============== GROUP GAMES ===============

GAME_EMOJIS = {
    "تاس": "🎲",
    "بولینگ": "🎳",
    "دارت": "🎯",
}


async def create_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global game_seq

    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if GAME_GROUP_ID and chat.id != GAME_GROUP_ID:
        await message.reply_text("بازی فقط در گروه تعیین‌شده فعال است.")
        return

    if not await membership_ok(context, user.id):
        await message.reply_text(
            "برای بازی اول عضو کانال و گروه شو.",
            reply_markup=join_keyboard()
        )
        return

    match = re.fullmatch(
        r"\s*([1-3])\s+(تاس|بولینگ|دارت)\s+(\d+)\s*",
        message.text or ""
    )
    if not match:
        return

    rolls = int(match.group(1))
    game_type = match.group(2)
    display_amount = int(match.group(3))

    if not 70 <= display_amount <= 3000:
        await message.reply_text(
            "عدد واردشده باید بین ۷۰ تا ۳۰۰۰ باشد. "
            "این عدد فقط برچسب چالش است و موجودی را تغییر نمی‌دهد."
        )
        return

    game_seq += 1
    gid = game_seq
    games[gid] = {
        "creator": user.id,
        "creator_name": user.first_name,
        "chat_id": chat.id,
        "rolls": rolls,
        "type": game_type,
        "display_amount": display_amount,
        "state": "waiting",
    }

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤖 بازی با ربات",
                                 callback_data=f"botgame:{gid}"),
            InlineKeyboardButton("👥 بازی با دوستان",
                                 callback_data=f"friendgame:{gid}")
        ],
        [InlineKeyboardButton("❌ لغو", callback_data=f"cancel:{gid}")]
    ])

    await message.reply_text(
        f"🎮 چالش جدید\n"
        f"بازی: {GAME_EMOJIS[game_type]} {game_type}\n"
        f"تعداد پرتاب: {rolls}\n"
        f"عدد چالش: {display_amount} (صرفاً نمایشی)\n"
        f"سازنده: {html.escape(user.first_name)}\n\n"
        f"امتیازها تغییر نمی‌کنند و شرط‌بندی وجود ندارد.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    async def expire():
        await asyncio.sleep(120)
        game = games.get(gid)
        if game and game["state"] == "waiting":
            game["state"] = "expired"
            try:
                await context.bot.send_message(
                    chat.id, f"⌛ چالش شماره {gid} منقضی شد."
                )
            except TelegramError:
                pass

    asyncio.create_task(expire())


async def do_roll(context, chat_id, uid, game_type, rolls):
    emoji = {
        "تاس": "🎲",
        "بولینگ": "🎳",
        "دارت": "🎯",
    }[game_type]
    scores = []

    for _ in range(rolls):
        result = await context.bot.send_dice(chat_id=chat_id, emoji=emoji)
        scores.append(result.dice.value)
        await asyncio.sleep(1)

    return sum(scores)


async def game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    ensure_user(user)

    data = query.data.split(":")
    action = data[0]

    if action == "check_join":
        if await membership_ok(context, user.id):
            await query.edit_message_text("عضویت تأیید شد ✅")
        else:
            await query.answer("هنوز عضو هر دو مورد نشده‌ای.", show_alert=True)
        return

    if len(data) != 2 or not data[1].isdigit():
        return

    gid = int(data[1])
    game = games.get(gid)
    if not game:
        await query.answer("این بازی وجود ندارد یا منقضی شده.", show_alert=True)
        return

    if action == "cancel":
        if user.id != game["creator"]:
            await query.answer("فقط سازنده می‌تواند لغو کند.", show_alert=True)
            return
        game["state"] = "cancelled"
        await query.edit_message_text("بازی لغو شد. موجودی تغییر نکرد.")
        return

    if game["state"] != "waiting":
        await query.answer("این چالش قبلاً شروع شده.", show_alert=True)
        return

    if action == "botgame":
        if user.id != game["creator"]:
            await query.answer("فقط سازنده می‌تواند بازی را شروع کند.", show_alert=True)
            return

        game["state"] = "playing"
        await query.edit_message_text("🎮 بازی شروع شد!")

        try:
            player_score = await do_roll(
                context, game["chat_id"], user.id,
                game["type"], game["rolls"]
            )
            bot_score = await do_roll(
                context, game["chat_id"], user.id,
                game["type"], game["rolls"]
            )

            if player_score > bot_score:
                result = "🏆 تو بردی!"
            elif player_score < bot_score:
                result = "🤖 این بار ربات برد."
            else:
                result = "🤝 مساوی شد!"

            await context.bot.send_message(
                game["chat_id"],
                f"{result}\nنتیجه تو: {player_score}\n"
                f"نتیجه ربات: {bot_score}\n"
                f"موجودی تغییر نکرد."
            )
        except TelegramError:
            await context.bot.send_message(
                game["chat_id"], "بازی به دلیل خطای تلگرام متوقف شد."
            )
        finally:
            games.pop(gid, None)

    elif action == "friendgame":
        if user.id == game["creator"]:
            await query.answer("سازنده نمی‌تواند حریف خودش باشد.", show_alert=True)
            return

        if not await membership_ok(context, user.id):
            await query.answer("اول عضو کانال و گروه شو.", show_alert=True)
            return

        game["state"] = "playing"
        game["opponent"] = user.id
        await query.edit_message_text(
            f"بازی شروع شد!\n"
            f"{html.escape(game['creator_name'])} در برابر "
            f"{html.escape(user.first_name)}",
            parse_mode="HTML"
        )

        try:
            first = await do_roll(
                context, game["chat_id"], game["creator"],
                game["type"], game["rolls"]
            )
            second = await do_roll(
                context, game["chat_id"], user.id,
                game["type"], game["rolls"]
            )

            if first > second:
                result = "سازنده برنده شد 🏆"
            elif second > first:
                result = "حریف برنده شد 🏆"
            else:
                result = "بازی مساوی شد 🤝"

            await context.bot.send_message(
                game["chat_id"],
                f"🎮 نتیجه بازی\n{result}\n"
                f"نتیجه سازنده: {first}\nنتیجه حریف: {second}\n"
                f"موجودی هیچ‌کس تغییر نکرد."
            )
        except TelegramError:
            await context.bot.send_message(
                game["chat_id"], "بازی به دلیل خطای تلگرام متوقف شد."
            )
        finally:
            games.pop(gid, None)


# =============== ADMIN PANEL ===============

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 آمار", callback_data="adm:stats")],
        [InlineKeyboardButton("👥 کاربران", callback_data="adm:users:0")],
        [InlineKeyboardButton("➕ افزایش موجودی", callback_data="adm:add")],
        [InlineKeyboardButton("➖ کسر موجودی", callback_data="adm:sub")],
        [InlineKeyboardButton("🔗 تغییر پاداش زیرمجموعه",
                              callback_data="adm:reward")],
        [InlineKeyboardButton("📥 درخواست‌های برداشت",
                              callback_data="adm:withdrawals")],
        [InlineKeyboardButton("📣 پیام همگانی", callback_data="adm:broadcast")]
    ])


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.effective_message.reply_text("دسترسی نداری.")
        return
    await update.effective_message.reply_text(
        "🛠 پنل مدیریت موج بات", reply_markup=admin_keyboard()
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if not is_admin(user.id):
        await query.answer("دسترسی نداری.", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1]

    if action == "stats":
        with connect() as con:
            users = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            total = con.execute("SELECT COALESCE(SUM(balance),0) n FROM users").fetchone()["n"]
            transfers = con.execute("SELECT COUNT(*) n FROM transfers").fetchone()["n"]
        await query.edit_message_text(
            f"📊 آمار\nکاربران: {users}\n"
            f"مجموع موجودی: {total}\nانتقال‌های ثبت‌شده: {transfers}",
            reply_markup=admin_keyboard()
        )

    elif action == "users":
        page = int(parts[2]) if len(parts) > 2 else 0
        with connect() as con:
            rows = con.execute(
                "SELECT * FROM users ORDER BY user_id LIMIT 20 OFFSET ?",
                (page * 20,)
            ).fetchall()
        if not rows:
            text = "کاربری در این صفحه نیست."
        else:
            text = "\n".join(
                f"{page * 20 + i + 1}_ {user_label(row)} "
                f"{row['balance']} داگز موج بات"
                for i, row in enumerate(rows)
            )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("صفحه بعد ➡️",
                                  callback_data=f"adm:users:{page+1}")],
            [InlineKeyboardButton("بازگشت", callback_data="adm:home")]
        ])
        await query.edit_message_text(text, reply_markup=keyboard)

    elif action in ("add", "sub", "reward", "broadcast"):
        user_states[user.id] = action
        prompts = {
            "add": "شناسه عددی و مبلغ را بفرست:\nمثال: 123456789 500",
            "sub": "شناسه عددی و مبلغ را بفرست:\nمثال: 123456789 200",
            "reward": "مبلغ پاداش جدید را بفرست؛ مثلاً 40",
            "broadcast": "متن پیام همگانی را بفرست."
        }
        await query.message.reply_text(prompts[action])

    elif action == "withdrawals":
        with connect() as con:
            rows = con.execute("""
                SELECT * FROM withdrawals WHERE status='pending'
                ORDER BY id DESC LIMIT 20
            """).fetchall()
        if not rows:
            text = "درخواست معلقی وجود ندارد."
        else:
            text = "\n".join(
                f"#{r['id']} | کاربر {r['user_id']} | "
                f"{r['amount']} داگز موج بات"
                for r in rows
            )
        await query.edit_message_text(
            text, reply_markup=admin_keyboard()
        )

    elif action == "home":
        await query.edit_message_text(
            "🛠 پنل مدیریت", reply_markup=admin_keyboard()
        )


async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id) or user.id not in user_states:
        return

    action = user_states.pop(user.id)
    text = (update.effective_message.text or "").strip()

    if action in ("add", "sub"):
        parts = text.split()
        if len(parts) != 2 or not all(x.isdigit() for x in parts):
            await update.effective_message.reply_text(
                "فرمت نادرست است. دوباره /admin را بزن."
            )
            return

        uid, amount = map(int, parts)
        if amount <= 0 or not get_user(uid):
            await update.effective_message.reply_text(
                "کاربر پیدا نشد یا مبلغ نامعتبر است."
            )
            return

        if action == "add":
            with connect() as con:
                con.execute(
                    "UPDATE users SET balance=balance+? WHERE user_id=?",
                    (amount, uid)
                )
            result = "موجودی افزایش یافت."
        else:
            with connect() as con:
                cur = con.execute("""
                    UPDATE users SET balance=balance-?
                    WHERE user_id=? AND balance>=?
                """, (amount, uid, amount))
            result = (
                "موجودی کسر شد." if cur.rowcount
                else "موجودی کاربر کافی نیست."
            )

        await update.effective_message.reply_text(result)

    elif action == "reward":
        if not text.isdigit():
            await update.effective_message.reply_text("فقط عدد بفرست.")
            return
        set_setting("referral_reward", int(text))
        await update.effective_message.reply_text(
            f"پاداش زیرمجموعه به {text} داگز موج بات تغییر کرد."
        )

    elif action == "broadcast":
        with connect() as con:
            rows = con.execute("SELECT user_id FROM users").fetchall()
        sent = 0
        for row in rows:
            try:
                await context.bot.send_message(row["user_id"], text)
                sent += 1
            except TelegramError:
                pass
        await update.effective_message.reply_text(
            f"پیام همگانی برای {sent} کاربر ارسال شد."
        )


# =============== GROUP TEXT ROUTER ===============

async def group_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return

    ensure_user(user)
    text = message.text.strip()

    if text in ("م", "موجودی"):
        await balance_command(update, context)
        return

    if text == "بازی‌ها":
        await message.reply_text(
            "🎮 بازی‌های گروه:\n"
            "1 تاس 100\n2 بولینگ 500\n3 دارت 1000\n"
            "حداکثر ۳ پرتاب. عدد آخر فقط نمایشی است؛ "
            "موجودی تغییر نمی‌کند."
        )
        return

    if text.startswith("انتقال "):
        await transfer(update, context)
        return

    if re.fullmatch(r"[1-3]\s+(تاس|بولینگ|دارت)\s+\d+", text):
        await create_game(update, context)


async def private_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    if update.effective_user.id in user_states:
        await admin_text(update, context)
        return
    if (update.effective_message.text or "").startswith("انتقال "):
        await transfer(update, context)


async def error_handler(update, context):
    log.exception("Unhandled error", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN در متغیرهای محیطی تنظیم نشده است.")

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("referral", referral))
    app.add_handler(CommandHandler("language", language))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CommandHandler("admin", admin))

    app.add_handler(CallbackQueryHandler(game_callback,
                                         pattern=r"^(check_join|botgame:|friendgame:|cancel:)"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))

    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
        group_router
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        private_router
    ))

    app.add_error_handler(error_handler)
    log.info("Moj Bot is running")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
