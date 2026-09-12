import { FileStore, hash, canonical, fail, ReanchorError } from './store.mjs';
import { buildHandoff, buildPacket, parseReceipt } from './protocol.mjs';
export { ReanchorError } from './store.mjs';

function text(value, name, max = 65536) {
  if (typeof value !== 'string' || !value.trim() || Buffer.byteLength(value) > max) fail('INVALID_INPUT', `${name} must be nonempty bounded text`);
  return value;
}
function contextValue(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('INVALID_CONTEXT', 'Explicit host/root/revision/epoch required');
  const keys = Object.keys(value).sort().join(',');
  if (keys !== 'epoch,host,revision,root') fail('INVALID_CONTEXT', 'context requires exactly host, root, revision and epoch');
  for (const k of ['host', 'root', 'epoch']) text(value[k], `context.${k}`, 4096);
  if (value.revision !== null) text(value.revision, 'context.revision', 4096);
  return structuredClone(value);
}
function userSource(source) {
  if (!source || Object.keys(source).sort().join(',') !== 'id,text') fail('INVALID_SOURCE', 'User source is exact {id,text} from trusted ingress');
  return { id: text(source.id, 'source.id', 256), text: text(source.text, 'source.text'), role: 'user' };
}
function observation(source) {
  if (!source || Object.keys(source).sort().join(',') !== 'id,origin,role,text' || !['assistant', 'tool', 'runtime', 'worker', 'memory'].includes(source.role)) fail('INVALID_SOURCE', 'Observation needs id/text/role/origin, and cannot be user authority');
  text(source.id, 'source.id', 256); text(source.text, 'source.text');
  if (!source.origin || typeof source.origin !== 'object' || Array.isArray(source.origin)) fail('INVALID_SOURCE', 'origin must describe the source as JSON data');
  if (Buffer.byteLength(canonical(source.origin)) > 8192) fail('INVALID_SOURCE', 'origin metadata too large');
  return structuredClone(source);
}
function reduce(view) {
  let state = {
    directives: [], observations: [], checkpoint: null, pending: null,
    replies: Object.create(null), deliveryMessageIds: [], lastRejection: null,
    authorityChangedSinceCheckpoint: false,
    tip: view.tip
  };
  for (const event of view.events) {
    const d = event.data;
    switch (event.kind) {
      case 'created': state = { ...state, scope: event.scope, owner: d.owner, target: d.target, context: d.context, directives: [d.source] }; break;
      case 'user':
        state.directives.push(d.source);
        state.lastRejection = null;
        if (state.checkpoint) state.authorityChangedSinceCheckpoint = true;
        break;
      case 'observation': state.observations.push(d.source); break;
      case 'context': state.context = d.context; state.lastRejection = null; break;
      case 'prepared': state.pending = { packet: d.packet, stage: 'PREPARED', messageId: null }; break;
      case 'delivery_claimed': state.pending.stage = 'SENDING'; break;
      case 'delivered':
        state.pending.stage = 'DELIVERED';
        state.pending.messageId = d.messageId;
        if (!state.deliveryMessageIds.includes(d.messageId)) state.deliveryMessageIds.push(d.messageId);
        break;
      case 'cancelled': state.pending = null; break;
      case 'accepted':
        state.checkpoint = { ...d.checkpoint, at: event.at };
        state.pending = null;
        state.lastRejection = null;
        state.authorityChangedSinceCheckpoint = false;
        state.replies[d.response.messageId] = d;
        break;
      case 'rejected':
        state.pending = null;
        state.lastRejection = {
          packetId: d.packetId,
          responseMessageId: d.response.messageId,
          code: d.result.code,
          message: d.result.message,
          at: event.at
        };
        state.replies[d.response.messageId] = d;
        break;
      default: fail('CORRUPT_STORE', `Unsupported event kind ${event.kind}`);
    }
  }
  return state;
}
function owned(state, owner) {
  if (!state.scope) fail('NOT_FOUND', 'Task not initialized');
  if (state.owner !== owner) fail('NOT_OWNER', 'Only the coordinator can mutate this scope');
}
function authorityHash(s) {
  return hash({ directives: s.directives, context: s.context, checkpoint: s.checkpoint, target: s.target });
}
function inputHash(s) {
  return hash({ directives: s.directives, observations: s.observations, context: s.context,
    checkpoint: s.checkpoint, target: s.target });
}
function pending(s, packetId) {
  if (!s.pending || s.pending.packet.id !== packetId) fail('STALE_PACKET', 'Packet is not current for this scope');
  return s.pending;
}
function fresh(s, p) { if (p.packet.basisHash !== inputHash(s)) fail('STALE_INPUT', 'User/source/context changed after preparing this packet'); }
function resultEvent(kind, data, result) { return { event: { kind, data }, result }; }
function addSource(s, source, kind) {
  const existing = [...s.directives, ...s.observations].find(x => x.id === source.id);
  if (existing) {
    if (canonical(existing) !== canonical(source)) fail('SOURCE_CONFLICT', `Source ID ${source.id} already contains different data`);
    return { result: { status: 'UNCHANGED', duplicate: true } };
  }
  return resultEvent(kind, { source }, { status: 'RECORDED', sourceId: source.id });
}

