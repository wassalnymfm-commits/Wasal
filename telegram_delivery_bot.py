#!/usr/bin/env python3
# telegram_delivery_bot_live.py
"""
Telegram Delivery Bot - Live Location (Drivers) + Orders + Google Sheets
- Separate sheets: Drivers, Users, Orders
- Drivers share Live Location (choose duration in Telegram)
- Bot updates driver coords on each incoming location update
- Drivers considered inactive automatically if last_update older than INACTIVE_THRESHOLD
- Configurable logging (DEBUG_MODE) to console + bot_debug.log
"""

import os
import sys
import logging
import time
import math
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
)

# Add this right after imports
import sys
import traceback

# Better error logging for Render
def log_exception(exc_type, exc_value, exc_traceback):
    """Log uncaught exceptions"""
    logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = log_exception
# --------------------------- CONFIG ---------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8555773876:AAESFpUDxPM1HosaDi-yQckpgk8gC-VWLT8")
GOOGLE_CREDS_PATH = os.environ.get("GOOGLE_CREDS_PATH", "credentials.json")
#SHEET_ID = os.environ.get("SHEET_ID", "1dD1d39YQD3z-bKXpUZqgjipVUw8I4HZimAxOtrTn79w")
SHEET_ID = os.environ.get("SHEET_ID", "1n5ip_fxjAzVu2U_YG2pGlyhwcGTDEnTP4_byKiW4bnY")

# Logging
DEBUG_MODE = True
LOG_FILE_PATH = "bot_debug.log"

# Live location / inactivity
INACTIVE_THRESHOLD = 10  # minutes after last_update driver is considered inactive
MAX_DISPLAY_DRIVERS = 10
CURRENCY = "SAR"

# Sheets names
ORDERS_SHEET_NAME = "Orders"
DRIVERS_SHEET_NAME = "Drivers"
USERS_SHEET_NAME = "Users"

# --------------------------- Logging Setup ---------------------------
logger = logging.getLogger("telegram_delivery_bot_live")
logger.setLevel(logging.DEBUG if DEBUG_MODE else logging.WARNING)
# clear handlers if re-run
if logger.handlers:
    for h in list(logger.handlers):
        logger.removeHandler(h)

ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG if DEBUG_MODE else logging.WARNING)
ch.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(ch)

if DEBUG_MODE:
    fh = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s"))
    logger.addHandler(fh)

logger.info("Logger initialized (DEBUG_MODE=%s)", DEBUG_MODE)

# --------------------------- Google Sheets helpers ---------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

def connect_sheets(creds_path: str, sheet_id: str):
    logger.debug("Connecting to Google Sheets: %s", creds_path)
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(sheet_id)

SHEET = None
orders_ws = None
drivers_ws = None
users_ws = None

def ensure_sheet_structure():
    global orders_ws, drivers_ws, users_ws
    try:
        orders_ws = SHEET.worksheet(ORDERS_SHEET_NAME)
        logger.debug("Found Orders worksheet")
    except Exception:
        orders_ws = SHEET.add_worksheet(title=ORDERS_SHEET_NAME, rows=4000, cols=30)
        orders_ws.append_row([
            "order_id", "client_id", "client_name", "pickup_loc", "pickup_desc",
            "dest_loc", "dest_desc", "client_price", "currency", "status",
            "driver_id", "driver_name", "driver_price", "counter_price", "timestamp"
        ])
        logger.debug("Created Orders worksheet")

    try:
        drivers_ws = SHEET.worksheet(DRIVERS_SHEET_NAME)
        logger.debug("Found Drivers worksheet")
    except Exception:
        drivers_ws = SHEET.add_worksheet(title=DRIVERS_SHEET_NAME, rows=4000, cols=30)
        drivers_ws.append_row([
            "driver_id", "driver_name", "chat_id", "age", "nationality", "phone",
            "vehicle_type", "vehicle_make", "vehicle_year", "gender",
            "latitude", "longitude", "last_update", "active"
        ])
        logger.debug("Created Drivers worksheet")

    try:
        users_ws = SHEET.worksheet(USERS_SHEET_NAME)
        logger.debug("Found Users worksheet")
    except Exception:
        users_ws = SHEET.add_worksheet(title=USERS_SHEET_NAME, rows=4000, cols=10)
        users_ws.append_row(["user_id", "name", "role", "timestamp"])
        logger.debug("Created Users worksheet")

# --------------------------- Helpers ---------------------------
def format_price(value):
    try:
        v = float(value)
        return f"{int(v) if v.is_integer() else v} {CURRENCY}"
    except Exception:
        return f"{value} {CURRENCY}"

def new_order_id():
    return f"O{int(time.time())}"

def register_user(user_id: int, name: str, role: str):
    try:
        recs = users_ws.get_all_records()
        for r in recs:
            if str(r.get("user_id")) == str(user_id):
                # Update role if changed
                if r.get("role") != role:
                    for i, user_rec in enumerate(recs, start=2):
                        if str(user_rec.get("user_id")) == str(user_id):
                            users_ws.update_cell(i, 3, role)
                            logger.info("Updated user %s role to %s", user_id, role)
                return
        users_ws.append_row([user_id, name, role, datetime.utcnow().isoformat()])
        logger.info("Registered user %s as %s", user_id, role)
    except Exception as e:
        logger.exception("register_user error: %s", e)

def get_user_role(user_id: int):
    """Get the role of a user"""
    try:
        recs = users_ws.get_all_records()
        for r in recs:
            if str(r.get("user_id")) == str(user_id):
                return r.get("role", "")
        return ""
    except Exception as e:
        logger.exception("get_user_role error: %s", e)
        return ""

