/**
 * Local stand-in for the Telegram Serverless SDK.
 *
 * The real `sdk` module only exists inside Telegram's runtime, which would leave the
 * handlers completely untested. `tests/stubs/loader.mjs` maps `sdk` and `sdk/db` onto
 * this file so the real handler code can run under `node --test`.
 *
 * It implements the subset of the documented SDK that this bot uses:
 *   - db: table/integer/text/index/sql, eq/and/or/gt/lt/desc/asc/isNotNull,
 *     select/insert/update/delete builders with awaitable terminals, and $count;
 *   - api: a recording Bot API proxy that returns plausible results;
 *   - fetch: a programmable outbound HTTP stub.
 *
 * Behaviour that matters for correctness is copied from the docs, not invented:
 * every terminal is async, and `timestamp` columns hold Date objects.
 */

const TABLES = new Map();
const ROWS = new Map();

export class BotApiError extends Error {
  constructor(method, description, { code = 400, parameters = null } = {}) {
    super(`${method}: ${description}`);
    this.name = 'BotApiError';
    this.method = method;
    this.code = code;
    this.description = description;
    this.parameters = parameters;
  }
}

export class EndpointError extends Error {
  constructor(description, parameters = {}) {
    super(description);
    this.name = 'EndpointError';
    this.parameters = parameters;
  }
}

export class InputFile {
  constructor(bytes, filename, options = {}) {
    this.bytes = bytes;
    this.filename = filename;
    this.type = options.type;
  }
}

/* ------------------------------------------------------------------ schema DSL */

function columnBuilder(kind, name, options) {
  const descriptor = {
    kind,
    column: name ?? null,
    options,
    primaryKey(extra = {}) {
      descriptor.isPrimaryKey = true;
      descriptor.options = { ...descriptor.options, ...extra };
      return descriptor;
    },
    notNull() {
      descriptor.isNotNull = true;
      return descriptor;
    },
    unique() {
      descriptor.isUnique = true;
      return descriptor;
    },
    default(value) {
      descriptor.defaultValue = value;
      return descriptor;
    },
    constraint(text) {
      descriptor.extraConstraint = text;
      return descriptor;
    },
    generatedAlwaysAs() {
      return descriptor;
    },
    references() {
      throw new Error('references() is not supported: the runtime runs with foreign_keys off');
    },
    deprecated() {
      return descriptor;
    }
  };
  return descriptor;
}

export function integer(name, options = {}) {
  return columnBuilder('integer', name, options);
}
export function text(name, options = {}) {
  return columnBuilder('text', name, options);
}
export function real(name, options = {}) {
  return columnBuilder('real', name, options);
}
export function boolean(name, options = {}) {
  return columnBuilder('boolean', name, options);
}
export function json(name, options = {}) {
  return columnBuilder('json', name, options);
}
export function index(name) {
  const descriptor = { kind: 'index', name };
  descriptor.on = () => descriptor;
  descriptor.where = () => descriptor;
  return descriptor;
}
export function sql(strings, ...values) {
  return { kind: 'sql', strings, values };
}

/** Mirrors `table(name, columns, extras?)`: columns become `t.<jsKey>` column refs. */
export function table(name, columns, extras) {
  const descriptor = { __name: name, __columns: {} };
  for (const [key, column] of Object.entries(columns ?? {})) {
    descriptor[key] = { kind: 'column', table: name, key, column: column.column ?? key, options: column.options ?? {} };
    descriptor.__columns[key] = descriptor[key];
  }
  descriptor.__extras = typeof extras === 'function' ? extras(descriptor) : extras;
  TABLES.set(name, descriptor);
  if (!ROWS.has(name)) ROWS.set(name, []);
  return descriptor;
}

/* ------------------------------------------------------------------- operators */

export function eq(column, value) {
  return { op: 'eq', column, value };
}
export function ne(column, value) {
  return { op: 'ne', column, value };
}
export function gt(column, value) {
  return { op: 'gt', column, value };
}
export function gte(column, value) {
  return { op: 'gte', column, value };
}
export function lt(column, value) {
  return { op: 'lt', column, value };
}
export function lte(column, value) {
  return { op: 'lte', column, value };
}
export function isNull(column) {
  return { op: 'isNull', column };
}
export function isNotNull(column) {
  return { op: 'isNotNull', column };
}
export function and(...conditions) {
  return { op: 'and', conditions };
}
export function or(...conditions) {
  return { op: 'or', conditions };
}
export function desc(column) {
  return { op: 'desc', column };
}
export function asc(column) {
  return { op: 'asc', column };
}
export function inArray(column, values) {
  return { op: 'in', column, values };
}
export const count = () => ({ op: 'count' });
export const sum = () => ({ op: 'sum' });

function valueOf(condition, row) {
  const key = condition?.column?.key ?? condition?.column;
  return row[key];
}

