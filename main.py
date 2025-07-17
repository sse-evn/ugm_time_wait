import os
import re
import sqlite3
import logging
from datetime import datetime
import pytz

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ParseMode
from dotenv import load_dotenv

# --- Конфигурация ---
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
GROUP_TIMEZONE = pytz.timezone('Etc/GMT-5') # Часовой пояс UTC+5

# --- ID администраторов, которые могут использовать команду /report ---
# Читаем из .env и преобразуем строку "123,456,789" в множество {123, 456, 789}
ADMIN_IDS_STR = os.getenv("ADMIN_IDS")
if ADMIN_IDS_STR:
    try:
        ADMIN_IDS = {int(uid.strip()) for uid in ADMIN_IDS_STR.split(',')}
    except ValueError:
        logging.error("Ошибка при парсинге ADMIN_IDS из .env. Убедитесь, что это список чисел, разделенных запятыми.")
        ADMIN_IDS = set() # Пустой набор, если ошибка парсинга
else:
    ADMIN_IDS = set() # Пустой набор, если переменная не задана

if not ADMIN_IDS:
    logging.warning("Внимание: ADMIN_IDS не настроены в файле .env или содержат ошибки. Команда /report будет недоступна.")


# --- Настройка логирования ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# --- Инициализация бота и диспетчера ---
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# --- База данных SQLite ---
def init_db():
    """Инициализирует базу данных и создает таблицу, если она не существует."""
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
    """Добавляет новую смену в базу данных."""
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO shifts (user_id, full_name, photo_file_id, shift_date, start_time, end_time, zone, witag, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, full_name, photo_id, s_date, s_time, e_time, zone, witag, datetime.now(GROUP_TIMEZONE)))
    conn.commit()
    conn.close()

def get_shifts_for_date(report_date):
    """Получает все смены на указанную дату."""
    conn = sqlite3.connect('shifts.db')
    cur = conn.cursor()
    cur.execute("SELECT full_name, start_time, end_time, zone, witag FROM shifts WHERE shift_date = ?", (report_date,))
    rows = cur.fetchall()
    conn.close()
    return rows

# --- Вспомогательные функции для валидации ---
def is_valid_date(date_str, fmt='%d.%m.%y'):
    """Проверяет корректность формата даты."""
    try:
        datetime.strptime(date_str, fmt)
        return True
    except ValueError:
        return False

