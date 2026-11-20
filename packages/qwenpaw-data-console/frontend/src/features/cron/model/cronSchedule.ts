import type { CronSchedule } from '../../console/api/cron';

export type CronFrequency =
  'interval' | 'hourly' | 'daily' | 'weekday' | 'weekly' | 'monthly' | 'custom';

export type CronIntervalUnit = 'minute' | 'hour';

export const CRON_DAYS = [
  { value: 'mon', label: '周一' },
  { value: 'tue', label: '周二' },
  { value: 'wed', label: '周三' },
  { value: 'thu', label: '周四' },
  { value: 'fri', label: '周五' },
  { value: 'sat', label: '周六' },
  { value: 'sun', label: '周日' },
] as const;

export type CronDay = (typeof CRON_DAYS)[number]['value'];

export interface CronPreset {
  frequency: CronFrequency;
  time: string;
  cron: string;
  minute: number;
  daysOfWeek: CronDay[];
  dayOfMonth: number;
  intervalValue: number;
  intervalUnit: CronIntervalUnit;
}

export const DEFAULT_CRON = '30 9 * * 1-5';

const DEFAULT_PRESET_FIELDS = {
  time: '09:30',
  minute: 0,
  daysOfWeek: ['mon'] as CronDay[],
  dayOfMonth: 1,
  intervalValue: 15,
  intervalUnit: 'minute' as CronIntervalUnit,
};

const CRON_DAY_ORDER = CRON_DAYS.map(({ value }) => value);
const CRON_DAY_ALIASES: Record<string, CronDay> = {
  '0': 'sun',
  '1': 'mon',
  '2': 'tue',
  '3': 'wed',
  '4': 'thu',
  '5': 'fri',
  '6': 'sat',
  '7': 'sun',
};

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

function parseTime(value: string): { hour: number; minute: number } {
  const match = /^(\d{2}):(\d{2})$/.exec(value);
  const hour = Number(match?.[1]);
  const minute = Number(match?.[2]);
  if (!match || hour > 23 || minute > 59) {
    throw new Error('执行时间无效');
  }
  return { hour, minute };
}

function parseCronNumber(value: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return parsed >= min && parsed <= max ? parsed : null;
}

function normalizeCronDay(value: string): CronDay | null {
  const normalized = value.toLowerCase();
  if (CRON_DAY_ORDER.includes(normalized as CronDay)) {
    return normalized as CronDay;
  }
  return CRON_DAY_ALIASES[normalized] ?? null;
}

function parseCronDays(value: string): CronDay[] | null {
  const selected = new Set<CronDay>();
  for (const segment of value.split(',')) {
    const [rawStart, rawEnd, ...rest] = segment.trim().split('-');
    if (!rawStart || rest.length) return null;
    const start = normalizeCronDay(rawStart);
    const end = rawEnd ? normalizeCronDay(rawEnd) : start;
    if (!start || !end) return null;
    const startIndex = CRON_DAY_ORDER.indexOf(start);
    const endIndex = CRON_DAY_ORDER.indexOf(end);
    if (startIndex < 0 || endIndex < startIndex) return null;
    CRON_DAY_ORDER.slice(startIndex, endIndex + 1).forEach((day) => selected.add(day));
  }
  return CRON_DAY_ORDER.filter((day) => selected.has(day));
}

function isWeekdaySelection(days: CronDay[]): boolean {
  return days.join(',') === 'mon,tue,wed,thu,fri';
}