function matches(row, condition) {
  if (!condition) return true;
  switch (condition.op) {
    case 'and':
      return condition.conditions.every((item) => matches(row, item));
    case 'or':
      return condition.conditions.some((item) => matches(row, item));
    case 'eq': {
      const left = valueOf(condition, row);
      const right = condition.value;
      if (left instanceof Date && right instanceof Date) return left.getTime() === right.getTime();
      return left === right;
    }
    case 'ne':
      return valueOf(condition, row) !== condition.value;
    case 'gt': {
      const left = valueOf(condition, row);
      const right = condition.value;
      const a = left instanceof Date ? left.getTime() : left;
      const b = right instanceof Date ? right.getTime() : right;
      return a > b;
    }
    case 'gte':
      return valueOf(condition, row) >= condition.value;
    case 'lt':
      return valueOf(condition, row) < condition.value;
    case 'lte':
      return valueOf(condition, row) <= condition.value;
    case 'isNull':
      return valueOf(condition, row) === null || valueOf(condition, row) === undefined;
    case 'isNotNull':
      return valueOf(condition, row) !== null && valueOf(condition, row) !== undefined;
    case 'in':
      return condition.values.includes(valueOf(condition, row));
    default:
      return true;
  }
}

/* --------------------------------------------------------------------- builders */

class SelectBuilder {
  constructor(projection) {
    this.projection = projection ?? null;
    this.table = null;
    this.conditions = [];
    this.order = [];
    this.limitValue = null;
    this.offsetValue = 0;
  }
  from(tableDescriptor) {
    this.table = tableDescriptor;
    return this;
  }
  where(...conditions) {
    this.conditions.push(...conditions);
    return this;
  }
  orderBy(...orders) {
    this.order.push(...orders);
    return this;
  }
  limit(value) {
    this.limitValue = value;
    return this;
  }
  offset(value) {
    this.offsetValue = value;
    return this;
  }
  #rows() {
    const all = ROWS.get(this.table.__name) ?? [];
    let rows = all.filter((row) => this.conditions.every((condition) => matches(row, condition)));

    for (const order of [...this.order].reverse()) {
      const key = order?.column?.key;
      const factor = order.op === 'desc' ? -1 : 1;
      rows = [...rows].sort((a, b) => {
        const left = a[key] instanceof Date ? a[key].getTime() : a[key];
        const right = b[key] instanceof Date ? b[key].getTime() : b[key];
        if (left === right) return 0;
        return left > right ? factor : -factor;
      });
    }

    if (this.offsetValue) rows = rows.slice(this.offsetValue);
    if (this.limitValue !== null) rows = rows.slice(0, this.limitValue);
    return rows;
  }
  async all() {
    return this.#rows().map((row) => ({ ...row }));
  }
  async get() {
    const row = this.#rows()[0];
    return row ? { ...row } : null;
  }
  async values() {
    return this.#rows().map((row) => Object.values(row));
  }
  then(resolve, reject) {
    return this.all().then(resolve, reject);
  }
}

class InsertBuilder {
  constructor(tableDescriptor) {
    this.table = tableDescriptor;
    this.payload = null;
  }
  values(payload) {
    this.payload = payload;
    return this;
  }
  returning() {
    return this;
  }
  async run() {
    const rows = ROWS.get(this.table.__name) ?? [];
    const list = Array.isArray(this.payload) ? this.payload : [this.payload];
    const inserted = [];
    for (const item of list) {
      const row = { ...item };
      // `mode: 'timestamp'` columns default to now, like `(unixepoch())` does.
      for (const [key, column] of Object.entries(this.table.__columns ?? {})) {
        if (column.options?.mode === 'timestamp' && row[key] === undefined) row[key] = new Date();
      }
      rows.push(row);
      inserted.push(row);
    }
    ROWS.set(this.table.__name, rows);
    return { rowsAffected: inserted.length, lastInsertRowid: rows.length, rows: inserted.map((row) => ({ ...row })) };
  }
  then(resolve, reject) {
    return this.run().then(resolve, reject);
  }
}

class UpdateBuilder {
  constructor(tableDescriptor) {
    this.table = tableDescriptor;
    this.changes = {};
    this.conditions = [];
  }
  set(changes) {
    this.changes = changes;
    return this;
  }
  where(...conditions) {
    this.conditions.push(...conditions);
    return this;
  }
  async run() {
    const rows = ROWS.get(this.table.__name) ?? [];
    let affected = 0;
    for (const row of rows) {
      if (!this.conditions.every((condition) => matches(row, condition))) continue;
      Object.assign(row, this.changes);
      affected += 1;
    }
    return { rowsAffected: affected, lastInsertRowid: 0, rows: [] };
  }
  then(resolve, reject) {
    return this.run().then(resolve, reject);
  }
}

