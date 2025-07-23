import os
import re
import sqlite3
import logging
from datetime import datetime
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
        
        # Проверяем наличие листа Report, создаем если отсутствует
        try:
            report_worksheet = spreadsheet.worksheet("Report")
        except gspread.exceptions.WorksheetNotFound:
            report_worksheet = spreadsheet.add_worksheet(title="Report", rows=100, cols=10)
            # Форматируем заголовки
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
        INSERT INTO shifts (user_id, full_name, photo_file_id, shift_date, start_time, end_time, zone, witag, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, full_name, photo_id, s_date, s_time, e_time, zone, witag, current_time_utc5))
    conn.commit()
    conn.close()
    return cur.lastrowid

# --- Добавление смены в Google Sheets ---
def add_shift_gsheets(worksheet, report_worksheet, user_id, full_name, photo_id, s_date, s_time, e_time, zone, witag, created_at):
    try:
        # Добавляем в Sheet1
        rows = worksheet.get_all_values()
        next_id = len(rows)  # ID = количество строк (заголовок + данные)
        worksheet.append_row([
            next_id, user_id, full_name, photo_id, s_date, s_time, e_time, zone, witag, created_at
        ])
        logging.info(f"Смена для {full_name} добавлена в Google Sheets (Sheet1).")
        
        # Обновляем Report лист
        update_report_worksheet(report_worksheet)
    except Exception as e:
        logging.error(f"Ошибка при добавлении в Google Sheets: {e}", exc_info=True)

# --- Обновление листа Report ---
def update_report_worksheet(report_worksheet):
    try:
        shifts = get_all_shifts_gsheets(report_worksheet.spreadsheet.worksheet("Sheet1"))
        if not shifts:
            return
        
        # Группировка по датам
        shifts_by_date = defaultdict(list)
        for shift in shifts:
            user_id, full_name, shift_date, start_time, end_time, zone, witag, created_at = shift
            shifts_by_date[shift_date].append({
                'full_name': full_name,
                'start_time': start_time,
                'end_time': end_time,
                'zone': zone,
                'witag': witag,
                'created_at': created_at
            })
        
        # Очищаем Report лист (кроме заголовков)
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
        
        # Заполняем данными
        row_index = 2
        for shift_date in sorted(shifts_by_date.keys(), reverse=True):
            report_worksheet.update(f'A{row_index}', [[shift_date]])
            fmt = CellFormat(textFormat=TextFormat(bold=True))
            format_cell_range(report_worksheet, f'A{row_index}', fmt)
            row_index += 1
            
            for shift in sorted(shifts_by_date[shift_date], key=lambda x: datetime.fromisoformat(x['created_at'])):
                report_worksheet.update(f'A{row_index}:E{row_index}', [[
                    '', shift['full_name'], f"{shift['start_time']}-{shift['end_time']}", shift['zone'], shift['witag']
                ]])
                row_index += 1
            
            row_index += 1  # Пустая строка между датами
        
        logging.info("Лист Report обновлен.")
    except Exception as e:
        logging.error(f"Ошибка при обновлении листа Report: {e}", exc_info=True)

# --- Получение всех смен из SQLite ---
def get_all_shifts_sqlite():
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT user_id, full_name, shift_date, start_time, end_time, zone, witag, created_at FROM shifts")
    rows = cur.fetchall()
    conn.close()
    return rows

# --- Получение всех смен из Google Sheets ---
def get_all_shifts_gsheets(worksheet):
    try:
        rows = worksheet.get_all_values()[1:]  # Пропускаем заголовок
        shifts = []
        for row in rows:
            shifts.append((int(row[1]), row[2], row[4], row[5], row[6], row[7], row[8], row[9]))  # user_id, full_name, shift_date, start_time, end_time, zone, witag, created_at
        return shifts
    except Exception as e:
        logging.error(f"Ошибка при получении данных из Google Sheets: {e}", exc_info=True)
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
            if int(row[1]) == user_id and row[4] == shift_date:  # user_id и shift_date
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

# --- Инициализация бота и диспетчера ---
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)
try:
    worksheet, report_worksheet = init_google_sheets()  # Инициализация Google Sheets
