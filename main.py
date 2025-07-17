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

# --- Настройка логирования ---
logging.basicConfig(level=logging.INFO)

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

# --- Обработчики команд и сообщений ---

@dp.message_handler(commands=['start', 'help'])
async def send_welcome(message: types.Message):
    """Отправляет приветственное сообщение."""
    await message.reply(
        "Привет! Я бот для учета смен. \n"
        "Отправьте фото с подписью в формате:\n\n"
        "Имя Фамилия\n"
        "дд.мм.гг\n"
        "чч:мм чч:мм\n"
        "Зона XX\n"
        "W witag XX (необязательно)"
    )


@dp.message_handler(content_types=['photo'])
async def handle_photo_with_caption(message: types.Message):
    """
    Обрабатывает получение фотографии с подписью, парсит данные
    и сохраняет их в базу данных.
    """
    if not message.caption:
        return

    # Паттерн для разбора подписи с помощью регулярных выражений
    pattern = re.compile(
        r'^(?P<name>[\w\sА-Яа-я]+)\n'
        r'(?P<date>\d{2}\.\d{2}\.\d{2,4})\n'
        r'(?P<start_time>\d{2}:\d{2})\s(?P<end_time>\d{2}:\d{2})\n'
        r'(?P<zone>Зона\s\d+)\s*'
        r'(?P<witag>W\s+witag\s+\d+)?$',
        re.MULTILINE | re.IGNORECASE
    )

    match = pattern.match(message.caption.strip())

    if not match:
        await message.reply("❌ Неверный формат данных в подписи. Пожалуйста, проверьте и попробуйте снова.")
        return

    data = match.groupdict()
    full_name = data['name'].strip()
    shift_date = data['date']
    start_time = data['start_time']
    end_time = data['end_time']
    zone = data['zone'].strip()
    witag = data['witag'].strip() if data['witag'] else "Нет"
    photo_file_id = message.photo[-1].file_id
    user_id = message.from_user.id

    try:
        # Валидация даты
        datetime.strptime(shift_date, '%d.%m.%y')
        add_shift(user_id, full_name, photo_file_id, shift_date, start_time, end_time, zone, witag)
        await message.reply(f"✅ Сотрудник **{full_name}** успешно записан на смену.", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await message.reply("❌ Неверный формат даты. Используйте ДД.ММ.ГГ.")
    except Exception as e:
        logging.error(f"Ошибка при добавлении смены: {e}")
        await message.reply("❗️ Произошла внутренняя ошибка. Попробуйте позже.")


@dp.message_handler(commands=['report'])
async def get_report(message: types.Message):
    """
    Формирует и отправляет отчет по сменам для администраторов.
    Отчет формируется на текущую дату по часовому поясу UTC+5.
    """
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    # Проверяем, является ли пользователь администратором чата
    try:
        chat_admins = await bot.get_chat_administrators(chat_id)
        admin_ids = {admin.user.id for admin in chat_admins}
        if user_id not in admin_ids:
            await message.reply("🚫 Эта команда доступна только для администраторов группы.", parse_mode=ParseMode.MARKDOWN)
            return
    except Exception as e:
        # Если бот не может получить список админов (например, в личных сообщениях)
        logging.warning(f"Не удалось получить список администраторов: {e}")
        await message.reply("🚫 Эту команду можно использовать только в групповом чате.")
        return

    # Формируем отчет на текущую дату
    today_date_str = datetime.now(GROUP_TIMEZONE).strftime('%d.%m.%y')
    shifts = get_shifts_for_date(today_date_str)

    if not shifts:
        await message.reply(f"📄 На {today_date_str} смен не найдено.")
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
    report_text = [f"**Отчет по сменам на {today_date_str}**\n"]
    
    if morning_shift_employees:
        report_text.append("**☀️ Утренняя смена (07:00 - 15:00):**")
        report_text.extend(morning_shift_employees)
    else:
        report_text.append("**☀️ Утренняя смена (07:00 - 15:00):**\n  - *Нет сотрудников*")
    
    report_text.append("\n") # Пустая строка для разделения
    
    if evening_shift_employees:
        report_text.append("**🌙 Вечерняя смена (15:00 - 23:00):**")
        report_text.extend(evening_shift_employees)
    else:
        report_text.append("**🌙 Вечерняя смена (15:00 - 23:00):**\n  - *Нет сотрудников*")

    await message.reply("\n".join(report_text), parse_mode=ParseMode.MARKDOWN)


# --- Запуск бота ---
if __name__ == '__main__':
    init_db()  # Создаем БД и таблицу при первом запуске
    executor.start_polling(dp, skip_updates=True)