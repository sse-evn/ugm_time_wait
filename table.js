require('dotenv').config();
const sqlite3 = require('sqlite3').verbose();
const { Telegraf, Markup } = require('telegraf');
const { format } = require('date-fns');
const winston = require('winston');

// Configure logging
const logger = winston.createLogger({
  level: 'debug',
  format: winston.format.combine(
    winston.format.timestamp(),
    winston.format.printf(({ timestamp, level, message }) => {
      return `${timestamp} - ${level} - ${message}`;
    })
  ),
  transports: [
    new winston.transports.Console(),
    new winston.transports.File({ filename: 'bot.log' })
  ]
});

// Load group configurations from .env
const groupConfigs = [];
let groupIndex = 1;
while (process.env[`GROUP${groupIndex}_ID`]) {
  const groupId = process.env[`GROUP${groupIndex}_ID`];
  const adminUsernames = process.env[`GROUP${groupIndex}_ADMINS`];
  const groupTimezone = process.env[`GROUP${groupIndex}_TIMEZONE`];

  if (!adminUsernames || !groupTimezone) {
    logger.error(`Incomplete configuration for GROUP${groupIndex}`);
    process.exit(1);
  }

  groupConfigs.push({
    groupId,
    adminUsernames: adminUsernames.split(',').map(u => u.trim()),
    timezone: groupTimezone
  });
  console.log('Loaded group config:', { groupId, adminUsernames, timezone: groupTimezone });

  groupIndex++;
}

if (groupConfigs.length === 0) {
  logger.error('No group configurations found in .env');
  process.exit(1);
}

// Database setup
const db = new sqlite3.Database('shifts.db');

// Initialize database
db.serialize(() => {
  db.run(`
    CREATE TABLE IF NOT EXISTS shifts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      group_id TEXT NOT NULL,
      username TEXT NOT NULL,
      full_name TEXT NOT NULL,
      photo_file_id TEXT,
      shift_date TEXT NOT NOT NULL,
      start_time TEXT NOT NULL,
      end_time TEXT NOT NULL,
      actual_end_time TEXT,
      worked_hours TEXT,
      zone TEXT NOT NULL,
      witag TEXT,
      status TEXT DEFAULT 'active',
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
  `);
});

// Database functions
const dbGet = (query, params = []) => {
  return new Promise((resolve, reject) => {
    db.get(query, params, (err, row) => {
      if (err) reject(err);
      else resolve(row);
    });
  });
};

const dbAll = (query, params = []) => {
  return new Promise((resolve, reject) => {
    db.all(query, params, (err, rows) => {
      if (err) reject(err);
      else resolve(rows);
    });
  });
};

const dbRun = (query, params = []) => {
  return new Promise((resolve, reject) => {
    db.run(query, params, function(err) {
      if (err) reject(err);
      else resolve(this);
    });
  });
};

// Helper functions
const getGroupConfig = (groupId) => {
  const config = groupConfigs.find(config => config.groupId === groupId);
  return config ? {
    ...config,
    botToken: process.env.BOT_TOKEN
  } : null;
};

const isAdmin = (username, adminUsernames) => {
  return adminUsernames.includes(username);
};

const getCurrentDate = (timezone) => {
  console.log('Timezone:', timezone);
  if (!timezone) throw new Error('Timezone is undefined');
  try {
    return new Intl.DateTimeFormat('en-GB', {
      day: '2-digit',
      month: '2-digit',
      year: '2-digit',
      timeZone: timezone,
    }).format(new Date()).split('/').join('.');
  } catch (err) {
    console.error('Error in getCurrentDate:', err);
    throw err;
  }
};

const isValidTime = (time) => /^([01]\d|2[0-3]):([0-5]\d)$/.test(time);

const calculateWorkedHours = (start, end) => {
  const [sh, sm] = start.split(':').map(Number);
  const [eh, em] = end.split(':').map(Number);
  
  let minutes = (eh * 60 + em) - (sh * 60 + sm);
  if (minutes < 0) minutes += 24 * 60;
  
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
};

// Keyboards
const adminKeyboard = Markup.inlineKeyboard([
  [Markup.button.callback('📊 Отчет', 'admin_report')],
  [Markup.button.callback('📋 Активные', 'active_shifts')],
  [Markup.button.callback('📝 Табель', 'timesheet')],
  [
    Markup.button.callback('🛑 Завершить', 'end_shift_menu'),
    Markup.button.callback('❌ Отменить', 'cancel_shift_menu')
  ]
]);