def register_driver(info: dict):
    """
    info should include:
      driver_name, chat_id, age, nationality, phone, vehicle_type, vehicle_make, vehicle_year, gender
    """
    try:
        recs = drivers_ws.get_all_records()
        for i, r in enumerate(recs, start=2):
            if str(r.get("chat_id")) == str(info.get("chat_id")):
                # update fields
                drivers_ws.update_cell(i, 2, info.get("driver_name", r.get("driver_name", "")))
                drivers_ws.update_cell(i, 4, info.get("age", r.get("age", "")))
                drivers_ws.update_cell(i, 5, info.get("nationality", r.get("nationality", "")))
                drivers_ws.update_cell(i, 6, info.get("phone", r.get("phone", "")))
                drivers_ws.update_cell(i, 7, info.get("vehicle_type", r.get("vehicle_type", "")))
                drivers_ws.update_cell(i, 8, info.get("vehicle_make", r.get("vehicle_make", "")))
                drivers_ws.update_cell(i, 9, info.get("vehicle_year", r.get("vehicle_year", "")))
                drivers_ws.update_cell(i, 10, info.get("gender", r.get("gender", "")))
                # ensure active and last_update set if provided
                drivers_ws.update_cell(i, 14, "yes")
                drivers_ws.update_cell(i, 13, datetime.utcnow().isoformat())
                logger.info("Updated driver record chat_id=%s", info.get("chat_id"))
                return r.get("driver_id")
        # append new driver
        driver_id = f"D{int(time.time())}"
        drivers_ws.append_row([
            driver_id,
            info.get("driver_name", ""),
            str(info.get("chat_id", "")),  # Ensure chat_id is string
            info.get("age", ""),
            info.get("nationality", ""),
            info.get("phone", ""),
            info.get("vehicle_type", ""),
            info.get("vehicle_make", ""),
            info.get("vehicle_year", ""),
            info.get("gender", ""),
            info.get("latitude", ""),
            info.get("longitude", ""),
            datetime.utcnow().isoformat(),
            "yes"
        ])
        logger.info("Added new driver %s for chat_id %s", driver_id, info.get("chat_id"))
        return driver_id
    except Exception as e:
        logger.exception("register_driver error: %s", e)
        return None

def update_driver_location(chat_id: int, lat: float, lon: float):
    try:
        recs = drivers_ws.get_all_records()
        for i, r in enumerate(recs, start=2):
            # Compare as strings to avoid type issues
            if str(r.get("chat_id")) == str(chat_id):
                drivers_ws.update_cell(i, 11, lat)  # latitude
                drivers_ws.update_cell(i, 12, lon)  # longitude
                drivers_ws.update_cell(i, 13, datetime.utcnow().isoformat())  # last_update
                drivers_ws.update_cell(i, 14, "yes")
                logger.debug("Updated location for driver %s -> (%s,%s)", chat_id, lat, lon)
                return True
        logger.warning("Driver chat_id=%s not found when updating location", chat_id)
        return False
    except Exception as e:
        logger.exception("update_driver_location error: %s", e)
        return False

def set_driver_active(chat_id: int, active: bool):
    try:
        recs = drivers_ws.get_all_records()
        for i, r in enumerate(recs, start=2):
            if str(r.get("chat_id")) == str(chat_id):
                drivers_ws.update_cell(i, 14, "yes" if active else "no")
                drivers_ws.update_cell(i, 13, datetime.utcnow().isoformat())
                logger.debug("Set driver %s active=%s", chat_id, active)
                return True
        return False
    except Exception as e:
        logger.exception("set_driver_active error: %s", e)
        return False

def add_order_to_sheet(order: dict):
    try:
        row = [
            order.get("order_id"), order.get("client_id"), order.get("client_name"),
            order.get("pickup_loc"), order.get("pickup_desc"),
            order.get("dest_loc"), order.get("dest_desc"),
            order.get("client_price"), order.get("currency", CURRENCY),
            order.get("status"), order.get("driver_id", ""), order.get("driver_name", ""),
            order.get("driver_price", ""), order.get("counter_price", ""), order.get("timestamp")
        ]
        orders_ws.append_row(row)
        logger.info("Order %s appended", order.get("order_id"))
    except Exception as e:
        logger.exception("add_order_to_sheet error: %s", e)

def update_order_in_sheet(order_id: str, updates: dict):
    try:
        recs = orders_ws.get_all_records()
        for i, r in enumerate(recs, start=2):
            if str(r.get("order_id")) == str(order_id):
                if "status" in updates:
                    orders_ws.update_cell(i, 10, updates.get("status"))
                if "driver_id" in updates:
                    orders_ws.update_cell(i, 11, updates.get("driver_id"))
                if "driver_name" in updates:
                    orders_ws.update_cell(i, 12, updates.get("driver_name"))
                if "driver_price" in updates:
                    orders_ws.update_cell(i, 13, updates.get("driver_price"))
                if "counter_price" in updates:
                    orders_ws.update_cell(i, 14, updates.get("counter_price"))
                logger.debug("Order %s updated with %s", order_id, updates)
                return True
        logger.debug("Order %s not found", order_id)
        return False
    except Exception as e:
        logger.exception("update_order_in_sheet error: %s", e)
        return False

