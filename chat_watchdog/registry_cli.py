from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DEFAULT_CONTROL_URL = "http://127.0.0.1:9235"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chat-watchdog-registry",
        description="Register exact ChatGPT conversations with a running watchdog registry.",
    )
    parser.add_argument(
        "--control-url",
        default=os.environ.get("CHAT_WATCHDOG_CONTROL_URL", DEFAULT_CONTROL_URL),
        help=f"watchdog registry control URL; default: {DEFAULT_CONTROL_URL}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add", aliases=["start"], help="watch one exact ChatGPT conversation URL")
    add.add_argument("url")
    add.add_argument("--task-id", help="reuse a caller-owned supervised task identity")
    add.add_argument("--task-label", help="optional human-readable task label")

    rebind = subparsers.add_parser(
        "rebind",
        help="move one supervised task to a replacement ChatGPT conversation URL",
    )
    rebind.add_argument("task_id")
    rebind.add_argument("url")

    prompt_get = subparsers.add_parser(
        "prompt-get",
        help="read the current adaptive continuation prompt state for one task",
    )
    prompt_get.add_argument("task_id")

    prompt_set = subparsers.add_parser(
        "prompt-set",
        help="CAS-update the adaptive continuation prompt for one task",
    )
    prompt_set.add_argument("task_id")
    prompt_set.add_argument("--expected-version", type=int, required=True)
    prompt_set.add_argument("--step-index", type=int, required=True)
    prompt_input = prompt_set.add_mutually_exclusive_group(required=True)
    prompt_input.add_argument("--prompt", help="adaptive prompt text for the next watchdog continuation")
    prompt_input.add_argument("--file", help="read adaptive prompt text from a UTF-8 file")
    prompt_input.add_argument("--clear", action="store_true", help="clear the adaptive prompt and use only the watchdog envelope")
    prompt_set.add_argument("--updated-by", help="optional writer identity for diagnostics")

    remove = subparsers.add_parser("remove", aliases=["finish"], help="stop watching a conversation UUID or URL")
    remove.add_argument("conversation")

    list_parser = subparsers.add_parser("list", help="list currently watched conversations")
    list_parser.add_argument("--json", action="store_true")
    return parser


def _request(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urlopen(request, timeout=3) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"watchdog registry HTTP {error.code}: {message}") from error
    except URLError as error:
        raise RuntimeError(f"watchdog registry unavailable: {error.reason}") from error
    if not isinstance(body, dict):
        raise RuntimeError("watchdog registry returned non-object JSON")
    return body


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in {"add", "start"}:
            payload: dict[str, object] = {"url": args.url}
            if args.task_id:
                payload["task_id"] = args.task_id
            if args.task_label:
                payload["task_label"] = args.task_label
            result = _request(args.control_url, "POST", "/register", payload)
            state = "created" if result.get("created") is True else "existing"
            print(f"{result.get('task_id')}\t{result.get('conversation_id')}\t{state}")
            return 0

        if args.command == "rebind":
            result = _request(
                args.control_url,
                "POST",
                "/rebind",
                {"task_id": args.task_id, "url": args.url},
            )
            state = "rebound" if result.get("changed") is True else "unchanged"
            print(
                f"{result.get('task_id')}\t"
                f"{result.get('previous_conversation_id')}\t"
                f"{result.get('conversation_id')}\t{state}"
            )
            return 0

        if args.command == "prompt-get":
            result = _request(
                args.control_url,
                "GET",
                f"/prompt?task_id={quote(args.task_id, safe='')}",
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0

        if args.command == "prompt-set":
            if args.file:
                step_prompt = Path(args.file).read_text(encoding="utf-8")
            elif args.clear:
                step_prompt = None
            else:
                step_prompt = args.prompt
            payload = {
                "task_id": args.task_id,
                "expected_version": args.expected_version,
                "step_index": args.step_index,
                "step_prompt": step_prompt,
                "updated_by": args.updated_by,
            }
            if payload["updated_by"] is None:
                payload.pop("updated_by")
            result = _request(
                args.control_url,
                "POST",
                "/prompt",
                payload,
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0

        if args.command in {"remove", "finish"}:
            key = "url" if "://" in args.conversation else "conversation_id"
            result = _request(
                args.control_url,
                "POST",
                "/unregister",
                {key: args.conversation},
            )
            state = "removed" if result.get("removed") is True else "missing"
            print(f"{result.get('conversation_id')}\t{state}")
            return 0

        if args.command == "list":
            result = _request(args.control_url, "GET", "/watches")
            watches = result.get("watches", [])
            if not isinstance(watches, list):
                raise RuntimeError("watchdog registry returned invalid watches list")
            if args.json:
                print(json.dumps(watches, ensure_ascii=False))
            else:
                for item in watches:
                    if isinstance(item, dict):
                        print(
                            f"{item.get('task_id')}\t"
                            f"{item.get('conversation_id')}\t"
                            f"{item.get('target_url')}"
                        )
            return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
