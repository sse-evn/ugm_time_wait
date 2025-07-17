import os
import re
import sqlite3
import logging
from datetime import datetime, time
import pytz

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ParseMode
from dotenv import load_dotenv

# --- Конфигурация ---
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
GROUP_TIMEZONE = pytz.timezone('Etc/GMT-5') # Часовой пояс UTC+5

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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# --- Инициализация бота и диспетчера ---
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# --- База данных SQLite ---
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

def add_shift(user_id, full_name, photo_id, s_date, s_time, e_time, zone, witag):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO shifts (user_id, full_name, photo_file_id, shift_date, start_time, end_time, zone, witag, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, full_name, photo_id, s_date, s_time, e_time, zone, witag, datetime.now(GROUP_TIMEZONE)))
    conn.commit()
    conn.close()

def get_shifts_for_date(report_date):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT full_name, start_time, end_time, zone, witag FROM shifts WHERE shift_date = ?", (report_date,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_user_shifts_for_date(user_id, shift_date):
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT start_time, end_time FROM shifts WHERE user_id = ? AND shift_date = ?", (user_id, shift_date))
    rows = cur.fetchall()
    conn.close()
    return rows

# --- Вспомогательные функции для валидации ---
def is_valid_time(time_str, fmt='%H:%M'):
    try:
        datetime.strptime(time_str, fmt).time()
        return True
    except ValueError:
        return False

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
    и сохраняет их в базу данных.
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
    existing_shifts = get_user_shifts_for_date(user_id, shift_date)
    
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
        add_shift(user_id, full_name, photo_file_id, shift_date, start_time_str, end_time_str, zone, witag)
        logging.info(f"Смена для {full_name} на {shift_date} ({start_time_str}-{end_time_str}) успешно добавлена.")
        await message.reply(
            f"✅ **{full_name}** записан на смену.\n"
            f"Дата: `{shift_date}`\n"
            f"Время: `{start_time_str}-{end_time_str}`\n"
            f"Зона: `{zone}`\n"
            f"Witag: `{witag}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logging.error(f"Ошибка при добавлении смены для {full_name}: {e}", exc_info=True)
        await message.reply("❗️ Внутренняя ошибка. Попробуйте позже.")


@dp.message_handler(commands=['report'])
async def get_report(message: types.Message):
    """Отчет по сменам для админов на текущую дату."""
    user_id = message.from_user.id
    
    if user_id not in ADMIN_IDS:
        logging.warning(f"ID {user_id} пытался использовать /report.")
        await message.reply("🚫 Команда только для авторизованных админов.")
        return

    logging.info(f"ID {user_id} запросил отчет.")

    today_date_str = datetime.now(GROUP_TIMEZONE).strftime('%d.%m.%y')
    shifts = get_shifts_for_date(today_date_str)

    if not shifts:
        await message.reply(f"📄 На **{today_date_str}** смен не найдено.", parse_mode=ParseMode.MARKDOWN)
        return

    morning_shift_employees = []
    evening_shift_employees = []
    full_day_shift_employees = []

    for name, start, end, zone, witag in shifts:
        shift_info = f"  - `{name}` ({zone}, Witag: {witag})"
        if start == "07:00" and end == "15:00":
            morning_shift_employees.append(shift_info)
        elif start == "15:00" and end == "23:00":
            evening_shift_employees.append(shift_info)
        elif start == "07:00" and end == "23:00":
            full_day_shift_employees.append(shift_info)

    total_employees = len(morning_shift_employees) + \
                      len(evening_shift_employees) + \
                      len(full_day_shift_employees)

    report_text = [f"**📊 Отчет на {today_date_str}**\n"]
    
    report_text.append(f"**Общее количество людей: {total_employees}**\n")

    if morning_shift_employees:
        report_text.append(f"**☀️ Утренняя смена (07:00 - 15:00): {len(morning_shift_employees)} чел.**")
        report_text.extend(sorted(morning_shift_employees))
    else:
        report_text.append("**☀️ Утренняя смена (07:00 - 15:00): 0 чел.**\n  - *Нет сотрудников*")
    
    report_text.append("\n")
    
    if evening_shift_employees:
        report_text.append(f"**🌙 Вечерняя смена (15:00 - 23:00): {len(evening_shift_employees)} чел.**")
        report_text.extend(sorted(evening_shift_employees))
    else:
        report_text.append("**🌙 Вечерняя смена (15:00 - 23:00): 0 чел.**\n  - *Нет сотрудников*")

    report_text.append("\n")

    if full_day_shift_employees:
        report_text.append(f"**🗓️ Целый день (07:00 - 23:00): {len(full_day_shift_employees)} чел.**")
        report_text.extend(sorted(full_day_shift_employees))
    else:
        report_text.append("**🗓️ Целый день (07:00 - 23:00): 0 чел.**\n  - *Нет сотрудников*")


    await message.reply("\n".join(report_text), parse_mode=ParseMode.MARKDOWN)


# --- Запуск бота ---
if __name__ == '__main__':
    init_db()
    logging.info("Бот запущен...")
    executor.start_polling(dp, skip_updates=True)