def get_active_drivers_records(mark_inactive=True):
    """
    Return drivers whose active flag is yes and last_update within INACTIVE_THRESHOLD minutes.
    If mark_inactive True, set the 'active' column to 'no' for stale drivers.
    """
    out = []
    try:
        recs = drivers_ws.get_all_records()
        now = datetime.utcnow()
        for i, r in enumerate(recs, start=2):
            active_flag = str(r.get("active", "")).lower() in ("yes", "true")
            last_update = r.get("last_update")
            if last_update:
                try:
                    last_dt = datetime.fromisoformat(last_update)
                except Exception:
                    last_dt = now - timedelta(minutes=9999)
            else:
                last_dt = now - timedelta(minutes=9999)
            minutes_diff = (now - last_dt).total_seconds() / 60.0
            if active_flag and minutes_diff <= INACTIVE_THRESHOLD:
                out.append(r)
            elif active_flag and minutes_diff > INACTIVE_THRESHOLD and mark_inactive:
                # mark as inactive in sheet to keep sheet accurate
                try:
                    drivers_ws.update_cell(i, 14, "no")
                    logger.info("Marked driver chat_id=%s inactive (last_update=%s)", r.get("chat_id"), last_update)
                except Exception as e:
                    logger.warning("Could not mark driver inactive: %s", e)
        logger.debug("Active drivers returned: %d", len(out))
    except Exception as e:
        logger.exception("get_active_drivers_records error: %s", e)
    return out

def haversine(lat1, lon1, lat2, lon2):
    # returns kilometers
    try:
        R = 6371.0
        phi1 = math.radians(float(lat1))
        phi2 = math.radians(float(lat2))
        dphi = math.radians(float(lat2) - float(lat1))
        dlambda = math.radians(float(lon2) - float(lon1))
        a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c
    except Exception:
        return None

def build_maps_link(client_loc, drivers):
    base = "https://www.google.com/maps/dir/"
    parts = []
    if client_loc:
        parts.append(f"{client_loc[0]},{client_loc[1]}")
    for d in drivers:
        lat = d.get("latitude")
        lon = d.get("longitude")
        parts.append(f"{lat},{lon}")
    return base + "/".join(parts)

def filter_and_sort_drivers(client_loc, nation=None, vtype=None, gender=None):
    candidates = get_active_drivers_records()
    filtered = []
    for d in candidates:
        try:
            if nation and str(d.get("nationality", "")).strip().lower() != nation.strip().lower():
                continue
            if vtype and str(d.get("vehicle_type", "")).strip().lower() != vtype.strip().lower():
                continue
            if gender and str(d.get("gender", "")).strip().lower() != gender.strip().lower():
                continue
            lat = d.get("latitude")
            lon = d.get("longitude")
            latf = float(lat)
            lonf = float(lon)
            dist = None
            if client_loc:
                dist = haversine(client_loc[0], client_loc[1], latf, lonf)
            filtered.append((d, dist))
        except Exception:
            continue
    if client_loc:
        filtered.sort(key=lambda x: x[1] if x[1] is not None else 99999)
    logger.debug("filter_and_sort_drivers returned %d candidates", len(filtered))
    return filtered[:MAX_DISPLAY_DRIVERS]

async def display_nearby_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE, client_loc, client_price="25"):
    """Display nearby drivers to client"""
    filtered = filter_and_sort_drivers(client_loc)
    
    if not filtered:
        await update.message.reply_text("❌ لم يتم العثور على سائقين قريبين من موقعك حالياً.")
        return

    drivers_only = [d for d, _ in filtered]
    maps_link = build_maps_link(client_loc, drivers_only)

    # Ask for price if not provided
    if not client_price:
        await update.message.reply_text(f"📍 تم تحديد موقعك. أدخل السعر المقترح بالـ{CURRENCY} (رقم فقط)، مثال: 25")
        context.user_data['awaiting_price'] = True
        context.user_data['client_search_loc'] = client_loc
        return

    # Display drivers with the default or provided price
    for d, dist in filtered:
        name = d.get("driver_name", "—")
        nat = d.get("nationality", "—")
        v = d.get("vehicle_type", "—")
        vm = d.get("vehicle_make", "")
        vy = d.get("vehicle_year", "")
        gen = d.get("gender", "—")
        phone = d.get("phone", "—")
        lat = d.get("latitude", "")
        lon = d.get("longitude", "")
        dist_text = f" — {dist:.2f} km" if dist is not None else ""
        text = (
            f"👤 {name} ({nat}){dist_text}\n"
            f"🚘 {v} {vm} ({vy})\n"
            f"🚹 الجنس: {gen}\n"
            f"📞 {phone}\n"
            f"📍 موقع: https://www.google.com/maps/search/?api=1&query={lat},{lon}\n"
            f"💰 السعر المقترح: {format_price(client_price)}"
        )
        cbdata = f"request:{d.get('chat_id')}:{client_price}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚕 اطلب من هذا السائق", callback_data=cbdata)]])
        await update.message.reply_text(text, reply_markup=kb)

    await update.message.reply_text(f"🔗 عرض جميع السائقين على الخريطة:\n{maps_link}")

# ------------------ States ------------------
(
    ROLE,
    DRIVER_AGE, DRIVER_NATION, DRIVER_PHONE, DRIVER_VTYPE, DRIVER_VMAKE, DRIVER_VYEAR, DRIVER_GENDER,
    CLIENT_PICK_LOC, CLIENT_NATION, CLIENT_VTYPE, CLIENT_GENDER, CLIENT_PRICE, CLIENT_DISPLAY_CHOICE
) = range(14)