def is_valid_time(time_str, fmt='%H:%M'):
    """Проверяет корректность формата времени."""
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
        "👋 Привет! Я бот для учета смен.\n"
        "Чтобы записаться на смену, отправьте фото с подписью **СТРОГО** в следующем формате:\n\n"
        "```\n"
        "Имя Фамилия\n"
        "ДД.ММ.ГГ (например, 17.07.25)\n"
        "ЧЧ:ММ ЧЧ:ММ (например, 07:00 15:00)\n"
        "Зона XX (например, Зона 12)\n"
        "W witag XX (необязательно, если нет, просто пропустите эту строку)\n"
        "```\n\n"
        "**Пример правильной подписи:**\n"
        "```\n"
        "Иван Петров\n"
        "17.07.25\n"
        "07:00 15:00\n"
        "Зона 10\n"
        "W witag 5\n"
        "```\n\n"
        "Если у вас нет `W witag`, просто не указывайте последнюю строку.\n"
        "```\n"
        "Иван Петров\n"
        "17.07.25\n"
        "07:00 15:00\n"
        "Зона 10\n"
        "```\n\n"
        "Пожалуйста, будьте внимательны к формату! 😊",
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
        await message.reply("❌ Пожалуйста, добавьте подпись к фотографии в указанном формате.")
        return

    # Паттерн для разбора подписи с помощью регулярных выражений
    # Учитываем, что witag может отсутствовать
    pattern = re.compile(
        r'^(?P<name>[\w\sА-Яа-я]+)\n'
        r'(?P<date>\d{2}\.\d{2}\.\d{2,4})\n'
        r'(?P<start_time>\d{2}:\d{2})\s(?P<end_time>\d{2}:\d{2})\n'
        r'(?P<zone>Зона\s+\d+)\s*$'
        r'|' # Или паттерн с witag
        r'^(?P<name_w>[\w\sА-Яа-я]+)\n'
        r'(?P<date_w>\d{2}\.\d{2}\.\d{2,4})\n'
        r'(?P<start_time_w>\d{2}:\d{2})\s(?P<end_time_w>\d{2}:\d{2})\n'
        r'(?P<zone_w>Зона\s+\d+)\n'
        r'(?P<witag_val>W\s+witag\s+\d+)$',
        re.MULTILINE | re.IGNORECASE
    )

    match = pattern.match(message.caption.strip())

    if not match:
        logging.warning(f"Неверный формат подписи от {user_full_name}: '{message.caption}'")
        await message.reply(
            "❌ Неверный формат данных в подписи. Пожалуйста, проверьте и попробуйте снова.\n"
            "Используйте формат:\n"
            "```\n"
            "Имя Фамилия\n"
            "ДД.ММ.ГГ\n"
            "ЧЧ:ММ ЧЧ:ММ\n"
            "Зона XX\n"
            "W witag XX (необязательно)\n"
            "```",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Извлекаем данные в зависимости от того, какой паттерн совпал
    if match.group('name'): # Совпал паттерн без witag
        full_name = match.group('name').strip()
        shift_date = match.group('date')
        start_time = match.group('start_time')
        end_time = match.group('end_time')
        zone = match.group('zone').strip()
        witag = "Нет"
    else: # Совпал паттерн с witag
        full_name = match.group('name_w').strip()
        shift_date = match.group('date_w')
        start_time = match.group('start_time_w')
        end_time = match.group('end_time_w')
        zone = match.group('zone_w').strip()
        witag = match.group('witag_val').strip()

    photo_file_id = message.photo[-1].file_id

    # --- Валидация данных ---
    if not is_valid_date(shift_date):
        await message.reply("❌ Неверный формат даты. Используйте формат **ДД.ММ.ГГ** (например, 17.07.25).")
        return

    if not is_valid_time(start_time) or not is_valid_time(end_time):
        await message.reply("❌ Неверный формат времени. Используйте формат **ЧЧ:ММ** (например, 07:00).")
        return

    try:
        start_dt = datetime.strptime(start_time, '%H:%M').time()
        end_dt = datetime.strptime(end_time, '%H:%M').time()
        if start_dt >= end_dt:
            await message.reply("❌ Время начала смены должно быть раньше времени окончания смены.")
            return
    except Exception:
        # Это должно быть поймано is_valid_time, но как подстраховка
        await message.reply("❌ Не удалось разобрать время смены. Убедитесь, что формат ЧЧ:ММ.")
        return

    try:
        add_shift(user_id, full_name, photo_file_id, shift_date, start_time, end_time, zone, witag)
        logging.info(f"Смена для {full_name} на {shift_date} ({start_time}-{end_time}) успешно добавлена.")
        await message.reply(
            f"✅ Сотрудник **{full_name}** успешно записан на смену.\n"
            f"Дата: `{shift_date}`\n"
            f"Время: `{start_time}-{end_time}`\n"
            f"Зона: `{zone}`\n"
            f"Witag: `{witag}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logging.error(f"Ошибка при добавлении смены для {full_name}: {e}", exc_info=True)
        await message.reply("❗️ Произошла внутренняя ошибка при сохранении данных. Попробуйте позже.")


@dp.message_handler(commands=['report'])
async def get_report(message: types.Message):
    """
    Формирует и отправляет отчет по сменам для администраторов.
    Отчет формируется на текущую дату по часовому поясу UTC+5.
    """
    user_id = message.from_user.id
    
    # --- Проверка ID пользователя на принадлежность к ADMIN_IDS ---
    if user_id not in ADMIN_IDS:
        logging.warning(f"Пользователь с ID {user_id} попытался использовать команду /report, но не является администратором.")
        await message.reply("🚫 Эта команда доступна только для авторизованных администраторов.")
        return

    logging.info(f"Пользователь с ID {user_id} запросил отчет.")

    # Формируем отчет на текущую дату
    today_date_str = datetime.now(GROUP_TIMEZONE).strftime('%d.%m.%y')
    shifts = get_shifts_for_date(today_date_str)

    if not shifts:
        await message.reply(f"📄 На **{today_date_str}** смен не найдено.", parse_mode=ParseMode.MARKDOWN)
        return

    # Разделяем на утреннюю и вечернюю смены
    morning_shift_employees = []
    evening_shift_employees = []
    
    for name, start, end, zone, witag in shifts:
        shift_info = f"  - `{name}` ({zone}, Witag: {witag})"
        if start == "07:00" and end == "15:00":
            morning_shift_employees.append(shift_info)
        elif start == "15:00" and end == "23:00":
            evening_shift_employees.append(shift_info)

    # Формируем текст отчета
    report_text = [f"**📊 Отчет по сменам на {today_date_str}**\n"]
    
    if morning_shift_employees:
        report_text.append("**☀️ Утренняя смена (07:00 - 15:00):**")
        report_text.extend(sorted(morning_shift_employees)) # Сортировка для единообразия
    else:
        report_text.append("**☀️ Утренняя смена (07:00 - 15:00):**\n  - *Нет сотрудников*")
    
    report_text.append("\n") # Пустая строка для разделения
    
    if evening_shift_employees:
        report_text.append("**🌙 Вечерняя смена (15:00 - 23:00):**")
        report_text.extend(sorted(evening_shift_employees)) # Сортировка для единообразия
    else:
        report_text.append("**🌙 Вечерняя смена (15:00 - 23:00):**\n  - *Нет сотрудников*")

    await message.reply("\n".join(report_text), parse_mode=ParseMode.MARKDOWN)


# --- Запуск бота ---
if __name__ == '__main__':
    init_db()  # Создаем БД и таблицу при первом запуске
    logging.info("Бот запущен...")
    executor.start_polling(dp, skip_updates=True)