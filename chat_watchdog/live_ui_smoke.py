from __future__ import annotations

import argparse
from contextlib import suppress
import json
import time

from .model import Phase
from .relay_cdp import RelayCdpProtocol
from .relay_page import RelayChatGPTPage, _build_submit_expression, discover_websocket_url


DEFAULT_FIRST_PROMPT = "Write the numbers 1 through 400, one per line."
DEFAULT_FOLLOWUP_PROMPT = "Reply exactly WATCHDOG_LIVE_SMOKE_OK."


def _wait_for_composer(page: RelayChatGPTPage, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    expression = """
(() => {
  const editor = document.querySelector('#prompt-textarea') ||
    document.querySelector('[contenteditable="true"][data-lexical-editor="true"]') ||
    document.querySelector('div[contenteditable="true"]');
  if (!editor) return false;
  const style = window.getComputedStyle(editor);
  const rect = editor.getBoundingClientRect();
  return style.display !== 'none' &&
    style.visibility !== 'hidden' &&
    rect.width > 0 &&
    rect.height > 0 &&
    editor.getAttribute('aria-disabled') !== 'true';
})()
""".strip()
    while time.monotonic() < deadline:
        if page.protocol.evaluate(page.session_id, expression):
            return
        time.sleep(0.1)
    raise RuntimeError("composer did not become ready")


def _wait_for_active_turn(page: RelayChatGPTPage, timeout: float):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = page.snapshot()
        if (
            "/c/" in page.current_url()
            and last.phase in (Phase.THINKING, Phase.RESPONDING)
            and last.turn_key
        ):
            return last
        time.sleep(0.05)
    raise RuntimeError(
        "assistant never reached a live active turn with stable identity; "
        f"last_phase={getattr(last, 'phase', None)!r} "
        f"last_turn={getattr(last, 'turn_key', '')!r}"
    )


def run_live_smoke(
    *,
    relay_url: str,
    first_prompt: str,
    followup_prompt: str,
    timeout: float,
    keep_tab: bool,
) -> dict:
    import websocket

    socket = websocket.create_connection(
        discover_websocket_url(relay_url),
        timeout=3.0,
        suppress_origin=True,
    )
    protocol = RelayCdpProtocol(socket, request_timeout=8.0)
    target_id = protocol.create_target("https://chatgpt.com/")
    session_id = protocol.attach_target(target_id)
    page = RelayChatGPTPage(
        target_id=target_id,
        target_url="https://chatgpt.com/",
        session_id=session_id,
        socket=socket,
        protocol=protocol,
        match_url="https://chatgpt.com/",
    )
    try:
        _wait_for_composer(page, timeout)
        initial = page.protocol.evaluate(
            page.session_id,
            _build_submit_expression(first_prompt),
            await_promise=True,
        )
        if not isinstance(initial, dict) or initial.get("submitted") is not True:
            raise RuntimeError(f"initial prompt was not submitted: {initial!r}")

        active = _wait_for_active_turn(page, timeout)
        submit_results: list[dict] = []
        original_evaluate = page.protocol.evaluate

        def recording_evaluate(session_id: str, expression: str, *, await_promise: bool = False):
            result = original_evaluate(
                session_id,
                expression,
                await_promise=await_promise,
            )
            if followup_prompt in expression and isinstance(result, dict):
                submit_results.append(result)
            return result

        page.protocol.evaluate = recording_evaluate  # type: ignore[method-assign]
        delivery = page.send_simple_continue(
            followup_prompt,
            active.turn_key,
            acceptance_timeout=timeout,
        )
        page.protocol.evaluate = original_evaluate  # type: ignore[method-assign]

        if not delivery.accepted:
            raise RuntimeError(
                "follow-up delivery was not accepted: "
                f"stale={delivery.stale} uncertain={delivery.uncertain}"
            )

        after = page.snapshot()
        submit = submit_results[-1] if submit_results else {}
        return {
            "ok": True,
            "conversation_url": page.current_url(),
            "initial_submit_path": initial.get("path"),
            "followup_submit_path": submit.get("path"),
            "followup_stopped": bool(submit.get("stopped")),
            "active_phase": active.phase.value,
            "active_assistant_turn_id": active.turn_key,
            "delivery_message_id": delivery.message_id,
            "receipt_seq": after.submission_receipt_seq,
            "receipt_id": after.submission_receipt_id,
            "receipt_text": after.submission_receipt_text,
            "user_text": after.user_text,
            "composer_has_draft": after.composer_has_draft,
        }
    finally:
        if not keep_tab:
            with suppress(Exception):
                protocol.command("Target.closeTarget", {"targetId": target_id})
        page.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Live ChatGPT Web gate: create an isolated conversation, start a "
            "streaming response, then verify Watchdog follow-up delivery."
        )
    )
    parser.add_argument("--relay-url", default="http://127.0.0.1:9224")
    parser.add_argument("--first-prompt", default=DEFAULT_FIRST_PROMPT)
    parser.add_argument("--followup-prompt", default=DEFAULT_FOLLOWUP_PROMPT)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--keep-tab", action="store_true")
    args = parser.parse_args()

    try:
        result = run_live_smoke(
            relay_url=args.relay_url,
            first_prompt=args.first_prompt,
            followup_prompt=args.followup_prompt,
            timeout=args.timeout,
            keep_tab=args.keep_tab,
        )
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