# ------------------ Handlers ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_role = get_user_role(user_id)
    
    if current_role:
        # User already registered
        if current_role == "driver":
            await update.message.reply_text(
                f"مرحبًا مرة أخرى كسائق! 🚗\n\n"
                f"الأوامر المتاحة:\n"
                f"/start_tracking - مشاركة موقعك الحي\n"
                f"/stop_tracking - إيقاف مشاركة الموقع\n"
                f"/help - المساعدة"
            )
        else:
            await update.message.reply_text(
                f"مرحبًا مرة أخرى كعميل! 🛍️\n\n"
                f"الأوامر المتاحة:\n"
                f"/find_driver - البحث عن سائق\n"
                f"/become_driver - التسجيل كسائق\n"
                f"/help - المساعدة"
            )
        return ConversationHandler.END
    else:
        # New user
        kb = [["🛍️ أنا عميل", "🚗 أنا سائق"]]
        await update.message.reply_text("مرحبًا! اختر نوعك:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        logger.debug("User %s ran /start", user_id)
        return ROLE

async def role_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text or ""
    user = update.effective_user
    logger.debug("role_choice: %s from %s", txt, user.id)
    if txt == "🛍️ أنا عميل":
        register_user(user.id, user.full_name, "client")
        await update.message.reply_text(
            "مرحبًا كعميل! 🛍️\n\n"
            "يمكنك:\n"
            "• استخدام /find_driver للبحث عن سائقين قريبين\n"
            "• إرسال موقعك وسيتم عرض السائقين القريبين تلقائياً\n"
            "• استخدام /become_driver إذا أردت التسجيل كسائق لاحقًا\n"
            "• استخدام /help للمساعدة"
        )
        return ConversationHandler.END
    if txt == "🚗 أنا سائق":
        register_user(user.id, user.full_name, "driver")
        context.user_data['driver_temp'] = {}
        await update.message.reply_text("أدخل عمرك:")
        return DRIVER_AGE
    await update.message.reply_text("اختر من الأزرار من فضلك.")
    return ROLE

# New command to switch from client to driver
async def become_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_role = get_user_role(user_id)
    
    if current_role == "driver":
        await update.message.reply_text("أنت مسجل بالفعل كسائق! 🚗")
        return
    
    # Start driver registration
    register_user(user_id, update.effective_user.full_name, "driver")
    context.user_data['driver_temp'] = {}
    await update.message.reply_text(
        "مرحبًا! سنقوم بتسجيلك كسائق. 🚗\n\n"
        "أدخل عمرك:"
    )
    return DRIVER_AGE

# Driver registration flow
async def driver_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['driver_temp']['age'] = update.message.text
    await update.message.reply_text("ما هي جنسيتك؟")
    return DRIVER_NATION

async def driver_nation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['driver_temp']['nationality'] = update.message.text
    await update.message.reply_text("رقم الجوال:")
    return DRIVER_PHONE

async def driver_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['driver_temp']['phone'] = update.message.text
    await update.message.reply_text("نوع المركبة:")
    return DRIVER_VTYPE

async def driver_vtype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['driver_temp']['vehicle_type'] = update.message.text
    await update.message.reply_text("ماركة المركبة:")
    return DRIVER_VMAKE

async def driver_vmake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['driver_temp']['vehicle_make'] = update.message.text
    await update.message.reply_text("سنة الصنع:")
    return DRIVER_VYEAR

async def driver_vyear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['driver_temp']['vehicle_year'] = update.message.text
    await update.message.reply_text("ما هو جنسك؟ (ذكر/انثى)")
    return DRIVER_GENDER

async def driver_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gen = update.message.text or ""
    context.user_data['driver_temp']['gender'] = gen
    info = context.user_data['driver_temp']
    info.update({
        'driver_name': update.effective_user.full_name,
        'chat_id': update.effective_user.id,
        'latitude': '', 'longitude': '', 'last_update': ''
    })
    driver_id = register_driver(info)
    if driver_id:
        await update.message.reply_text(
            f"تم تسجيلك كسائق بنجاح! ✅\n"
            f"رقم السائق: {driver_id}\n\n"
            f"الآن يمكنك:\n"
            f"• استخدام /start_tracking لمشاركة موقعك الحي\n"
            f"• استخدام /stop_tracking لإيقاف المشاركة\n"
            f"• ستتلقى طلبات التوصيل من العملاء تلقائيًا"
        )
    else:
        await update.message.reply_text("❌ حدث خطأ أثناء التسجيل. يرجى المحاولة مرة أخرى.")
    return ConversationHandler.END

# Driver: request Live Location to share for chosen period
async def start_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Request live location sharing from driver"""
    user_id = update.effective_user.id
    current_role = get_user_role(user_id)
    
    if current_role != "driver":
        await update.message.reply_text(
            "❌ هذه الخاصية للسائقين فقط!\n\n"
            "إذا كنت ترغب في أن تصبح سائقاً، استخدم:\n"
            "/become_driver للتسجيل كسائق"
        )
        return
    
    # Clear any previous location confirmation
    context.user_data['location_confirmed'] = False
    
    # Create keyboard with live location button
    kb = [[KeyboardButton("📍 مشاركة موقعي الحي", request_location=True)]]
    
    message_text = (
        "📍 لمشاركة موقعك الحي:\n\n"
        "1. اضغط على زر '📍 مشاركة موقعي الحي' أدناه\n"
        "2. في شاشة التليجرام، اختر مدة المشاركة (15 دقيقة / 1 ساعة / 8 ساعات)\n"
        "3. سيتم تحديث موقعك تلقائياً خلال الفترة المحددة\n\n"
        "لإيقاف التتبع، استخدم /stop_tracking"
    )
    
    await update.message.reply_text(
        message_text,
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=False)
    )
    logger.debug("Prompted driver %s to share Live Location", user_id)

# Driver stops tracking manually (optional)
async def stop_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_role = get_user_role(user_id)
    
    if current_role != "driver":
        await update.message.reply_text("❌ هذه الخاصية للسائقين فقط!")
        return
        
    ok = set_driver_active(user_id, False)
    await update.message.reply_text("تم إيقاف تتبع موقعك — لم تعد تظهر كسائق نشط." if ok else "حدث خطأ أثناء محاولة إيقاف التتبع.")
    logger.info("Driver %s requested stop_tracking", user_id)

# Improved Handler for Driver Live Location (continuous updates)
async def driver_live_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle live location updates from drivers only"""
    try:
        # Check if we have a valid message with location
        if not update.message:
            logger.debug("No message in update")
            return
            
        if not update.message.location:
            logger.debug("No location in message - message type: %s", update.message.content_type)
            return
        
        loc = update.message.location
        chat_id = update.effective_user.id
        
        # Additional validation for location coordinates
        if not loc.latitude or not loc.longitude:
            logger.debug("Invalid location coordinates: lat=%s, lon=%s", loc.latitude, loc.longitude)
            return
            
        logger.debug("Received location from user %s: %s,%s", chat_id, loc.latitude, loc.longitude)
        
        # Check user role first
        user_role = get_user_role(chat_id)
        
        if user_role == "driver":
            # Driver location update
            ok = update_driver_location(chat_id, loc.latitude, loc.longitude)
            if ok:
                # Only send confirmation message for the first update to avoid spam
                if not context.user_data.get('location_confirmed'):
                    await update.message.reply_text("✅ تم تفعيل التتبع الحي - سيتم تحديث موقعك تلقائياً")
                    context.user_data['location_confirmed'] = True
                else:
                    # Silent update for subsequent location updates
                    logger.debug("Silent location update for driver %s", chat_id)
            else:
                logger.error("Failed to update location for driver %s", chat_id)
                await update.message.reply_text("⚠️ حدث خطأ أثناء تحديث موقعك.")
        else:
            # Client sending live location - automatically show nearby drivers
            logger.debug("User %s is client, showing nearby drivers", chat_id)
            client_loc = (loc.latitude, loc.longitude)
            context.user_data['client_search_loc'] = client_loc
            
            # Show nearby drivers immediately with default price
            await update.message.reply_text("🔍 جاري البحث عن سائقين قريبين من موقعك...")
            await display_nearby_drivers(update, context, client_loc, "25")
            
    except Exception as e:
        logger.exception("Error in driver_live_location handler: %s", e)

# Handler for Client Single Location (for search)
async def client_single_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle single location from clients for search purposes"""
    try:
        # Check if we have a valid message with location
        if not update.message or not update.message.location:
            logger.debug("No location found in update message")
            return
        
        loc = update.message.location
        chat_id = update.effective_user.id
        
        # Additional validation for location coordinates
        if not loc.latitude or not loc.longitude:
            logger.debug("Invalid location coordinates from client: lat=%s, lon=%s", loc.latitude, loc.longitude)
            return
            
        logger.debug("Received single location from client %s: %s,%s", chat_id, loc.latitude, loc.longitude)
        
        # Store for client search and show nearby drivers immediately
        client_loc = (loc.latitude, loc.longitude)
        context.user_data['client_search_loc'] = client_loc
        
        await update.message.reply_text("🔍 جاري البحث عن سائقين قريبين من موقعك...")
        await display_nearby_drivers(update, context, client_loc, "25")
        
    except Exception as e:
        logger.exception("Error in client_single_location handler: %s", e)

# Handle price input from clients
async def handle_client_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle price input from clients after location sharing"""
    if context.user_data.get('awaiting_price'):
        txt = (update.message.text or "").strip()
        try:
            client_price = float(txt)
            client_loc = context.user_data.get('client_search_loc')
            if client_loc:
                await display_nearby_drivers(update, context, client_loc, str(client_price))
                context.user_data['awaiting_price'] = False
            else:
                await update.message.reply_text("❌ لم يتم تحديد موقع. يرجى إرسال موقعك أولاً.")
        except ValueError:
            await update.message.reply_text("❌ الرجاء إدخال رقم صالح للسعر (مثال: 25)")

# Client search flow
async def find_driver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_role = get_user_role(user_id)
    
    if current_role == "driver":
        await update.message.reply_text("❌ أنت سائق! يمكنك استخدام /start_tracking لمشاركة موقعك.")
        return ConversationHandler.END
        
    kb = [[KeyboardButton("📍 إرسال موقعي الحالي", request_location=True)], ["تخطي الموقع"]]
    await update.message.reply_text("أرسل موقعك الحالي أو اختر 'تخطي الموقع' للبحث بدون موقع.", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return CLIENT_PICK_LOC

async def client_pick_loc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text or ""
    if txt.strip() == "تخطي الموقع":
        context.user_data['client_search_loc'] = None
        await update.message.reply_text("فلترة حسب الجنسية؟ اكتب اسم الجنسية أو 'لا' للتخطي")
        return CLIENT_NATION
    else:
        # If user sends text instead of location, prompt again
        kb = [[KeyboardButton("📍 إرسال موقعي الحالي", request_location=True)], ["تخطي الموقع"]]
        await update.message.reply_text(
            "الرجاء استخدام الزر أدناه لإرسال موقعك الحالي أو اختر 'تخطي الموقع'",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
        )
        return CLIENT_PICK_LOC

async def client_nation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    context.user_data['filter_nation'] = None if txt == "لا" else txt
    await update.message.reply_text("فلترة حسب نوع المركبة؟ اكتب النوع أو 'لا' للتخطي")
    return CLIENT_VTYPE

async def client_vtype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    context.user_data['filter_vtype'] = None if txt == "لا" else txt
    await update.message.reply_text("فلترة حسب جنس السائق؟ اكتب 'ذكر' أو 'انثى' أو 'لا' للتخطي")
    return CLIENT_GENDER

async def client_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    context.user_data['filter_gender'] = None if txt == "لا" else txt
    await update.message.reply_text(f"أدخل السعر المقترح بالـ{CURRENCY} (رقم فقط)، مثال: 25")
    return CLIENT_PRICE

async def client_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    context.user_data['client_price'] = txt
    kb = [["قائمة نصية", "خرائط (روابط)"]]
    await update.message.reply_text("اختر طريقة عرض النتائج:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return CLIENT_DISPLAY_CHOICE

async def client_display_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = (update.message.text or "").strip()
    client_loc = context.user_data.get("client_search_loc")
    nation = context.user_data.get("filter_nation")
    vtype = context.user_data.get("filter_vtype")
    gender = context.user_data.get("filter_gender")
    client_price = context.user_data.get("client_price")

    filtered = filter_and_sort_drivers(client_loc, nation, vtype, gender)
    if not filtered:
        await update.message.reply_text("❌ لم يتم العثور على سائقين مطابقين للمعايير.")
        return ConversationHandler.END

    drivers_only = [d for d, _ in filtered]
    maps_link = build_maps_link(client_loc, drivers_only)

    for d, dist in filtered:
        name = d.get("driver_name", "—")
        nat = d.get("nationality", "—")
        v = d.get("vehicle_type", "—")
        vm = d.get("vehicle_make", "")
        vy = d.get("vehicle_year", "")
        gen = d.get("gender", "—")
        phone = d.get("phone", "—")
        lat = d.get("latitude", "")
        lon = d.get("longitude", "")
        dist_text = f" — {dist:.2f} km" if dist is not None else ""
        text = (
            f"👤 {name} ({nat}){dist_text}\n"
            f"🚘 {v} {vm} ({vy})\n"
            f"🚹 الجنس: {gen}\n"
            f"📞 {phone}\n"
            f"📍 موقع: https://www.google.com/maps/search/?api=1&query={lat},{lon}\n"
            f"💰 سعرك المقترح: {format_price(client_price)}"
        )
        cbdata = f"request:{d.get('chat_id')}:{client_price}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚕 اطلب من هذا السائق", callback_data=cbdata)]])
        if choice == "قائمة نصية":
            await update.message.reply_text(text, reply_markup=kb)
        else:
            short = f"{name}{dist_text} — {v} — {format_price(client_price)}"
            await update.message.reply_text(short, reply_markup=kb)

    await update.message.reply_text(f"🔗 عرض جميع السائقين على الخريطة:\n{maps_link}")
    return ConversationHandler.END

