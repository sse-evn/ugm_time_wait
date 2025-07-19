import os
import re
import sqlite3
import logging
import fcntl
import sys
import asyncio
from datetime import datetime, time, timedelta
from typing import Dict, List
import pytz
import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ParseMode
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv()

required_vars = [
    'BOT_TOKEN',
    'ZONE_A_CHAT_ID',
    'ZONE_B_CHAT_ID',
    'REPORT_CHAT_ID',
    'ADMIN_IDS',
    'GOOGLE_SHEETS_CREDENTIALS_PATH',
    'GOOGLE_SHEETS_ID'
]

for var in required_vars:
    if not os.getenv(var):
        logging.error(f'❌ Отсутствует переменная: {var}')
        exit(1)

config = {
    'BOT_TOKEN': os.getenv('BOT_TOKEN'),
    'ZONE_A_CHAT_ID': int(os.getenv('ZONE_A_CHAT_ID')),
    'ZONE_B_CHAT_ID': int(os.getenv('ZONE_B_CHAT_ID')),
    'REPORT_CHAT_ID': int(os.getenv('REPORT_CHAT_ID')),
    'ADMIN_IDS': list(map(int, os.getenv('ADMIN_IDS').split(','))),
    'TIMEZONE': pytz.timezone(os.getenv('TIMEZONE', 'Asia/Almaty')),
    'CHECK_INTERVAL': int(os.getenv('CHECK_INTERVAL', '300')),
    'INACTIVITY_THRESHOLD': int(os.getenv('INACTIVITY_THRESHOLD', '1800')),
    'MORNING_SHIFT': (int(os.getenv('MORNING_SHIFT_START', '7')), int(os.getenv('MORNING_SHIFT_END', '15'))),
    'EVENING_SHIFT': (int(os.getenv('EVENING_SHIFT_START', '15')), int(os.getenv('EVENING_SHIFT_END', '23'))),
    'ZONE_NAMES': {
        'A': os.getenv('ZONE_A_NAME', 'Отчёты скаутов Е.О.М'),
        'B': os.getenv('ZONE_B_NAME', '10 аумақ-зона')
    },
    'GOOGLE_SHEETS_CREDENTIALS_PATH': os.getenv('GOOGLE_SHEETS_CREDENTIALS_PATH'),
    'GOOGLE_SHEETS_ID': os.getenv('GOOGLE_SHEETS_ID')
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)

