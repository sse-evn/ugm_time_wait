require('dotenv').config();
const sqlite3 = require('sqlite3').verbose();
const { Telegraf, Markup } = require('telegraf');
const { format } = require('date-fns');
const winston = require('winston');
const { google } = require('googleapis');
const cron = require('node-cron');

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

// Initialize Google Sheets API
const auth = new google.auth.GoogleAuth({
  keyFile: process.env.GOOGLE_SHEETS_CREDENTIALS_PATH,
  scopes: ['https://www.googleapis.com/auth/spreadsheets']
});
const sheets = google.sheets({ version: 'v4', auth });
const spreadsheetId = '1QWCYpeBQGofESEkD4WWYAIl0fvVDt7VZvWOE-qKe_RE';

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
    adminUsernames: adminUsernames.split(',').map(u => u.trim().replace('@', '')),
    timezone: groupTimezone
  });
  console.log('Loaded group config:', { groupId, adminUsernames: adminUsernames.split(',').map(u => u.trim()), timezone: groupTimezone });

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
      shift_date TEXT NOT NULL,
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
const escapeMarkdownV2 = (text) => {
  if (!text) return text;
  return text.replace(/([_*[\]()~`>#+\-=|{}.!])/g, '\\$1');
};

const getGroupConfig = (groupId) => {
  const config = groupConfigs.find(config => config.groupId === groupId);
  return config ? {
    ...config,
    botToken: process.env.BOT_TOKEN
  } : null;
};

const isAdmin = (username, adminUsernames) => {
  return adminUsernames.includes(username.replace('@', ''));
};

const getCurrentDate = (timezone) => {
  if (!timezone) throw new Error('Timezone is undefined');
  try {
    return new Intl.DateTimeFormat('en-GB', {
      day: '2-digit',
      month: '2-digit',
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
    Markup.button.callback('🔚 Завершить вручную', 'manual_end_shift_menu'),
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

// Cron job for marking day off at 18:16
groupConfigs.forEach(config => {
  cron.schedule('16 18 * * *', async () => {
    try {
      const shiftDate = getCurrentDate(config.timezone);
      const sheetName = 'Sheet1';
      const sheetRange = `${sheetName}!A:G`;

      // Get all employees from Google Sheets
      const response = await sheets.spreadsheets.values.get({
        spreadsheetId,
        range: sheetRange,
      });

      const values = response.data.values || [];
      if (values.length === 0) return;

      // Skip header row
      const employees = values.slice(1);

      for (const row of employees) {
        const username = row[2]?.replace('@', '');
        const fullName = row[1];
        const existingDates = (row[5] || '').split(',').map(s => s.trim());

        // Check if employee had a shift today
        const shifts = await dbAll(
          "SELECT * FROM shifts WHERE username = ? AND shift_date = ? AND group_id = ?",
          [username, shiftDate, config.groupId]
        );

        // Skip if employee had a shift or already marked as day off
        if (shifts.length > 0 || existingDates.includes(`${shiftDate} (выходной)`)) {
          continue;
        }

        // Add day off to Google Sheets
        existingDates.push(`${shiftDate} (выходной)`);
        row[5] = existingDates.join(', ');
        await sheets.spreadsheets.values.update({
          spreadsheetId,
          range: `${sheetName}!A${values.indexOf(row) + 1}:G${values.indexOf(row) + 1}`,
          valueInputOption: 'RAW',
          resource: { values: [row] },
        });

        // Send notification to employee
        const user = await dbGet(
          "SELECT DISTINCT username FROM shifts WHERE username = ? AND group_id = ?",
          [username, config.groupId]
        );
        if (user) {
          try {
            await bot.telegram.sendMessage(
              `@${username}`,
              `📌 Сегодня, ${shiftDate}, у вас был выходной.`
            );
          } catch (err) {
            logger.error(`Failed to notify ${username} about day off:`, err);
          }
        }
      }
    } catch (err) {
      logger.error('Day off cron error:', err);
    }
  }, {
    timezone: config.timezone
  });
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
  await ctx.replyWithMarkdownV2(`
👋 Бот для учета смен\\. Отправьте фото с подписью в формате:

\`\`\`
Имя Фамилия
07:00 15:00
Зона 1
W witag 1
\`\`\`

*Команды:*
/myshifts \\- Ваши смены
/today \\- Смены сегодня
/admin \\- Админ панель
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

    let message = `📋 Ваши смены \\(@${escapeMarkdownV2(username)}\\)\\n`;
    let currentDate = '';

    for (const shift of shifts) {
      if (shift.shift_date !== currentDate) {
        currentDate = shift.shift_date;
        message += `\\n📅 *${escapeMarkdownV2(currentDate)}*\\n`;
      }

      const status = shift.status === 'active' ? '✅' : 
                    shift.status === 'completed' ? '⏹️' : '❌';
      
      message += `${status} ${escapeMarkdownV2(shift.start_time)}\\-${escapeMarkdownV2(shift.end_time)} ${escapeMarkdownV2(shift.zone)}`;
      if (shift.witag && shift.witag !== 'Нет') message += ` \\(${escapeMarkdownV2(shift.witag)}\\)`;
      message += '\\n';
    }

    await ctx.replyWithMarkdownV2(message);
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

    let message = `📅 Смены на ${escapeMarkdownV2(today)}\\n`;
    for (const shift of shifts) {
      const status = shift.status === 'active' ? '✅' : 
                    shift.status === 'completed' ? '⏹️' : '❌';
      
      message += `\\n${status} @${escapeMarkdownV2(shift.username)} \\(${escapeMarkdownV2(shift.full_name)}\\)\\n`;
      message += `${escapeMarkdownV2(shift.start_time)}\\-${escapeMarkdownV2(shift.end_time)} ${escapeMarkdownV2(shift.zone)}`;
      if (shift.witag && shift.witag !== 'Нет') message += ` \\(${escapeMarkdownV2(shift.witag)}\\)`;
    }

    await ctx.replyWithMarkdownV2(message);
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
    return ctx.replyWithMarkdownV2(`
❌ Неверный формат\\. Пример:
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
  const shiftDate = getCurrentDate(timezone);

  if (!isValidTime(startTime) || !isValidTime(endTime)) {
    return ctx.reply('❌ Неверный формат времени (ЧЧ:ММ)');
  }

  try {
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

    const sheetName = 'Sheet1';
    const sheetRange = `${sheetName}!A:G`;

    const response = await sheets.spreadsheets.values.get({
      spreadsheetId,
      range: sheetRange,
    });

    const values = response.data.values || [];
    const existingRowIndex = values.findIndex(row => row[2] === `@${username}`);

    if (existingRowIndex >= 0) {
      const existingDates = (values[existingRowIndex][5] || '').split(',').map(s => s.trim());
      if (!existingDates.includes(shiftDate)) {
        existingDates.push(shiftDate);
        values[existingRowIndex][5] = existingDates.join(', ');
        values[existingRowIndex][6] = 'Работал';
        await sheets.spreadsheets.values.update({
          spreadsheetId,
          range: `${sheetName}!A${existingRowIndex + 1}:G${existingRowIndex + 1}`,
          valueInputOption: 'RAW',
          resource: { values: [values[existingRowIndex]] },
        });
      }
    } else {
      const newRow = [
        values.length + 1,
        fullName,
        `@${username}`,
        zone,
        witag,
        shiftDate,
        'Работал'
      ];
      await sheets.spreadsheets.values.append({
        spreadsheetId,
        range: sheetRange,
        valueInputOption: 'RAW',
        resource: { values: [newRow] },
      });
    }

    await ctx.replyWithMarkdownV2(`
✅ *${escapeMarkdownV2(fullName)}* записан на смену
📅 *Дата:* \`${escapeMarkdownV2(shiftDate)}\`
⏰ *Время:* \`${escapeMarkdownV2(startTime)}\\-${escapeMarkdownV2(endTime)}\`
📍 *Зона:* \`${escapeMarkdownV2(zone)}\`
🔖 *Witag:* \`${escapeMarkdownV2(witag)}\`
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

    let message = '📊 *Отчет по сменам*\\n';
    let currentDate = '';

    for (const shift of shifts) {
      if (shift.shift_date !== currentDate) {
        currentDate = shift.shift_date;
        message += `\\n📅 *${escapeMarkdownV2(currentDate)}*\\n`;
      }

      const status = shift.status === 'active' ? '✅' : 
                    shift.status === 'completed' ? '⏹️' : '❌';
      
      message += `${status} @${escapeMarkdownV2(shift.username)} \\(${escapeMarkdownV2(shift.full_name)}\\)\\n`;
      message += `${escapeMarkdownV2(shift.start_time)}\\-${escapeMarkdownV2(shift.end_time)} ${escapeMarkdownV2(shift.zone)}`;
      if (shift.witag && shift.witag !== 'Нет') message += ` \\(${escapeMarkdownV2(shift.witag)}\\)`;
      message += ` \\[ID:${shift.id}\\]\\n`;
    }

    await ctx.replyWithMarkdownV2(message);
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

    let message = '📋 *Активные смены*\\n';
    for (const shift of shifts) {
      message += `\\n🆔 *${shift.id}* @${escapeMarkdownV2(shift.username)} \\(${escapeMarkdownV2(shift.full_name)}\\)\\n`;
      message += `📅 ${escapeMarkdownV2(shift.shift_date)} ⏰ ${escapeMarkdownV2(shift.start_time)}\\-${escapeMarkdownV2(shift.end_time)}\\n`;
      message += `📍 ${escapeMarkdownV2(shift.zone)}`;
      if (shift.witag && shift.witag !== 'Нет') message += ` 🔖 ${escapeMarkdownV2(shift.witag)}`;
    }

    await ctx.replyWithMarkdownV2(message);
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
    const shifts = await dbAll(
      "SELECT * FROM shifts WHERE group_id = ? ORDER BY full_name, shift_date",
      [ctx.groupConfig.groupId]
    );

    if (!shifts.length) return ctx.reply('📄 Смены не найдены');

    const userShifts = {};
    for (const shift of shifts) {
      if (!userShifts[shift.full_name]) {
        userShifts[shift.full_name] = [];
      }
      userShifts[shift.full_name].push(shift);
    }

    const timesheetData = [];
    const dates = [...new Set(shifts.map(s => s.shift_date))].sort();
    for (const [fullName, userShiftList] of Object.entries(userShifts)) {
      const row = [fullName];
      let totalHours = 0;
      for (const date of dates) {
        const shift = userShiftList.find(s => s.shift_date === date);
        if (shift && shift.status === 'completed') {
          const hours = parseInt(shift.worked_hours.split('h')[0]) || 0;
          row.push(hours.toString());
          totalHours += hours;
        } else {
          row.push('');
        }
      }
      row.push('', totalHours.toString());
      timesheetData.push(row);
    }

    await sheets.spreadsheets.values.update({
      spreadsheetId,
      range: 'Report!A1',
      valueInputOption: 'RAW',
      resource: {
        values: [
          ['Full Name', ...dates, 'Total Hours'],
          ...timesheetData
        ]
      }
    });

    let message = '📝 *Табель учета*\\n```\\n';
    message += ['Full Name', ...dates, 'Total Hours'].map(escapeMarkdownV2).join('\t') + '\\n';
    for (const row of timesheetData) {
      message += row.map(escapeMarkdownV2).join('\t') + '\\n';
    }
    message += '```';

    await ctx.replyWithMarkdownV2(message);
  } catch (err) {
    logger.error('timesheet error:', err);
    await ctx.reply('❌ Ошибка табеля');
  }
});

bot.action(['end_shift_menu', 'cancel_shift_menu', 'manual_end_shift_menu'], async (ctx) => {
  const username = ctx.from.username;
  const groupId = ctx.groupConfig.groupId;

  if (!username || !isAdmin(username, ctx.groupConfig.adminUsernames)) {
    return ctx.answerCbQuery('🚫 Доступ запрещен');
  }

  const action = ctx.callbackQuery.data === 'end_shift_menu' ? 'end' :
                ctx.callbackQuery.data === 'manual_end_shift_menu' ? 'manual_end' : 'cancel';

  try {
    const shifts = await dbAll(
      "SELECT id, username, full_name, start_time, end_time FROM shifts WHERE status = 'active' AND group_id = ?",
      [groupId]
    );

    if (!shifts.length) return ctx.reply('📄 Активных смен нет');

    const buttons = shifts.map(shift => [
      Markup.button.callback(
        `${shift.id} @${escapeMarkdownV2(shift.username)} ${shift.start_time}-${shift.end_time}`,
        `${action}_shift_${shift.id}`
      )
    ]);

    await ctx.reply(
      `Выберите смену для ${action === 'end' ? 'завершения' : action === 'manual_end' ? 'ручного завершения' : 'отмены'}:`,
      Markup.inlineKeyboard(buttons)
    );
  } catch (err) {
    logger.error('shift menu error:', err);
    await ctx.reply('❌ Ошибка выбора смены');
  }
});

bot.action(/^(end_shift_|cancel_shift_|manual_end_shift_)(\d+)$/, async (ctx) => {
  const username = ctx.from.username;
  const groupId = ctx.groupConfig.groupId;

  if (!username || !isAdmin(username, ctx.groupConfig.adminUsernames)) {
    return ctx.answerCbQuery('🚫 Доступ запрещен');
  }

  const shiftId = parseInt(ctx.match[2]);
  const action = ctx.match[1].startsWith('end') ? 'end' :
                ctx.match[1].startsWith('manual_end') ? 'manual_end' : 'cancel';

  try {
    const shift = await dbGet(
      "SELECT username, full_name, start_time, end_time FROM shifts WHERE id = ? AND group_id = ?",
      [shiftId, groupId]
    );

    if (!shift) return ctx.reply('❌ Смена не найдена');

    if (action === 'manual_end') {
      await ctx.replyWithMarkdownV2(
        `Введите время фактического завершения для смены \\(ID: ${shiftId}\\):\\n` +
        `@${escapeMarkdownV2(shift.username)} \\(${escapeMarkdownV2(shift.full_name)}\\) ` +
        `${escapeMarkdownV2(shift.start_time)}\\-${escapeMarkdownV2(shift.end_time)}\\n` +
        `Формат: ЧЧ:ММ \\(например, 18:59\\)`,
        Markup.forceReply()
      );
      ctx.session = { awaitingManualEnd: true, shiftId };
    } else {
      await ctx.replyWithMarkdownV2(
        `Подтвердите ${action === 'end' ? 'завершение' : 'отмену'} смены:\\n` +
        `🆔 *${shiftId}* @${escapeMarkdownV2(shift.username)} \\(${escapeMarkdownV2(shift.full_name)}\\)\\n` +
        `⏰ ${escapeMarkdownV2(shift.start_time)}\\-${escapeMarkdownV2(shift.end_time)}`,
        shiftActionsKeyboard(shiftId)
      );
    }
  } catch (err) {
    logger.error('shift action error:', err);
    await ctx.reply('❌ Ошибка подтверждения');
  }
});

bot.on('text', async (ctx) => {
  if (ctx.session?.awaitingManualEnd && ctx.message.reply_to_message) {
    const username = ctx.from.username;
    const groupId = ctx.groupConfig.groupId;
    const shiftId = ctx.session.shiftId;

    if (!username || !isAdmin(username, ctx.groupConfig.adminUsernames)) {
      return ctx.reply('🚫 Доступ запрещен');
    }

    const actualEndTime = ctx.message.text.trim();
    if (!isValidTime(actualEndTime)) {
      return ctx.reply('❌ Неверный формат времени (ЧЧ:ММ)');
    }

    try {
      const shift = await dbGet(
        "SELECT start_time, full_name, username FROM shifts WHERE id = ? AND group_id = ?",
        [shiftId, groupId]
      );

      if (!shift) {
        return ctx.reply('❌ Смена не найдена');
      }

      const workedHours = calculateWorkedHours(shift.start_time, actualEndTime);
      await dbRun(
        "UPDATE shifts SET status = ?, actual_end_time = ?, worked_hours = ? WHERE id = ? AND group_id = ?",
        ['completed', actualEndTime, workedHours, shiftId, groupId]
      );

      // Update Google Sheets status
      const sheetName = 'Sheet1';
      const sheetRange = `${sheetName}!A:G`;
      const response = await sheets.spreadsheets.values.get({
        spreadsheetId,
        range: sheetRange,
      });

      const values = response.data.values || [];
      const existingRowIndex = values.findIndex(row => row[2] === `@${shift.username}`);

      if (existingRowIndex >= 0) {
        values[existingRowIndex][6] = 'Прерван';
        await sheets.spreadsheets.values.update({
          spreadsheetId,
          range: `${sheetName}!A${existingRowIndex + 1}:G${existingRowIndex + 1}`,
          valueInputOption: 'RAW',
          resource: { values: [values[existingRowIndex]] },
        });
      }

      await ctx.replyWithMarkdownV2(
        `✅ Смена завершена вручную\\.\n` +
        `🆔 *${shiftId}* @${escapeMarkdownV2(shift.username)} \\(${escapeMarkdownV2(shift.full_name)}\\)\\n` +
        `Отработано: ${escapeMarkdownV2(workedHours)}`
      );

      delete ctx.session.awaitingManualEnd;
      delete ctx.session.shiftId;
    } catch (err) {
      logger.error('Manual end shift error:', err);
      await ctx.reply('❌ Ошибка завершения смены');
    }
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
    const shift = await dbGet(
      "SELECT start_time, end_time FROM shifts WHERE id = ? AND group_id = ?",
      [shiftId, groupId]
    );

    const workedHours = action === 'completed' ? calculateWorkedHours(shift.start_time, shift.end_time) : null;

    await dbRun(
      "UPDATE shifts SET status = ?, worked_hours = ? WHERE id = ? AND group_id = ?",
      [action, workedHours, shiftId, groupId]
    );

    // Update Google Sheets status for completed shifts
    if (action === 'completed') {
      const sheetName = 'Sheet1';
      const sheetRange = `${sheetName}!A:G`;
      const response = await sheets.spreadsheets.values.get({
        spreadsheetId,
        range: sheetRange,
      });

      const values = response.data.values || [];
      const existingRowIndex = values.findIndex(row => row[2] === `@${username}`);

      if (existingRowIndex >= 0) {
        values[existingRowIndex][6] = 'Работал';
        await sheets.spreadsheets.values.update({
          spreadsheetId,
          range: `${sheetName}!A${existingRowIndex + 1}:G${existingRowIndex + 1}`,
          valueInputOption: 'RAW',
          resource: { values: [values[existingRowIndex]] },
        });
      }
    }

    await ctx.reply(`✅ Смена ${shiftId} ${action === 'completed' ? 'завершена' : 'отменена'}`);
  } catch (err) {
    logger.error('confirm action error:', err);
    await ctx.reply('❌ Ошибка обновления');
  }
});

// Auto-complete shifts at 23:05
groupConfigs.forEach(config => {
  cron.schedule('5 23 * * *', async () => {
    try {
      const shiftDate = getCurrentDate(config.timezone);
      const shifts = await dbAll(
        "SELECT id, start_time, end_time, username FROM shifts WHERE status = 'active' AND shift_date = ? AND group_id = ?",
        [shiftDate, config.groupId]
      );

      for (const shift of shifts) {
        const workedHours = calculateWorkedHours(shift.start_time, shift.end_time);
        await dbRun(
          "UPDATE shifts SET status = ?, worked_hours = ? WHERE id = ? AND group_id = ?",
          ['completed', workedHours, shift.id, config.groupId]
        );

        const sheetName = 'Sheet1';
        const sheetRange = `${sheetName}!A:G`;
        const response = await sheets.spreadsheets.values.get({
          spreadsheetId,
          range: sheetRange,
        });

        const values = response.data.values || [];
        const existingRowIndex = values.findIndex(row => row[2] === `@${shift.username}`);

        if (existingRowIndex >= 0) {
          values[existingRowIndex][6] = 'Работал';
          await sheets.spreadsheets.values.update({
            spreadsheetId,
            range: `${sheetName}!A${existingRowIndex + 1}:G${existingRowIndex + 1}`,
            valueInputOption: 'RAW',
            resource: { values: [values[existingRowIndex]] },
          });
        }
      }
    } catch (err) {
      logger.error('Auto-complete shifts error:', err);
    }
  }, {
    timezone: config.timezone
  });
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