const shiftActionsKeyboard = (shiftId) => Markup.inlineKeyboard([
  [
    Markup.button.callback('✅ Подтвердить', `confirm_action_${shiftId}`),
    Markup.button.callback('❌ Отменить', 'cancel_action')
  ]
]);

// Initialize bot
const bot = new Telegraf(process.env.BOT_TOKEN);

// Middleware to load group config
bot.use(async (ctx, next) => {
  const groupId = ctx.chat?.id?.toString();
  if (!groupId || ctx.chat.type === 'private') {
    return ctx.reply('❌ Этот бот работает только в группах');
  }

  const groupConfig = getGroupConfig(groupId);
  if (!groupConfig) {
    return ctx.reply('❌ Эта группа не настроена');
  }

  ctx.groupConfig = groupConfig;
  return next();
});

// Command handlers
bot.command('admin', async (ctx) => {
  const username = ctx.from.username;
  if (!username || !isAdmin(username, ctx.groupConfig.adminUsernames)) {
    return ctx.reply('🚫 Доступ запрещен');
  }
  await ctx.reply('👨‍💻 Админ панель', adminKeyboard);
});

bot.command(['start', 'help'], async (ctx) => {
  await ctx.replyWithMarkdown(`
👋 Бот для учета смен. Отправьте фото с подписью в формате:

\`\`\`
Имя Фамилия
07:00 15:00
Зона 1
W witag 1
\`\`\`

*Команды:*
/myshifts - Ваши смены
/today - Смены сегодня
/admin - Админ панель
  `);
});

bot.command('myshifts', async (ctx) => {
  const username = ctx.from.username;
  const groupId = ctx.groupConfig.groupId;

  if (!username) return ctx.reply('❌ Установите username в Telegram');

  try {
    const shifts = await dbAll(
      "SELECT * FROM shifts WHERE username = ? AND group_id = ? ORDER BY shift_date DESC, start_time", 
      [username, groupId]
    );

    if (!shifts.length) return ctx.reply('📄 У вас нет смен');

    let message = `📋 Ваши смены (@${username})\n`;
    let currentDate = '';

    for (const shift of shifts) {
      if (shift.shift_date !== currentDate) {
        currentDate = shift.shift_date;
        message += `\n📅 *${currentDate}*\n`;
      }

      const status = shift.status === 'active' ? '✅' : 
                    shift.status === 'completed' ? '⏹️' : '❌';
      
      message += `${status} ${shift.start_time}-${shift.end_time} ${shift.zone}`;
      if (shift.witag && shift.witag !== 'Нет') message += ` (${shift.witag})`;
      message += '\n';
    }

    await ctx.replyWithMarkdown(message);
  } catch (err) {
    logger.error('myshifts error:', err);
    await ctx.reply('❌ Ошибка получения смен');
  }
});

bot.command('today', async (ctx) => {
  try {
    const today = getCurrentDate(ctx.groupConfig.timezone);
    const groupId = ctx.groupConfig.groupId;
    const shifts = await dbAll(
      "SELECT * FROM shifts WHERE shift_date = ? AND group_id = ? ORDER BY start_time",
      [today, groupId]
    );

    if (!shifts.length) return ctx.reply(`📅 На ${today} смен нет`);

    let message = `📅 Смены на ${today}\n`;
    for (const shift of shifts) {
      const status = shift.status === 'active' ? '✅' : 
                    shift.status === 'completed' ? '⏹️' : '❌';
      
      message += `\n${status} @${shift.username} (${shift.full_name})\n`;
      message += `${shift.start_time}-${shift.end_time} ${shift.zone}`;
      if (shift.witag && shift.witag !== 'Нет') message += ` (${shift.witag})`;
    }

    await ctx.reply(message);
  } catch (err) {
    logger.error('today error:', err);
    await ctx.reply('❌ Ошибка получения смен');
  }
});

