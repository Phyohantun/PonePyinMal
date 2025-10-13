import os
import logging
import json
import io
import httpx
import asyncio
import time
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
)
from PIL import Image
import replicate
import firebase_admin
from firebase_admin import credentials, firestore
from threading import Lock
from datetime import datetime, date

# --- 1. SETUP AND INITIALIZATION ---

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
REPLICATE_TOKEN = os.getenv("REPLICATE_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- 2. FIREBASE INITIALIZATION (SERVER-SAFE) ---

firebase_creds_json_str = os.getenv('FIREBASE_CREDENTIALS_JSON_CONTENT')
if firebase_creds_json_str:
    logging.info("Loading Firebase credentials from environment variable (for server).")
    creds_dict = json.loads(firebase_creds_json_str)
    cred = credentials.Certificate(creds_dict)
else:
    logging.info("Loading Firebase credentials from local file path (for local testing).")
    cred_path = os.getenv('FIREBASE_CREDENTIALS_PATH')
    if not cred_path:
        raise ValueError("ERROR: FIREBASE_CREDENTIALS_PATH not found in .env file for local development.")
    cred = credentials.Certificate(cred_path)

firebase_admin.initialize_app(cred)
db = firestore.client()
logging.info("Firebase successfully initialized.")

# --- 3. REPLICATE CLIENT INITIALIZATION ---
replicate_client = replicate.Client(api_token=REPLICATE_TOKEN)

# --- 4. CONSTANTS ---
MAX_PIXELS = 2560 * 1440
TRIAL_LIMIT = 3
GIF_PATH = os.path.join(os.path.dirname(__file__), "AnimatedSticker.tgs")

# --- 5. MULTI-PHOTO TRACKING ---
user_photo_counters = {}
user_photo_queues = {}
user_processing_locks = {}
user_loading_messages = {}
user_single_loading_messages = {}
media_group_tracker = {}

# --- 5b. USERS CACHE AND WATCHERS (REAL-TIME SYNC) ---
users_cache = {}
users_cache_lock = Lock()
user_watchers = {}

def _start_user_watch(user_id: int):
    """Start a real-time listener for a specific user's document to keep cache fresh."""
    try:
        doc_ref = db.collection("users").document(str(user_id))

        def on_snapshot(doc_snapshot, changes, read_time):
            # doc_snapshot is a list with one element for document watches
            try:
                for doc in doc_snapshot:
                    with users_cache_lock:
                        users_cache[user_id] = doc.to_dict() if doc.exists else None
            except Exception as e:
                logging.error(f"users_cache on_snapshot error for {user_id}: {e}")

        watch = doc_ref.on_snapshot(on_snapshot)
        user_watchers[user_id] = watch
        logging.info(f"Started Firestore watch for user {user_id}")
    except Exception as e:
        logging.error(f"Failed to start Firestore watch for user {user_id}: {e}")

def compute_effective_status(user_data: dict) -> str:
    """
    Derive effective status from stored fields and renewal_date.
    - If status is paid but renewal_date has passed -> expired
    - If status is expired but renewal_date is in the future -> paid
    - Otherwise return stored status
    """
    try:
        status = (user_data or {}).get("status", "trial")
        renewal = (user_data or {}).get("renewal_date")

        def to_datetime(val):
            if val is None:
                return None
            if isinstance(val, datetime):
                return val
            if isinstance(val, date):
                return datetime(val.year, val.month, val.day)
            if isinstance(val, str):
                # Try ISO then YYYY-MM-DD
                try:
                    return datetime.fromisoformat(val)
                except Exception:
                    try:
                        return datetime.strptime(val, "%Y-%m-%d")
                    except Exception:
                        return None
            return None

        rdt = to_datetime(renewal)
        if rdt is None:
            return status

        now = datetime.now(tz=rdt.tzinfo) if rdt.tzinfo else datetime.now()

        if status == "paid":
            return "expired" if rdt < now else "paid"
        if status == "expired":
            return "paid" if rdt >= now else "expired"
        return status
    except Exception as e:
        logging.error(f"compute_effective_status error: {e}")
        return (user_data or {}).get("status", "trial")

async def cleanup_media_group_entry(mgid: str, delay: float = 60.0):
    """Remove media group tracking after a short TTL to avoid memory growth."""
    try:
        await asyncio.sleep(delay)
        media_group_tracker.pop(mgid, None)
    except Exception as e:
        logging.error(f"Error cleaning media group tracker: {e}")

async def delayed_batch_check(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Clean up counter after batch is complete"""
    await asyncio.sleep(120.0)
    # Don't delete - let process_photo_queue handle cleanup
    pass

async def loading_animation(context, user_id, loading_msg):
    """Simple loading animation for multiple photos"""
    animation_chars = ["⏳", "⌛"]
    counter = 0
    last_processed = -1  # force initial update
    
    while user_id in user_photo_counters:
        try:
            total = user_photo_counters[user_id]['total']
            processed = user_photo_counters[user_id]['processed']
            
            if processed >= total:
                break
            
            # Always update to show animation, even if processed hasn't changed
            char = animation_chars[counter % len(animation_chars)]
            text = f"{char} {processed}/{total}"
            
            try:
                await context.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=loading_msg.message_id,
                    text=text
                )
                if processed != last_processed:
                    last_processed = processed
                counter += 1
            except Exception as e:
                logging.error(f"Error updating loading message: {e}")
            
            await asyncio.sleep(0.5)
            
        except Exception as e:
            logging.error(f"Loading animation error: {e}")
            break

async def animated_loading(context, chat_id, message_id):
    """Animated loading dots like chat typing"""
    dots = [".", "..", "...", ""]
    counter = 0
    
    try:
        while True:
            dot = dots[counter % len(dots)]
            text = f"⏳{dot}"
            
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text
                )
            except Exception as e:
                logging.error(f"Error updating animated loading: {e}")
                break
            
            counter += 1
            await asyncio.sleep(0.5)
            
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.error(f"Animated loading error: {e}")

async def keep_typing(context, chat_id):
    """Keep showing typing indicator continuously"""
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            await asyncio.sleep(4)  # Telegram typing lasts ~5 seconds, refresh every 4
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.error(f"Typing indicator error: {e}")

async def delete_message_after_delay(bot, chat_id: int, message_id: int, delay_seconds: float = 3.0):
    """Delete a message after a short delay without blocking the main flow."""
    try:
        await asyncio.sleep(delay_seconds)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.error(f"Error deleting message after delay: {e}")

SUBSCRIPTION_CREDITS = 150
ADMIN_USERNAME = "@ismecy"

# --- 6. DATABASE HELPER FUNCTIONS ---

def get_or_create_user(user_id: int) -> dict:
    """Return latest user data with real-time cache; create doc if missing.
    Also ensures a doc-level watcher is started so future updates apply immediately."""
    # Serve from cache if present
    try:
        with users_cache_lock:
            cached = users_cache.get(user_id)
        if cached is not None:
            # Ensure watch started
            if user_id not in user_watchers:
                _start_user_watch(user_id)
            return cached
    except Exception:
        pass

    user_ref = db.collection("users").document(str(user_id))
    user_doc = user_ref.get()
    if not user_doc.exists:
        logging.info(f"Creating new trial user for ID: {user_id}")
        new_user_data = {
            "status": "trial", "trial_credits_used": 0,
            "paid_credits_remaining": 0, "renewal_date": None,
        }
        user_ref.set(new_user_data)
        with users_cache_lock:
            users_cache[user_id] = new_user_data
        if user_id not in user_watchers:
            _start_user_watch(user_id)
        return new_user_data

    data = user_doc.to_dict()
    with users_cache_lock:
        users_cache[user_id] = data
    if user_id not in user_watchers:
        _start_user_watch(user_id)
    return data

# --- 7. TELEGRAM BOT COMMAND HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the welcome message in Burmese."""
    user_name = update.message.from_user.first_name
    
    msg = f"""👋 မင်္ဂလာပါ {user_name}

ဝါးနေတဲ့ ဓာတ်ပုံတွေကို AI နဲ့ ကြည်လင်အောင် ပြုပြင်ပေးနေပါတယ်။ \nအခြား App တွေသုံးရင် ကြေငြာကြည့်ရတာ အချိန်ကုန်ခေါင်းကိုက်ပါတယ်။ ကိုယ်ပြင်ချင်တဲ့ပုံကိုဒီ bot ကို အလွယ်တကူ ပို့လိုက်တာနဲ့ တန်းပီး ပြုပြင်ပေးမှာပါ။ ဘာလို့အလုပ်ရှုတ်ခံတော့မှာလည်းဟုတ်တယ်မလား?။

✨ အခမဲ့ {TRIAL_LIMIT} ပုံ စမ်းသုံးနိုင်ပါတယ်

၃ခါ စမ်းဖို့ အခုပဲပုံပို့ပေးလိုက်ပါ။
"""
    
    await update.message.reply_text(msg)

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends payment instructions in Burmese."""
    
    msg = f"""
 ရနိုင်တဲ့ Package များ 

⭐ **Standard Package**
• ဓာတ်ပုံ ၁၅၀ ပြုပြင်လို့ရပါမယ် / ၁၀,၀၀၀ ကျပ်

🚀 **Pro Package**
• ဓာတ်ပုံ ၃၅၀ ပြုပြင်လို့ရပါမယ် / ၂၀,၀၀၀ ကျပ်

ဒီထက် ပိုများတဲ့ Credit လိုချင်ရင် အောက်က အကောင့် စီမှာ စုံစမ်းနိုင်ပါတယ်။

အောက်က ပြထားတဲ့ အကောင့်မှာ ငွေလွှဲပီး Screenshot ကို Admin အကောင့် {ADMIN_USERNAME} ကို ပို့ပေးပါ။ \nငွေလွှဲစစ်ဆေးပီးတာနဲ့ Packageအလိုက်ပြန်လည်လုပ်ဆောင်ပေးပါမယ်။ 
• KBZPay: 09428340086
• Name: Naw Wai Wai Lwin

\nထိုင်းဘတ် နဲ့လွှဲချင်ရင်တော့ အကောင့်မှာ လာပြောပေးပါဗျ။

🙏 အားလုံးကိုကျေးဇူးတင်ပါတယ်
"""
    # Inline keyboard with copy-style helper
    keyboard = [
        [
            InlineKeyboardButton("KBZpay နံပါတ်", callback_data="copy_kbz"),
            InlineKeyboardButton("Adminအကောင့်", url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}")
        ]
    ]
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def copy_kbz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the KBZPay number so user can long-press to copy (Telegram can't access clipboard)."""
    try:
        query = update.callback_query
        if query:
            await query.answer()
        await context.bot.send_message(chat_id=update.effective_chat.id, text="09428340086")
    except Exception as e:
        logging.error(f"copy_kbz_callback error: {e}")

async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the user their Telegram ID."""
    user_id = update.message.from_user.id
    msg = f"🆔 `{user_id}`"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show remaining credits and renewal date for paid users."""
    user_id = update.message.from_user.id
    try:
        user_data = get_or_create_user(user_id)
        status = compute_effective_status(user_data)

        # Helper to format renewal date robustly
        def format_renewal(value):
            try:
                # Lazy import to avoid top-level changes
                from datetime import datetime, date
                if value is None:
                    return "မသတ်မှတ်ထားသေးပါ"
                # Firestore may return a datetime
                if isinstance(value, (datetime, date)):
                    return value.strftime("%Y-%m-%d")
                # If it's a string, return as-is
                return str(value)
            except Exception:
                return str(value)

        if status in ("paid", "expired"):
            remaining = int(user_data.get("paid_credits_remaining", 0))
            renewal = format_renewal(user_data.get("renewal_date"))
            base = (
                "💼 သင့်အကောင့် အချက်အလက်\n"
                f"• ကျန်ရှိသော Credits: {remaining}\n"
                f"• သက်တမ်းကုန်ရက်: {renewal}"
            )
            # If credits are zero, advise to subscribe again
            if remaining <= 0:
                base += (
                    "\n\n🚫 သင့် Credits ကုန်ပြီ\n"
                    f"/subscribe နဲ့ Plan ပြန်ရွေးပီး {ADMIN_USERNAME} ကိုဆက်သွယ်နိုင်ပါတယ်။"
                )
            if status == "expired":
                base += (
                    "\n\n⛔ သင့် Package သက်တမ်းကုန်ပြီ\n"
                    f"/subscribe နဲ့ Plan ပြန်ရွေးပီး {ADMIN_USERNAME} ကို ဆက်သွယ်နိုင်ပါတယ်။"
                )
            await update.message.reply_text(base)
        elif status == "trial":
            await update.message.reply_text(
                "🔒 /credits ကို Premium သုံးသူများသာကြည့်နိုင်ပါတယ်။\n/subscribe ကိုနှိပ်ပြီး စာရင်းသွင်းနိုင်ပါတယ်။"
            )
        else:
            # Fallback for unknown statuses
            await update.message.reply_text("ℹ️ သင့်အကောင့်အခြေအနေကို မတွေ့နိုင်ပါ။ /subscribe ကိုနှိပ်ပြီး ပက်ကေ့ချ် အကြောင်းစုံစမ်းနိုင်ပါတယ်")
    except Exception as e:
        logging.error(f"Error in /credits: {e}")
        await update.message.reply_text(f"❌ အချက်အလက်ပြရန် Errorဖြစ်နေပါတယ်။ Admin ကိုဆက်သွယ်ပါ {ADMIN_USERNAME}")

async def non_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Politely tell users to send only images when they send anything else."""
    try:
        # Only respond if there's a message object
        if update.message:
            await update.message.reply_text(
                "⚠️ ကျေးဇူးပြု၍ ပုံပဲ ပို့ပေးပါ။\nပုံမဟုတ်ပဲ စာတွေ Emoji တွေဆိုရင်တော့အလုပ်လုပ်မှာမဟုတ်ပါဘူး။"
            )
    except Exception as e:
        logging.error(f"Error in non_photo_handler: {e}")

# --- 8. CORE PHOTO PROCESSING LOGIC ---

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    media_group_id = getattr(update.message, "media_group_id", None)

    # Trial users: if sending an album (media group), reject the entire album (no processing)
    try:
        user_data = get_or_create_user(user_id)
        effective_status = compute_effective_status(user_data)
        if effective_status == "trial" and media_group_id is not None:
            entry = media_group_tracker.get(media_group_id)
            if entry is None:
                media_group_tracker[media_group_id] = {"user_id": user_id, "notified": True, "timestamp": time.time()}
                # Notify once for this album, then reject (no processing)
                await update.message.reply_text(
                    "🚫 စမ်းသုံးတာဖြစ်တဲ့ အတွက် တစ်ကြိမ်လျှင် ပုံတစ်ပုံသာ ပို့နိုင်ပါသည်။\n ပုံအများကြီးတစ်ခါထဲပို့ချင်ရင် /subscribe ကိုနှိပ်ပီး Package အတွက်စုံစမ်းနိုင်ပါတယ်"
                )
                # schedule cleanup for this group id
                asyncio.create_task(cleanup_media_group_entry(media_group_id, 90.0))
                return
            else:
                # Already notified for this media group; silently ignore without processing
                return
    except Exception as e:
        logging.error(f"Error enforcing trial album restriction: {e}")
    if user_id not in user_photo_queues:
        user_photo_queues[user_id] = []
    if user_id not in user_processing_locks:
        user_processing_locks[user_id] = asyncio.Lock()
    if user_id not in user_photo_counters:
        user_photo_counters[user_id] = {'total': 0, 'processed': 0, 'batch_start_time': time.time(), 'last_photo_time': time.time()}
    else:
        current_time = time.time()
        if current_time - user_photo_counters[user_id]['last_photo_time'] > 10.0:
            user_photo_counters[user_id] = {'total': 0, 'processed': 0, 'batch_start_time': current_time, 'last_photo_time': current_time}
    
    user_photo_counters[user_id]['last_photo_time'] = time.time()
    user_photo_counters[user_id]['total'] += 1
    current_photo_num = user_photo_counters[user_id]['total']
    
    user_photo_queues[user_id].append({
        'update': update,
        'context': context,
        'photo_num': current_photo_num
    })
    
    if current_photo_num == 1:
        asyncio.create_task(delayed_batch_check(context, user_id))
    
    asyncio.create_task(process_photo_queue(user_id))

async def process_photo_queue(user_id: int):
    """Process photos one by one from the queue"""
    
    if user_id not in user_processing_locks:
        return
    
    async with user_processing_locks[user_id]:
        last_context = None
        loading_msg = None
        animation_task = None
        
        while user_id in user_photo_queues and len(user_photo_queues[user_id]) > 0:
            photo_data = user_photo_queues[user_id].pop(0)
            update = photo_data['update']
            context = photo_data['context']
            current_photo_num = photo_data['photo_num']
            last_context = context
            
            if user_id in user_photo_counters:
                total = user_photo_counters[user_id]['total']
                if total > 1 and current_photo_num == 1:
                    loading_msg = await context.bot.send_message(
                        chat_id=user_id,
                        text=f"⏳ 0/{total}"
                    )
                    user_loading_messages[user_id] = loading_msg
                    animation_task = asyncio.create_task(loading_animation(context, user_id, loading_msg))
                elif total == 1 and current_photo_num == 1:
                    # Single-image flow: before showing loader, ensure trial users still have credits
                    try:
                        user_data = get_or_create_user(user_id)
                        effective_status = compute_effective_status(user_data)

                        # Expired users: do NOT show loader, prompt to subscribe
                        if effective_status == "expired":
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=f"⛔ သင် ဝယ်ထားတဲ့ Package သက်တမ်းကုန်ပါပြီ \n/subscribe မှာပက်ကေ့ချ်ထပ်ရွေးမယ် ဒါမှမဟုတ် {ADMIN_USERNAME} ကို ဆက်သွယ်ပါ။"
                            )
                            if user_id in user_photo_counters:
                                user_photo_counters[user_id]['processed'] += 1
                            continue

                        # Paid users with zero credits: do NOT show loader
                        if effective_status == "paid" and int(user_data.get("paid_credits_remaining", 0)) <= 0:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text="🚫 ဒီလအတွက် ပုံထုတ်တဲ့ပမာဏ ကုန်ဆုံးပါပြီ"
                            )
                            if user_id in user_photo_counters:
                                user_photo_counters[user_id]['processed'] += 1
                            continue

                        if effective_status == "trial" and user_data.get("trial_credits_used", 0) >= TRIAL_LIMIT:
                            # Trial exhausted: do NOT show sticker/text, just send subscribe prompt and mark processed
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=f"🚫 အခမဲ့ {TRIAL_LIMIT}ခါအသုံးပြုပြီးပါပြီ\n/subscribe ကိုနှိပ်ပြီး Premium Version ကို စုံစမ်းနိုင်ပါတယ်"
                            )
                            if user_id in user_photo_counters:
                                user_photo_counters[user_id]['processed'] += 1
                            # Skip actual processing for this photo
                            continue
                        # If this is an album (media group) and user is trial, do NOT show loader
                        media_group_id = getattr(update.message, "media_group_id", None)
                        if not (effective_status == "trial" and media_group_id is not None):
                            # Otherwise show the animated sticker and a text under it
                            if os.path.isfile(GIF_PATH):
                                with open(GIF_PATH, "rb") as sticker_file:
                                    sticker_msg = await context.bot.send_sticker(
                                        chat_id=user_id,
                                        sticker=sticker_file
                                    )
                                text_msg = await context.bot.send_message(
                                    chat_id=user_id,
                                    text="ခနစောင့်ပေးပါဗျ"
                                )
                                user_single_loading_messages[user_id] = {
                                    'sticker': sticker_msg,
                                    'text': text_msg
                                }
                            else:
                                logging.warning(f"Sticker not found at {GIF_PATH}. Skipping single-image animation.")
                    except Exception as e:
                        logging.error(f"Error handling single-image loader/trial check: {e}")
            
            await process_single_photo(update, context, user_id, current_photo_num)

            # If this is a single-image batch, remove the sticker and text after processing
            try:
                if user_id in user_single_loading_messages:
                    msgs = user_single_loading_messages.get(user_id, {})
                    # Delete text first (it's below sticker), then sticker
                    if isinstance(msgs, dict):
                        if 'text' in msgs and msgs['text'] is not None:
                            try:
                                await context.bot.delete_message(
                                    chat_id=user_id,
                                    message_id=msgs['text'].message_id
                                )
                            except Exception as e:
                                logging.error(f"Error deleting single-image text: {e}")
                        if 'sticker' in msgs and msgs['sticker'] is not None:
                            try:
                                await context.bot.delete_message(
                                    chat_id=user_id,
                                    message_id=msgs['sticker'].message_id
                                )
                            except Exception as e:
                                logging.error(f"Error deleting single-image sticker: {e}")
                    else:
                        # Backward compatibility: if stored as a single Message
                        try:
                            await context.bot.delete_message(
                                chat_id=user_id,
                                message_id=msgs.message_id
                            )
                        except Exception as e:
                            logging.error(f"Error deleting legacy single-image loader msg: {e}")
                    user_single_loading_messages.pop(user_id, None)
            except Exception as e:
                logging.error(f"Error cleaning up single-image loader messages: {e}")
        
        # Clean up after all photos processed
        if user_id in user_photo_counters and last_context is not None:
            counter = user_photo_counters[user_id]
            
            # Wait a bit for animation to catch up
            await asyncio.sleep(0.5)
            
            if counter['total'] > 1:
                try:
                    # Cancel animation task first
                    if animation_task and not animation_task.done():
                        animation_task.cancel()
                        try:
                            await animation_task
                        except asyncio.CancelledError:
                            pass
                    
                    # Delete loading message
                    if user_id in user_loading_messages:
                        try:
                            await last_context.bot.delete_message(
                                chat_id=user_id,
                                message_id=user_loading_messages[user_id].message_id
                            )
                        except Exception as e:
                            logging.error(f"Error deleting loading message: {e}")
                        del user_loading_messages[user_id]
                    
                    # Send completion message and auto-delete after ~3 seconds
                    final_msg = await last_context.bot.send_message(
                        chat_id=user_id,
                        text=f"ပို့ထားတဲ့{counter['total']}ပုံရပါပြီဗျ⚪"
                    )
                    # Schedule deletion without blocking
                    asyncio.create_task(
                        delete_message_after_delay(
                            last_context.bot, user_id, final_msg.message_id, 3.0
                        )
                    )
                except Exception as e:
                    logging.error(f"Error sending batch completion message: {e}")
            
            # Always delete counter when done processing
            del user_photo_counters[user_id]

async def process_single_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, current_photo_num: int):
    """Process a single photo completely"""
    user_data = get_or_create_user(user_id)
    user_ref = db.collection("users").document(str(user_id))
    
    can_proceed, is_trial = False, False
    status_effective = compute_effective_status(user_data)
    if status_effective == "trial":
        if user_data.get("trial_credits_used", 0) < TRIAL_LIMIT:
            can_proceed, is_trial = True, True
        else:
            await update.message.reply_text(f"🚫 အခမဲ့ {TRIAL_LIMIT} ပုံ သုံးပြီးပါပြီ\n ကြိုက်နှစ်သက်ရင်/subscribe ကိုနှိပ်ပီး Premium Version ကို စုံစမ်းနိုင်ပါတယ်")
            user_photo_counters[user_id]['processed'] += 1
            return
    elif status_effective == "paid":
        if user_data.get("paid_credits_remaining", 0) > 0:
            can_proceed = True
        else:
            await update.message.reply_text("🚫 ဒီလအတွက် ပုံထုတ်တဲ့ပမာဏ ကုန်ဆုံးပါပြီ")
            user_photo_counters[user_id]['processed'] += 1
            return
    elif status_effective == "expired":
        await update.message.reply_text(f"🚫 သက်တမ်းကုန်ပါပြီ\n{ADMIN_USERNAME} သို့ ဆက်သွယ်ပါ")
        user_photo_counters[user_id]['processed'] += 1
        return

    if not can_proceed:
        await update.message.reply_text(f"❌ Error တက်နေပါတယ်ဗျ Admin {ADMIN_USERNAME}ကိုဆက်သွယ်ပါဗျ")
        user_photo_counters[user_id]['processed'] += 1
        return
    
    try:
        total_photos = user_photo_counters[user_id]['total']
        
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = io.BytesIO()
        await file.download_to_memory(file_bytes)
        file_bytes.seek(0)

        with Image.open(file_bytes) as img:
            width, height = img.size
            if width * height > MAX_PIXELS:
                await update.message.reply_text(f"❌ ပုံကြီးလွန်းပါတယ် ({width}x{height})\n1440p အောက်ပို့ပါ")
                user_photo_counters[user_id]['processed'] += 1
                return
        
        file_bytes.seek(0)

        model_version = "nightmareai/real-esrgan:f121d640bd286e1fdc67f9799164c1d5be36ff74576ee11c803ae5b665dd46aa"
        # Run blocking Replicate call in a thread to keep event loop responsive for animation
        output = await asyncio.to_thread(
            replicate_client.run,
            model_version,
            input={"image": file_bytes, "scale": 2}
        )
        
        logging.info(f"Replicate output: {output}")

        output_url = None
        try:
            if hasattr(output, "url") and isinstance(getattr(output, "url"), str):
                output_url = output.url
            elif isinstance(output, str):
                output_url = output
            elif isinstance(output, list) and len(output) > 0:
                first = output[0]
                if hasattr(first, "url") and isinstance(getattr(first, "url"), str):
                    output_url = first.url
                else:
                    output_url = str(first)
            else:
                output_url = str(output)
        except Exception as norm_err:
            logging.error(f"Failed to normalize output: {norm_err}")
        
        if output_url:
            await send_enhanced_image_safely(update, context, output_url, user_ref, is_trial)
            user_photo_counters[user_id]['processed'] += 1
            return
        else:
            raise ValueError(f"Unexpected output format: {output}")
            
    except Exception as e:
        logging.error(f"Error processing photo: {e}")
        await update.message.reply_text(f"❌ Error တက်နေပါတယ်ဗျ Admin {ADMIN_USERNAME}ကိုဆက်သွယ်ပါဗျ")
        user_photo_counters[user_id]['processed'] += 1

async def send_enhanced_image_safely(update, context, output_url, user_ref, is_trial):
    """Send enhanced image without extra messages"""
    try:
        logging.info(f"Downloading from: {output_url}")
        
        async with httpx.AsyncClient() as client:
            response = await client.get(output_url, timeout=60.0)
            response.raise_for_status()
            image_data = response.content
        
        PHOTO_SIZE_LIMIT = 10 * 1024 * 1024
        
        if len(image_data) < PHOTO_SIZE_LIMIT:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id, 
                photo=io.BytesIO(image_data)
            )
        else:
            await context.bot.send_document(
                chat_id=update.effective_chat.id, 
                document=io.BytesIO(image_data),
                filename="enhanced.png"
            )
        
        logging.info("Image sent successfully")
        
        try:
            if is_trial:
                user_ref.update({"trial_credits_used": firestore.Increment(1)})
            else:
                user_ref.update({"paid_credits_remaining": firestore.Increment(-1)})
        except:
            pass
            
    except Exception as e:
        logging.error(f"Error sending image: {e}")

# --- 9. MAIN BOT EXECUTION ---
def main():
    logging.info("Building application...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CallbackQueryHandler(copy_kbz_callback, pattern="^copy_kbz$"))
    app.add_handler(CommandHandler("myid", my_id))
    app.add_handler(CommandHandler("credits", credits))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    # Catch-all for non-photo, non-command messages to enforce image-only input
    app.add_handler(MessageHandler(~filters.PHOTO & ~filters.COMMAND, non_photo_handler))
    logging.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()