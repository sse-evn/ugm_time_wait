import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta
import pytz
import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.dispatcher import FSMContext
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from dotenv import load_dotenv
from collections import defaultdict
from gspread_formatting import *

load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    logging.error("BOT_TOKEN is not set")
    raise ValueError("BOT_TOKEN is not set")
logging.info(f"Loaded BOT_TOKEN: {API_TOKEN[:10]}...")
GROUP_TIMEZONE = pytz.timezone('Asia/Almaty')
GOOGLE_SHEETS_ID = "1QWCYpeBQGofESEkD4WWYAIl0fvVDt7VZvWOE-qKe_RE"

ADMIN_IDS_STR = os.getenv("ADMIN_IDS")
if ADMIN_IDS_STR:
    ADMIN_IDS = {int(uid.strip()) for uid in ADMIN_IDS_STR.split(',')}
else:
    ADMIN_IDS = set()

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

storage = MemoryStorage()
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot, storage=storage)

def init_google_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH"), scopes=scope)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(GOOGLE_SHEETS_ID)

    # Создание или получение листа "Report"
    try:
        report_worksheet = spreadsheet.worksheet("Report")
    except gspread.exceptions.WorksheetNotFound:
        report_worksheet = spreadsheet.add_worksheet(title="Report", rows=100, cols=10)
        report_worksheet.update(range_name="A1:E1", values=[['Дата', 'Имя', 'Время', 'Зона', 'Witag']])
        set_row_height(report_worksheet, '1', 40)
        set_column_width(report_worksheet, 'A:E', 120)
        fmt = CellFormat(
            backgroundColor=Color(0.9, 0.9, 0.9),
            textFormat=TextFormat(bold=True),
            horizontalAlignment='CENTER'
        )
        format_cell_range(report_worksheet, 'A1:E1', fmt)

    # Создание или получение листа "Timesheet"
    try:
        timesheet_worksheet = spreadsheet.worksheet("Timesheet")
    except gspread.exceptions.WorksheetNotFound:
        timesheet_worksheet = spreadsheet.add_worksheet(title="Timesheet", rows=100, cols=40)
        
        headers = ["Сотрудник"] + [str(i) for i in range(1, 32)] + ["Итого"]
        column_count = len(headers)  # 34
        end_column_letter = chr(ord('A') + column_count - 1)  # AH
        range_str = f"A1:{end_column_letter}1"
        
        timesheet_worksheet.update(range_name=range_str, values=[headers])
        set_column_width(timesheet_worksheet, f"A:{end_column_letter}", 60)
        fmt = CellFormat(
            backgroundColor=Color(0.9, 0.9, 0.9),
            textFormat=TextFormat(bold=True),
            horizontalAlignment='CENTER'
        )
        format_cell_range(timesheet_worksheet, range_str, fmt)
    
    # Получаем "Sheet1", если нужен
    try:
        worksheet = spreadsheet.worksheet("Sheet1")
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title="Sheet1", rows=100, cols=10)

    return worksheet, report_worksheet, timesheet_worksheet

