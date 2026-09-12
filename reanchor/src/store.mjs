import { createHash, randomUUID } from 'node:crypto';
import { mkdir, readdir, readFile, lstat, open, rename, unlink, rmdir } from 'node:fs/promises';
import { resolve, join } from 'node:path';

export class ReanchorError extends Error {
  constructor(code, message, details = undefined) {
    super(message); this.name = 'ReanchorError'; this.code = code;
    if (details !== undefined) this.details = details;
  }
}
export function fail(code, message, details) { throw new ReanchorError(code, message, details); }
export function canonical(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number' && Number.isFinite(value)) return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype) {
    return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  }
  fail('INVALID_JSON', 'Only finite JSON data is permitted');
}
export function hash(value) { return createHash('sha256').update(canonical(value)).digest('hex'); }
export function checkScope(scope) {
  if (typeof scope !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$/.test(scope)) {
    fail('INVALID_SCOPE', 'scope must be a 1..64 character identifier, not a path');
  }
}
async function checkDirectory(path) {
  const stat = await lstat(path);
  if (!stat.isDirectory() || stat.isSymbolicLink()) fail('UNSAFE_PATH', `Not a real directory: ${path}`);
}

/** Immutable event chain. Local lock only; cross-host forks fail closed on read. */
export class FileStore {
  constructor(root) {
    if (typeof root !== 'string' || !root.trim()) fail('INVALID_STORE', 'Explicit memory directory required');
    this.root = resolve(root);
  }
  paths(scope) { checkScope(scope); return { task: join(this.root, scope), events: join(this.root, scope, 'events') }; }
  async read(scope) {
    const { task, events: dir } = this.paths(scope);
    let names;
    try {
      await checkDirectory(this.root); await checkDirectory(task); await checkDirectory(dir);
      names = await readdir(dir);
    } catch (error) { if (error.code === 'ENOENT') return { events: [], tip: null }; throw error; }
    names = names.filter(n => !n.startsWith('.') || !n.endsWith('.tmp'));
    if (names.length > 10000) fail('STORE_LIMIT', 'Too many events; archive this scope explicitly');
    const all = new Map(); let bytes = 0;
    for (const name of names) {
      if (!/^[a-f0-9]{64}\.json$/.test(name)) fail('CORRUPT_STORE', `Unexpected event filename: ${name}`);
      const path = join(dir, name); const stat = await lstat(path);
      if (!stat.isFile() || stat.isSymbolicLink()) fail('UNSAFE_PATH', 'Event must be a regular file');
      bytes += stat.size;
      if (stat.size > 1024 * 1024 || bytes > 64 * 1024 * 1024) fail('STORE_LIMIT', 'Memory exceeds bounded V0 read limit');
      let data;
      try { data = JSON.parse(await readFile(path, 'utf8')); }
      catch { fail('CORRUPT_STORE', `Unparseable event: ${name}`); }
      if (hash(data) !== name.slice(0, -5) || data.scope !== scope || data.schema !== 1 ||
          !Number.isInteger(data.seq) || typeof data.kind !== 'string' || !data.data || typeof data.data !== 'object') {
        fail('CORRUPT_STORE', `Invalid event or hash: ${name}`);
      }
      all.set(name.slice(0, -5), { ...data, id: name.slice(0, -5) });
    }
    if (!all.size) return { events: [], tip: null };
    const children = new Map();
    for (const event of all.values()) {
      if (event.parent !== null && !all.has(event.parent)) fail('CORRUPT_STORE', 'Missing predecessor');
      const list = children.get(event.parent) ?? []; list.push(event); children.set(event.parent, list);
    }
    if ((children.get(null) ?? []).length !== 1) fail('DIVERGED', 'Multiple or missing roots; do not choose by timestamp');
    for (const list of children.values()) if (list.length > 1) fail('DIVERGED', 'Concurrent histories need explicit reconciliation');
    const ordered = []; let next = children.get(null)[0];
    while (next) {
      if (next.seq !== ordered.length + 1 || ordered.length >= all.size) fail('CORRUPT_STORE', 'Invalid sequence/cycle');
      ordered.push(next); next = children.get(next.id)?.[0];
    }
    if (ordered.length !== all.size) fail('CORRUPT_STORE', 'Unreachable events');
    return { events: ordered, tip: ordered.at(-1).id };
  }
  async transact(scope, mutate) {
    const { task, events: dir } = this.paths(scope);
    await mkdir(this.root, { recursive: true }); await checkDirectory(this.root);
    await mkdir(task, { recursive: true }); await checkDirectory(task);
    await mkdir(dir, { recursive: true }); await checkDirectory(dir);
    const lock = join(task, '.scope-lock');
    try { await mkdir(lock); }
    catch (error) { if (error.code === 'EEXIST') fail('BUSY', 'Scope writer lock exists; do not auto-steal it'); throw error; }
    let temporary;
    try {
      const current = await this.read(scope);
      const change = await mutate(structuredClone(current));
      if (!change?.event) return change?.result;
      const event = { schema: 1, scope, seq: current.events.length + 1, parent: current.tip,
        at: new Date().toISOString(), kind: change.event.kind, data: change.event.data };
      const id = hash(event); const body = JSON.stringify(event, null, 2) + '\n';
      if (Buffer.byteLength(body) > 1024 * 1024) fail('STORE_LIMIT', 'One event exceeds 1 MiB');
      temporary = join(dir, `.${randomUUID()}.tmp`);
      const fd = await open(temporary, 'wx', 0o600);
      try { await fd.writeFile(body); await fd.sync(); } finally { await fd.close(); }
      await rename(temporary, join(dir, `${id}.json`)); temporary = undefined;
      // POSIX directory durability. Windows requires a separate host crash gate.
      if (process.platform !== 'win32') {
        const directory = await open(dir, 'r');
        try { await directory.sync(); } finally { await directory.close(); }
      }
      return change.result === undefined ? { ...event, id } : change.result;
    } finally {
      if (temporary) await unlink(temporary).catch(() => {});
      await rmdir(lock);
    }
  }
}
