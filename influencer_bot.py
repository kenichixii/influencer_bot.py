"""
Opt-in influencer contact directory. Buyers pay 25 USDT (BEP20 / BNB Smart Chain) to unlock a contact.
Influencers register themselves, consent to being listed, and are approved by the admin.

Install:  pip install "python-telegram-bot[job-queue]==21.*" httpx
Env vars: BOT_TOKEN, ADMIN_ID (your Telegram user id), WALLET (your 0x address),
          ETHERSCAN_KEY (free key from etherscan.io/apis, works for BSC)
Run:      python influencer_bot.py
"""
import os, sqlite3, random, time, httpx
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, ConversationHandler, ContextTypes, filters)

TOKEN = os.environ["BOT_TOKEN"]
ADMIN = int(os.environ["ADMIN_ID"])
WALLET = os.environ["WALLET"].lower()

ETHERSCAN_KEY = os.environ["ETHERSCAN_KEY"]
USDT_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"  # USDT on BSC (18 decimals)
PRICE_MICRO = 25 * 1_000_000   # amounts tracked in 6 decimals; BSC value = micro * 10**12
EXPIRY = 3600                  # orders expire after 1 hour

db = sqlite3.connect("bot.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS influencers(
  id INTEGER PRIMARY KEY, tg_id INTEGER UNIQUE, name TEXT, niche TEXT,
  platform TEXT, contact TEXT, status TEXT DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, buyer_id INTEGER, influencer_id INTEGER,
  amount INTEGER, created REAL, status TEXT DEFAULT 'pending', tx_id TEXT);
