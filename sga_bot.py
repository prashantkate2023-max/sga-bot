import os
import logging
import sqlite3
import threading
from datetime import datetime, time
from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
import razorpay

# ================== CONFIG FROM ENVIRONMENT ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOSS_ID = int(os.getenv("BOSS_ID", "0"))
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

if not BOT_TOKEN or not BOSS_ID:
    raise ValueError("BOT_TOKEN and BOSS_ID must be set in environment variables")

# ================== RAZORPAY CLIENT ==================
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)) if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET else None

# ================== FLASK APP ==================
app = Flask(__name__)

# ================== DATABASE ==================
def init_db():
    conn = sqlite3.connect("sga.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS employees (
        user_id INTEGER PRIMARY KEY,
        name TEXT,
        skills TEXT,
        platforms TEXT,
        hours TEXT,
        total_earned INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS daily_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        date TEXT,
        earned INTEGER DEFAULT 0,
        skill TEXT,
        tasks TEXT,
        score TEXT,
        raw_report TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS payments (
        payment_id TEXT PRIMARY KEY,
        amount INTEGER,
        purpose TEXT,
        status TEXT,
        created_at TEXT
    )""")
    defaults = {
        "paused": "false",
        "daily_target": "3000",
        "forced_skill": "High-Ticket Closing Basics",
        "focus": "WhatsApp + LinkedIn",
        "agents": "Sales Agent,Learning Agent"
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()

def get_setting(key, default=None):
    conn = sqlite3.connect("sga.db")
    c = conn.cursor()
    c.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key, value):
    conn = sqlite3.connect("sga.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

# ================== HELPERS ==================
def is_boss(user_id: int) -> bool:
    return user_id == BOSS_ID

def create_razorpay_payment_link(amount: int, purpose: str = "Service Payment") -> str:
    if not razorpay_client:
        return "Razorpay not configured"
    try:
        payment_link = razorpay_client.payment_link.create({
            "amount": amount * 100,
            "currency": "INR",
            "accept_partial": False,
            "description": purpose,
            "notify": {"sms": True, "email": True},
            "reminder_enable": True,
            "notes": {"source": "SGA Bot"}
        })
        return payment_link.get("short_url", "Error creating link")
    except Exception as e:
        return f"Error: {str(e)}"

def get_tomorrow_plan() -> str:
    target = get_setting("daily_target", "3000")
    skill = get_setting("forced_skill", "High-Ticket Closing Basics")
    focus = get_setting("focus", "WhatsApp + LinkedIn")
    agents = get_setting("agents", "Sales Agent, Learning Agent")
    return f"""📅 TOMORROW’S PLAN

Date: {datetime.now().strftime('%d %b %Y')}

🎯 Non-Negotiable Targets:
1. Minimum Earning Target: ₹{target}
2. Skill to Learn: {skill}

📚 Learning Plan (45–60 min):
- Skill: {skill}
- Practice: Apply this skill in real conversations today

💼 Revenue Action Plan:
1. Contact at least 15 people on {focus}
2. Send strong offers + Razorpay links
3. Follow up properly
4. Close deals

🛠️ Active Sub-Agents:
{agents}

⏰ Submit before 8:00 PM:
- Earned amount
- Skill learned
- Tasks done
- Score out of 10

⚠️ No zero day. Payments only via Razorpay.
"""

# ================== RAZORPAY WEBHOOK ==================
@app.route("/razorpay-webhook", methods=["POST"])
def razorpay_webhook():
    if not RAZORPAY_WEBHOOK_SECRET:
        return jsonify({"status": "error", "message": "Webhook secret not set"}), 400

    payload = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")

    try:
        razorpay_client.utility.verify_webhook_signature(payload, signature, RAZORPAY_WEBHOOK_SECRET)
    except Exception as e:
        print(f"Webhook signature verification failed: {e}")
        return jsonify({"status": "error"}), 400

    data = request.get_json()
    event = data.get("event")

    if event in ["payment.captured", "payment_link.paid"]:
        try:
            payment_entity = data.get("payload", {}).get("payment", {}).get("entity", {})
            amount = payment_entity.get("amount", 0) // 100
            payment_id = payment_entity.get("id", "")
            notes = payment_entity.get("notes", {})
            purpose = notes.get("source", "Payment")

            # Save payment
            conn = sqlite3.connect("sga.db")
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO payments (payment_id, amount, purpose, status, created_at) VALUES (?, ?, ?, ?, ?)",
                      (payment_id, amount, purpose, "captured", datetime.now().isoformat()))
            
            # Add to first active employee (you can improve this later)
            c.execute("SELECT user_id FROM employees WHERE status = 'active' LIMIT 1")
            emp = c.fetchone()
            if emp:
                c.execute("UPDATE employees SET total_earned = total_earned + ? WHERE user_id = ?", (amount, emp[0]))
            conn.commit()
            conn.close()

            # Notify Boss
            if application:
                application.create_task(
                    application.bot.send_message(
                        chat_id=BOSS_ID,
                        text=f"✅ Automatic Payment Received!\n\nAmount: ₹{amount}\nPayment ID: {payment_id}\nPurpose: {purpose}"
                    )
                )
        except Exception as e:
            print(f"Error processing payment: {e}")

    return jsonify({"status": "ok"}), 200

@app.route("/")
def home():
    return "SGA Bot is running!"

# ================== TELEGRAM BOT ==================
application = None

async def send_daily_plan_job(context: ContextTypes.DEFAULT_TYPE):
    if get_setting("paused") == "true":
        return
    plan = get_tomorrow_plan()
    conn = sqlite3.connect("sga.db")
    c = conn.cursor()
    c.execute("SELECT user_id FROM employees WHERE status = 'active'")
    users = c.fetchall()
    conn.close()
    for (uid,) in users:
        try:
            await context.bot.send_message(chat_id=uid, text=plan)
        except:
            pass
    await context.bot.send_message(chat_id=BOSS_ID, text=f"✅ Automatic Daily Plan sent to {len(users)} employees.")

async def handle_boss_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_boss(update.effective_user.id):
        return
    text = update.message.text.strip().lower()
    original = update.message.text.strip()

    if text in ["status", "/status"]:
        conn = sqlite3.connect("sga.db")
        c = conn.cursor()
        c.execute("SELECT COUNT(*), SUM(total_earned) FROM employees")
        count, total = c.fetchone()
        conn.close()
        total = total or 0
        await update.message.reply_text(
            f"📊 SGA STATUS\n\nPaused: {get_setting('paused')}\nEmployees: {count}\nTotal Earned: ₹{total}\n"
            f"Daily Target: ₹{get_setting('daily_target')}\nFocus: {get_setting('focus')}\nSkill: {get_setting('forced_skill')}"
        )
        return

    if text in ["progress", "/progress"]:
        conn = sqlite3.connect("sga.db")
        c = conn.cursor()
        c.execute("SELECT SUM(total_earned) FROM employees")
        total = c.fetchone()[0] or 0
        conn.close()
        remaining = 20000000 - total
        await update.message.reply_text(f"📉 DEBT PROGRESS\n\nTotal Earned: ₹{total}\nRemaining: ₹{remaining}")
        return

    if text in ["pause", "/pause"]:
        set_setting("paused", "true")
        await update.message.reply_text("⏸ System PAUSED.")
        return

    if text in ["resume", "/resume"]:
        set_setting("paused", "false")
        await update.message.reply_text("▶️ System RESUMED.")
        return

    if text.startswith("set target"):
        try:
            amount = int(text.replace("set target", "").strip())
            set_setting("daily_target", amount)
            await update.message.reply_text(f"🎯 Target set to ₹{amount}")
        except:
            await update.message.reply_text("Usage: set target 5000")
        return

    if text.startswith("set skill"):
        skill = original.replace("set skill", "").strip()
        set_setting("forced_skill", skill)
        await update.message.reply_text(f"📚 Skill set to: {skill}")
        return

    if text.startswith("focus"):
        focus = original.replace("focus", "").strip()
        set_setting("focus", focus)
        await update.message.reply_text(f"🎯 Focus set to: {focus}")
        return

    if text == "send plan":
        if get_setting("paused") == "true":
            await update.message.reply_text("System is paused.")
            return
        plan = get_tomorrow_plan()
        conn = sqlite3.connect("sga.db")
        c = conn.cursor()
        c.execute("SELECT user_id FROM employees WHERE status = 'active'")
        users = c.fetchall()
        conn.close()
        for (uid,) in users:
            try:
                await context.bot.send_message(chat_id=uid, text=plan)
            except:
                pass
        await update.message.reply_text(f"✅ Plan sent to {len(users)} employees.")
        return

    if text.startswith("razorpay link"):
        try:
            parts = original.replace("razorpay link", "").strip().split(" ", 1)
            amount = int(parts[0])
            purpose = parts[1] if len(parts) > 1 else "Service Payment"
            link = create_razorpay_payment_link(amount, purpose)
            await update.message.reply_text(f"💳 Razorpay Link Created\n\nAmount: ₹{amount}\nPurpose: {purpose}\n\n{link}")
        except Exception as e:
            await update.message.reply_text(f"Usage: razorpay link 5000 Service Name\nError: {e}")
        return

    if text in ["today reports", "reports"]:
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect("sga.db")
        c = conn.cursor()
        c.execute("""SELECT e.name, d.raw_report FROM daily_records d
                     JOIN employees e ON d.user_id = e.user_id WHERE d.date = ?""", (today,))
        rows = c.fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text("No reports today.")
            return
        msg = "📊 TODAY’S REPORTS\n\n"
        for name, report in rows:
            msg += f"👤 {name}\n{report}\n\n----------\n\n"
        await update.message.reply_text(msg)
        return

    await update.message.reply_text(f"✅ Instruction received:\n\n{original}")

# Onboarding states
READY, ACCEPT, NAME, SKILLS, PLATFORMS, HOURS = range(6)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_boss(update.effective_user.id):
        await update.message.reply_text("Welcome Boss. Full control active.\nType any command.")
        return ConversationHandler.END
    await update.message.reply_text("Welcome to SGA.\n\nType READY to start onboarding.")
    return READY

async def ready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Rules:\n1. Earn every day\n2. Learn every day\n3. Razorpay only\n4. Boss has full control\n\nType I ACCEPT")
    return ACCEPT

async def accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter your Full Name:")
    return NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text
    await update.message.reply_text("Your existing skills?")
    return SKILLS

async def get_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["skills"] = update.message.text
    await update.message.reply_text("Active platforms? (LinkedIn / Instagram / WhatsApp)")
    return PLATFORMS

async def get_platforms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["platforms"] = update.message.text
    await update.message.reply_text("How many hours can you work daily?")
    return HOURS

async def get_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["hours"] = update.message.text
    user_id = update.effective_user.id
    conn = sqlite3.connect("sga.db")
    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO employees (user_id, name, skills, platforms, hours)
                 VALUES (?, ?, ?, ?, ?)""",
              (user_id, context.user_data["name"], context.user_data["skills"],
               context.user_data["platforms"], context.user_data["hours"]))
    conn.commit()
    conn.close()
    await update.message.reply_text("Onboarding Complete. You are now active.")
    await context.bot.send_message(chat_id=BOSS_ID, text=f"✅ New Employee: {context.user_data['name']}")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