# Request flow and driver responses (Accept / Counter / Reject)
async def request_driver_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 3:
        await query.edit_message_text("خطأ في بيانات الطلب.")
        return
    driver_chat_id = parts[1]
    client_price = parts[2]
    client = query.from_user
    client_chat_id = client.id
    client_name = client.full_name
    pickup_loc = context.user_data.get("client_search_loc", "")
    order = {
        "order_id": new_order_id(),
        "client_id": client_chat_id,
        "client_name": client_name,
        "pickup_loc": f"{pickup_loc}" if pickup_loc else "",
        "pickup_desc": "",
        "dest_loc": "",
        "dest_desc": "",
        "client_price": f"{client_price} {CURRENCY}",
        "currency": CURRENCY,
        "status": "pending",
        "driver_id": "",
        "driver_name": "",
        "driver_price": "",
        "counter_price": "",
        "timestamp": datetime.utcnow().isoformat(),
    }
    add_order_to_sheet(order)
    await query.edit_message_text("تم إرسال طلبك إلى السائق — ننتظر رده.")
    logger.info("Client %s requested driver %s order %s", client_chat_id, driver_chat_id, order["order_id"])

    # send request to driver with inline buttons
    driver_record = None
    recs = drivers_ws.get_all_records()
    for r in recs:
        if str(r.get("chat_id")) == str(driver_chat_id):
            driver_record = r
            break
    if not driver_record:
        await context.bot.send_message(chat_id=client_chat_id, text="لم أستطع إيجاد السائق في السجلات.")
        logger.warning("Driver record not found for chat_id=%s", driver_chat_id)
        return

    # store pending mapping in application.user_data for driver chat
    context.application.user_data[int(driver_chat_id)] = {
        "pending_order_id": order["order_id"],
        "client_chat_id": client_chat_id,
        "client_name": client_name,
        "client_price": client_price
    }

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ قبول الطلب", callback_data=f"driver_accept:{order['order_id']}:{client_chat_id}:{client_price}")],
        [InlineKeyboardButton("💬 اقترح سعرًا آخر", callback_data=f"driver_counter:{order['order_id']}:{client_chat_id}:{client_price}")],
        [InlineKeyboardButton("❌ رفض الطلب", callback_data=f"driver_reject:{order['order_id']}:{client_chat_id}")]
    ])

    pickup_text = f"موقع العميل: {pickup_loc}" if pickup_loc else "موقع العميل غير متوفر"
    msg = (
        f"📦 لديك طلب توصيل جديد من {client_name}\n"
        f"السعر المقترح: {format_price(client_price)}\n"
        f"{pickup_text}\n"
        f"يمكنك قبول الطلب، أو اقتراح سعر آخر، أو رفضه."
    )
    try:
        await context.bot.send_message(chat_id=int(driver_chat_id), text=msg, reply_markup=kb)
        logger.info("Sent request %s to driver %s", order["order_id"], driver_chat_id)
    except Exception as e:
        logger.exception("Could not send request to driver %s: %s", driver_chat_id, e)
        await context.bot.send_message(chat_id=client_chat_id, text="تعذر إرسال الطلب للسائق (خطأ بالتواصل).")