// Photo handler
bot.on('photo', async (ctx) => {
  const username = ctx.from.username;
  const groupId = ctx.groupConfig.groupId;
  const timezone = ctx.groupConfig.timezone;

  if (!username) return ctx.reply('❌ Установите username в Telegram');

  if (!ctx.message.caption) {
    return ctx.reply('❌ Отправьте фото с подписью');
  }

  const match = ctx.message.caption.match(
    /^([^\n]+)\n(\d{2}:\d{2})\s(\d{2}:\d{2})\n(Зона\s+\d+)(?:\n(W\s+witag\s+\d+))?/i
  );

  if (!match) {
    return ctx.replyWithMarkdown(`
❌ Неверный формат. Пример:
\`\`\`
Имя Фамилия
07:00 15:00
Зона 1
W witag 1
\`\`\`
    `);
  }

  const fullName = match[1].trim();
  const startTime = match[2];
  const endTime = match[3];
  const zone = match[4].trim();
  const witag = match[5] ? match[5].trim() : 'Нет';

  if (!isValidTime(startTime) || !isValidTime(endTime)) {
    return ctx.reply('❌ Неверный формат времени (ЧЧ:ММ)');
  }

  try {
    const shiftDate = getCurrentDate(timezone);
    const existingShifts = await dbAll(
      "SELECT start_time, end_time FROM shifts WHERE username = ? AND shift_date = ? AND group_id = ?",
      [username, shiftDate, groupId]
    );

    for (const shift of existingShifts) {
      if ((startTime < shift.end_time) && (endTime > shift.start_time)) {
        return ctx.reply(
          `❌ Пересечение с существующей сменой ${shift.start_time}-${shift.end_time}`
        );
      }
    }

    const photoId = ctx.message.photo[ctx.message.photo.length - 1].file_id;
    await dbRun(
      `INSERT INTO shifts (group_id, username, full_name, photo_file_id, shift_date, 
       start_time, end_time, zone, witag)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      [groupId, username, fullName, photoId, shiftDate, startTime, endTime, zone, witag]
    );

    await ctx.replyWithMarkdown(`
✅ *${fullName}* записан на смену
📅 *Дата:* \`${shiftDate}\`
⏰ *Время:* \`${startTime}-${endTime}\`
📍 *Зона:* \`${zone}\`
🔖 *Witag:* \`${witag}\`
    `);
  } catch (err) {
    logger.error('Shift registration error:', err);
    await ctx.reply('❌ Ошибка регистрации смены');
  }
});

// Admin handlers
bot.action('admin_report', async (ctx) => {
  const username = ctx.from.username;
  const groupId = ctx.groupConfig.groupId;

  if (!username || !isAdmin(username, ctx.groupConfig.adminUsernames)) {
    return ctx.answerCbQuery('🚫 Доступ запрещен');
  }

  try {
    const shifts = await dbAll(
      "SELECT * FROM shifts WHERE group_id = ? ORDER BY shift_date DESC, start_time",
      [groupId]
    );

    if (!shifts.length) return ctx.reply('📄 Смены не найдены');

    let message = '📊 *Отчет по сменам*\n';
    let currentDate = '';

    for (const shift of shifts) {
      if (shift.shift_date !== currentDate) {
        currentDate = shift.shift_date;
        message += `\n📅 *${currentDate}*\n`;
      }

      const status = shift.status === 'active' ? '✅' : 
                    shift.status === 'completed' ? '⏹️' : '❌';
      
      message += `${status} @${shift.username} (${shift.full_name})\n`;
      message += `${shift.start_time}-${shift.end_time} ${shift.zone}`;
      if (shift.witag && shift.witag !== 'Нет') message += ` (${shift.witag})`;
      message += ` [ID:${shift.id}]\n`;
    }

    await ctx.replyWithMarkdown(message);
  } catch (err) {
    logger.error('admin_report error:', err);
    await ctx.reply('❌ Ошибка отчета');
  }
});

bot.action('active_shifts', async (ctx) => {
  const username = ctx.from.username;
  const groupId = ctx.groupConfig.groupId;

  if (!username || !isAdmin(username, ctx.groupConfig.adminUsernames)) {
    return ctx.answerCbQuery('🚫 Доступ запрещен');
  }

  try {
    const shifts = await dbAll(
      "SELECT * FROM shifts WHERE status = 'active' AND group_id = ? ORDER BY shift_date, start_time",
      [groupId]
    );

    if (!shifts.length) return ctx.reply('📄 Активных смен нет');

    let message = '📋 *Активные смены*\n';
    for (const shift of shifts) {
      message += `\n🆔 *${shift.id}* @${shift.username} (${shift.full_name})\n`;
      message += `📅 ${shift.shift_date} ⏰ ${shift.start_time}-${shift.end_time}\n`;
      message += `📍 ${shift.zone}`;
      if (shift.witag && shift.witag !== 'Нет') message += ` 🔖 ${shift.witag}`;
    }

    await ctx.replyWithMarkdown(message);
  } catch (err) {
    logger.error('active_shifts error:', err);
    await ctx.reply('❌ Ошибка получения смен');
  }
});