async def handle_employee_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_boss(user_id):
        return
    text = update.message.text.strip()
    lower = text.lower()
    if any(w in lower for w in ["earned", "₹", "rs", "skill", "score"]):
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect("sga.db")
        c = conn.cursor()
        c.execute("INSERT INTO daily_records (user_id, date, raw_report) VALUES (?, ?, ?)", (user_id, today, text))
        conn.commit()
        conn.close()
        await update.message.reply_text("✅ Report submitted to Boss.")
        await context.bot.send_message(chat_id=BOSS_ID, text=f"📥 Report:\n\n{text}")
        return
    await update.message.reply_text("Follow your daily plan.\n\nTo submit report use:\n\nEarned: ₹XXXX\nSkill: ...\nTasks: ...\nScore: X/10")

def run_bot():
    global application
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    onboard = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            READY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ready)],
            ACCEPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, accept)],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            SKILLS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_skills)],
            PLATFORMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_platforms)],
            HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_hours)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(onboard)
    application.add_handler(MessageHandler(filters.TEXT & filters.User(BOSS_ID), handle_boss_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.User(BOSS_ID), handle_employee_message))

    # Daily plan at 8 AM
    application.job_queue.run_daily(send_daily_plan_job, time=time(hour=8, minute=0), name="daily_plan")

    print("Telegram bot starting...")
    application.run_polling(drop_pending_updates=True)

# Start Telegram bot in background thread
bot_thread = threading.Thread(target=run_bot, daemon=True)
bot_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)