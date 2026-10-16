import os
import sys
import logging
import secrets
import asyncio
from typing import Optional, Dict

import config
import database

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# State tracking for Admin actions (e.g. which stage admin is currently recording)
admin_recording_state: Dict[int, Optional[int]] = {}

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        Application,
        CommandHandler,
        CallbackQueryHandler,
        MessageHandler,
        ContextTypes,
        filters,
    )
except ImportError:
    # If python-telegram-bot is not installed yet (e.g. inside build container)
    Update = None
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None
    Application = None
    CommandHandler = None
    CallbackQueryHandler = None
    MessageHandler = None
    ContextTypes = None
    filters = None

# --- UI Keyboards ---

def get_admin_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔗 ساخت لینک دعوت یکبارمصرف", callback_data="admin_gen_invite")],
        [InlineKeyboardButton("📁 مدیریت و بارگذاری محتوای مراحل", callback_data="admin_manage_stages")],
        [InlineKeyboardButton("🎙 افزودن ویس جدید به بخش ۴", callback_data="admin_record_4")],
        [InlineKeyboardButton("📊 لیست و وضعیت دانش‌آموزان", callback_data="admin_stats")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_stages_selection_keyboard():
    keyboard = [
        [InlineKeyboardButton("📥 بارگذاری مرحله ۱", callback_data="admin_record_1")],
        [InlineKeyboardButton("📥 بارگذاری مرحله ۲", callback_data="admin_record_2")],
        [InlineKeyboardButton("📥 بارگذاری مرحله ۳", callback_data="admin_record_3")],
        [InlineKeyboardButton("🎙 بارگذاری بخش ۴ (نکات جدید)", callback_data="admin_record_4")],
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="admin_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_stop_recording_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ اتمام ذخیره پیام‌ها", callback_data="admin_stop_recording")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_student_stage_keyboard(next_stage: int):
    if next_stage <= 3:
        btn_text = f"✅ گوش دادم | رفتن به مرحله {next_stage}"
        callback = f"student_goto_{next_stage}"
    else:
        btn_text = "🎉 اتمام دوره | ورود به بخش ۴ (نکات جدید)"
        callback = "student_goto_4"
    keyboard = [
        [InlineKeyboardButton(btn_text, callback_data=callback)],
        [InlineKeyboardButton("🎙 دسترسی به بخش ۴ (نکات و ویس‌های مکمل)", callback_data="student_view_4")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_student_hub_keyboard():
    keyboard = [
        [InlineKeyboardButton("🎙 باز کردن بخش ۴ (ویس‌ها و نکات جدید)", callback_data="student_view_4")],
        [InlineKeyboardButton("🔄 دریافت مجدد مرحله فعلی", callback_data="student_repeat_current")]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Message Delivery Helpers ---

async def deliver_stage_content(bot, chat_id: int, stage: int, next_stage: int):
    msgs = database.get_stage_messages(config.DB_PATH, stage)
    if not msgs:
        await bot.send_message(
            chat_id=chat_id,
            text=f"📌 محتوای **مرحله {stage}** هنوز توسط مشاور بارگذاری نشده است. به زودی در دسترس قرار می‌گیرد.",
            parse_mode="Markdown"
        )
        return

    await bot.send_message(
        chat_id=chat_id,
        text=f"📚 **محتوای مرحله {stage} برای شما ارسال می‌شود:**
لطفاً ویس‌ها و نکات زیر را با دقت گوش داده و یادداشت‌برداری کنید.",
        parse_mode="Markdown"
    )

    for item in msgs:
        try:
            await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=item["from_chat_id"],
                message_id=item["message_id"]
            )
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.warning(f"Failed to copy message {item[message_id]} for stage {stage}: {e}")

    await bot.send_message(
        chat_id=chat_id,
        text=f"✨ تمام ویس‌های **مرحله {stage}** ارسال شد.\nپس از گوش دادن کامل به این بخش، دکمه زیر را لمس کنید تا مرحله بعدی برای شما باز شود:",
        parse_mode="Markdown",
        reply_markup=get_student_stage_keyboard(next_stage)
    )

async def deliver_section_4(bot, chat_id: int):
    msgs = database.get_stage_messages(config.DB_PATH, 4)
    if not msgs:
        await bot.send_message(
            chat_id=chat_id,
            text="🎙 **بخش ۴: ویس‌ها و نکات جدید**\n\nدر حال حاضر ویس جدیدی آپلود نشده است. به محض ضبط فایل جدید توسط مشاور، در این بخش اضافه خواهد شد.",
            parse_mode="Markdown",
            reply_markup=get_student_hub_keyboard()
        )
        return

    await bot.send_message(
        chat_id=chat_id,
        text=f"🎙 **بخش ۴: ویس‌ها و نکات جدید**\nتعداد {len(msgs)} فایل/نکته مکمل برای شما ارسال می‌شود:",
        parse_mode="Markdown"
    )

    for item in msgs:
        try:
            await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=item["from_chat_id"],
                message_id=item["message_id"]
            )
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.warning(f"Failed to copy section 4 message {item[message_id]}: {e}")

    await bot.send_message(
        chat_id=chat_id,
        text="✅ تمام فایل‌های بخش ۴ ارسال شد.",
        reply_markup=get_student_hub_keyboard()
    )

# --- Core Handlers ---

async def start_handler(update, context):
    if not update or not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.full_name or "دانش‌آموز"
    username = update.effective_user.username

    # 1. Admin check
    if config.ADMIN_ID and user_id == config.ADMIN_ID:
        await update.message.reply_text(
            f"👑 **سلام آقای دکتر / مشاور گرامی**\nخوش آمدید به پنل مدیریت ربات مشاوره هاشمی.",
            parse_mode="Markdown",
            reply_markup=get_admin_main_keyboard()
        )
        return

    # 2. Check for invite argument (/start inv_XXXXXX)
    args = context.args if context else []
    if args and len(args) > 0 and args[0].startswith("inv_"):
        code = args[0]
        success = database.validate_and_use_invite(
            config.DB_PATH, code, user_id, username, user_name
        )
        if success:
            await update.message.reply_text(
                f"🌸 **سلام {user_name} عزیز، به ربات مشاوره تحصیلی خوش آمدید!**\n\n"
                f"شما با موفقیت ثبت‌نام شدید. فرآیند آموزشی شما در ۳ مرحله طراحی شده است.\n"
                f"آماده‌اید؟ مرحله اول همین الان برای شما ارسال می‌شود:",
                parse_mode="Markdown"
            )
            await deliver_stage_content(context.bot, user_id, stage=1, next_stage=2)
            return
        else:
            await update.message.reply_text(
                "❌ **لینک دعوت نامعتبر یا منقضی شده است.**\n"
                "این لینک قبلاً استفاده شده است یا وجود ندارد. لطفاً جهت دریافت لینک جدید با مشاور هماهنگ کنید.",
                parse_mode="Markdown"
            )
            return

    # 3. Existing registered student check
    student = database.get_student(config.DB_PATH, user_id)
    if student:
        stage = student["current_stage"]
        if stage <= 3:
            await update.message.reply_text(
                f"👋 **سلام مجدد {user_name} عزیز.**\n"
                f"شما در حال حاضر در **مرحله {stage}** هستید. محتوای این مرحله مجدداً برای شما ارسال می‌شود:",
                parse_mode="Markdown"
            )
            await deliver_stage_content(context.bot, user_id, stage=stage, next_stage=stage + 1)
        else:
            await update.message.reply_text(
                f"🎉 **سلام {user_name} عزیز!**\n"
                f"شما دوره‌های ۳ مرحله‌ای اولیه را تکمیل کرده‌اید و به بخش ۴ دسترسی کامل دارید.",
                parse_mode="Markdown",
                reply_markup=get_student_hub_keyboard()
            )
        return

    # 4. Unauthorized visitor
    await update.message.reply_text(
        "⛔️ **دسترسی غیرمجاز**\n\n"
        "ورود به ربات مشاوره فقط با «لینک دعوت اختصاصی» که مشاور در اختیارتان می‌گذارد امکان‌پذیر است.",
        parse_mode="Markdown"
    )

# --- Admin Callback Handler ---

async def admin_callback_handler(update, context):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if config.ADMIN_ID and user_id != config.ADMIN_ID:
        await query.edit_message_text("⛔️ شما به این بخش دسترسی ندارید.")
        return

    data = query.data

    if data == "admin_menu":
        admin_recording_state[user_id] = None
        await query.edit_message_text(
            "👑 **پنل مدیریت ربات مشاوره هاشمی:**",
            parse_mode="Markdown",
            reply_markup=get_admin_main_keyboard()
        )

    elif data == "admin_gen_invite":
        token = secrets.token_hex(4)  # 8 char hex
        code = f"inv_{token}"
        database.create_invite(config.DB_PATH, code)

        bot_info = await context.bot.get_me()
        bot_user = bot_info.username
        invite_link = f"https://t.me/{bot_user}?start={code}"

        msg = (
            f"🔗 **لینک دعوت اختصاصی یکبارمصرف ساخته شد:**\n\n"
            f"`{invite_link}`\n\n"
            f"📌 **نکات مهم:**\n"
            f"• این لینک را کپی کرده و برای دانش‌آموز جدید ارسال کنید.\n"
            f"• لینک فقط ۱ بار قابل استفاده است و به محض ورود دانش‌آموز خودکار باطل می‌شود."
        )
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif data == "admin_manage_stages":
        await query.edit_message_text(
            "📁 **مرحله مورد نظر برای بارگذاری محتوا را انتخاب کنید:**\n\n"
            "پس از انتخاب، پیام‌ها را از گروه مشاوره هاشمی مستقیماً به اینجا فوروارد کنید.",
            parse_mode="Markdown",
            reply_markup=get_stages_selection_keyboard()
        )

    elif data.startswith("admin_record_"):
        stage_num = int(data.split("_")[-1])
        admin_recording_state[user_id] = stage_num
        stage_title = f"مرحله {stage_num}" if stage_num <= 3 else "بخش ۴ (ویس‌های جدید و مکمل)"

        await query.edit_message_text(
            f"🎙 **حالت ذخیره‌سازی برای «{stage_title}» فعال شد.**\n\n"
            f"📥 هم‌اکنون پیام‌ها، ویس‌ها، متن‌ها و فایل‌های این مرحله را از گروه مشاوره به این چت فوروارد کنید.\n"
            f"هر پیامی بفرستید خودکار ذخیره می‌شود.\n"
            f"وقتی تمام پیام‌های این بخش را فرستادید، دکمه «اتمام ذخیره» را بزنید.",
            parse_mode="Markdown",
            reply_markup=get_stop_recording_keyboard()
        )

    elif data == "admin_stop_recording":
        curr_stage = admin_recording_state.get(user_id)
        admin_recording_state[user_id] = None
        count = len(database.get_stage_messages(config.DB_PATH, curr_stage)) if curr_stage else 0

        await query.edit_message_text(
            f"✅ **ذخیره پیام‌های مرحله با موفقیت به پایان رسید.**\n"
            f"مجموع پیام‌های ذخیره شده در این مرحله: {count} پیام.\n\n"
            f"به پنل اصلی بازگشتید.",
            parse_mode="Markdown",
            reply_markup=get_admin_main_keyboard()
        )

    elif data == "admin_stats":
        students = database.list_students(config.DB_PATH)
        counts = database.get_student_count_by_stage(config.DB_PATH)

        s1 = counts.get(1, 0)
        s2 = counts.get(2, 0)
        s3 = counts.get(3, 0)
        s4 = counts.get(4, 0)
        total = len(students)

        report = (
            f"📊 **آمار و وضعیت دانش‌آموزان:**\n\n"
            f"👥 کل دانش‌آموزان عضو: {total} نفر\n"
            f"• در حال گذراندن مرحله ۱: {s1} نفر\n"
            f"• در حال گذراندن مرحله ۲: {s2} نفر\n"
            f"• در حال گذراندن مرحله ۳: {s3} نفر\n"
            f"• فارغ‌التحصیل و در بخش ۴: {s4} نفر\n\n"
            f"📋 **آخرین دانش‌آموزان ثبت‌نام شده:**\n"
        )
        for idx, st in enumerate(students[:10], start=1):
            uname = f"@{st[username]}" if st["username"] else "بدون یوزرنیم"
            report += f"{idx}. {st[full_name]} ({uname}) - مرحله {st[current_stage]}\n"

        if total == 0:
            report += "هنوز دانش‌آموزی ثبت‌نام نکرده است.\n"

        await query.message.reply_text(report, parse_mode="Markdown")

# --- Student Callback Handler ---

async def student_callback_handler(update, context):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    student = database.get_student(config.DB_PATH, user_id)
    if not student:
        await query.message.reply_text("⛔️ دسترسی شما یافت نشد. لطفاً مجدد با لینک دعوت استارت کنید.")
        return

    data = query.data

    if data.startswith("student_goto_"):
        next_stage = int(data.split("_")[-1])
        database.update_student_stage(config.DB_PATH, user_id, next_stage)

        if next_stage <= 3:
            await query.message.reply_text(
                f"👏 **آفرین بر شما! مرحله {next_stage} باز شد.**\n"
                f"فایل‌های آموزشی این مرحله ارسال می‌شوند:",
                parse_mode="Markdown"
            )
            await deliver_stage_content(context.bot, user_id, stage=next_stage, next_stage=next_stage + 1)
        else:
            await query.message.reply_text(
                "🎉 **تبریک ویژه! شما هر ۳ مرحله اصلی آموزش مشاوره را به پایان رساندید.**\n\n"
                "از این پس می‌توانید ویس‌ها و تجربیات جدید اضافه شده را در «بخش ۴» بشنوید.",
                parse_mode="Markdown",
                reply_markup=get_student_hub_keyboard()
            )

    elif data == "student_view_4":
        await deliver_section_4(context.bot, user_id)

    elif data == "student_repeat_current":
        curr_stage = student["current_stage"]
        if curr_stage <= 3:
            await deliver_stage_content(context.bot, user_id, stage=curr_stage, next_stage=curr_stage + 1)
        else:
            await deliver_section_4(context.bot, user_id)

# --- Admin Message Forward Collector ---

async def admin_message_collector(update, context):
    if not update or not update.message:
        return

    user_id = update.effective_user.id
    if config.ADMIN_ID and user_id != config.ADMIN_ID:
        return

    curr_stage = admin_recording_state.get(user_id)
    if not curr_stage:
        return

    msg = update.message
    caption = msg.caption or (msg.text[:50] if msg.text else None)

    database.add_stage_message(
        config.DB_PATH,
        stage=curr_stage,
        from_chat_id=msg.chat_id,
        message_id=msg.message_id,
        caption=caption
    )

    stage_title = f"مرحله {curr_stage}" if curr_stage <= 3 else "بخش ۴"
    count = len(database.get_stage_messages(config.DB_PATH, curr_stage))

    await msg.reply_text(
        f"✅ پیام شماره {count} برای **{stage_title}** ذخیره شد.",
        parse_mode="Markdown"
    )

# --- Application Setup ---

def create_application():
    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN is not set in config or environment variables!")
        return None

    app = Application.builder().token(config.BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(student_callback_handler, pattern="^student_"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, admin_message_collector))

    return app

def main():
    database.init_db(config.DB_PATH)
    logger.info("Database initialized successfully.")

    # Start 24/7 keep-alive server and 5-minute ping loop for Render
    from keep_alive import start_keep_alive
    start_keep_alive(port=config.PORT, ping_url=config.RENDER_EXTERNAL_URL)

    app = create_application()
    if not app:
        print("Please configure BOT_TOKEN in .env or environment variables.")
        sys.exit(1)

    logger.info("Bot starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