# Driver accept/reject/counter flows
async def driver_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) < 4:
        await query.edit_message_text("بيانات ناقصة.")
        return
    order_id = parts[1]
    client_chat_id = int(parts[2])
    client_price = parts[3]
    driver_chat_id = query.from_user.id
    driver_name = query.from_user.full_name

    update_order_in_sheet(order_id, {
        "status": "accepted",
        "driver_id": f"D{driver_chat_id}",
        "driver_name": driver_name,
        "driver_price": f"{client_price} {CURRENCY}"
    })
    await query.edit_message_text(f"لقد قبلت الطلب {order_id} — تم إعلام العميل.")
    logger.info("Driver %s accepted order %s", driver_chat_id, order_id)

    # notify client
    recs = drivers_ws.get_all_records()
    phone = "—"; vehicle = "—"; lat = lon = None
    for r in recs:
        if str(r.get("chat_id")) == str(driver_chat_id):
            phone = r.get("phone", "—"); vehicle = f"{r.get('vehicle_type','')} {r.get('vehicle_make','')}".strip()
            lat = r.get("latitude"); lon = r.get("longitude")
            break
    maps_link = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}" if lat and lon else ""
    try:
        await context.bot.send_message(
            chat_id=client_chat_id,
            text=(
                f"✅ تم قبول طلبك {order_id} من قبل {driver_name}\n"
                f"🚗 المركبة: {vehicle}\n"
                f"📞 الجوال: {phone}\n"
                f"💰 السعر المتفق عليه: {format_price(client_price)}\n"
                f"📍 موقع السائق: {maps_link}"
            )
        )
    except Exception as e:
        logger.warning("Could not notify client %s: %s", client_chat_id, e)

