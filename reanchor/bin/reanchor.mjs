#!/usr/bin/env node
import { readFile, stat } from 'node:fs/promises';
import { Reanchor } from '../src/engine.mjs';
import { fail } from '../src/store.mjs';

const HELP = `reanchor <command> --store <explicit-memory-directory> [--input <request.json>] [--prompt]

Commands: create, user, observe, bind, handoff, prepare, claim, delivered, accept, cancel, status, source
Input: one JSON request from --input or stdin. No command strings are evaluated.
--prompt: output only the generated prompt (prepare or handoff).
--help: this message.

Example (Windows/Linux; no embedded JSON shell quoting):
  node bin/reanchor.mjs create --store <mymem>/projects/my-project/reanchor --input examples/create.json
  node bin/reanchor.mjs prepare --store <mymem>/projects/my-project/reanchor --input examples/prepare.json --prompt

The returned model state is an advisory claim, never verified semantic completion.
An uncertain claimed delivery is not safe to resend automatically.
`;
const METHODS = { create: 'create', user: 'recordUser', observe: 'recordObservation', bind: 'bindContext',
  handoff: 'handoff', prepare: 'prepare', claim: 'claimDelivery', delivered: 'recordDelivery', accept: 'accept',
  cancel: 'cancel', status: 'status', source: 'source' };
async function readInput(file) {
  const limit = 2 * 1024 * 1024;
  if (file) {
    if ((await stat(file)).size > limit) fail('INPUT_LIMIT', 'Input file exceeds 2 MiB');
    return readFile(file, 'utf8');
  }
  const chunks = []; let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length; if (size > limit) fail('INPUT_LIMIT', 'stdin exceeds 2 MiB'); chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}
async function main() {
  const args = process.argv.slice(2);
  if (args.length === 1 && ['--help', '-h'].includes(args[0])) { process.stdout.write(HELP); return; }
  const command = args.shift();
  if (!Object.hasOwn(METHODS, command)) fail('USAGE', 'Unknown command. Use --help');
  const options = {};
  while (args.length) {
    const option = args.shift();
    if (!['--store', '--input', '--prompt'].includes(option) || Object.hasOwn(options, option)) fail('USAGE', `Invalid/duplicate option: ${option}`);
    if (option === '--prompt') { options[option] = true; continue; }
    const value = args.shift(); if (!value || value.startsWith('--')) fail('USAGE', `Value required for ${option}`);
    options[option] = value;
  }
  if (!options['--store'] || (options['--prompt'] && !['prepare', 'handoff'].includes(command))) fail('USAGE', '--store is required; --prompt only supports prepare or handoff');
  let data;
  try { data = JSON.parse(await readInput(options['--input'])); }
  catch (error) { if (error.code) throw error; fail('INVALID_JSON', error.message); }
  if (!data || typeof data !== 'object' || Array.isArray(data)) fail('INVALID_INPUT', 'Request must be one JSON object');
  const engine = new Reanchor(options['--store']);
  let result;
  if (command === 'status') {
    const s = await engine.status(data.scope);
    result = { scope: s.scope ?? null, owner: s.owner ?? null, target: s.target ?? null, context: s.context ?? null,
      tip: s.tip, directives: s.directives.length, observations: s.observations.length, checkpoint: s.checkpoint,
      pending: s.pending ? { packetId: s.pending.packet.id, stage: s.pending.stage, messageId: s.pending.messageId } : null,
      deliveryMessageIds: s.deliveryMessageIds, lastRejection: s.lastRejection,
      authorityChangedSinceCheckpoint: s.authorityChangedSinceCheckpoint };
  } else { result = await engine[METHODS[command]](data); }
  if (options['--prompt']) {
    if (!result.prompt || (result.deliveryStage && result.deliveryStage !== 'PREPARED')) fail('NOT_READY', 'No safe unsent prompt is available');
    process.stdout.write(result.prompt + '\n');
  } else process.stdout.write(JSON.stringify(result, null, 2) + '\n');
}
main().catch(error => {
  process.stderr.write(JSON.stringify({ error: { code: error.code ?? 'INTERNAL_ERROR', message: error.message, ...(error.details ? { details: error.details } : {}) } }) + '\n');
  process.exitCode = 1;
});