except Exception as e:
    logging.error(f"Не удалось инициализировать Google Sheets: {e}")
    worksheet, report_worksheet = None, None  # Продолжаем работать с SQLite, если Google Sheets недоступен

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
        "Можно указать смену на весь день: `07:00 23:00`",
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
                f"({existing_start_str}-{existing_end_str}) на сегодня. "
                f"Нельзя записываться на две совпадающие смены."
            )
            logging.info(f"Пользователь {user_full_name} (ID: {user_id}) пытался добавить пересекающуюся смену.")
            return

    # --- Добавление смены ---
    try:
        # Добавляем в SQLite
        shift_id = add_shift_sqlite(user_id, full_name, photo_file_id, shift_date, start_time_str, end_time_str, zone, witag)
        # Добавляем в Google Sheets, если доступно
        if worksheet and report_worksheet:
            current_time_utc5 = datetime.now(GROUP_TIMEZONE).isoformat()
            add_shift_gsheets(worksheet, report_worksheet, user_id, full_name, photo_file_id, shift_date, start_time_str, end_time_str, zone, witag, current_time_utc5)
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

@dp.message_handler(commands=['report'])
async def get_report(message: types.Message):
    """Отчет по сменам для админов с группировкой по датам и статистикой по сотрудникам."""
    user_id = message.from_user.id
    
    if user_id not in ADMIN_IDS:
        logging.warning(f"ID {user_id} пытался использовать /report.")
        await message.reply("🚫 Команда только для авторизованных админов.")
        return

    logging.info(f"ID {user_id} запросил отчет.")

    # Получаем все смены
    shifts = get_all_shifts_gsheets(worksheet) if worksheet else get_all_shifts_sqlite()
    if not shifts:
        await message.reply("📄 Смены не найдены.", parse_mode=ParseMode.MARKDOWN)
        return

    # Группировка смен по датам
    shifts_by_date = defaultdict(list)
    for shift in shifts:
        user_id, full_name, shift_date, start_time, end_time, zone, witag, created_at = shift
        shift_type = (
            "☀️ Утро" if start_time == "07:00" and end_time == "15:00" else
            "🌙 Вечер" if start_time == "15:00" and end_time == "23:00" else
            "🗓️ Полный день" if start_time == "07:00" and end_time == "23:00" else
            "⏰ Другое"
        )
        shifts_by_date[shift_date].append({
            'user_id': user_id,
            'full_name': full_name,
            'start_time': start_time,
            'end_time': end_time,
            'zone': zone,
            'witag': witag,
            'created_at': created_at,
            'shift_type': shift_type
        })

    # Подсчет смен по сотрудникам
    shift_counts = defaultdict(int)
    for shift in shifts:
        shift_counts[shift[1]] += 1  # full_name

    # Формирование отчета
    report_text = ["**📊 Отчет по сменам**"]
    
    # Сортировка дат в порядке убывания (от новых к старым)
    for shift_date in sorted(shifts_by_date.keys(), reverse=True):
        report_text.append(f"\n**📅 {shift_date}**")
        
        # Заголовок таблицы
        report_text.append("```")
        report_text.append("| Тип смены       | Имя              | Время        | Зона      | Witag |")
        report_text.append("|-----------------|------------------|--------------|-----------|-------|")
        
        # Сортировка смен по времени создания
        sorted_shifts = sorted(shifts_by_date[shift_date], key=lambda x: datetime.fromisoformat(x['created_at']) if worksheet else x['created_at'])
        
        for shift in sorted_shifts:
            report_text.append(
                f"| {shift['shift_type']:<15} | {shift['full_name']:<16} | {shift['start_time']}-{shift['end_time']} | {shift['zone']:<9} | {shift['witag']:<5} |"
            )
        
        report_text.append("```")
        report_text.append(f"**Всего смен: {len(shifts_by_date[shift_date])}**")

    # Статистика по сотрудникам
    report_text.append("\n**📈 Статистика по сотрудникам**")
    report_text.append("```")
    report_text.append("| Имя              | Кол-во смен |")
    report_text.append("|------------------|-------------|")
    for name, count in sorted(shift_counts.items()):
        report_text.append(f"| {name:<16} | {count:<11} |")
    report_text.append("```")

    # Общее количество смен
    report_text.append(f"\n**Общее количество смен: {len(shifts)}**")

    # Отправка отчета
    await message.reply("\n".join(report_text), parse_mode=ParseMode.MARKDOWN)

# --- Запуск бота ---
if __name__ == '__main__':
    init_db()
    logging.info("Бот запущен...")
    executor.start_polling(dp, skip_updates=True)