/** All semantic content is authored externally. Owner/role are trusted adapter assertions, not authentication. */
export class Reanchor {
  constructor(root) { this.store = new FileStore(root); }
  async status(scope) { return reduce(await this.store.read(scope)); }
  async create({ scope, owner, target, context, source }) {
    text(owner, 'owner', 256); text(target, 'target', 2048);
    const data = { owner, target, context: contextValue(context), source: userSource(source) };
    return this.store.transact(scope, view => {
      if (view.events.length) fail('ALREADY_EXISTS', 'Use the existing scope; never replace its original user source');
      return resultEvent('created', data, { status: 'CREATED', scope });
    });
  }
  async recordUser({ scope, owner, source }) {
    const value = userSource(source);
    return this.store.transact(scope, v => { const s = reduce(v); owned(s, owner); return addSource(s, value, 'user'); });
  }
  async recordObservation({ scope, owner, source }) {
    const value = observation(source);
    return this.store.transact(scope, v => { const s = reduce(v); owned(s, owner); return addSource(s, value, 'observation'); });
  }
  async bindContext({ scope, owner, context }) {
    const value = contextValue(context);
    return this.store.transact(scope, v => {
      const s = reduce(v); owned(s, owner);
      return canonical(s.context) === canonical(value) ? { result: { status: 'UNCHANGED' } } : resultEvent('context', { context: value }, { status: 'BOUND' });
    });
  }
  async source({ scope, id }) {
    const s = await this.status(scope);
    const found = [...s.directives, ...s.observations].find(x => x.id === id);
    if (!found) fail('SOURCE_NOT_FOUND', `Source is not in scope ${scope}`);
    return found;
  }
  async handoff({ scope, owner, maxBytes = 32768 }) {
    const s = await this.status(scope);
    owned(s, owner);
    const cursor = s.checkpoint?.observedThrough ?? 0;
    const priorEvidence = new Set(s.checkpoint?.evidenceIds ?? []);
    const observations = s.observations.filter((x, i) => i >= cursor || priorEvidence.has(x.id));
    const previousCheckpoint = s.checkpoint ? {
      ...s.checkpoint,
      contextMatches: canonical(s.checkpoint.context) === canonical(s.context),
      advisoryOnly: true
    } : null;
    return buildHandoff({
      scope,
      target: s.target,
      context: s.context,
      parentTip: s.tip,
      authorityChangedSinceCheckpoint: s.authorityChangedSinceCheckpoint,
      directives: s.directives,
      previousCheckpoint,
      observations
    }, maxBytes);
  }
  async prepare({ scope, owner, reason = 'manual', safe = false, maxBytes = 32768 }) {
    return this.store.transact(scope, v => {
      const s = reduce(v); owned(s, owner);
      if (safe !== true) return { result: { status: 'DEFERRED', reason: 'No safe conversation boundary confirmed' } };
      if (s.pending) {
        if (s.pending.packet.basisHash === inputHash(s) || s.pending.stage !== 'PREPARED') {
          return { result: { ...s.pending.packet, reused: true, deliveryStage: s.pending.stage, inputsChanged: s.pending.packet.basisHash !== inputHash(s) } };
        }
      }
      const cursor = s.checkpoint?.observedThrough ?? 0;
      const priorEvidence = new Set(s.checkpoint?.evidenceIds ?? []);
      const observations = s.observations.filter((x, i) => i >= cursor || priorEvidence.has(x.id));
      const previousCheckpoint = s.checkpoint ? { ...s.checkpoint, contextMatches: canonical(s.checkpoint.context) === canonical(s.context), advisoryOnly: true } : null;
      const packet = buildPacket({ scope, target: s.target, context: s.context, basisHash: inputHash(s), reason,
        authorityHash: authorityHash(s), observedThrough: s.observations.length,
        directives: s.directives, previousCheckpoint, observations }, maxBytes);
      return resultEvent('prepared', { packet }, packet);
    });
  }
  async claimDelivery({ scope, owner, packetId }) {
    return this.store.transact(scope, v => {
      const s = reduce(v); owned(s, owner); const p = pending(s, packetId); fresh(s, p);
      if (p.stage !== 'PREPARED') fail('DELIVERY_UNCERTAIN', 'Already claimed; never resend merely because acknowledgement is missing');
      return resultEvent('delivery_claimed', { packetId }, { status: 'CLAIMED', packetId });
    });
  }
  async recordDelivery({ scope, owner, packetId, target, messageId }) {
    text(messageId, 'messageId', 512);
    return this.store.transact(scope, v => {
      const s = reduce(v); owned(s, owner); const p = pending(s, packetId);
      if (target !== s.target) fail('TARGET_MISMATCH', 'Delivery belongs to a different target');
      if (p.stage === 'DELIVERED' && p.messageId === messageId) return { result: { status: 'DELIVERED', duplicate: true } };
      if (p.stage !== 'SENDING') fail('NOT_CLAIMED', 'Reserve delivery before the external send');
      // Do not require freshness: even a raced delivery must be recorded honestly.
      return resultEvent('delivered', { packetId, messageId }, { status: 'DELIVERED', packetId, messageId });
    });
  }
  async cancel({ scope, owner, packetId }) {
    return this.store.transact(scope, v => {
      const s = reduce(v); owned(s, owner); const p = pending(s, packetId);
      if (p.stage !== 'PREPARED') fail('DELIVERY_UNCERTAIN', 'Cannot discard an in-flight side effect without reconciliation');
      return resultEvent('cancelled', { packetId }, { status: 'CANCELLED' });
    });
  }
  async accept({ scope, owner, packetId, response }) {
    if (!response || typeof response !== 'object') fail('UNBOUND_REPLY', 'Authoritative response metadata required');
    text(response.messageId, 'response.messageId', 512);
    text(response.text, 'response.text', 1024 * 1024);
    return this.store.transact(scope, v => {
      const s = reduce(v); owned(s, owner);
      const responseHash = hash(response);
      const prior = s.replies[response.messageId];
      if (prior) {
        if (prior.packetId !== packetId || prior.responseHash !== responseHash) fail('REPLY_CONFLICT', 'Same response ID has different payload or packet');
        return { result: { status: 'DUPLICATE_REPLY', packetId, duplicate: true, semanticVerified: false } };
      }
      const p = pending(s, packetId);
      if (p.stage !== 'DELIVERED') fail('NOT_DELIVERED', 'No authoritative provider delivery ID recorded');
      if (response.role !== 'assistant' || response.final !== true || response.target !== s.target || response.replyTo !== p.messageId) fail('UNBOUND_REPLY', 'Reply must be final assistant response to the exact delivered message and target');
      let receipt;
      try {
        if (p.packet.authorityHash !== authorityHash(s)) fail('STALE_INPUT', 'User/context changed after preparing this packet');
        receipt = parseReceipt(response.text, packetId);
        const known = new Set([...s.directives, ...s.observations].map(x => x.id));
        if (receipt.evidenceIds.some(x => !known.has(x))) fail('UNKNOWN_EVIDENCE', 'Receipt cites a source not recorded in this task');
      } catch (error) {
        if (!(error instanceof ReanchorError)) throw error;
        const result = { status: 'REJECTED', packetId, code: error.code, message: error.message, semanticVerified: false };
        return resultEvent('rejected', { packetId, response, responseHash, result }, result);
      }
      const checkpoint = { ...receipt, packetId, context: s.context, observedThrough: p.packet.observedThrough,
        sourceMessageId: response.messageId, evidenceMeaning: 'references exist; semantic support not verified' };
      const result = { status: 'CLAIM_RECORDED', packetId, reportedState: receipt.state, semanticVerified: false };
      return resultEvent('accepted', { packetId, checkpoint, response, responseHash, result }, result);
    });
  }
}