def init_db():
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            full_name TEXT,
            photo_file_id TEXT,
            shift_date TEXT,
            start_time TEXT,
            end_time TEXT,
            actual_end_time TEXT,
            worked_hours TEXT,
            zone TEXT,
            witag TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def add_shift_sqlite(user_id, full_name, photo_id, s_date, s_time, e_time, zone, witag):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    current_time_utc5 = datetime.now(GROUP_TIMEZONE)
    cur.execute('''
        INSERT INTO shifts (user_id, full_name, photo_file_id, shift_date, start_time, end_time, actual_end_time, worked_hours, zone, witag, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, full_name, photo_id, s_date, s_time, e_time, None, None, zone, witag, current_time_utc5))
    conn.commit()
    shift_id = cur.lastrowid
    conn.close()
    return shift_id

def update_shift_status(shift_id, status):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("UPDATE shifts SET status = ? WHERE id = ?", (status, shift_id))
    conn.commit()
    affected_rows = cur.rowcount
    conn.close()
    return affected_rows > 0

def get_active_shifts():
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, full_name, shift_date, start_time, end_time, zone FROM shifts WHERE status = 'active'")
    rows = cur.fetchall()
    conn.close()
    return rows

def get_all_shifts_sqlite():
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT * FROM shifts")
    rows = cur.fetchall()
    conn.close()
    return rows

def get_user_shifts_sqlite(user_id):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT * FROM shifts WHERE user_id = ?", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_today_shifts_sqlite(today_date):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT * FROM shifts WHERE shift_date = ?", (today_date,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_user_shifts_for_date_sqlite(user_id, shift_date):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT start_time, end_time FROM shifts WHERE user_id = ? AND shift_date = ?", (user_id, shift_date))
    rows = cur.fetchall()
    conn.close()
    return rows

def is_valid_time(time_str, fmt='%H:%M'):
    try:
        datetime.strptime(time_str, fmt).time()
        return True
    except ValueError:
        return False

def calculate_worked_hours(start_time_str, end_time_str):
    start = datetime.strptime(start_time_str, '%H:%M')
    end = datetime.strptime(end_time_str, '%H:%M')
    if end < start:
        end = end.replace(day=end.day + 1)
    duration = end - start
    hours, remainder = divmod(int(duration.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}h {minutes}m" if hours or minutes else "0h 0m"

def get_admin_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📊 Отчет по сменам", callback_data="admin_report"),
        InlineKeyboardButton("📋 Активные смены", callback_data="active_shifts"),
        InlineKeyboardButton("📝 Табель учета", callback_data="timesheet"),
        InlineKeyboardButton("🛑 Завершить смену", callback_data="end_shift_menu"),
        InlineKeyboardButton("❌ Отменить смену", callback_data="cancel_shift_menu")
    )
    return keyboard

def get_shift_actions_keyboard(shift_id):
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_action_{shift_id}"),
        InlineKeyboardButton("❌ Отменить", callback_data="cancel_action")
    )
    return keyboard

@dp.message_handler(commands=['admin'])
async def admin_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("🚫 Доступ запрещен")
        return
    await message.reply("👨‍💻 Панель администратора", reply_markup=get_admin_keyboard())

@dp.callback_query_handler(lambda c: c.data == 'admin_report')
async def process_admin_report(callback_query: types.CallbackQuery):
    if callback_query.from_user.id not in ADMIN_IDS:
        await bot.answer_callback_query(callback_query.id, "🚫 Доступ запрещен")
        return
    
    shifts = get_all_shifts_sqlite()
    if not shifts:
        await bot.send_message(callback_query.from_user.id, "📄 Смены не найдены.")
        return
    
    report_text = ["📊 Отчет по всем сменам"]
    for shift in shifts:
        report_text.append(
            f"\n📅 {shift[4]} | 👤 {shift[2]} | ⏰ {shift[5]}-{shift[6]} | "
            f"📍 {shift[9]} | 🔖 {shift[10]} | {'✅ Активна' if len(shift) > 11 and shift[11] == 'active' else '❌ Завершена'}"
        )
    
    await bot.send_message(callback_query.from_user.id, "\n".join(report_text))

@dp.callback_query_handler(lambda c: c.data == 'active_shifts')
async def process_active_shifts(callback_query: types.CallbackQuery):
    if callback_query.from_user.id not in ADMIN_IDS:
        await bot.answer_callback_query(callback_query.id, "🚫 Доступ запрещен")
        return
    
    active_shifts = get_active_shifts()
    if not active_shifts:
        await bot.send_message(callback_query.from_user.id, "📄 Активных смен нет.")
        return
    
    report_text = ["📋 Активные смены"]
    for shift in active_shifts:
        report_text.append(
            f"\n🆔 {shift[0]} | 👤 {shift[2]} | 📅 {shift[3]} | ⏰ {shift[4]}-{shift[5]} | "
            f"📍 {shift[6]}"
        )
    
    await bot.send_message(callback_query.from_user.id, "\n".join(report_text))

@dp.callback_query_handler(lambda c: c.data in ['end_shift_menu', 'cancel_shift_menu'])
async def process_shift_action_menu(callback_query: types.CallbackQuery):
    if callback_query.from_user.id not in ADMIN_IDS:
        await bot.answer_callback_query(callback_query.id, "🚫 Доступ запрещен")
        return
    
    action = "end" if callback_query.data == 'end_shift_menu' else "cancel"
    active_shifts = get_active_shifts()
    
    if not active_shifts:
        await bot.send_message(callback_query.from_user.id, "📄 Активных смен нет.")
        return
    
    keyboard = InlineKeyboardMarkup()
    for shift in active_shifts:
        keyboard.add(InlineKeyboardButton(
            f"🆔 {shift[0]} | 👤 {shift[2]} | ⏰ {shift[4]}-{shift[5]}",
            callback_data=f"{action}_shift_{shift[0]}"
        ))
    
    action_text = "завершить" if action == "end" else "отменить"
    await bot.send_message(
        callback_query.from_user.id,
        f"Выберите смену для {action_text}:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith(('end_shift_', 'cancel_shift_')))
async def process_shift_action(callback_query: types.CallbackQuery):
    if callback_query.from_user.id not in ADMIN_IDS:
        await bot.answer_callback_query(callback_query.id, "🚫 Доступ запрещен")
        return
    
    shift_id = int(callback_query.data.split('_')[-1])
    action = callback_query.data.split('_')[0]
    
    await bot.send_message(
        callback_query.from_user.id,
        f"Вы уверены, что хотите {'завершить' if action == 'end' else 'отменить'} смену ID {shift_id}?",
        reply_markup=get_shift_actions_keyboard(shift_id)
    )

@dp.callback_query_handler(lambda c: c.data.startswith('confirm_action_'))
async def process_confirm_action(callback_query: types.CallbackQuery):
    if callback_query.from_user.id not in ADMIN_IDS:
        await bot.answer_callback_query(callback_query.id, "🚫 Доступ запрещен")
        return
    
    shift_id = int(callback_query.data.split('_')[-1])
    status = 'completed' if 'end' in callback_query.data else 'canceled'
    success = update_shift_status(shift_id, status)
    
    if success:
        await bot.send_message(callback_query.from_user.id, f"✅ Смена ID {shift_id} успешно {'завершена' if status == 'completed' else 'отменена'}")
    else:
        await bot.send_message(callback_query.from_user.id, f"❌ Не удалось обновить смену ID {shift_id}")

@dp.callback_query_handler(lambda c: c.data == 'timesheet')
async def process_timesheet(callback_query: types.CallbackQuery):
    if callback_query.from_user.id not in ADMIN_IDS:
        await bot.answer_callback_query(callback_query.id, "🚫 Доступ запрещен")
        return
    
    timesheet_data = [
        ["Берикулы Айсар СС", "TRUE", "TRUE", "TRUE", "TRUE", "TRUE", "FALSE", "TRUE", "TRUE", "TRUE", "TRUE", "TRUE", "TRUE", "TRUE", "TRUE", "TRUE", "", "", "210000", "TRUE", "TRUE", "TRUE", "TRUE", "FALSE", "TRUE", "TRUE", "TRUE", "TRUE", "TRUE", "FALSE", "FALSE", "FALSE", "FALSE", "FALSE", "FALSE", "", "", "135000"],
        ["Алимжан Дархан", "8", "8", "16", "7", "16", "15", "", "8", "8", "8", "8", "8", "16", "8", "", "", "", "167500", "8", "8", "8", "8", "", "8", "8", "8", "7", "", "", "", "", "", "", "", "", "", "78750"],
    ]
    
    message_text = "📊 Табель учета рабочего времени\n\n"
    for row in timesheet_data:
        message_text += " | ".join(str(item) for item in row) + "\n"
    
    await bot.send_message(callback_query.from_user.id, f"<pre>{message_text}</pre>", parse_mode=ParseMode.HTML)

@dp.message_handler(commands=['start', 'help'])
async def send_welcome(message: types.Message):
    await message.reply(
        "👋 Я бот для учета смен.\n"
        "Отправьте фото с подписью в формате:\n\n"
        "```\n"
        "Ербакыт Муратбек\n"
        "07:00 15:00\n"
        "Зона 12\n"
        "W witag 5\n"
        "```\n\n"
        "Команды:\n"
        "- /myshifts - Ваши смены\n"
        "- /today - Смены сегодня\n"
        "- /admin - Панель админа",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message_handler(content_types=['photo'])
async def handle_photo_with_caption(message: types.Message):
    user_id = message.from_user.id
    user_full_name = message.from_user.full_name

    if not message.caption:
        await message.reply("❌ Отправьте фото с подписью.")
        return

    shift_date = datetime.now(GROUP_TIMEZONE).strftime('%d.%m.%y')

    pattern = re.compile(
        r'^(?P<name>[\w\sА-Яа-я]+)\s+'
        r'(?P<start_time>\d{2}:\d{2})\s(?P<end_time>\d{2}:\d{2})\s+'
        r'(?P<zone>Зона\s+\d+)\s*'
        r'(?P<witag_val>W\s+witag\s+\d+)?$',
        re.MULTILINE | re.IGNORECASE
    )
    
    match = pattern.match(message.caption.strip())

    if not match:
        await message.reply(
            "❌ Неверный формат подписи.\n"
            "Пример:\n"
            "```\n"
            "Ербакыт Муратбек\n"
            "07:00 15:00\n"
            "Зона 12\n"
            "```",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    full_name = match.group('name').strip()
    start_time_str = match.group('start_time')
    end_time_str = match.group('end_time')
    zone = match.group('zone').strip()
    witag = match.group('witag_val').strip() if match.group('witag_val') else "Нет"

    if not is_valid_time(start_time_str) or not is_valid_time(end_time_str):
        await message.reply("❌ Неверный формат времени (ЧЧ:ММ).")
        return

    try:
        new_start_time = datetime.strptime(start_time_str, '%H:%M').time()
        new_end_time = datetime.strptime(end_time_str, '%H:%M').time()

        if new_start_time >= new_end_time:
            await message.reply("❌ Время начала должно быть раньше окончания.")
            return
    except Exception:
        await message.reply("❌ Ошибка разбора времени.")
        return

    existing_shifts = get_user_shifts_for_date_sqlite(user_id, shift_date)
    for existing_start_str, existing_end_str in existing_shifts:
        existing_start_time = datetime.strptime(existing_start_str, '%H:%M').time()
        existing_end_time = datetime.strptime(existing_end_str, '%H:%M').time()

        if (new_start_time < existing_end_time) and (new_end_time > existing_start_time):
            await message.reply(
                f"❌ Пересечение с существующей сменой "
                f"({existing_start_str}-{existing_end_str})"
            )
            return

    try:
        shift_id = add_shift_sqlite(user_id, full_name, message.photo[-1].file_id, shift_date, start_time_str, end_time_str, zone, witag)
        await message.reply(
            f"✅ **{full_name}** записан на смену.\n"
            f"📅 Дата: `{shift_date}`\n"
            f"⏰ Время: `{start_time_str}-{end_time_str}`\n"
            f"📍 Зона: `{zone}`\n"
            f"🔖 Witag: `{witag}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logging.error(f"Ошибка при добавлении смены: {e}")
        await message.reply("❗️ Ошибка. Попробуйте позже.")

@dp.message_handler(commands=['myshifts'])
async def get_my_shifts(message: types.Message):
    user_id = message.from_user.id
    shifts = get_user_shifts_sqlite(user_id)
    
    if not shifts:
        await message.reply("📄 У вас нет смен.", parse_mode=ParseMode.MARKDOWN)
        return

    shifts_by_date = defaultdict(list)
    for shift in shifts:
        shift_id, _, full_name, shift_date, start_time, end_time, _, _, _, zone, witag, status, _ = shift
        shift_type = (
            "☀️ Утро" if start_time == "07:00" and end_time == "15:00" else
            "🌙 Вечер" if start_time == "15:00" and end_time == "23:00" else
            "🗓️ Полный день" if start_time == "07:00" and end_time == "23:00" else
            "⏰ Другое"
        )
        shifts_by_date[shift_date].append({
            'shift_id': shift_id,
            'full_name': full_name,
            'start_time': start_time,
            'end_time': end_time,
            'zone': zone,
            'witag': witag,
            'status': status,
            'shift_type': shift_type
        })

    report_text = [f"**📋 Ваши смены, {message.from_user.full_name}**"]
    
    for shift_date in sorted(shifts_by_date.keys(), reverse=True):
        report_text.append(f"\n**📅 {shift_date}**")
        report_text.append("```")
        report_text.append("| ID | Тип смены       | Время        | Зона      | Статус   |")
        report_text.append("|----|-----------------|--------------|-----------|----------|")
        
        for shift in shifts_by_date[shift_date]:
            status = "✅ Активна" if shift['status'] == 'active' else "❌ Завершена" if shift['status'] == 'completed' else "🚫 Отменена"
            report_text.append(
                f"| {shift['shift_id']:<2} | {shift['shift_type']:<15} | {shift['start_time']}-{shift['end_time']} | {shift['zone']:<9} | {status:<8} |"
            )
        
        report_text.append("```")

    await message.reply("\n".join(report_text), parse_mode=ParseMode.MARKDOWN)

@dp.message_handler(commands=['today'])
async def get_today_shifts(message: types.Message):
    today_date = datetime.now(GROUP_TIMEZONE).strftime('%d.%m.%y')
    shifts = get_today_shifts_sqlite(today_date)
    
    if not shifts:
        await message.reply(f"📄 На {today_date} смен нет.", parse_mode=ParseMode.MARKDOWN)
        return

    report_text = [f"**📅 Смены за {today_date}**"]
    report_text.append("```")
    report_text.append("| Тип смены       | Имя              | Время        | Зона      | Статус   |")
    report_text.append("|-----------------|------------------|--------------|-----------|----------|")

    for shift in shifts:
        _, _, full_name, _, start_time, end_time, _, _, _, zone, _, status, _ = shift
        shift_type = (
            "☀️ Утро" if start_time == "07:00" and end_time == "15:00" else
            "🌙 Вечер" if start_time == "15:00" and end_time == "23:00" else
            "🗓️ Полный день" if start_time == "07:00" and end_time == "23:00" else
            "⏰ Другое"
        )
        status = "✅ Активна" if status == 'active' else "❌ Завершена" if status == 'completed' else "🚫 Отменена"
        report_text.append(
            f"| {shift_type:<15} | {full_name:<16} | {start_time}-{end_time} | {zone:<9} | {status:<8} |"
        )

    report_text.append("```")
    await message.reply("\n".join(report_text), parse_mode=ParseMode.MARKDOWN)

if __name__ == '__main__':
    init_db()
    try:
        worksheet, report_worksheet, timesheet_worksheet = init_google_sheets()
    except Exception as e:
        logging.error(f"Google Sheets init failed: {e}")
        worksheet, report_worksheet, timesheet_worksheet = None, None, None
    
    logging.info("Бот запущен...")
    executor.start_polling(dp, skip_updates=True)
