import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta
import pytz
import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ParseMode
from dotenv import load_dotenv
from collections import defaultdict
from gspread_formatting import *

# --- Конфигурация ---
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    logging.error("BOT_TOKEN is not set in environment variables.")
    raise ValueError("BOT_TOKEN is not set")
logging.info(f"Loaded BOT_TOKEN: {API_TOKEN[:10]}...")
GROUP_TIMEZONE = pytz.timezone('Asia/Almaty')  # UTC+5
GOOGLE_SHEETS_ID = "1QWCYpeBQGofESEkD4WWYAIl0fvVDt7VZvWOE-qKe_RE"  # ID таблицы

# --- ID администраторов из .env ---
ADMIN_IDS_STR = os.getenv("ADMIN_IDS")
if ADMIN_IDS_STR:
    try:
        ADMIN_IDS = {int(uid.strip()) for uid in ADMIN_IDS_STR.split(',')}
    except ValueError:
        logging.error("Ошибка парсинга ADMIN_IDS. Проверьте .env: список чисел через запятую.")
        ADMIN_IDS = set()
else:
    ADMIN_IDS = set()

if not ADMIN_IDS:
    logging.warning("ADMIN_IDS не настроены или содержат ошибки. Команда /report будет недоступна.")

# --- Настройка логирования ---
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# --- Инициализация Google Sheets ---
def init_google_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    try:
        creds = Credentials.from_service_account_file(os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH"), scopes=scope)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEETS_ID)
        logging.info("Успешно подключились к Google Sheets!")
        
        try:
            report_worksheet = spreadsheet.worksheet("Report")
        except gspread.exceptions.WorksheetNotFound:
            report_worksheet = spreadsheet.add_worksheet(title="Report", rows=100, cols=10)
            report_worksheet.update('A1:E1', [['Дата', 'Имя', 'Время', 'Зона', 'Witag']])
            set_row_height(report_worksheet, '1', 40)
            set_column_width(report_worksheet, 'A:E', 120)
            fmt = CellFormat(
                backgroundColor=Color(0.9, 0.9, 0.9),
                textFormat=TextFormat(bold=True),
                horizontalAlignment='CENTER'
            )
            format_cell_range(report_worksheet, 'A1:E1', fmt)
        
        return spreadsheet.worksheet("Sheet1"), report_worksheet
    except Exception as e:
        logging.error(f"Ошибка подключения к Google Sheets: {e}", exc_info=True)
        raise

# --- Инициализация SQLite ---
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
            created_at TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# --- Добавление смены в SQLite ---
def add_shift_sqlite(user_id, full_name, photo_id, s_date, s_time, e_time, zone, witag):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    current_time_utc5 = datetime.now(GROUP_TIMEZONE)
    cur.execute('''
        INSERT INTO shifts (user_id, full_name, photo_file_id, shift_date, start_time, end_time, actual_end_time, worked_hours, zone, witag, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, full_name, photo_id, s_date, s_time, e_time, None, None, zone, witag, current_time_utc5))
    conn.commit()
    conn.close()
    return cur.lastrowid

# --- Удаление смены из SQLite ---
def delete_shift_sqlite(shift_id):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("DELETE FROM shifts WHERE id = ?", (shift_id,))
    conn.commit()
    affected_rows = cur.rowcount
    conn.close()
    return affected_rows > 0

# --- Добавление смены в Google Sheets ---
def add_shift_gsheets(worksheet, report_worksheet, user_id, full_name, photo_id, s_date, s_time, e_time, zone, witag, created_at):
    try:
        rows = worksheet.get_all_values()
        next_id = len(rows)  # ID = количество строк (заголовок + данные)
        worksheet.append_row([
            next_id, user_id, full_name, photo_id, s_date, s_time, e_time, None, None, zone, witag, created_at
        ])
        logging.info(f"Смена для {full_name} добавлена в Google Sheets (Sheet1).")
        update_report_worksheet(report_worksheet)
    except Exception as e:
        logging.error(f"Ошибка при добавлении в Google Sheets: {e}", exc_info=True)

# --- Удаление смены из Google Sheets ---
def delete_shift_gsheets(worksheet, report_worksheet, shift_id):
    try:
        rows = worksheet.get_all_values()
        for i, row in enumerate(rows[1:], start=2):  # Пропускаем заголовок
            if int(row[0]) == shift_id:
                worksheet.delete_rows(i)
                logging.info(f"Смена с ID {shift_id} удалена из Google Sheets.")
                update_report_worksheet(report_worksheet)
                return True
        return False
    except Exception as e:
        logging.error(f"Ошибка при удалении смены из Google Sheets: {e}", exc_info=True)
        return False

# --- Обновление листа Report ---
def update_report_worksheet(report_worksheet):
    try:
        shifts = get_all_shifts_gsheets(report_worksheet.spreadsheet.worksheet("Sheet1"))
        if not shifts:
            return
        
        report_worksheet.clear()
        report_worksheet.update('A1:E1', [['Дата', 'Имя', 'Время', 'Зона', 'Witag']])
        set_row_height(report_worksheet, '1', 40)
        set_column_width(report_worksheet, 'A:E', 120)
        fmt = CellFormat(
            backgroundColor=Color(0.9, 0.9, 0.9),
            textFormat=TextFormat(bold=True),
            horizontalAlignment='CENTER'
        )
        format_cell_range(report_worksheet, 'A1:E1', fmt)
        
        row_index = 2
        for shift in shifts:
            report_worksheet.append_row([shift[4], shift[2], f"{shift[5]}-{shift[6] or shift[7] or ''}", shift[9], shift[10]])
        logging.info("Лист Report обновлен.")
    except Exception as e:
        logging.error(f"Ошибка при обновлении листа Report: {e}", exc_info=True)

# --- Получение всех смен из SQLite ---
def get_all_shifts_sqlite():
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT user_id, full_name, shift_date, start_time, end_time, actual_end_time, worked_hours, zone, witag, created_at FROM shifts")
    rows = cur.fetchall()
    conn.close()
    return rows

# --- Получение смен пользователя из SQLite ---
def get_user_shifts_sqlite(user_id):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT id, full_name, shift_date, start_time, end_time, actual_end_time, worked_hours, zone, witag, created_at FROM shifts WHERE user_id = ?", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

# --- Получение всех смен из Google Sheets ---
def get_all_shifts_gsheets(worksheet):
    try:
        rows = worksheet.get_all_values()[1:]  # Пропускаем заголовок
        shifts = []
        for row in rows:
            shifts.append((int(row[1]), row[2], row[4], row[5], row[6], row[7], row[8], row[9], row[10], row[11]))  # user_id, full_name, shift_date, start_time, end_time, actual_end_time, worked_hours, zone, witag, created_at
        return shifts
    except Exception as e:
        logging.error(f"Ошибка при получении данных из Google Sheets: {e}", exc_info=True)
        return []

# --- Получение смен пользователя из Google Sheets ---
def get_user_shifts_gsheets(worksheet, user_id):
    try:
        rows = worksheet.get_all_values()[1:]  # Пропускаем заголовок
        shifts = []
        for row in rows:
            if int(row[1]) == user_id:
                shifts.append((int(row[0]), row[2], row[4], row[5], row[6], row[7], row[8], row[9], row[10], row[11]))  # id, full_name, shift_date, start_time, end_time, actual_end_time, worked_hours, zone, witag, created_at
        return shifts
    except Exception as e:
        logging.error(f"Ошибка при получении данных пользователя из Google Sheets: {e}", exc_info=True)
        return []

# --- Получение смен за сегодня из SQLite ---
def get_today_shifts_sqlite(today_date):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT user_id, full_name, shift_date, start_time, end_time, actual_end_time, worked_hours, zone, witag, created_at FROM shifts WHERE shift_date = ?", (today_date,))
    rows = cur.fetchall()
    conn.close()
    return rows

# --- Получение смен за сегодня из Google Sheets ---
def get_today_shifts_gsheets(worksheet, today_date):
    try:
        rows = worksheet.get_all_values()[1:]  # Пропускаем заголовок
        shifts = []
        for row in rows:
            if row[4] == today_date:
                shifts.append((int(row[1]), row[2], row[4], row[5], row[6], row[7], row[8], row[9], row[10], row[11]))  # user_id, full_name, shift_date, start_time, end_time, actual_end_time, worked_hours, zone, witag, created_at
        return shifts
    except Exception as e:
        logging.error(f"Ошибка при получении данных за сегодня из Google Sheets: {e}", exc_info=True)
        return []

# --- Проверка на пересечение смен в SQLite ---
def get_user_shifts_for_date_sqlite(user_id, shift_date):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT start_time, end_time FROM shifts WHERE user_id = ? AND shift_date = ?", (user_id, shift_date))
    rows = cur.fetchall()
    conn.close()
    return rows

# --- Проверка на пересечение смен в Google Sheets ---
def get_user_shifts_for_date_gsheets(worksheet, user_id, shift_date):
    try:
        rows = worksheet.get_all_values()[1:]  # Пропускаем заголовок
        shifts = []
        for row in rows:
            if int(row[1]) == user_id and row[4] == shift_date:
                shifts.append((row[5], row[6]))  # start_time, end_time
        return shifts
    except Exception as e:
        logging.error(f"Ошибка при проверке пересечений в Google Sheets: {e}", exc_info=True)
        return []

# --- Вспомогательные функции для валидации ---
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

# --- Инициализация бота и диспетчера ---
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)
try:
    worksheet, report_worksheet = init_google_sheets()  # Инициализация Google Sheets
except Exception as e:
    logging.error(f"Не удалось инициализировать Google Sheets: {e}")
    worksheet, report_worksheet = None, None  # Продолжаем работать с SQLite

# --- Обработчики команд и сообщений ---

@dp.message_handler(commands=['start', 'help'])
async def send_welcome(message: types.Message):
    """Отправляет приветственное сообщение и инструкции."""
    await message.reply(
        "👋 Я бот для учета смен.\n"
        "Отправьте фото с подписью **построчно** в формате:\n\n"
        "```\n"
        "Ербакыт Муратбек\n"
        "07:00 15:00\n"
        "Зона 12\n"
        "W witag 5 (необязательно)\n"
        "```\n\n"
        "**Каждая строка — это Enter!** Дата ставится автоматически.\n"
        "Можно указать смену на весь день: `07:00 23:00`\n\n"
        "Команды:\n"
        "- /myshifts - Посмотреть свои смены\n"
        "- /today - Смены за сегодня\n"
        "- /report - Полный отчет (админы)\n"
        "- /delete_shift [ID] - Удалить смену (админы)\n"
        "- /stats - Статистика по сменам (админы)\n"
        "- /admin_panel - Панель админа для редактирования (админы)",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message_handler(content_types=['photo'])
async def handle_photo_with_caption(message: types.Message):
    """
    Обрабатывает получение фотографии с подписью, парсит данные
    и сохраняет их в SQLite и Google Sheets.
    """
    user_id = message.from_user.id
    user_full_name = message.from_user.full_name
    logging.info(f"Получено фото от {user_full_name} (ID: {user_id}).")

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
    
    cleaned_caption = message.caption.strip()
    match = pattern.match(cleaned_caption)

    if not match:
        logging.warning(f"Неверный формат подписи от {user_full_name}: '{message.caption}'")
        await message.reply(
            "❌ Неверный формат подписи. **Каждая строка — это Enter!**\n"
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

    photo_file_id = message.photo[-1].file_id

    # --- Валидация времени ---
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

    # --- Проверка на пересечение смен ---
    existing_shifts_sqlite = get_user_shifts_for_date_sqlite(user_id, shift_date)
    existing_shifts_gsheets = get_user_shifts_for_date_gsheets(worksheet, user_id, shift_date) if worksheet else []
    existing_shifts = existing_shifts_sqlite + existing_shifts_gsheets

    for existing_start_str, existing_end_str in existing_shifts:
        existing_start_time = datetime.strptime(existing_start_str, '%H:%M').time()
        existing_end_time = datetime.strptime(existing_end_str, '%H:%M').time()

        if (new_start_time < existing_end_time) and (new_end_time > existing_start_time):
            await message.reply(
                f"❌ Вы уже записаны на смену, которая пересекается с этим временем "
                f"({existing_start_str}-{existing_end_str}) на сегодня."
            )
            logging.info(f"Пользователь {user_full_name} (ID: {user_id}) пытался добавить пересекающуюся смену.")
            return

    # --- Добавление смены ---
    try:
        shift_id = add_shift_sqlite(user_id, full_name, photo_file_id, shift_date, start_time_str, end_time_str, None, None, zone, witag)
        if worksheet and report_worksheet:
            current_time_utc5 = datetime.now(GROUP_TIMEZONE).isoformat()
            add_shift_gsheets(worksheet, report_worksheet, user_id, full_name, photo_file_id, shift_date, start_time_str, end_time_str, None, None, zone, witag, current_time_utc5)
        logging.info(f"Смена для {full_name} на {shift_date} ({start_time_str}-{end_time_str}) успешно добавлена.")
        await message.reply(
            f"✅ **{full_name}** записан на смену.\n"
            f"📅 Дата: `{shift_date}`\n"
            f"⏰ Время: `{start_time_str}-{end_time_str}`\n"
            f"📍 Зона: `{zone}`\n"
            f"🔖 Witag: `{witag}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logging.error(f"Ошибка при добавлении смены для {full_name}: {e}", exc_info=True)
        await message.reply("❗️ Внутренняя ошибка. Попробуйте позже.")

@dp.message_handler(commands=['myshifts'])
async def get_my_shifts(message: types.Message):
    """Показывает смены пользователя, сгруппированные по датам."""
    user_id = message.from_user.id
    logging.info(f"ID {user_id} запросил свои смены.")

    shifts = get_user_shifts_gsheets(worksheet, user_id) if worksheet else get_user_shifts_sqlite(user_id)
    if not shifts:
        await message.reply("📄 У вас нет записанных смен.", parse_mode=ParseMode.MARKDOWN)
        return

    shifts_by_date = defaultdict(list)
    for shift in shifts:
        shift_id, full_name, shift_date, start_time, end_time, actual_end_time, worked_hours, zone, witag, created_at = shift
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
            'actual_end_time': actual_end_time,
            'worked_hours': worked_hours,
            'zone': zone,
            'witag': witag,
            'created_at': created_at,
            'shift_type': shift_type
        })

    report_text = [f"**📋 Ваши смены, {message.from_user.full_name}**"]
    
    for shift_date in sorted(shifts_by_date.keys(), reverse=True):
        report_text.append(f"\n**📅 {shift_date}**")
        report_text.append("```")
        report_text.append("| ID | Тип смены       | Время        | Работал     | Зона      | Witag |")
        report_text.append("|----|-----------------|--------------|-------------|-----------|-------|")
        
        for shift in sorted(shifts_by_date[shift_date], key=lambda x: datetime.fromisoformat(x['created_at']) if worksheet else x['created_at']):
            worked = shift['worked_hours'] if shift['worked_hours'] else f"{shift['start_time']}-{shift['end_time']}"
            report_text.append(
                f"| {shift['shift_id']:<2} | {shift['shift_type']:<15} | {worked} | {shift['zone']:<9} | {shift['witag']:<5} |"
            )
        
        report_text.append("```")
        report_text.append(f"**Всего смен: {len(shifts_by_date[shift_date])}**")

    report_text.append(f"\n**Общее количество ваших смен: {len(shifts)}**")
    await message.reply("\n".join(report_text), parse_mode=ParseMode.MARKDOWN)

@dp.message_handler(commands=['delete_shift'])
async def delete_shift(message: types.Message):
    """Удаляет смену по ID (только для админов)."""
    user_id = message.from_user.id
    
    if user_id not in ADMIN_IDS:
        logging.warning(f"ID {user_id} пытался использовать /delete_shift.")
        await message.reply("🚫 Команда только для авторизованных админов.")
        return

    args = message.get_args()
    if not args or not args.isdigit():
        await message.reply("❌ Укажите ID смены: /delete_shift [ID]", parse_mode=ParseMode.MARKDOWN)
        return

    shift_id = int(args)
    logging.info(f"ID {user_id} запросил удаление смены с ID {shift_id}.")

    sqlite_success = delete_shift_sqlite(shift_id)
    gsheets_success = delete_shift_gsheets(worksheet, report_worksheet, shift_id) if worksheet and report_worksheet else False

    if sqlite_success or gsheets_success:
        await message.reply(f"✅ Смена с ID {shift_id} успешно удалена.", parse_mode=ParseMode.MARKDOWN)
    else:
        await message.reply(f"❌ Смена с ID {shift_id} не найдена.", parse_mode=ParseMode.MARKDOWN)

@dp.message_handler(commands=['today'])
async def get_today_shifts(message: types.Message):
    """Показывает смены за текущий день."""
    today_date = datetime.now(GROUP_TIMEZONE).strftime('%d.%m.%y')
    logging.info(f"ID {message.from_user.id} запросил смены за {today_date}.")

    shifts = get_today_shifts_gsheets(worksheet, today_date) if worksheet else get_today_shifts_sqlite(today_date)
    if not shifts:
        await message.reply(f"📄 На **{today_date}** смен не найдено.", parse_mode=ParseMode.MARKDOWN)
        return

    report_text = [f"**📅 Смены за {today_date}**"]
    report_text.append("```")
    report_text.append("| Тип смены       | Имя              | Время        | Работал     | Зона      | Witag |")
    report_text.append("|-----------------|------------------|--------------|-------------|-----------|-------|")

    for shift in sorted(shifts, key=lambda x: datetime.fromisoformat(x[9]) if worksheet else x[9]):
        user_id, full_name, shift_date, start_time, end_time, actual_end_time, worked_hours, zone, witag, created_at = shift
        shift_type = (
            "☀️ Утро" if start_time == "07:00" and end_time == "15:00" else
            "🌙 Вечер" if start_time == "15:00" and end_time == "23:00" else
            "🗓️ Полный день" if start_time == "07:00" and end_time == "23:00" else
            "⏰ Другое"
        )
        worked = worked_hours if worked_hours else f"{start_time}-{end_time}"
        report_text.append(
            f"| {shift_type:<15} | {full_name:<16} | {worked} | {zone:<9} | {witag:<5} |"
        )

    report_text.append("```")
    report_text.append(f"**Всего смен: {len(shifts)}**")
    await message.reply("\n".join(report_text), parse_mode=ParseMode.MARKDOWN)

@dp.message_handler(commands=['stats'])
async def get_stats(message: types.Message):
    """Показывает статистику по сменам (только для админов)."""
    user_id = message.from_user.id
    
    if user_id not in ADMIN_IDS:
        logging.warning(f"ID {user_id} пытался использовать /stats.")
        await message.reply("🚫 Команда только для авторизованных админов.")
        return

    logging.info(f"ID {user_id} запросил статистику.")

    shifts = get_all_shifts_gsheets(worksheet) if worksheet else get_all_shifts_sqlite()
    if not shifts:
        await message.reply("📄 Смены не найдены.", parse_mode=ParseMode.MARKDOWN)
        return

    shift_counts = defaultdict(int)
    zone_counts = defaultdict(int)
    for shift in shifts:
        shift_counts[shift[1]] += 1  # full_name
        zone_counts[shift[7]] += 1   # zone

    report_text = ["**📈 Статистика по сменам**"]
    
    report_text.append("\n**👷 Сотрудники**")
    report_text.append("```")
    report_text.append("| Имя              | Кол-во смен |")
    report_text.append("|------------------|-------------|")
    for name, count in sorted(shift_counts.items()):
        report_text.append(f"| {name:<16} | {count:<11} |")
    report_text.append("```")

    report_text.append("\n**📍 Зоны**")
    report_text.append("```")
    report_text.append("| Зона      | Кол-во смен |")
    report_text.append("|-----------|-------------|")
    for zone, count in sorted(zone_counts.items()):
        report_text.append(f"| {zone:<9} | {count:<11} |")
    report_text.append("```")

    report_text.append(f"\n**Общее количество смен: {len(shifts)}**")
    await message.reply("\n".join(report_text), parse_mode=ParseMode.MARKDOWN)

@dp.message_handler(commands=['report'])
async def get_report(message: types.Message):
    """Отчет по сменам для админов в табличном формате с колонками по сотрудникам."""
    user_id = message.from_user.id
    
    if user_id not in ADMIN_IDS:
        logging.warning(f"ID {user_id} пытался использовать /report.")
        await message.reply("🚫 Команда только для авторизованных админов.")
        return

    logging.info(f"ID {user_id} запросил отчет.")

    shifts = get_all_shifts_gsheets(worksheet) if worksheet else get_all_shifts_sqlite()
    if not shifts:
        await message.reply("📄 Смены не найдены.", parse_mode=ParseMode.MARKDOWN)
        return

    shifts_by_user = defaultdict(list)
    for shift in shifts:
        user_id, full_name, shift_date, start_time, end_time, actual_end_time, worked_hours, zone, witag, created_at = shift
        shifts_by_user[full_name].append({
            'shift_date': shift_date,
            'start_time': start_time,
            'end_time': end_time,
            'actual_end_time': actual_end_time,
            'worked_hours': worked_hours,
            'zone': zone,
            'witag': witag,
            'created_at': created_at
        })

    unique_dates = sorted(set(shift['shift_date'] for user_shifts in shifts_by_user.values() for shift in user_shifts), reverse=True)
    
    report_text = ["**📊 Отчет по сменам**"]
    report_text.append("```")
    
    headers = ["Дата"] + [name for name in shifts_by_user.keys()]
    report_text.append("| " + " | ".join(headers) + " |")
    report_text.append("| " + " | ".join(["-" * 15] * (len(headers))) + " |")

    for date in unique_dates:
        row = [date]
        for name in headers[1:]:
            user_shifts = [s for s in shifts_by_user[name] if s['shift_date'] == date]
            if user_shifts:
                times = []
                worked = []
                for shift in user_shifts:
                    time_range = f"{shift['start_time']}-{shift['actual_end_time'] or shift['end_time']}"
                    worked_hours = shift['worked_hours'] if shift['worked_hours'] else ""
                    times.append(time_range)
                    if worked_hours:
                        worked.append(f"Worked: {worked_hours}")
                sequence = " ".join(times[:7] + [""] * (7 - len(times))) if times else ""
                worked_seq = " ".join(worked[:7] + [""] * (7 - len(worked))) if worked else ""
                row.append(f"{sequence} ({worked_seq})")
            else:
                row.append("")
        report_text.append("| " + " | ".join(row) + " |")

    report_text.append("```")
    report_text.append(f"\n**Общее количество смен: {len(shifts)}**")
    await message.reply("\n".join(report_text), parse_mode=ParseMode.MARKDOWN)

@dp.message_handler(commands=['admin_panel'])
async def admin_panel(message: types.Message):
    """Панель админа для управления сменами."""
    user_id = message.from_user.id
    
    if user_id not in ADMIN_IDS:
        logging.warning(f"ID {user_id} пытался использовать /admin_panel.")
        await message.reply("🚫 Команда только для авторизованных админов.")
        return

    logging.info(f"ID {user_id} открыл админ-панель.")

    shifts = get_all_shifts_gsheets(worksheet) if worksheet else get_all_shifts_sqlite()
    if not shifts:
        await message.reply("📄 Смены не найдены.", parse_mode=ParseMode.MARKDOWN)
        return

    report_text = ["**📋 Админ-панель**"]
    report_text.append("Список смен для редактирования:")
    report_text.append("```")
    report_text.append("| ID | Имя          | Дата   | Время       | Зона   | Действие       |")
    report_text.append("|----|--------------|--------|-------------|--------|----------------|")
    
    for shift in shifts:
        shift_id, full_name, shift_date, start_time, end_time, actual_end_time, worked_hours, zone, witag, created_at = shift
        time_display = f"{start_time}-{actual_end_time or end_time}"
        report_text.append(f"| {shift_id:<2} | {full_name[:12]:<12} | {shift_date} | {time_display} | {zone} | /edit_{shift_id} |")
    
    report_text.append("```")
    report_text.append("Команды:\n- /edit_[ID] - Редактировать смену (например, /edit_1)\n- Укажите 'Home HH:MM' (например, 'Home 18:23') для отправки домой с указанием времени.")
    await message.reply("\n".join(report_text), parse_mode=ParseMode.MARKDOWN)

@dp.message_handler(lambda message: message.text.startswith('/edit_'))
async def edit_shift_with_state(message: types.Message):
    """Редактирование смены админом с установкой состояния."""
    user_id = message.from_user.id
    
    if user_id not in ADMIN_IDS:
        logging.warning(f"ID {user_id} пытался использовать /edit_.")
        await message.reply("🚫 Команда только для авторизованных админов.")
        return

    try:
        shift_id = int(message.text.split('_')[1])
        message.expected_shift_id = shift_id  # Устанавливаем состояние
        logging.info(f"ID {user_id} запросил редактирование смены с ID {shift_id}.")
        await message.reply("📝 Введите новое время (ЧЧ:ММ-ЧЧ:ММ) или 'Home HH:MM' (например, 'Home 18:23') для отправки домой:", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await message.reply("❌ Неверный ID смены.", parse_mode=ParseMode.MARKDOWN)

@dp.message_handler(content_types=['text'], state=None)
async def process_edit_input(message: types.Message):
    user_id = message.from_user.id
    
    if user_id not in ADMIN_IDS or not hasattr(message, 'expected_shift_id'):
        return

    shift_id = message.expected_shift_id
    text = message.text.strip()
    logging.info(f"ID {user_id} вводит данные для редактирования смены {shift_id}.")

    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT start_time, end_time FROM shifts WHERE id = ?", (shift_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        await message.reply(f"❌ Смена с ID {shift_id} не найдена.", parse_mode=ParseMode.MARKDOWN)
        del message.expected_shift_id
        return

    start_time, end_time = row

    if text.startswith("Home") and len(text.split()) == 2:
        actual_end_time = text.split()[1]
        if is_valid_time(actual_end_time):
            worked_hours = calculate_worked_hours(start_time, actual_end_time)
            conn = sqlite3.connect('shifts.db')
            cur = conn.cursor()
            cur.execute("UPDATE shifts SET actual_end_time = ?, worked_hours = ? WHERE id = ?", (actual_end_time, worked_hours, shift_id))
            conn.commit()
            conn.close()
            if worksheet:
                rows = worksheet.get_all_values()[1:]
                for i, row in enumerate(rows, start=2):
                    if int(row[0]) == shift_id:
                        worksheet.update_cell(i, 7, actual_end_time)  # actual_end_time
                        worksheet.update_cell(i, 8, worked_hours)     # worked_hours
                        break
                update_report_worksheet(report_worksheet)
            await message.reply(f"✅ Смена с ID {shift_id} завершена в {actual_end_time}. Работал: {worked_hours}.", parse_mode=ParseMode.MARKDOWN)
        else:
            await message.reply("❌ Неверный формат времени для 'Home'. Используйте 'Home HH:MM'.", parse_mode=ParseMode.MARKDOWN)
    elif is_valid_time(text.split('-')[0]) and is_valid_time(text.split('-')[1]):
        new_start, new_end = text.split('-')
        worked_hours = calculate_worked_hours(new_start, new_end)
        conn = sqlite3.connect('shifts.db')
        cur = conn.cursor()
        cur.execute("UPDATE shifts SET start_time = ?, end_time = ?, actual_end_time = ?, worked_hours = ? WHERE id = ?", (new_start, new_end, None, None, shift_id))
        conn.commit()
        conn.close()
        if worksheet:
            rows = worksheet.get_all_values()[1:]
            for i, row in enumerate(rows, start=2):
                if int(row[0]) == shift_id:
                    worksheet.update_cell(i, 6, new_start)  # start_time
                    worksheet.update_cell(i, 7, new_end)    # end_time
                    worksheet.update_cell(i, 8, "")         # clear actual_end_time
                    worksheet.update_cell(i, 9, "")         # clear worked_hours
                    break
            update_report_worksheet(report_worksheet)
        await message.reply(f"✅ Смена с ID {shift_id} обновлена на {text}.", parse_mode=ParseMode.MARKDOWN)
    else:
        await message.reply("❌ Неверный формат времени (ЧЧ:ММ-ЧЧ:ММ) или значение 'Home HH:MM'.", parse_mode=ParseMode.MARKDOWN)

    del message.expected_shift_id

# --- Запуск бота ---
if __name__ == '__main__':
    init_db()
    logging.info("Бот запущен...")
    executor.start_polling(dp, skip_updates=True)
