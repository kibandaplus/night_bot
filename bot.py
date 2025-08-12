import os
import re
import logging
from datetime import datetime, timedelta
import pytz
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
     MessageHandler, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
import dns.resolver
import asyncio
import sys

# Optional SRV check (no failures stop)
try:
    print(dns.resolver.resolve('_mongodb._tcp.cluster0.tthivro.mongodb.net', 'SRV'))
except Exception:
    pass

# ====== LOAD ENV ======
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID"))
SUBSCRIPTIONS_GROUP_ID = int(os.getenv("SUBSCRIPTIONS_GROUP_ID"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))  # optional

# ====== LOGGING ======
class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[37m",   # White
        logging.INFO: "\033[36m",    # Cyan
        logging.WARNING: "\033[33m", # Yellow
        logging.ERROR: "\033[31m",   # Red
        logging.CRITICAL: "\033[41m" # Red background
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        message = super().format(record)
        return f"{color}{message}{self.RESET}"

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(ColorFormatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    "%Y-%m-%d %H:%M:%S"
))

logger = logging.getLogger("subscription-bot")
logger.setLevel(logging.INFO)
logger.addHandler(handler)
logger.propagate = False

# ====== TIMEZONE ======
TZ = pytz.timezone("Africa/Nairobi")

# ====== SUBSCRIPTION TIERS ======
SUB_TIERS = {
    "1H": {"price": 10, "duration": timedelta(hours=1)},
    "1D": {"price": 60, "duration": timedelta(days=1)},
    "1W": {"price": 150, "duration": timedelta(weeks=1)},
    "1M": {"price": 599, "duration": timedelta(days=30)},
}

PAYMENT_DETAILS = (
    "Payment Instructions:\n\n"
    "Send the exact amount for your chosen tier to:\n"
    "Till Number: `123456`\n\n"
    "Then confirm your payment with:\n"
    "/confirm <M-Pesa phone number>"
)

# ====== DB SETUP ======
client = MongoClient(MONGO_URI)
db = client["subscription_bot"]
members_col = db["members"]
payments_col = db["payments"]

# ====== HELPERS ======
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def valid_phone(phone: str) -> bool:
    # Accept +2547..., 07..., 01...
    return re.fullmatch(r"(\+254|0)(1|7)\d{8}", phone) is not None

def fmt_dt_naq(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)
    return dt.astimezone(TZ).strftime("%A, %d %B %Y at %I:%M %p %Z")

def time_left_text(dt: datetime) -> str:
    now = datetime.utcnow().replace(tzinfo=pytz.UTC).astimezone(TZ)
    exp = dt if dt.tzinfo else pytz.UTC.localize(dt)
    exp = exp.astimezone(TZ)
    rem = exp - now
    if rem.total_seconds() <= 0:
        return "less than a minute"
    days = rem.days
    hours = rem.seconds // 3600
    minutes = (rem.seconds % 3600) // 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "less than a minute"

def ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else pytz.UTC.localize(dt)

# ====== USER COMMANDS ======
async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "That command doesn’t exist. Neither does your attention span."
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
         "Welcome to the subscription bot. You pay, you play. You don’t, you’re out.\n\n"
    "Commands:\n"
    "/pay – See how much you’re about to give us.\n"
    "/status – See if you still belong here.\n"
    "/confirm <phone> – The exact phone number you used to send the payment. "
    "If you mess this up, that’s on you.\n"
    )
    await update.message.reply_text(text)

async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    member = members_col.find_one({"user_id": user_id, "status": "active"})
    if member and member.get("active_until") and ensure_utc(member["active_until"]) > datetime.utcnow().replace(tzinfo=pytz.UTC):
        expiry = fmt_dt_naq(member["active_until"])
        await update.message.reply_text(f"⚠️ You already have an active subscription until {expiry}.")
        return

    buttons = []
    for code, info in SUB_TIERS.items():
        buttons.append([InlineKeyboardButton(f"{code} - KES {info['price']}", callback_data=f"tier_{code}")])
    await update.message.reply_text("Pick a subscription tier. The higher you pay, the less we mock you.", reply_markup=InlineKeyboardMarkup(buttons))