bot.action('timesheet', async (ctx) => {
  const username = ctx.from.username;
  if (!username || !isAdmin(username, ctx.groupConfig.adminUsernames)) {
    return ctx.answerCbQuery('🚫 Доступ запрещен');
  }

  try {
    const timesheetData = [
      ["Берикулы Айсар СС", "8", "8", "8", "8", "8", "0", "8", "8", "8", "8", "8", "8", "8", "8", "8", "", "", "210000"],
      ["Алимжан Дархан", "8", "8", "16", "7", "16", "15", "", "8", "8", "8", "8", "8", "16", "8", "", "", "", "167500"]
    ];

    let message = '📝 *Табель учета*\n```\n';
    for (const row of timesheetData) {
      message += row.join('\t') + '\n';
    }
    message += '```';

    await ctx.replyWithMarkdown(message);
  } catch (err) {
    logger.error('timesheet error:', err);
    await ctx.reply('❌ Ошибка табеля');
  }
});

bot.action(['end_shift_menu', 'cancel_shift_menu'], async (ctx) => {
  const username = ctx.from.username;
  const groupId = ctx.groupConfig.groupId;

  if (!username || !isAdmin(username, ctx.groupConfig.adminUsernames)) {
    return ctx.answerCbQuery('🚫 Доступ запрещен');
  }

  const action = ctx.callbackQuery.data === 'end_shift_menu' ? 'end' : 'cancel';

  try {
    const shifts = await dbAll(
      "SELECT id, username, full_name, start_time, end_time FROM shifts WHERE status = 'active' AND group_id = ?",
      [groupId]
    );

    if (!shifts.length) return ctx.reply('📄 Активных смен нет');

    const buttons = shifts.map(shift => [
      Markup.button.callback(
        `${shift.id} @${shift.username} ${shift.start_time}-${shift.end_time}`,
        `${action}_shift_${shift.id}`
      )
    ]);

    await ctx.reply(
      `Выберите смену для ${action === 'end' ? 'завершения' : 'отмены'}:`,
      Markup.inlineKeyboard(buttons)
    );
  } catch (err) {
    logger.error('shift menu error:', err);
    await ctx.reply('❌ Ошибка выбора смены');
  }
});

bot.action(/^(end_shift_|cancel_shift_)(\d+)$/, async (ctx) => {
  const username = ctx.from.username;
  const groupId = ctx.groupConfig.groupId;

  if (!username || !isAdmin(username, ctx.groupConfig.adminUsernames)) {
    return ctx.answerCbQuery('🚫 Доступ запрещен');
  }

  const shiftId = parseInt(ctx.match[2]);
  const action = ctx.match[1].startsWith('end') ? 'end' : 'cancel';

  try {
    const shift = await dbGet(
      "SELECT username, full_name, start_time, end_time FROM shifts WHERE id = ? AND group_id = ?",
      [shiftId, groupId]
    );

    if (!shift) return ctx.reply('❌ Смена не найдена');

    await ctx.replyWithMarkdown(
      `Подтвердите ${action === 'end' ? 'завершение' : 'отмену'} смены:\n` +
      `🆔 *${shiftId}* @${shift.username} (${shift.full_name})\n` +
      `⏰ ${shift.start_time}-${shift.end_time}`,
      shiftActionsKeyboard(shiftId)
    );
  } catch (err) {
    logger.error('shift action error:', err);
    await ctx.reply('❌ Ошибка подтверждения');
  }
});

bot.action(/^confirm_action_(\d+)$/, async (ctx) => {
  const username = ctx.from.username;
  const groupId = ctx.groupConfig.groupId;

  if (!username || !isAdmin(username, ctx.groupConfig.adminUsernames)) {
    return ctx.answerCbQuery('🚫 Доступ запрещен');
  }

  const shiftId = parseInt(ctx.match[1]);
  const action = ctx.callbackQuery.data.includes('end') ? 'completed' : 'canceled';

  try {
    const result = await dbRun(
      "UPDATE shifts SET status = ? WHERE id = ? AND group_id = ?",
      [action, shiftId, groupId]
    );

    if (result.changes > 0) {
      await ctx.reply(`✅ Смена ${shiftId} ${action === 'completed' ? 'завершена' : 'отменена'}`);
    } else {
      await ctx.reply('❌ Смена не найдена');
    }
  } catch (err) {
    logger.error('confirm action error:', err);
    await ctx.reply('❌ Ошибка обновления');
  }
});

bot.catch((err, ctx) => {
  logger.error(`Error for ${ctx.updateType}:`, err);
  ctx.reply('❌ Произошла ошибка');
});

// Start bot
bot.launch().then(() => logger.info('Bot started'));

// Enable graceful stop
process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));
