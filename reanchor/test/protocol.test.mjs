import test from 'node:test';
import assert from 'node:assert/strict';
import { formatReceipt, parseReceipt } from '../src/protocol.mjs';

const packet = { id: '32ee4b10-1b43-427d-86de-b9a8bce928c' };
const other = { id: '8dded0c1-7c4f-45bc-bdf6-a22936bfcc90' };
const receipt = { state: 'COMPLETE', checkpoint: 'Controlled fixture returned READY.', evidenceIds: ['fixture-seed'] };

test('a valid receipt must echo the exact current packet nonce', () => {
  const text = formatReceipt(packet, receipt);
  assert.ok(text.startsWith(`[REANCHOR_RESULT: ${packet.id}]\n`));
  assert.deepEqual(parseReceipt(text, packet.id), receipt);
});

test('a receipt from a different packet is rejected even when its JSON is valid', () => {
  assert.throws(() => parseReceipt(formatReceipt(other, receipt), packet.id), { code: 'INVALID_RECEIPT' });
});

test('unbound JSON cannot replace the required nonce block', () => {
  assert.throws(() => parseReceipt(JSON.stringify({ kind: 'reanchor_result', ...receipt }), packet.id), { code: 'INVALID_RECEIPT' });
});

test('truncation, quoted examples, duplicates and trailing text cannot mark completion', () => {
  const valid = formatReceipt(packet, receipt);
  for (const text of [valid.slice(0, -4), `\`\`\`\n${valid}\n\`\`\``, `${valid}\n${valid}`, `${valid}\nextra`, `prefix ${valid}`]) {
    assert.throws(() => parseReceipt(text, packet.id), { code: 'INVALID_RECEIPT' });
  }
});

test('invalid state, unknown fields and duplicate evidence IDs are rejected', () => {
  for (const value of [
    { ...receipt, state: 'DONE' },
    { ...receipt, bypass: true },
    { ...receipt, evidenceIds: ['fixture-seed', 'fixture-seed'] },
  ]) assert.throws(() => parseReceipt(formatReceipt(packet, value), packet.id), { code: 'INVALID_RECEIPT' });
});