async def driver_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) < 3:
        await query.edit_message_text("بيانات ناقصة.")
        return
    order_id = parts[1]
    client_chat_id = int(parts[2])
    update_order_in_sheet(order_id, {"status": "rejected"})
    await query.edit_message_text("تم رفض الطلب.")
    try:
        await context.bot.send_message(chat_id=client_chat_id, text="⚠️ للأسف تم رفض طلبك من قبل السائق. يمكنك اختيار سائق آخر.")
        logger.info("Client %s notified of rejection for order %s", client_chat_id, order_id)
    except Exception as e:
        logger.warning("Could not notify client of rejection: %s", e)

async def driver_counter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) < 4:
        await query.edit_message_text("بيانات ناقصة.")
        return
    order_id = parts[1]
    client_chat_id = int(parts[2])
    client_price = parts[3]
    driver_chat_id = query.from_user.id
    # store pending counter on application.user_data
    context.application.user_data[driver_chat_id] = {"pending_counter_order": order_id, "client_chat_id": client_chat_id, "client_price": client_price}
    await query.edit_message_text("أدخل السعر الجديد الذي تقترحه (رقم فقط)، ثم أرسله هنا.")
    logger.debug("Driver %s entering counter for order %s", driver_chat_id, order_id)

async def handle_driver_text_for_counter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = context.application.user_data.get(user.id)
    if not data or "pending_counter_order" not in data:
        return
    txt = (update.message.text or "").strip()
    try:
        proposed = float(txt)
    except Exception:
        await update.message.reply_text("الرجاء إرسال رقم صالح للسعر (مثال: 30).")
        return
    order_id = data["pending_counter_order"]
    client_chat_id = data["client_chat_id"]
    driver_chat_id = user.id
    driver_name = user.full_name

    update_order_in_sheet(order_id, {"status": "counter_proposed", "counter_price": f"{proposed} {CURRENCY}"})
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ قبول العرض", callback_data=f"client_accept_counter:{order_id}:{driver_chat_id}:{proposed}")],
        [InlineKeyboardButton("❌ رفض العرض", callback_data=f"client_reject_counter:{order_id}:{driver_chat_id}")]
    ])
    try:
        await context.bot.send_message(chat_id=client_chat_id, text=(f"💬 السائق {driver_name} اقترح سعرًا جديدًا للطلب {order_id}: {format_price(proposed)}\nهل تقبل العرض؟"), reply_markup=kb)
        await update.message.reply_text("تم إرسال عرضك إلى العميل.")
        logger.info("Driver %s sent counter %s for order %s", driver_chat_id, proposed, order_id)
    except Exception as e:
        logger.warning("Could not send counter to client %s: %s", client_chat_id, e)
    context.application.user_data.pop(user.id, None)

async def client_accept_counter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) < 4:
        await query.edit_message_text("بيانات ناقصة.")
        return
    order_id = parts[1]
    driver_chat_id = int(parts[2])
    proposed = parts[3]
    client_chat_id = query.from_user.id

    update_order_in_sheet(order_id, {"status": "accepted", "driver_id": f"D{driver_chat_id}", "driver_price": f"{proposed} {CURRENCY}", "counter_price": f"{proposed} {CURRENCY}"})
    await query.edit_message_text(f"✅ قبلت العرض. تم تأكيد السائق للطلب {order_id}.")
    try:
        await context.bot.send_message(chat_id=driver_chat_id, text=(f"✅ تم قبول عرضك للطلب {order_id} من قبل العميل. السعر المتفق عليه: {format_price(proposed)}"))
        logger.info("Client %s accepted counter %s for order %s", client_chat_id, proposed, order_id)
    except Exception as e:
        logger.warning("Could not notify driver about accepted counter: %s", e)