""")

NAME, NICHE, PLATFORM, CONTACT, CONSENT = range(5)


# ---------- Buyer flow ----------
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "Influencer directory.\n/browse – see listed influencers\n"
        "/register – list yourself as an influencer\n/remove – remove your listing")


async def browse(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = db.execute("SELECT id,name,niche FROM influencers WHERE status='approved'").fetchall()
    if not rows:
        return await u.message.reply_text("No listings yet.")
    kb = [[B(f"{n} – {ni}", callback_data=f"buy:{i}")] for i, n, ni in rows]
    await u.message.reply_text("Tap one to unlock contact info (25 USDT):", reply_markup=M(kb))


async def buy(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    iid = int(q.data.split(":")[1])
    if not db.execute("SELECT 1 FROM influencers WHERE id=? AND status='approved'", (iid,)).fetchone():
        return await q.message.reply_text("That listing is no longer available.")
    used = {r[0] for r in db.execute("SELECT amount FROM orders WHERE status='pending'")}
    while True:  # unique amount identifies the order
        amount = PRICE_MICRO + random.randint(1, 9999)
        if amount not in used:
            break
    db.execute("INSERT INTO orders(buyer_id,influencer_id,amount,created) VALUES(?,?,?,?)",
               (q.from_user.id, iid, amount, time.time()))
    db.commit()
    await q.message.reply_text(
        f"Send EXACTLY {amount / 1e6:.6f} USDT (BEP20 / BNB Smart Chain) to:\n\n{WALLET}\n\n"
        "The exact amount matters. Contact info is sent automatically once the "
        "payment confirms. This order expires in 1 hour.")


async def check_payments(c: ContextTypes.DEFAULT_TYPE):
    db.execute("UPDATE orders SET status='expired' WHERE status='pending' AND created<?",
               (time.time() - EXPIRY,))
    db.commit()
    pending = db.execute("SELECT id,buyer_id,influencer_id,amount,created FROM orders "
                         "WHERE status='pending'").fetchall()
    if not pending:
        return
    min_ts = int(min(p[4] for p in pending))
    try:
        async with httpx.AsyncClient(timeout=20) as h:
            r = await h.get("https://api.etherscan.io/v2/api", params={
                "chainid": 56, "module": "account", "action": "tokentx",
                "contractaddress": USDT_CONTRACT, "address": WALLET,
                "page": 1, "offset": 100, "sort": "desc", "apikey": ETHERSCAN_KEY})
            result = r.json().get("result", [])
            txs = result if isinstance(result, list) else []
            if not txs and not (isinstance(result, str) and "No transactions" in result):
                print("Etherscan response:", result)
    except Exception as e:
        print("Etherscan error:", e)
        return
    by_amount = {p[3] * 10**12: p for p in pending}
    for tx in txs:
        if int(tx["timeStamp"]) < min_ts or tx["to"].lower() != WALLET:
            continue
        order = by_amount.get(int(tx["value"]))
        if not order:
            continue
        if db.execute("SELECT 1 FROM orders WHERE tx_id=?", (tx["hash"],)).fetchone():
            continue
        oid, buyer, iid, _, _ = order
        db.execute("UPDATE orders SET status='paid', tx_id=? WHERE id=?", (tx["hash"], oid))
        db.commit()
        name, platform, contact = db.execute(
            "SELECT name,platform,contact FROM influencers WHERE id=?", (iid,)).fetchone()
        await c.bot.send_message(buyer, f"Payment confirmed.\n\n{name}\n{platform}\nContact: {contact}")


# ---------- Influencer self-registration (opt-in) ----------
async def reg_start(u, c):
    await u.message.reply_text("Let's list you. What name should buyers see?")
    return NAME

async def reg_name(u, c):
    c.user_data["name"] = u.message.text
    await u.message.reply_text("Your niche? (e.g. fitness, travel, tech)")
    return NICHE

async def reg_niche(u, c):
    c.user_data["niche"] = u.message.text
    await u.message.reply_text("Link to your main profile (Instagram, TikTok, YouTube...)")
    return PLATFORM

async def reg_platform(u, c):
    c.user_data["platform"] = u.message.text
    await u.message.reply_text("Business contact buyers will receive (email, @handle, etc.)")
    return CONTACT

async def reg_contact(u, c):
    c.user_data["contact"] = u.message.text
    await u.message.reply_text(
        "Reply YES to confirm you consent to this contact being shown to buyers "
        "who pay. You can use /remove any time.")
    return CONSENT

async def reg_consent(u, c):
    if u.message.text.strip().upper() != "YES":
        await u.message.reply_text("Cancelled.")
        return ConversationHandler.END
    d = c.user_data
    db.execute("INSERT OR REPLACE INTO influencers(tg_id,name,niche,platform,contact,status) "
               "VALUES(?,?,?,?,?,'pending')",
               (u.effective_user.id, d["name"], d["niche"], d["platform"], d["contact"]))
    db.commit()
    iid = db.execute("SELECT id FROM influencers WHERE tg_id=?", (u.effective_user.id,)).fetchone()[0]
    await c.bot.send_message(
        ADMIN, f"New listing:\n{d['name']} | {d['niche']}\n{d['platform']}",
        reply_markup=M([[B("Approve", callback_data=f"ok:{iid}"),
                         B("Reject", callback_data=f"no:{iid}")]]))
    await u.message.reply_text("Submitted. You'll go live once approved.")
    return ConversationHandler.END

async def cancel(u, c):
    await u.message.reply_text("Cancelled.")
    return ConversationHandler.END

async def moderate(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if q.from_user.id != ADMIN:
        return
    action, iid = q.data.split(":")
    status = "approved" if action == "ok" else "rejected"
    db.execute("UPDATE influencers SET status=? WHERE id=?", (status, int(iid)))
    db.commit()
    tg_id = db.execute("SELECT tg_id FROM influencers WHERE id=?", (int(iid),)).fetchone()[0]
    await q.edit_message_text(f"Listing {status}.")
    await c.bot.send_message(tg_id, f"Your listing was {status}.")

async def remove(u, c):
    db.execute("UPDATE influencers SET status='removed' WHERE tg_id=?", (u.effective_user.id,))
    db.commit()
    await u.message.reply_text("Your listing has been removed.")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("register", reg_start)],
        states={s: [MessageHandler(filters.TEXT & ~filters.COMMAND, f)] for s, f in
                [(NAME, reg_name), (NICHE, reg_niche), (PLATFORM, reg_platform),
                 (CONTACT, reg_contact), (CONSENT, reg_consent)]},
        fallbacks=[CommandHandler("cancel", cancel)]))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("browse", browse))
    app.add_handler(CommandHandler("remove", remove))
    app.add_handler(CallbackQueryHandler(buy, pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(moderate, pattern=r"^(ok|no):"))
    app.job_queue.run_repeating(check_payments, interval=30, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()
