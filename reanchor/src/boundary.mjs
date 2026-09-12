import { fail } from './store.mjs';

/**
 * Provider-neutral seam. The host implements observe/send with authoritative
 * state and IDs; this module neither discovers nor operates a browser/agent.
 * A failed send remains uncertain; reconcile using transport history before
 * calling engine.recordDelivery. Never blindly retry a claimed packet.
 */
export async function prepareAndSend({ engine, scope, owner, reason = 'manual', transport, maxBytes = 32768 }) {
  if (!transport || typeof transport.observe !== 'function' || typeof transport.send !== 'function') {
    fail('INVALID_TRANSPORT', 'transport must provide observe() and send()');
  }
  async function observeAndRecord() {
    const state = await engine.status(scope);
    const observed = await transport.observe({ target: state.target });
    if (!observed || observed.target !== state.target) fail('TARGET_MISMATCH', 'Observed target does not match the registered scope');
    await engine.bindContext({ scope, owner, context: observed.context });
    const users = observed.userSources ?? []; const observations = observed.observations ?? [];
    if (!Array.isArray(users) || !Array.isArray(observations)) fail('INVALID_TRANSPORT', 'Source collections must be arrays');
    for (const source of users) await engine.recordUser({ scope, owner, source });
    for (const source of observations) await engine.recordObservation({ scope, owner, source });
    return observed;
  }
  const first = await observeAndRecord();
  const packet = await engine.prepare({ scope, owner, reason, safe: first.safe === true, maxBytes });
  if (packet.status === 'DEFERRED') return packet;
  if (packet.deliveryStage === 'SENDING') return { status: 'DELIVERY_UNCERTAIN', packetId: packet.id };
  if (packet.deliveryStage === 'DELIVERED') return { status: 'AWAITING_REPLY', packetId: packet.id };
  const last = await observeAndRecord();
  if (last.safe !== true) return { status: 'DEFERRED', packetId: packet.id };
  await engine.claimDelivery({ scope, owner, packetId: packet.id });
  try {
    const ack = await transport.send({ text: packet.prompt, expectedTarget: packet.target, idempotencyKey: packet.id });
    if (!ack || typeof ack !== 'object') fail('INVALID_TRANSPORT', 'Provider delivery identity required');
    return await engine.recordDelivery({ scope, owner, packetId: packet.id, target: ack.target, messageId: ack.messageId });
  } catch (error) {
    // Reservation remains durable even when the process exits on the next line.
    return { status: 'DELIVERY_UNCERTAIN', packetId: packet.id, error: String(error.message ?? error) };
  }
}