async def client_reject_counter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) < 3:
        await query.edit_message_text("بيانات ناقصة.")
        return
    order_id = parts[1]
    driver_chat_id = int(parts[2])
    update_order_in_sheet(order_id, {"status": "rejected"})
    await query.edit_message_text("تم رفض عرض السائق. يمكنك اختيار سائق آخر.")
    try:
        await context.bot.send_message(chat_id=driver_chat_id, text=(f"⚠️ تم رفض عرضك للطلب {order_id} من قبل العميل."))
        logger.info("Client rejected counter for order %s", order_id)
    except Exception as e:
        logger.warning("Could not notify driver about rejected counter: %s", e)

# Help command
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_role = get_user_role(user_id)
    
    if current_role == "driver":
        help_text = (
            "🚗 **أوامر السائقين**:\n\n"
            "/start_tracking - مشاركة موقعك الحي\n"
            "/stop_tracking - إيقاف مشاركة الموقع\n"
            "/help - عرض هذه الرسالة\n\n"
            "كسائق، سيتم تحديث موقعك تلقائيًا وستتلقى طلبات التوصيل من العملاء."
        )
    elif current_role == "client":
        help_text = (
            "🛍️ **أوامر العملاء**:\n\n"
            "/find_driver - البحث عن سائقين قريبين\n"
            "أو أرسل موقعك مباشرة لعرض السائقين القريبين\n"
            "/become_driver - التسجيل كسائق\n"
            "/help - عرض هذه الرسالة\n\n"
            "يمكنك إرسال موقعك وسيتم عرض السائقين القريبين تلقائيًا."
        )
    else:
        help_text = (
            "مرحبًا! 👋\n\n"
            "هذا بوت توصيل يمكنك استخدامه ك:\n\n"
            "🛍️ **عميل**: للبحث عن سائقين وتقديم طلبات توصيل\n"
            "🚗 **سائق**: لمشاركة موقعك وتلقي طلبات التوصيل\n\n"
            "استخدم /start للبدء والتسجيل."
        )
    
    await update.message.reply_text(help_text)

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log errors and handle them gracefully"""
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    # Notify user about the error
    if update and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ حدث خطأ غير متوقع. يرجى المحاولة مرة أخرى لاحقًا."
            )
        except Exception:
            pass

# ------------------ MAIN ------------------
def main():
    global SHEET, orders_ws, drivers_ws, users_ws
    if BOT_TOKEN.startswith("PUT_YOUR_BOT_TOKEN"):
        logger.error("BOT_TOKEN not set. Please set BOT_TOKEN environment variable or edit the script.")
        return
    if SHEET_ID.startswith("PUT_YOUR_SHEET_ID"):
        logger.error("SHEET_ID not set. Please set SHEET_ID environment variable or edit the script.")
        return

    logger.info("Connecting to Google Sheets...")
    try:
        SHEET = connect_sheets(GOOGLE_CREDS_PATH, SHEET_ID)
    except Exception as e:
        logger.exception("Failed to connect to Google Sheets: %s", e)
        return

    ensure_sheet_structure()
    orders_ws = SHEET.worksheet(ORDERS_SHEET_NAME)
    drivers_ws = SHEET.worksheet(DRIVERS_SHEET_NAME)
    users_ws = SHEET.worksheet(USERS_SHEET_NAME)
    logger.info("Google Sheets connected and ready.")

    # Build application
    app = Application.builder().token(BOT_TOKEN).build()

    # Add error handler
    app.add_error_handler(error_handler)

    # Conversation handler (registration + client find flow)
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, role_choice)],
            DRIVER_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, driver_age)],
            DRIVER_NATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, driver_nation)],
            DRIVER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, driver_phone)],
            DRIVER_VTYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, driver_vtype)],
            DRIVER_VMAKE: [MessageHandler(filters.TEXT & ~filters.COMMAND, driver_vmake)],
            DRIVER_VYEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, driver_vyear)],
            DRIVER_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, driver_gender)],
            CLIENT_PICK_LOC: [
                MessageHandler(filters.LOCATION, client_single_location),  # Single location for clients
                MessageHandler(filters.TEXT & ~filters.COMMAND, client_pick_loc)
            ],
            CLIENT_NATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_nation)],
            CLIENT_VTYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_vtype)],
            CLIENT_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_gender)],
            CLIENT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_price)],
            CLIENT_DISPLAY_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_display_choice)],
        },
        fallbacks=[CommandHandler("help", help_command)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # commands & handlers
    app.add_handler(CommandHandler("find_driver", find_driver_start))
    app.add_handler(CommandHandler("start_tracking", start_tracking))
    app.add_handler(CommandHandler("stop_tracking", stop_tracking))
    app.add_handler(CommandHandler("become_driver", become_driver))
    app.add_handler(CommandHandler("help", help_command))

    # Improved location handler with better filtering
    app.add_handler(MessageHandler(
        filters.LOCATION & 
        filters.ChatType.PRIVATE & 
        ~filters.UpdateType.EDITED_MESSAGE &
        ~filters.UpdateType.EDITED_CHANNEL_POST,
        driver_live_location
    ))
    
    # Handler for client price input
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_client_price_input
    ))

    # callback handlers
    app.add_handler(CallbackQueryHandler(request_driver_callback, pattern=r"^request:"))
    app.add_handler(CallbackQueryHandler(driver_accept_callback, pattern=r"^driver_accept:"))
    app.add_handler(CallbackQueryHandler(driver_reject_callback, pattern=r"^driver_reject:"))
    app.add_handler(CallbackQueryHandler(driver_counter_callback, pattern=r"^driver_counter:"))
    app.add_handler(CallbackQueryHandler(client_accept_counter_callback, pattern=r"^client_accept_counter:"))
    app.add_handler(CallbackQueryHandler(client_reject_counter_callback, pattern=r"^client_reject_counter:"))

    # driver text handler for counteroffers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_driver_text_for_counter))

    logger.info("Bot starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()