export function parseCronPreset(value: string | null | undefined): CronPreset {
  const cron = value?.trim() || DEFAULT_CRON;
  const parts = cron.split(/\s+/);
  if (parts.length !== 5) {
    return { frequency: 'custom', cron, ...DEFAULT_PRESET_FIELDS };
  }
  const [minute, hour, day, month, dayOfWeek] = parts;
  const intervalMinuteMatch = /^\*\/(\d+)$/.exec(minute);
  const intervalHourMatch = /^\*\/(\d+)$/.exec(hour);
  if (day === '*' && month === '*' && dayOfWeek === '*') {
    const intervalMinutes = intervalMinuteMatch
      ? parseCronNumber(intervalMinuteMatch[1], 1, 59)
      : null;
    if (intervalMinutes && hour === '*') {
      return {
        frequency: 'interval',
        cron,
        ...DEFAULT_PRESET_FIELDS,
        intervalValue: intervalMinutes,
        intervalUnit: 'minute',
      };
    }
    const intervalHours = intervalHourMatch ? parseCronNumber(intervalHourMatch[1], 1, 23) : null;
    if (intervalHours && minute === '0') {
      return {
        frequency: 'interval',
        cron,
        ...DEFAULT_PRESET_FIELDS,
        intervalValue: intervalHours,
        intervalUnit: 'hour',
      };
    }
  }

  const parsedMinute = parseCronNumber(minute, 0, 59);
  const parsedHour = parseCronNumber(hour, 0, 23);
  if (parsedMinute !== null && hour === '*' && day === '*' && month === '*' && dayOfWeek === '*') {
    return {
      frequency: 'hourly',
      cron,
      ...DEFAULT_PRESET_FIELDS,
      minute: parsedMinute,
    };
  }

  if (parsedHour !== null && parsedMinute !== null) {
    const time = `${pad(parsedHour)}:${pad(parsedMinute)}`;
    if (day === '*' && month === '*') {
      if (dayOfWeek === '*') {
        return { frequency: 'daily', cron, ...DEFAULT_PRESET_FIELDS, time };
      }
      const daysOfWeek = parseCronDays(dayOfWeek);
      if (daysOfWeek?.length) {
        return {
          frequency: isWeekdaySelection(daysOfWeek) ? 'weekday' : 'weekly',
          cron,
          ...DEFAULT_PRESET_FIELDS,
          time,
          daysOfWeek,
        };
      }
    }
    const dayOfMonth = parseCronNumber(day, 1, 31);
    if (dayOfMonth !== null && month === '*' && dayOfWeek === '*') {
      return {
        frequency: 'monthly',
        cron,
        ...DEFAULT_PRESET_FIELDS,
        time,
        dayOfMonth,
      };
    }
  }
  return { frequency: 'custom', cron, ...DEFAULT_PRESET_FIELDS };
}

export interface BuildCronExpressionInput {
  frequency: CronFrequency;
  time: string;
  custom: string;
  minute: number;
  daysOfWeek: CronDay[];
  dayOfMonth: number;
  intervalValue: number;
  intervalUnit: CronIntervalUnit;
}

export function buildCronExpression(input: BuildCronExpressionInput): string {
  if (input.frequency === 'custom') {
    const cron = input.custom.trim();
    if (cron.split(/\s+/).filter(Boolean).length !== 5) {
      throw new Error('Cron 表达式必须包含 5 段：分 时 日 月 周');
    }
    return cron;
  }
  if (input.frequency === 'interval') {
    const max = input.intervalUnit === 'minute' ? 59 : 23;
    if (
      !Number.isInteger(input.intervalValue) ||
      input.intervalValue < 1 ||
      input.intervalValue > max
    ) {
      throw new Error(`间隔必须是 1-${max} 的整数`);
    }
    return input.intervalUnit === 'minute'
      ? `*/${input.intervalValue} * * * *`
      : `0 */${input.intervalValue} * * *`;
  }
  if (input.frequency === 'hourly') {
    if (!Number.isInteger(input.minute) || input.minute < 0 || input.minute > 59) {
      throw new Error('每小时的执行分钟无效');
    }
    return `${input.minute} * * * *`;
  }

  const { hour, minute } = parseTime(input.time);
  if (input.frequency === 'daily') return `${minute} ${hour} * * *`;
  if (input.frequency === 'weekday') return `${minute} ${hour} * * mon-fri`;
  if (input.frequency === 'weekly') {
    const days = CRON_DAY_ORDER.filter((day) => input.daysOfWeek.includes(day));
    if (!days.length) throw new Error('请至少选择一个星期');
    return `${minute} ${hour} * * ${days.join(',')}`;
  }
  if (!Number.isInteger(input.dayOfMonth) || input.dayOfMonth < 1 || input.dayOfMonth > 31) {
    throw new Error('每月执行日期无效');
  }
  return `${minute} ${hour} ${input.dayOfMonth} * *`;
}

interface DateParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
}

function partsInTimeZone(date: Date, timeZone: string): DateParts {
  const formatter = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  });
  const parts = Object.fromEntries(
    formatter
      .formatToParts(date)
      .filter((part) => part.type !== 'literal')
      .map((part) => [part.type, part.value]),
  );
  return {
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: Number(parts.hour),
    minute: Number(parts.minute),
  };
}