class DeleteBuilder {
  constructor(tableDescriptor) {
    this.table = tableDescriptor;
    this.conditions = [];
  }
  where(...conditions) {
    this.conditions.push(...conditions);
    return this;
  }
  async run() {
    const rows = ROWS.get(this.table.__name) ?? [];
    const keep = rows.filter((row) => !this.conditions.every((condition) => matches(row, condition)));
    ROWS.set(this.table.__name, keep);
    return { rowsAffected: rows.length - keep.length, lastInsertRowid: 0, rows: [] };
  }
  then(resolve, reject) {
    return this.run().then(resolve, reject);
  }
}

export const db = {
  select: (projection) => new SelectBuilder(projection),
  insert: (tableDescriptor) => new InsertBuilder(tableDescriptor),
  update: (tableDescriptor) => new UpdateBuilder(tableDescriptor),
  delete: (tableDescriptor) => new DeleteBuilder(tableDescriptor),
  async $count(tableDescriptor, condition) {
    const rows = ROWS.get(tableDescriptor.__name) ?? [];
    return rows.filter((row) => matches(row, condition)).length;
  },
  async run() {
    return { rowsAffected: 0, lastInsertRowid: 0, rows: [] };
  },
  async all() {
    return [];
  },
  async get() {
    return null;
  }
};
export default db;

/* ------------------------------------------------------------------------- api */

const MAX_RECORDED_CALLS = 2_000;
const apiCalls = [];
const apiFailures = new Map();
let messageIdCounter = 1_000;

function record(method, params) {
  if (apiCalls.length < MAX_RECORDED_CALLS) apiCalls.push({ method, params: structuredCloneSafe(params) });
}

function structuredCloneSafe(value) {
  try {
    return JSON.parse(JSON.stringify(value));
  } catch {
    return value;
  }
}

export const api = new Proxy(
  {
    __calls: apiCalls,
    /** Makes the next call to `method` fail once. */
    __failOnce(method, { code = 400, description = 'Bad Request' } = {}) {
      apiFailures.set(method, { code, description });
    },
    __callsOf(method) {
      return apiCalls.filter((call) => call.method === method);
    },
    __lastOf(method) {
      const calls = apiCalls.filter((call) => call.method === method);
      return calls.length > 0 ? calls[calls.length - 1].params : null;
    },
    __reset() {
      apiCalls.length = 0;
      apiFailures.clear();
      messageIdCounter = 1_000;
    }
  },
  {
    get(target, property) {
      if (property in target) return target[property];
      if (typeof property !== 'string') return undefined;

      return async (params = {}) => {
        record(property, params);

        const failure = apiFailures.get(property);
        if (failure) {
          apiFailures.delete(property);
          throw new BotApiError(property, failure.description, { code: failure.code });
        }

        switch (property) {
          case 'sendMessage':
          case 'sendVideo':
          case 'sendAudio':
          case 'sendDocument':
          case 'sendPhoto':
            return { message_id: messageIdCounter++, chat: { id: params.chat_id }, text: params.text ?? params.caption };
          case 'editMessageText':
            return { message_id: params.message_id, chat: { id: params.chat_id }, text: params.text };
          case 'answerCallbackQuery':
          case 'deleteMessage':
          case 'sendChatAction':
            return true;
          case 'getMe':
            return { id: 1, username: 'stub_bot', is_bot: true };
          default:
            return { ok: true };
        }
      };
    }
  }
);

/* ----------------------------------------------------------------------- fetch */

const fetchCalls = [];
let fetchHandler = async () => {
  throw new Error('No fetch handler installed in the stub');
};

export const fetch = Object.assign(
  async (url, options = {}) => {
    fetchCalls.push({ url: String(url), options: structuredCloneSafe(options) });
    const response = await fetchHandler(String(url), options);
    if (response instanceof Error) throw response;
    return response;
  },
  {
    __calls: fetchCalls,
    __setHandler(handler) {
      fetchHandler = handler;
    },
    __reset() {
      fetchCalls.length = 0;
      fetchHandler = async () => {
        throw new Error('No fetch handler installed in the stub');
      };
    },
    __callsTo(fragment) {
      return fetchCalls.filter((call) => call.url.includes(fragment));
    },
    body: {
      json: (payload) => ({ type: 'json', payload }),
      text: (payload) => ({ type: 'text', payload }),
      form: (payload) => ({ type: 'form', payload })
    }
  }
);

export class StubFormData {
  #entries = [];
  append(key, value) {
    this.#entries.push([key, value]);
  }
  entries() {
    return this.#entries;
  }
}
export { StubFormData as FormData };

/* ----------------------------------------------------------------------- reset */

/** Clears every table and every recorded call. Called between tests. */
export function resetStub() {
  for (const name of ROWS.keys()) ROWS.set(name, []);
  api.__reset();
  fetch.__reset();
}

export function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return payload;
    },
    async text() {
      return JSON.stringify(payload);
    }
  };
}

export const __internals = { ROWS, TABLES, matches };
