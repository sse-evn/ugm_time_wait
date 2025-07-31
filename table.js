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
  logger.info('Loaded group config:', { groupId, adminUsernames, timezone: groupTimezone });

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
const dbGet = (query, params = []) => new Promise((resolve, reject) => db.get(query, params, (err, row) => err ? reject(err) : resolve(row)));
const dbAll = (query, params = []) => new Promise((resolve, reject) => db.all(query, params, (err, rows) => err ? reject(err) : resolve(rows)));
const dbRun = (query, params = []) => new Promise((resolve, reject) => db.run(query, params, function(err) { err ? reject(err) : resolve(this); }));

// Helper functions
const escapeMarkdownV2 = (text) => (text || '').replace(/([_*[\]()~`>#+\-=|{}.!])/g, '\\$1');
const getGroupConfig = (groupId) => groupConfigs.find(config => config.groupId === groupId) || { botToken: process.env.BOT_TOKEN };
const isAdmin = (username, adminUsernames) => adminUsernames.includes(username.replace('@', ''));
const getCurrentDate = (timezone) => {
  if (!timezone) throw new Error('Timezone is undefined');
  return new Intl.DateTimeFormat('en-GB', { day: '2-digit', month: '2-digit', timeZone: timezone }).format(new Date()).split('/').join('.');
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
    Markup.button.callback('🔚 Завершить вручную', 'manual_end_shift_menu'),
    Markup.button.callback('📋 /shifts', 'show_shifts_report')
  ]
]);

const shiftActionsKeyboard = (shiftId) => Markup.inlineKeyboard([
  [Markup.button.callback('✅ Подтвердить', `confirm_action_${shiftId}`), Markup.button.callback('❌ Отменить', 'cancel_action')]
]);

// Initialize bot
const bot = new Telegraf(process.env.BOT_TOKEN);

// Middleware to load group config
bot.use(async (ctx, next) => {
  const groupId = ctx.chat?.id?.toString();
  if (!groupId || ctx.chat.type === 'private') return ctx.reply('❌ Этот бот работает только в группах');
  const groupConfig = getGroupConfig(groupId);
  if (!groupConfig) return ctx.reply('❌ Эта группа не настроена');
  ctx.groupConfig = groupConfig;
  return next();
});

// Ensure sheet exists
async function ensureSheetExists(spreadsheetId, sheetName) {
  try {
    const spreadsheet = await sheets.spreadsheets.get({ spreadsheetId });
    const sheetExists = spreadsheet.data.sheets.some(sheet => sheet.properties.title === sheetName);
    if (!sheetExists) {
      await sheets.spreadsheets.batchUpdate({
        spreadsheetId,
        resource: { requests: [{ addSheet: { properties: { title: sheetName } } }] }
      });
      logger.info(`Created sheet: ${sheetName}`);
      let headers;
      if (sheetName === 'Timesheet') headers = ['№', 'Full Name', 'Username', 'Зона', 'Wiatag', 'Дни выхода', 'Статус дня', 'Start Time', 'End Time', 'Actual End Time', 'Worked Hours'];
      else if (sheetName === 'Report') headers = ['Full Name', ...Array(31).fill().map((_, i) => i + 1), 'Summary'];
      else if (sheetName === 'shift_fail') headers = ['Shift ID', 'Full Name', 'Username', 'Shift Date', 'Start Time', 'Actual End Time', 'Worked Hours', 'Action Date', 'Admin Username'];
      if (headers) {
        await sheets.spreadsheets.values.update({
          spreadsheetId,
          range: `${sheetName}!A1:${String.fromCharCode(64 + headers.length)}1`,
          valueInputOption: 'RAW',
          resource: { values: [headers] }
        });
        logger.info(`Initialized headers for ${sheetName}`);
      }
    }
  } catch (err) {
    logger.error(`Error ensuring sheet exists (${sheetName}):`, err);
    throw err;
  }
}

// Admin command
bot.command('admin', async (ctx) => {
  const username = ctx.from.username;
  const groupId = ctx.groupConfig.groupId;

  if (!username || !isAdmin(username, ctx.groupConfig.adminUsernames)) {
    return ctx.reply('🚫 У вас нет прав администратора.');
  }

  await ctx.reply('📋 Админ-панель:', adminKeyboard);
});

// Cron job for marking day off at 18:16
groupConfigs.forEach(config => {
  cron.schedule('16 18 * * *', async () => {
    try {
      const shiftDate = getCurrentDate(config.timezone);
      const timesheetRange = 'Timesheet!A:K';
      const response = await sheets.spreadsheets.values.get({ spreadsheetId, range: timesheetRange });
      const values = response.data.values || [];
      if (values.length === 0) return;

      const employees = values.slice(1); // Skip headers
      for (const row of employees) {
        const username = row[2]?.replace('@', '');
        const fullName = row[1];
        const existingDates = (row[5] || '').split(',').map(s => s.trim());

        const shifts = await dbAll(
          "SELECT * FROM shifts WHERE username = ? AND shift_date = ? AND group_id = ?",
          [username, shiftDate, config.groupId]
        );

        if (shifts.length > 0 || existingDates.includes(`${shiftDate} (выходной)`)) continue;

        existingDates.push(`${shiftDate} (выходной)`);
        row[5] = existingDates.join('\n');
        row[6] = 'Выходной';
        await sheets.spreadsheets.values.update({
          spreadsheetId,
          range: `Timesheet!A${values.indexOf(row) + 2}:K${values.indexOf(row) + 2}`,
          valueInputOption: 'RAW',
          resource: { values: [row] },
        });

        const user = await dbGet(
          "SELECT DISTINCT username FROM shifts WHERE username = ? AND group_id = ?",
          [username, config.groupId]
        );
        if (user) {
          try {
            await bot.telegram.sendMessage(`@${username}`, `📌 Сегодня, ${shiftDate}, у вас был выходной.`);
          } catch (err) {
            logger.error(`Failed to notify ${username} about day off:`, err);
          }
        }
      }
    } catch (err) {
      logger.error('Day off cron error:', err);
    }
  }, { timezone: config.timezone });
});

// Photo handler
bot.on('photo', async (ctx) => {
  const username = ctx.from.username;
  const groupId = ctx.groupConfig.groupId;
  const timezone = ctx.groupConfig.timezone;

  if (!username) return ctx.reply('❌ Установите username в Telegram');
  if (!ctx.message.caption) return ctx.reply('❌ Отправьте фото с подписью');

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

  if (!isValidTime(startTime) || !isValidTime(endTime)) return ctx.reply('❌ Неверный формат времени (ЧЧ:ММ)');

  try {
    const existingShifts = await dbAll(
      "SELECT start_time, end_time FROM shifts WHERE username = ? AND shift_date = ? AND group_id = ?",
      [username, shiftDate, groupId]
    );

    for (const shift of existingShifts) {
      if ((startTime < shift.end_time) && (endTime > shift.start_time)) {
        return ctx.reply(`❌ Пересечение с существующей сменой ${shift.start_time}-${shift.end_time}`);
      }
    }

    const photoId = ctx.message.photo[ctx.message.photo.length - 1].file_id;
    await dbRun(
      `INSERT INTO shifts (group_id, username, full_name, photo_file_id, shift_date, start_time, end_time, zone, witag)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      [groupId, username, fullName, photoId, shiftDate, startTime, endTime, zone, witag]
    );

    const timesheetRange = 'Timesheet!A:K';
    await ensureSheetExists(spreadsheetId, 'Timesheet');
    const timesheetResponse = await sheets.spreadsheets.values.get({ spreadsheetId, range: timesheetRange });
    const timesheetValues = timesheetResponse.data.values || [];
    const existingRowIndex = timesheetValues.findIndex(row => row[2] === `@${username}`);

    if (existingRowIndex >= 0) {
      const existingDates = (timesheetValues[existingRowIndex][5] || '').split('\n').map(s => s.trim());
      if (!existingDates.includes(shiftDate)) {
        existingDates.push(shiftDate);
        timesheetValues[existingRowIndex][5] = existingDates.join('\n');
        timesheetValues[existingRowIndex][6] = 'Работал';
        timesheetValues[existingRowIndex][7] = startTime;
        timesheetValues[existingRowIndex][8] = endTime;
        timesheetValues[existingRowIndex][9] = '';
        timesheetValues[existingRowIndex][10] = '';
        await sheets.spreadsheets.values.update({
          spreadsheetId,
          range: `Timesheet!A${existingRowIndex + 2}:K${existingRowIndex + 2}`,
          valueInputOption: 'RAW',
          resource: { values: [timesheetValues[existingRowIndex]] },
        });
      }
    } else {
      const newRow = [
        timesheetValues.length + 1,
        fullName,
        `@${username}`,
        zone,
        witag,
        shiftDate,
        'Работал',
        startTime,
        endTime,
        '',
        ''
      ];
      await sheets.spreadsheets.values.append({
        spreadsheetId,
        range: timesheetRange,
        valueInputOption: 'RAW',
        resource: { values: [newRow] },
      });
    }

    // Update Report sheet
    const reportRange = 'Report!A1:AF1'; // A to AF for 31 days + Full Name + Summary
    await ensureSheetExists(spreadsheetId, 'Report');
    const reportResponse = await sheets.spreadsheets.values.get({ spreadsheetId, range: reportRange });
    const reportValues = reportResponse.data.values || [];
    const month = new Date().getMonth() + 1;
    const reportRowIndex = reportValues.findIndex(row => row[0] === fullName);

    if (reportRowIndex >= 0) {
      const dayIndex = new Date(shiftDate.split('.').reverse().join('-')).getDate();
      reportValues[reportRowIndex][dayIndex] = 'вышел';
      reportValues[reportRowIndex][32] = reportValues[reportRowIndex].slice(1, 32).filter(d => d === 'вышел').length;
      await sheets.spreadsheets.values.update({
        spreadsheetId,
        range: `Report!A${reportRowIndex + 2}:AF${reportRowIndex + 2}`,
        valueInputOption: 'RAW',
        resource: { values: [reportValues[reportRowIndex]] },
      });
    } else {
      const newReportRow = [fullName, ...Array(31).fill(''), ''];
      const dayIndex = new Date(shiftDate.split('.').reverse().join('-')).getDate();
      newReportRow[dayIndex] = 'вышел';
      newReportRow[32] = 1;
      await sheets.spreadsheets.values.append({
        spreadsheetId,
        range: reportRange,
        valueInputOption: 'RAW',
        resource: { values: [newReportRow] },
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
    const reportRange = 'Report!A2:AF';
    const response = await sheets.spreadsheets.values.get({ spreadsheetId, range: reportRange });
    const values = response.data.values || [];
    if (!values.length) return ctx.reply('📄 Отчет пуст');

    let message = '📊 *Отчет по сменам*\n';
    values.forEach(row => {
      message += `\n📅 *${row[0]}*: ${row.slice(1, 32).map((d, i) => d ? `${i + 1} ${d}` : '').filter(Boolean).join(', ')} (Всего: ${row[32] || 0})\n`;
    });

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

    let message = '📋 *Активные смены*\n';
    shifts.forEach(shift => {
      message += `\n🆔 *${shift.id}* @${escapeMarkdownV2(shift.username)} (${escapeMarkdownV2(shift.full_name)})\n`;
      message += `📅 ${escapeMarkdownV2(shift.shift_date)} ⏰ ${escapeMarkdownV2(shift.start_time)}-${escapeMarkdownV2(shift.end_time)}\n`;
      message += `📍 ${escapeMarkdownV2(shift.zone)}${shift.witag && shift.witag !== 'Нет' ? ` 🔖 ${escapeMarkdownV2(shift.witag)}` : ''}`;
    });

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
    const timesheetRange = 'Timesheet!A2:K';
    const response = await sheets.spreadsheets.values.get({ spreadsheetId, range: timesheetRange });
    const values = response.data.values || [];
    if (!values.length) return ctx.reply('📄 Табель пуст');

    let message = '📝 *Табель учета*\n```';
    values.forEach(row => {
      message += `\n${row[0]}. ${row[1]} (@${row[2]})\nЗона: ${row[3]}${row[4] && row[4] !== 'Нет' ? `, Witag: ${row[4]}` : ''}\nДни: ${row[5]}\nСтатус: ${row[6]}\nВремя: ${row[7]}-${row[8]}${row[9] ? ` (Факт: ${row[9]})` : ''}, Отработано: ${row[10] || ''}`;
    });
    message += '```';

    await ctx.replyWithMarkdownV2(message);
  } catch (err) {
    logger.error('timesheet error:', err);
    await ctx.reply('❌ Ошибка табеля');
  }
});

bot.action(['manual_end_shift_menu'], async (ctx) => {
  const username = ctx.from.username;
  const groupId = ctx.groupConfig.groupId;

  if (!username || !isAdmin(username, ctx.groupConfig.adminUsernames)) {
    return ctx.answerCbQuery('🚫 Доступ запрещен');
  }

  try {
    const shifts = await dbAll(
      "SELECT id, username, full_name, start_time, end_time FROM shifts WHERE status = 'active' AND group_id = ?",
      [groupId]
    );

    if (!shifts.length) return ctx.reply('📄 Активных смен нет');

    const buttons = shifts.map(shift => [
      Markup.button.callback(
        `${shift.id} @${escapeMarkdownV2(shift.username)} ${shift.start_time}-${shift.end_time}`,
        `manual_end_shift_${shift.id}`
      )
    ]);

    await ctx.reply(
      'Выберите смену для ручного завершения:',
      Markup.inlineKeyboard(buttons)
    );
  } catch (err) {
    logger.error('shift menu error:', err);
    await ctx.reply('❌ Ошибка выбора смены');
  }
});

bot.action(/^(manual_end_shift_)(\d+)$/, async (ctx) => {
  const username = ctx.from.username;
  const groupId = ctx.groupConfig.groupId;

  if (!username || !isAdmin(username, ctx.groupConfig.adminUsernames)) {
    return ctx.answerCbQuery('🚫 Доступ запрещен');
  }

  const shiftId = parseInt(ctx.match[2]);

  try {
    const shift = await dbGet(
      "SELECT username, full_name, start_time, end_time FROM shifts WHERE id = ? AND group_id = ?",
      [shiftId, groupId]
    );

    if (!shift) return ctx.reply('❌ Смена не найдена');

    await ctx.replyWithMarkdownV2(
      `Введите время фактического завершения для смены (ID: ${shiftId}):\\n` +
      `@${escapeMarkdownV2(shift.username)} (${escapeMarkdownV2(shift.full_name)}) ` +
      `${escapeMarkdownV2(shift.start_time)}-${escapeMarkdownV2(shift.end_time)}\\n` +
      `Формат: ЧЧ:ММ (например, 18:59)`,
      Markup.forceReply()
    );
    ctx.session = { awaitingManualEnd: true, shiftId };
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
    if (!isValidTime(actualEndTime)) return ctx.reply('❌ Неверный формат времени (ЧЧ:ММ)');

    try {
      const shift = await dbGet(
        "SELECT start_time, full_name, username FROM shifts WHERE id = ? AND group_id = ?",
        [shiftId, groupId]
      );

      if (!shift) return ctx.reply('❌ Смена не найдена');

      const workedHours = calculateWorkedHours(shift.start_time, actualEndTime);
      await dbRun(
        "UPDATE shifts SET status = ?, actual_end_time = ?, worked_hours = ? WHERE id = ? AND group_id = ?",
        ['completed', actualEndTime, workedHours, shiftId, groupId]
      );

      // Update Timesheet
      const timesheetRange = 'Timesheet!A:K';
      const timesheetResponse = await sheets.spreadsheets.values.get({ spreadsheetId, range: timesheetRange });
      const timesheetValues = timesheetResponse.data.values || [];
      const existingRowIndex = timesheetValues.findIndex(row => row[2] === `@${shift.username}`);
      if (existingRowIndex >= 0) {
        timesheetValues[existingRowIndex][6] = 'Прерван';
        timesheetValues[existingRowIndex][9] = actualEndTime;
        timesheetValues[existingRowIndex][10] = workedHours;
        await sheets.spreadsheets.values.update({
          spreadsheetId,
          range: `Timesheet!A${existingRowIndex + 2}:K${existingRowIndex + 2}`,
          valueInputOption: 'RAW',
          resource: { values: [timesheetValues[existingRowIndex]] },
        });
      }

      // Log to shift_fail
      const actionDate = format(new Date(), 'dd.MM.yyyy HH:mm');
      const newFailRow = [
        shiftId,
        shift.full_name,
        shift.username,
        timesheetValues[existingRowIndex][5].split('\n').pop(),
        shift.start_time,
        actualEndTime,
        workedHours,
        actionDate,
        username
      ];
      await sheets.spreadsheets.values.append({
        spreadsheetId,
        range: 'shift_fail!A:I',
        valueInputOption: 'RAW',
        resource: { values: [newFailRow] },
      });

      await ctx.replyWithMarkdownV2(
        `✅ Смена завершена вручную.\n` +
        `🆔 *${shiftId}* @${escapeMarkdownV2(shift.username)} (${escapeMarkdownV2(shift.full_name)})\n` +
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

// New action for /shifts report
bot.action('show_shifts_report', async (ctx) => {
  const username = ctx.from.username;
  const groupId = ctx.groupConfig.groupId;

  if (!username || !isAdmin(username, ctx.groupConfig.adminUsernames)) {
    return ctx.answerCbQuery('🚫 Доступ запрещен');
  }

  try {
    const shifts = await dbAll(
      "SELECT id, full_name, username, shift_date, start_time, end_time, status FROM shifts WHERE group_id = ? ORDER BY shift_date, start_time",
      [groupId]
    );

    if (!shifts.length) return ctx.reply('📄 Нет зарегистрированных смен.');

    let message = '📋 *Отчёт по сменам (/shifts)*\n';
    shifts.forEach(shift => {
      message += `\n🆔 *${shift.id}* @${escapeMarkdownV2(shift.username)} (${escapeMarkdownV2(shift.full_name)})\n`;
      message += `📅 ${escapeMarkdownV2(shift.shift_date)} ⏰ ${escapeMarkdownV2(shift.start_time)}-${escapeMarkdownV2(shift.end_time)}\n`;
      message += `Статус: ${escapeMarkdownV2(shift.status)}`;
    });

    await ctx.replyWithMarkdownV2(message);
  } catch (err) {
    logger.error('show_shifts_report error:', err);
    await ctx.reply('❌ Ошибка отчёта /shifts');
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

        const timesheetRange = 'Timesheet!A:K';
        const timesheetResponse = await sheets.spreadsheets.values.get({ spreadsheetId, range: timesheetRange });
        const timesheetValues = timesheetResponse.data.values || [];
        const existingRowIndex = timesheetValues.findIndex(row => row[2] === `@${shift.username}`);
        if (existingRowIndex >= 0) {
          timesheetValues[existingRowIndex][6] = 'Работал';
          timesheetValues[existingRowIndex][10] = workedHours;
          await sheets.spreadsheets.values.update({
            spreadsheetId,
            range: `Timesheet!A${existingRowIndex + 2}:K${existingRowIndex + 2}`,
            valueInputOption: 'RAW',
            resource: { values: [timesheetValues[existingRowIndex]] },
          });
        }
      }
    } catch (err) {
      logger.error('Auto-complete shifts error:', err);
    }
  }, { timezone: config.timezone });
});

bot.catch((err, ctx) => {
  logger.error(`Error for ${ctx.updateType}:`, err);
  ctx.reply('❌ Произошла ошибка');
});

bot.launch().then(() => logger.info('Bot started'));
process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));