function partsAsUtc(parts: DateParts): number {
  return Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute);
}

export function wallTimeToIso(value: string, timeZone: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) throw new Error('执行日期与时间无效');
  const desired: DateParts = {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
    hour: Number(match[4]),
    minute: Number(match[5]),
  };
  const desiredUtc = partsAsUtc(desired);
  const desiredDate = new Date(desiredUtc);
  if (
    desired.month < 1 ||
    desired.month > 12 ||
    desired.day < 1 ||
    desired.day > 31 ||
    desired.hour > 23 ||
    desired.minute > 59 ||
    desiredDate.getUTCFullYear() !== desired.year ||
    desiredDate.getUTCMonth() + 1 !== desired.month ||
    desiredDate.getUTCDate() !== desired.day
  ) {
    throw new Error('执行日期与时间无效');
  }

  let instant = desiredUtc;
  for (let iteration = 0; iteration < 3; iteration += 1) {
    const adjustment = desiredUtc - partsAsUtc(partsInTimeZone(new Date(instant), timeZone));
    instant += adjustment;
    if (adjustment === 0) break;
  }
  if (partsAsUtc(partsInTimeZone(new Date(instant), timeZone)) !== desiredUtc) {
    throw new Error('所选时间在该时区中不存在');
  }
  return new Date(instant).toISOString();
}

export function isoToWallTimeInput(value: string | null | undefined, timeZone: string): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const parts = partsInTimeZone(date, timeZone);
  return `${parts.year}-${pad(parts.month)}-${pad(parts.day)}T${pad(parts.hour)}:${pad(parts.minute)}`;
}

export function formatCronSchedule(schedule: CronSchedule, language: 'zh' | 'en' = 'zh'): string {
  const locale = language === 'zh' ? 'zh-CN' : 'en-US';
  if (schedule.type === 'once') {
    if (!schedule.run_at) return language === 'zh' ? '单次执行' : 'Run once';
    const date = new Date(schedule.run_at);
    if (Number.isNaN(date.getTime())) return language === 'zh' ? '单次执行' : 'Run once';
    return `${language === 'zh' ? '单次' : 'Once'} · ${new Intl.DateTimeFormat(locale, {
      timeZone: schedule.timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).format(date)}`;
  }
  const preset = parseCronPreset(schedule.cron);
  if (preset.frequency === 'interval') {
    if (language === 'en') {
      const unit = preset.intervalUnit === 'minute' ? 'minute' : 'hour';
      return `Every ${preset.intervalValue} ${unit}${preset.intervalValue === 1 ? '' : 's'}`;
    }
    return `每 ${preset.intervalValue} ${preset.intervalUnit === 'minute' ? '分钟' : '小时'}`;
  }
  if (preset.frequency === 'hourly') {
    return language === 'zh'
      ? `每小时第 ${pad(preset.minute)} 分`
      : `Hourly at :${pad(preset.minute)}`;
  }
  if (preset.frequency === 'daily')
    return language === 'zh' ? `每天 ${preset.time}` : `Daily at ${preset.time}`;
  if (preset.frequency === 'weekday')
    return language === 'zh' ? `工作日 ${preset.time}` : `Weekdays at ${preset.time}`;
  if (preset.frequency === 'weekly') {
    const dayLabels = (
      language === 'zh'
        ? CRON_DAYS
        : ([
            { value: 'mon', label: 'Mon' },
            { value: 'tue', label: 'Tue' },
            { value: 'wed', label: 'Wed' },
            { value: 'thu', label: 'Thu' },
            { value: 'fri', label: 'Fri' },
            { value: 'sat', label: 'Sat' },
            { value: 'sun', label: 'Sun' },
          ] as const)
    )
      .filter(({ value }) => preset.daysOfWeek.includes(value))
      .map(({ label }) => (language === 'zh' ? label.replace('周', '') : label))
      .join(language === 'zh' ? '、' : ', ');
    return language === 'zh'
      ? `每周${dayLabels} ${preset.time}`
      : `Weekly on ${dayLabels} at ${preset.time}`;
  }
  if (preset.frequency === 'monthly') {
    return language === 'zh'
      ? `每月 ${preset.dayOfMonth} 日 ${preset.time}`
      : `Monthly on day ${preset.dayOfMonth} at ${preset.time}`;
  }
  return `Cron · ${preset.cron}`;
}