def acquire_lock():
    lock_file = 'bot.lock'
    try:
        fd = os.open(lock_file, os.O_CREAT | os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (IOError, OSError):
        logger.error('❌ Бот уже запущен! Завершаю работу.')
        sys.exit(1)

def init_google_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    try:
        creds = Credentials.from_service_account_file(config['GOOGLE_SHEETS_CREDENTIALS_PATH'], scopes=scope)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(config['GOOGLE_SHEETS_ID'])
        logger.info("Успешно подключились к Google Sheets!")
        return spreadsheet.worksheet("Sheet1")
    except Exception as e:
        logger.error(f"Ошибка подключения к Google Sheets: {e}", exc_info=True)
        return None

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
            zone TEXT,
            witag TEXT,
            created_at TIMESTAMP,
            last_activity TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def add_shift_sqlite(user_id, full_name, photo_id, s_date, s_time, e_time, zone, witag):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    current_time = datetime.now(config['TIMEZONE'])
    cur.execute('''
        INSERT INTO shifts (user_id, full_name, photo_file_id, shift_date, start_time, end_time, zone, witag, created_at, last_activity)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, full_name, photo_id, s_date, s_time, e_time, zone, witag, current_time, current_time))
    conn.commit()
    conn.close()

def update_user_activity(user_id):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    current_time = datetime.now(config['TIMEZONE'])
    cur.execute('''
        UPDATE shifts 
        SET last_activity = ?
        WHERE user_id = ? AND shift_date = ?
    ''', (current_time, user_id, current_time.strftime('%d.%m.%y')))
    conn.commit()
    conn.close()

def get_active_users():
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    current_time = datetime.now(config['TIMEZONE'])
    threshold = current_time - timedelta(seconds=config['INACTIVITY_THRESHOLD'])
    
    cur.execute('''
        SELECT user_id, full_name, zone, last_activity 
        FROM shifts 
        WHERE shift_date = ? AND last_activity > ?
    ''', (current_time.strftime('%d.%m.%y'), threshold))
    
    active_users = cur.fetchall()
    conn.close()
    return active_users

def get_inactive_users():
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    current_time = datetime.now(config['TIMEZONE'])
    threshold = current_time - timedelta(seconds=config['INACTIVITY_THRESHOLD'])
    
    cur.execute('''
        SELECT user_id, full_name, zone, last_activity 
        FROM shifts 
        WHERE shift_date = ? AND last_activity <= ?
    ''', (current_time.strftime('%d.%m.%y'), threshold))
    
    inactive_users = cur.fetchall()
    conn.close()
    return inactive_users

try:
    bot = Bot(token=config['BOT_TOKEN'])
    storage = MemoryStorage()
    dp = Dispatcher(bot, storage=storage)
    logger.info("🤖 Бот инициализирован")
except Exception as e:
    logger.error(f"❌ Ошибка инициализации бота: {e}")
    exit(1)

worksheet = init_google_sheets()
init_db()

def get_current_shift() -> str:
    now = datetime.now(config['TIMEZONE']).hour
    if config['MORNING_SHIFT'][0] <= now < config['MORNING_SHIFT'][1]:
        return 'morning'
    elif config['EVENING_SHIFT'][0] <= now < config['EVENING_SHIFT'][1]:
        return 'evening'
    return None

def is_valid_time(time_str, fmt='%H:%M'):
    try:
        datetime.strptime(time_str, fmt).time()
        return True
    except ValueError:
        return False

@dp.message_handler(commands=['start', 'help'])
async def send_welcome(message: types.Message):
    await message.reply(
        "👋 Я бот для учета смен и мониторинга активности.\n\n"
        "📌 Чтобы записаться на смену, отправьте фото с подписью в формате:\n"
        "```\n"
        "Имя Фамилия\n"
        "07:00 15:00\n"
        "Зона 12\n"
        "W witag 5 (необязательно)\n"
        "```\n\n"
        "📊 Администраторы могут использовать команды:\n"
        "/report - отчет по сменам\n"
        "/activity - проверка активности",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message_handler(content_types=['photo'])
async def handle_photo_with_caption(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    if chat_id not in [config['ZONE_A_CHAT_ID'], config['ZONE_B_CHAT_ID']]:
        return

    user_full_name = message.from_user.full_name
    logger.info(f"Получено фото от {user_full_name} (ID: {user_id}).")

    if not message.caption:
        await message.reply("❌ Отправьте фото с подписью.")
        return

    shift_date = datetime.now(config['TIMEZONE']).strftime('%d.%m.%y')

    pattern = re.compile(
        r'^(?P<name>[\w\sА-Яа-я]+)\s+'
        r'(?P<start_time>\d{2}:\d{2})\s(?P<end_time>\d{2}:\d{2})\s+'
        r'(?P<zone>Зона\s+\d+)\s*'
        r'(?P<witag_val>W\s+witag\s+\d+)?$',
        re.MULTILINE | re.IGNORECASE
    )
    
    cleaned_caption = message.caption.strip()
    match = pattern.match(cleaned_caption)

    if not match:
        await message.reply(
            "❌ Неверный формат подписи. Пример:\n"
            "```\n"
            "Имя Фамилия\n"
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
        await message.reply("❌ Ошибка разбора времени. Проверьте формат ЧЧ:ММ.")
        return

    photo_file_id = message.photo[-1].file_id
    current_time = datetime.now(config['TIMEZONE'])
    
    try:
        add_shift_sqlite(user_id, full_name, photo_file_id, shift_date, start_time_str, end_time_str, zone, witag)
        
        if worksheet:
            add_shift_gsheets(worksheet, user_id, full_name, photo_file_id, shift_date, 
                             start_time_str, end_time_str, zone, witag, current_time.isoformat())
        
        logger.info(f"Смена для {full_name} на {shift_date} ({start_time_str}-{end_time_str}) успешно добавлена.")
        await message.reply(
            f"✅ **{full_name}** записан на смену.\n"
            f"Дата: `{shift_date}`\n"
            f"Время: `{start_time_str}-{end_time_str}`\n"
            f"Зона: `{zone}`\n"
            f"Witag: `{witag}`",
            parse_mode=ParseMode.MARKDOWN
        )
        
        update_user_activity(user_id)
        
    except Exception as e:
        logger.error(f"Ошибка при добавлении смены: {e}", exc_info=True)
        await message.reply("❗️ Внутренняя ошибка. Попробуйте позже.")

async def check_activity():
    while True:
        current_shift = get_current_shift()
        if not current_shift:
            await asyncio.sleep(config['CHECK_INTERVAL'])
            continue

        inactive_users = get_inactive_users()
        if inactive_users:
            message = f"⚠️ <b>Неактивные пользователи ({current_shift} смена)</b>\n\n"
            
            for user_id, full_name, zone, last_activity in inactive_users:
                last_time = last_activity.strftime('%H:%M:%S') if last_activity else "никогда"
                message += f"• {full_name} ({zone}) - последняя активность: {last_time}\n"
            
            try:
                await bot.send_message(config['REPORT_CHAT_ID'], message, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения: {e}")

        await asyncio.sleep(config['CHECK_INTERVAL'])

@dp.message_handler(commands=['activity'])
async def check_activity_command(message: types.Message):
    if message.from_user.id not in config['ADMIN_IDS']:
        await message.reply("🚫 Команда только для администраторов.")
        return

    inactive_users = get_inactive_users()
    if not inactive_users:
        await message.reply("✅ Все сотрудники активны!")
        return

    response = "⚠️ <b>Неактивные сотрудники:</b>\n\n"
    for user_id, full_name, zone, last_activity in inactive_users:
        last_time = last_activity.strftime('%H:%M:%S') if last_activity else "никогда"
        response += f"• {full_name} ({zone}) - последняя активность: {last_time}\n"
    
    await message.reply(response, parse_mode='HTML')

@dp.message_handler(commands=['report'])
async def get_report(message: types.Message):
    if message.from_user.id not in config['ADMIN_IDS']:
        await message.reply("🚫 Команда только для администраторов.")
        return

    today_date_str = datetime.now(config['TIMEZONE']).strftime('%d.%m.%y')
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT full_name, start_time, end_time, zone, witag FROM shifts WHERE shift_date = ?", (today_date_str,))
    shifts = cur.fetchall()
    conn.close()

    if not shifts:
        await message.reply(f"📄 На **{today_date_str}** смен не найдено.", parse_mode=ParseMode.MARKDOWN)
        return

    morning_shift = []
    evening_shift = []
    full_day_shift = []

    for name, start, end, zone, witag in shifts:
        shift_info = f"  - `{name}` ({zone}, Witag: {witag})"
        if start == "07:00" and end == "15:00":
            morning_shift.append(shift_info)
        elif start == "15:00" and end == "23:00":
            evening_shift.append(shift_info)
        elif start == "07:00" and end == "23:00":
            full_day_shift.append(shift_info)

    total = len(morning_shift) + len(evening_shift) + len(full_day_shift)

    report = [
        f"**📊 Отчет на {today_date_str}**",
        f"**Общее количество: {total}**\n",
        f"**☀️ Утренняя смена (07:00-15:00): {len(morning_shift)}**"
    ] + morning_shift + [
        "\n**🌙 Вечерняя смена (15:00-23:00): {len(evening_shift)}**"
    ] + evening_shift + [
        "\n**🌞 Полный день (07:00-23:00): {len(full_day_shift)}**"
    ] + full_day_shift

    await message.reply("\n".join(report), parse_mode=ParseMode.MARKDOWN)

async def on_startup(_):
    asyncio.create_task(check_activity())
    try:
        await bot.send_message(config['ADMIN_IDS'][0], '🤖 Бот запущен и начал мониторинг')
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")

if __name__ == '__main__':
    lock_fd = acquire_lock()
    try:
        executor.start_polling(
            dp,
            on_startup=on_startup,
            skip_updates=True
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        try:
            os.remove('bot.lock')
        except:
            pass
        logger.info('Бот завершил работу')