async def tier_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    member = members_col.find_one({"user_id": user_id, "status": "active"})
    if member and member.get("active_until") and ensure_utc(member["active_until"]) > datetime.utcnow().replace(tzinfo=pytz.UTC):
        expiry = fmt_dt_naq(member["active_until"])
        await query.edit_message_text(f"⚠️ You already have an active subscription until {expiry}.")
        return

    tier_code = query.data.split("_", 1)[1]
    tier = SUB_TIERS.get(tier_code)
    if not tier:
        await query.edit_message_text("⚠️ Invalid tier selected.")
        return

    # Store pending tier in user_data so /confirm knows it
    context.user_data["pending_tier"] = tier_code

    await query.edit_message_text(
        f"✅ You selected {tier_code}.\n"
        f"Amount: KES {tier['price']}\n\n"
        f"{PAYMENT_DETAILS}"
    )

async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("❌ Usage: /confirm <phone_number>")
        return

    phone = context.args[0]
    user = update.effective_user
    user_id = user.id

    if not valid_phone(phone):
        await update.message.reply_text("❌ Invalid phone format. Use 07XXXXXXXX or +2547XXXXXXXX.")
        return

    member = members_col.find_one({"user_id": user_id})
    if member and member.get("status") == "active" and member.get("active_until") and ensure_utc(member["active_until"]) > datetime.utcnow().replace(tzinfo=pytz.UTC):
        expiry = fmt_dt_naq(member["active_until"])
        await update.message.reply_text(f"⚠️ You already have an active subscription until {expiry}.")
        return

    pending_tier = context.user_data.get("pending_tier") or (member.get("pending_tier") if member else None)
    if not pending_tier:
        await update.message.reply_text("⚠️ Please select a subscription tier first with /pay.")
        return

    # Insert payment request
    payments_col.insert_one({
        "user_id": user_id,
        "username": user.username,
        "full_name": f"{user.first_name} {user.last_name or ''}".strip(),
        "tier": pending_tier,
        "phone": phone,
        "timestamp": datetime.utcnow(),
        "status": "pending"
    })

    # mark member pending and store tier
    members_col.update_one(
        {"user_id": user_id},
        {"$set": {"status": "pending", "pending_tier": pending_tier, "active_until": None}},
        upsert=True
    )

    # Send confirmation to user
    await update.message.reply_text(f"✅ Payment confirmation received for {pending_tier}. Awaiting admin approval.")

    # send admin notification with approve/reject buttons (approve carries tier)
    approve_cb = InlineKeyboardButton("Approve ✅", callback_data=f"approve_{user_id}_{pending_tier}")
    reject_cb = InlineKeyboardButton("Reject ❌", callback_data=f"reject_{user_id}")
    keyboard = InlineKeyboardMarkup([[approve_cb, reject_cb]])

    admin_msg = (
        f"New subscription request\n\n"
        f"User: [{user.first_name}](tg://user?id={user_id}) (@{user.username or 'N/A'})\n"
        f"Tier: {pending_tier} - KES {SUB_TIERS[pending_tier]['price']}\n"
        f"Phone: `{phone}`\n"
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    await context.bot.send_message(chat_id=SUBSCRIPTIONS_GROUP_ID, text=admin_msg, parse_mode="Markdown", reply_markup=keyboard)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    member = members_col.find_one({"user_id": user_id})
    if not member or member.get("status") != "active" or not member.get("active_until"):
        await update.message.reply_text("⚠️ You have no active subscription.")
        return

    expiry = ensure_utc(member["active_until"])
    expiry_text = fmt_dt_naq(expiry)
    left = time_left_text(expiry)
    await update.message.reply_text(f"✅ Active until {expiry_text}\nTime left: {left}")

# ====== ADMIN COMMANDS ======
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("_")

    # patterns: approve_<user_id>_<tier>  OR reject_<user_id>
    if parts[0] == "approve" and len(parts) == 3:
        actor = query.from_user.id
        if not is_admin(actor):
            await query.edit_message_text("❌ Unauthorized.")
            return

        user_id = int(parts[1])
        tier = parts[2]
        if tier not in SUB_TIERS:
            await query.edit_message_text("⚠️ Unknown tier.")
            return

        duration = SUB_TIERS[tier]["duration"]
        expiry_dt = datetime.utcnow().replace(tzinfo=pytz.UTC) + duration

        members_col.update_one(
            {"user_id": user_id},
            {"$set": {"status": "active", "active_until": expiry_dt}, "$unset": {"pending_tier": ""}}
        )
        payments_col.update_one({"user_id": user_id, "status": "pending"}, {"$set": {"status": "approved", "admin_id": actor, "approved_at": datetime.utcnow()}})

        # clear any reminder flags if present
        members_col.update_one({"user_id": user_id}, {"$unset": {"reminder_sent": ""}})

        # send invite link and notify user
        try:
            invite = await context.bot.create_chat_invite_link(chat_id=GROUP_CHAT_ID, member_limit=1, expire_date=int((datetime.utcnow() + timedelta(hours=24)).timestamp()))
            await context.bot.send_message(chat_id=user_id, text=f"✅ Your subscription is active until {fmt_dt_naq(expiry_dt)}. Join: {invite.invite_link}")
        except Exception:
            # fallback: notify without link
            await context.bot.send_message(chat_id=user_id, text=f"✅ Your subscription is active until {fmt_dt_naq(expiry_dt)}.")

        await query.edit_message_text(f"✅ Approved {user_id} for {tier}.")
        if LOG_CHANNEL_ID:
            await context.bot.send_message(LOG_CHANNEL_ID, f"Approved {user_id} for {tier} until {fmt_dt_naq(expiry_dt)}")

    elif parts[0] == "reject" and len(parts) == 2:
        actor = query.from_user.id
        if not is_admin(actor):
            await query.edit_message_text("❌ Unauthorized.")
            return

        user_id = int(parts[1])
        payments_col.update_many({"user_id": user_id, "status": "pending"}, {"$set": {"status": "rejected", "admin_id": actor, "rejected_at": datetime.utcnow()}})
        members_col.update_one({"user_id": user_id}, {"$set": {"status": "rejected"}, "$unset": {"pending_tier": ""}})

        try:
            await context.bot.send_message(chat_id=user_id, text="❌ Your payment was rejected.")
        except Exception:
            pass

        await query.edit_message_text(f"❌ Rejected subscription for {user_id}.")
        if LOG_CHANNEL_ID:
            await context.bot.send_message(LOG_CHANNEL_ID, f"Rejected subscription for {user_id}")

# Admin dashboard
async def subs_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Unauthorized.")
        return

    total_active = members_col.count_documents({"status": "active"})
    total_expired = members_col.count_documents({"status": "expired"})
    total_pending = payments_col.count_documents({"status": "pending"})

    text = (
        f"Subscriptions dashboard\n\n"
        f"Total active: {total_active}\n"
        f"Total expired: {total_expired}\n"
        f"Pending payments: {total_pending}\n\n"
        "Use /history <user_id> to view a user's payment history."
    )
    await update.message.reply_text(text)

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Unauthorized.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("❌ Usage: /history <user_id>")
        return

    target_id = int(context.args[0])
    records = list(payments_col.find({"user_id": target_id}).sort("timestamp", -1).limit(20))
    if not records:
        await update.message.reply_text("No payment history found for that user.")
        return

    lines = []
    for r in records:
        t = r.get("timestamp")
        tstr = fmt_dt_naq(t) if t else "unknown"
        lines.append(f"{tstr} — Tier: {r.get('tier')} — Status: {r.get('status')} — Phone: {r.get('phone')}")
    await update.message.reply_text("\n".join(lines))

# ====== BACKGROUND TASKS (APSCHEDULER) ======
async def check_expiries(app):
    """
    Mark expired subscriptions in DB, kick from group, unban for rejoin, and DM them.
    """
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    async_bot = app.bot

    logger.info(f"[check_expiries] Running expiry check at {now_utc.isoformat()} UTC")

    # Get expired active users
    expired_members = list(members_col.find({
        "status": "active",
        "active_until": {"$lt": now_utc}
    }))

    logger.info(f"[check_expiries] Found {len(expired_members)} expired users to process.")

    for member in expired_members:
        uid = member["user_id"]
        logger.info(f"[check_expiries] Processing user {uid}")

        # 1. Update DB status
        members_col.update_one(
            {"user_id": uid},
            {"$set": {"status": "expired"}}
        )
        logger.info(f"[check_expiries] Marked user {uid} as expired in DB.")

        # 2. Ban + unban to allow rejoining on renewal
        try:
            await async_bot.ban_chat_member(chat_id=GROUP_CHAT_ID, user_id=uid)
            await async_bot.unban_chat_member(chat_id=GROUP_CHAT_ID, user_id=uid)
            logger.info(f"[check_expiries] Removed user {uid} from group.")
        except Exception as e:
            logger.warning(f"[check_expiries] Failed to remove {uid}: {e}")

        # 3. Send expiry DM
        expiry_dt = ensure_utc(member.get("active_until")) if member.get("active_until") else now_utc
        expiry_str = fmt_dt_naq(expiry_dt)  # Your custom date formatter
        try:
            await async_bot.send_message(
                chat_id=uid,
                text=f"❌ Your subscription expired on {expiry_str}.\nRenew with /pay."
            )
            logger.info(f"[check_expiries] Sent DM to user {uid}.")
        except Exception as e:
            logger.warning(f"[check_expiries] Failed to DM {uid}: {e}")

        # 4. Log to admin channel
        if LOG_CHANNEL_ID:
            try:
                await async_bot.send_message(
                    chat_id=LOG_CHANNEL_ID,
                    text=f"Expired user {uid} at {expiry_str}"
                )
                logger.info(f"[check_expiries] Logged expiry for {uid} to admin channel.")
            except Exception as e:
                logger.warning(f"[check_expiries] Failed to log expiry for {uid}: {e}")

    logger.info(f"[check_expiries] Expiry check complete. Processed {len(expired_members)} users.")



async def send_reminders(app):
    """
    Send reminder 24 hours before expiry. Use a flag 'reminder_sent' to avoid duplicates.
    """
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    window_end = now_utc + timedelta(hours=24)
    async_bot = app.bot
    cursor = members_col.find({
        "status": "active",
        "active_until": {"$gte": now_utc, "$lte": window_end},
        "reminder_sent": {"$ne": True}
    })
    for member in cursor:
        uid = member["user_id"]
        expiry_dt = ensure_utc(member.get("active_until"))
        try:
            await async_bot.send_message(chat_id=uid, text=f"⚠️ Your subscription ends tomorrow ({fmt_dt_naq(expiry_dt)}). Renew with /pay.")
            members_col.update_one({"user_id": uid}, {"$set": {"reminder_sent": True}})
        except Exception:
            pass

# ====== MAIN & SETUP ======
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # user handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("status", status))

    # admin handlers
    app.add_handler(CallbackQueryHandler(tier_select, pattern="^tier_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^(approve|reject)_"))
    app.add_handler(CommandHandler("subs", subs_dashboard))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # scheduler — use current loop
    loop = asyncio.get_event_loop()
    scheduler = AsyncIOScheduler(event_loop=loop)
    # scheduler.add_job(check_expiries, "interval", hours=1, args=[app])
    scheduler.add_job(check_expiries, "interval", minutes=2, args=[app])
    scheduler.add_job(send_reminders, "interval", hours=1, args=[app])
    scheduler.start()

    logger.info("Bot starting...")
    app.run_polling()  # no asyncio.run()

if __name__ == "__main__":
    main()
