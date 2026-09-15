from __future__ import annotations


DOM_SNAPSHOT_JS = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };

  let assistants = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
  if (!assistants.length) {
    assistants = Array.from(document.querySelectorAll(
      '[data-testid^="conversation-turn-"][data-turn="assistant"], article[data-turn="assistant"]'
    ));
  }
  const users = Array.from(document.querySelectorAll('[data-message-author-role="user"]'));
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

  const text = contentRoot
    ? (contentRoot.innerText || contentRoot.textContent || '')
    : sanitizedTurnText();
  const signatureRoot = contentRoot || turn || assistant;
  const htmlLength = signatureRoot ? signatureRoot.innerHTML.length : 0;
  const childCount = signatureRoot ? signatureRoot.childElementCount : 0;
  const tail = text.slice(-160);
  const signature = `${text.length}:${htmlLength}:${childCount}:${tail}`;
  const turnId = assistant?.getAttribute('data-message-id') || (turn
    ? (turn.getAttribute('data-turn-id') || turn.getAttribute('data-testid') || turn.id || `assistant-${assistants.length}`)
    : 'assistant-0');
  const userTurnId = user?.getAttribute('data-message-id') || (userTurn
    ? (userTurn.getAttribute('data-turn-id') || userTurn.getAttribute('data-testid') || userTurn.id || `user-${users.length}`)
    : 'user-0');
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
  };
}
"""
