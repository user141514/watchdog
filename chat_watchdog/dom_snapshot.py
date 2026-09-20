from __future__ import annotations


DOM_SNAPSHOT_JS = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const normalizeText = (value) => String(value || '').replace(/\r\n/g, '\n').trim();

  // Causal submission receipt. DOM cardinality and mounted-tail identity are
  // observations, not ordering. Record the UI send event first; only later
  // bind it to a newly mounted stable user message whose text matches the
  // submitted composer text and whose id was not mounted before the event.
  const receiptKey = '__chatWatchdogSubmissionReceiptV1';
  let submission = window[receiptKey];
  if (!submission || typeof submission !== 'object') {
    submission = {
      seq: 0,
      pendingSeq: 0,
      pendingText: '',
      pendingKnownIds: [],
      pendingAnchorAssistantId: '',
      pendingTrusted: false,
      receiptSeq: 0,
      receiptId: '',
      receiptText: '',
      trustedReceiptSeq: 0,
      trustedReceiptId: '',
      lastSignalAt: 0,
      installed: false,
    };
    window[receiptKey] = submission;
  }
  const messageUsers = () => Array.from(document.querySelectorAll('[data-message-author-role="user"]'));
  const messageAssistants = () => {
    let nodes = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
    if (!nodes.length) {
      nodes = Array.from(document.querySelectorAll(
        '[data-testid^="conversation-turn-"][data-turn="assistant"], article[data-turn="assistant"]'
      ));
    }
    return nodes;
  };
  const readComposerText = () => {
    const editor = document.querySelector('#prompt-textarea') ||
      document.querySelector('[contenteditable="true"][data-lexical-editor="true"]') ||
      document.querySelector('div[contenteditable="true"]');
    return editor
      ? normalizeText(typeof editor.value === 'string' ? editor.value : (editor.innerText || editor.textContent || ''))
      : '';
  };
  const recordSubmission = (trusted) => {
    const text = readComposerText();
    if (!text) return;
    const now = Date.now();
    if (
      submission.pendingSeq
      && submission.pendingText === text
      && now - Number(submission.lastSignalAt || 0) < 250
    ) {
      if (trusted) submission.pendingTrusted = true;
      submission.lastSignalAt = now;
      return;
    }
    const currentAssistants = messageAssistants();
    const anchorAssistant = currentAssistants.length
      ? currentAssistants[currentAssistants.length - 1]
      : null;
    const anchorAssistantId = anchorAssistant?.getAttribute?.('data-message-id') || '';
    // Without an exact stable anchor, the mounted window cannot prove that a
    // later user node is causally after this submission event. Fail closed.
    if (!anchorAssistantId) return;
    submission.seq = Number(submission.seq || 0) + 1;
    submission.pendingSeq = submission.seq;
    submission.pendingText = text;
    submission.pendingKnownIds = messageUsers()
      .map((node) => node.getAttribute?.('data-message-id') || '')
      .filter(Boolean);
    submission.pendingAnchorAssistantId = anchorAssistantId;
    submission.pendingTrusted = trusted === true;
    submission.lastSignalAt = now;
  };
  if (!submission.installed && typeof document.addEventListener === 'function') {
    document.addEventListener('click', (event) => {
      const target = event?.target;
      const button = target?.closest?.('button') || (target?.tagName === 'BUTTON' ? target : null);
      if (!button) return;
      const label = normalizeText(button.getAttribute?.('aria-label') || button.textContent || '').toLowerCase();
      if (button.getAttribute?.('data-testid') === 'send-button' || label === 'send' || label.includes('send message') || label.includes('发送')) {
        recordSubmission(event?.isTrusted === true);
      }
    }, true);
    document.addEventListener('keydown', (event) => {
      if (
        event?.key !== 'Enter'
        || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey || event.isComposing
      ) return;
      const target = event.target;
      const inComposer = target?.matches?.('#prompt-textarea, [contenteditable="true"][data-lexical-editor="true"], div[contenteditable="true"]')
        || target?.closest?.('#prompt-textarea, [contenteditable="true"][data-lexical-editor="true"], div[contenteditable="true"]');
      if (inComposer) recordSubmission(event?.isTrusted === true);
    }, true);
    submission.installed = true;
  }

  const assistants = messageAssistants();
  const users = messageUsers();
  if (submission.pendingSeq && submission.pendingText && submission.pendingAnchorAssistantId) {
    const knownIds = new Set(Array.isArray(submission.pendingKnownIds) ? submission.pendingKnownIds : []);
    const anchorAssistant = assistants.find(
      (node) => (node.getAttribute?.('data-message-id') || '') === submission.pendingAnchorAssistantId
    );
    const receiptNode = anchorAssistant
      ? [...users].reverse().find((node) => {
          const id = node.getAttribute?.('data-message-id') || '';
          const text = normalizeText(node.innerText || node.textContent || '');
          const followsAnchor = typeof anchorAssistant.compareDocumentPosition === 'function'
            && (anchorAssistant.compareDocumentPosition(node) & 4) !== 0;
          return id && !knownIds.has(id) && text === submission.pendingText && followsAnchor;
        })
      : null;
    if (receiptNode) {
      const id = receiptNode.getAttribute?.('data-message-id') || '';
      submission.receiptSeq = Number(submission.pendingSeq || 0);
      submission.receiptId = id;
      submission.receiptText = submission.pendingText;
      if (submission.pendingTrusted) {
        submission.trustedReceiptSeq = submission.receiptSeq;
        submission.trustedReceiptId = id;
      }
      submission.pendingSeq = 0;
      submission.pendingText = '';
      submission.pendingKnownIds = [];
      submission.pendingAnchorAssistantId = '';
      submission.pendingTrusted = false;
    }
  }
  const assistant = assistants.length ? assistants[assistants.length - 1] : null;
  const user = users.length ? users[users.length - 1] : null;
  const userTurnPending = !!(user && (
    !assistant ||
    (typeof assistant.compareDocumentPosition === 'function' &&
      (assistant.compareDocumentPosition(user) & 4) !== 0)
  ));
  const userTurn = user
    ? (user.closest('[data-testid^="conversation-turn-"]') || user.closest('article[data-turn="user"]') || user)
    : null;
  const turn = assistant
    ? (assistant.closest('[data-testid^="conversation-turn-"]') || assistant.closest('article[data-turn="assistant"]') || assistant)
    : null;

  // A vanished stop button is not proof of a final response. A background
  // tab can retain only a streamed prefix after the server has finished.
  const assistantFinalized = !!(turn &&
    turn.querySelector('[data-testid="copy-turn-action-button"], [data-testid="feedback-turn-action-button"]'));

  const stop = document.querySelector('[data-testid="stop-button"]');
  const assistantBusy = !!(turn && (
    turn.getAttribute('aria-busy') === 'true' || turn.querySelector('[aria-busy="true"]')
  ));

  const thinkingCandidates = turn ? Array.from(turn.querySelectorAll(
    '[data-testid*="reasoning" i], [data-testid*="thinking" i], [aria-label*="thinking" i], [aria-label*="reasoning" i], button[aria-expanded="true"]'
  )) : [];
  const activeThinkingText = /(thinking|reasoning|思考中|正在思考|推理中)/i;
  const thinkingVisible = thinkingCandidates.some((el) => {
    if (!visible(el)) return false;
    const marker = `${el.getAttribute('data-testid') || ''} ${el.getAttribute('aria-label') || ''} ${el.textContent || ''}`;
    return activeThinkingText.test(marker);
  });

  const composer = document.querySelector('#prompt-textarea') ||
    document.querySelector('[contenteditable="true"][data-lexical-editor="true"]') ||
    document.querySelector('div[contenteditable="true"]');
  const composerReady = !!(composer && visible(composer) && composer.getAttribute('aria-disabled') !== 'true');
  const composerText = composer
    ? (typeof composer.value === 'string' ? composer.value : (composer.innerText || composer.textContent || ''))
    : '';
  const composerHasDraft = composerReady && composerText.trim().length > 0;
  const interactionRequired = Array.from(
    document.querySelectorAll('[data-testid="tool-approval-card"]')
  ).some(visible);

  const retryButtonPattern = /^(重试|retry|try again)$/i;
  const retryButtons = Array.from(document.querySelectorAll('button')).filter((button) => {
    if (!visible(button)) return false;
    const label = (button.innerText || button.getAttribute('aria-label') || button.textContent || '').trim();
    return retryButtonPattern.test(label);
  });
  const retryContexts = retryButtons.map((button) => {
    let node = button;
    for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
      const value = (node.innerText || node.textContent || '').trim();
      if (/(消息发送超时|消息流断开|message[^\n]{0,80}(?:timed out|timeout)|stream[^\n]{0,80}interrupted|response[^\n]{0,80}interrupted)/i.test(value)) {
        return value;
      }
    }
    return '';
  });
  const sendTimeout = retryContexts.some((value) =>
    /(消息发送超时|message[^\n]{0,80}(?:timed out|timeout))/i.test(value)
  );
  const streamInterrupted = retryContexts.some((value) =>
    /(消息流断开|stream[^\n]{0,80}interrupted|response[^\n]{0,80}interrupted)/i.test(value)
  );
  const faultText = retryContexts.find((value) => value) || '';

  const contentCandidates = turn ? [
    turn.querySelector('[data-message-content]'),
    turn.querySelector('.markdown'),
    turn.querySelector('.prose'),
    turn.querySelector('[class*="markdown"]'),
  ] : [];
  const contentRoot = contentCandidates.find((node) => {
    if (!node) return false;
    const value = node.innerText || node.textContent || '';
    return value.trim().length > 0;
  }) || null;

  const sanitizedTurnText = () => {
    if (!turn) return '';
    const clone = turn.cloneNode(true);
    clone.querySelectorAll(
      'button, textarea, [role="button"], [contenteditable="true"], [data-testid*="turn-action" i], [data-testid*="copy" i], [aria-label*="copy" i], [aria-label*="feedback" i]'
    ).forEach((node) => node.remove());
    return (clone.textContent || '').trim();
  };

  const contentText = contentRoot
    ? (contentRoot.innerText || contentRoot.textContent || '')
    : '';
  const fullTurnText = sanitizedTurnText();
  // Long assistant responses can be rendered as multiple markdown/prose
  // content blocks. querySelector() intentionally keeps ordinary progress
  // tracking cheap, but a terminal supervisor marker may live in a later
  // block. Scan the sanitized whole turn for terminal protocol lines so a
  // visible NEED_INPUT/DONE marker cannot be lost by first-block selection.
  const terminalProtocolPattern = /(?:^|\n)(?:SUPERVISOR_DONE|\[SUPERVISOR_STATE\s*:\s*(?:NEED_INPUT|DONE)\])\s*$/i;
  const text = terminalProtocolPattern.test(fullTurnText)
    ? fullTurnText
    : (contentText || fullTurnText);
  // Progress must cover the whole assistant turn. ChatGPT may split a long
  // response across multiple markdown/prose blocks; tracking only the first
  // block can falsely look stalled while later blocks are still growing.
  const signatureText = fullTurnText || text;
  const signatureRoot = turn || contentRoot || assistant;
  const htmlLength = signatureRoot ? String(signatureRoot.innerHTML || '').length : 0;
  const childCount = signatureRoot ? Number(signatureRoot.childElementCount || 0) : 0;
  const tail = signatureText.slice(-160);
  const signature = `${signatureText.length}:${htmlLength}:${childCount}:${tail}`;
  const stableTurnId = (node) => {
    if (!node) return '';
    const candidate = (node.getAttribute?.('data-turn-id') || '').trim();
    if (!candidate || /^request[-_:]/i.test(candidate) || /^conversation-turn-\d+$/i.test(candidate)) return '';
    return candidate;
  };
  const turnId = assistant?.getAttribute('data-message-id') || stableTurnId(turn);
  const userTurnId = user?.getAttribute('data-message-id') || stableTurnId(userTurn);
  const userText = user
    ? (user.innerText || user.textContent || userTurn?.textContent || '')
    : '';

  return {
    stopVisible: visible(stop),
    assistantBusy,
    assistantFinalized,
    userTurnPending,
    thinkingVisible,
    composerReady,
    composerHasDraft,
    interactionRequired,
    sendTimeout,
    streamInterrupted,
    faultText,
    assistantTurnId: turnId,
    assistantTextSignature: signature,
    assistantText: text,
    assistantCount: assistants.length,
    userCount: users.length,
    userTurnId,
    userText,
    submissionSeq: Number(submission.seq || 0),
    submissionReceiptSeq: Number(submission.receiptSeq || 0),
    submissionReceiptId: String(submission.receiptId || ''),
    submissionReceiptText: String(submission.receiptText || ''),
    trustedSubmissionReceiptSeq: Number(submission.trustedReceiptSeq || 0),
    trustedSubmissionReceiptId: String(submission.trustedReceiptId || ''),
  };
}
"""
