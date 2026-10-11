/**
 * Tiny dependency-free structured logger.
 *
 * Production logs are one JSON object per line so a host like Render can index
 * them; LOG_PRETTY=true switches to a human format for local development.
 * Secrets are never logged: `redact()` is applied to every field value.
 */

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };

const SECRET_KEYS = /token|secret|key|authorization|password/i;

function redactValue(value, depth = 0) {
  if (value === null || value === undefined) return value;
  if (typeof value === 'string') return value.length > 400 ? `${value.slice(0, 400)}…` : value;
  if (typeof value === 'number' || typeof value === 'boolean') return value;
  if (value instanceof Error) {
    return { name: value.name, message: value.message, code: value.code };
  }
  if (depth > 3) return '[deep]';
  if (Array.isArray(value)) return value.slice(0, 20).map((item) => redactValue(item, depth + 1));
  if (typeof value === 'object') {
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      out[key] = SECRET_KEYS.test(key) ? '[redacted]' : redactValue(item, depth + 1);
    }
    return out;
  }
  return String(value);
}

export class Logger {
  constructor({ level = 'info', pretty = false, base = {} } = {}) {
    this.level = LEVELS[level] ?? LEVELS.info;
    this.pretty = pretty;
    this.base = base;
  }

  /** Returns a logger that adds `fields` to every line (e.g. a job id). */
  child(fields) {
    return new Logger({ level: nameOf(this.level), pretty: this.pretty, base: { ...this.base, ...fields } });
  }

  debug(message, fields) {
    this.#write('debug', message, fields);
  }

  info(message, fields) {
    this.#write('info', message, fields);
  }

  warn(message, fields) {
    this.#write('warn', message, fields);
  }

  error(message, fields) {
    this.#write('error', message, fields);
  }

  #write(level, message, fields) {
    if (LEVELS[level] < this.level) return;
    const payload = { ...this.base, ...(fields ? redactValue(fields) : {}) };
    const line = level === 'error' ? process.stderr : process.stdout;
    if (this.pretty) {
      const extra = Object.keys(payload).length > 0 ? ` ${JSON.stringify(payload)}` : '';
      line.write(`${new Date().toISOString()} ${level.toUpperCase().padEnd(5)} ${message}${extra}\n`);
      return;
    }
    line.write(
      `${JSON.stringify({
        ts: new Date().toISOString(),
        level,
        msg: message,
        ...payload
      })}\n`
    );
  }
}

function nameOf(value) {
  return Object.keys(LEVELS).find((key) => LEVELS[key] === value) ?? 'info';
}

export { redactValue };
