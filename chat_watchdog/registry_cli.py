from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
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
            result = _request(args.control_url, "POST", "/register", {"url": args.url})
            state = "created" if result.get("created") is True else "existing"
            print(f"{result.get('conversation_id')}\t{state}")
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
                        print(f"{item.get('conversation_id')}\t{item.get('target_url')}")
            return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
