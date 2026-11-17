#!/usr/bin/env python3
"""Extract a completed review from Pi's JSON events without logging model text."""

import json
import sys
from pathlib import Path


def extract(events):
    final = None
    for line in events:
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError("invalid event")
        if event.get("type") == "agent_end":
            final = event
    if not final or final.get("willRetry"):
        raise ValueError("no completed agent_end event")
    messages = final.get("messages")
    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict):
        raise ValueError("no final assistant message")
    message = messages[-1]
    if message.get("role") != "assistant":
        raise ValueError("no final assistant message")
    reason = message.get("stopReason")
    safe_reason = reason if reason in ("stop", "length", "toolUse", "error", "aborted") else "unknown"
    content = message.get("content")
    if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
        raise ValueError("invalid assistant content")
    texts = [block.get("text", "") for block in content if block.get("type") == "text"]
    if any(not isinstance(text, str) for text in texts):
        raise ValueError("invalid assistant text")
    text = "\n".join(texts)
    thinking = sum(block.get("type") == "thinking" for block in content)
    diagnostic = f"stopReason={safe_reason}; text characters={len(text)}; thinking blocks={thinking}"
    if reason != "stop" or not text.strip() or any(block.get("type") == "toolCall" for block in content):
        raise ValueError(diagnostic)
    return text


def main():
    events_path, stderr_path, status, output_path = sys.argv[1:]
    print(f"Pi process exit status={int(status)}; stderr bytes={Path(stderr_path).stat().st_size} "
          "(raw stderr withheld because it may contain upstream payloads)", file=sys.stderr)
    try:
        with open(events_path, encoding="utf-8") as events:
            text = extract(events)
    except (OSError, UnicodeError, ValueError) as error:
        # JSON decoder errors include untrusted input; only our fixed messages
        # and allowlisted completion metadata may reach the job log.
        diagnostic = "invalid JSON event stream" if isinstance(error, json.JSONDecodeError) else str(error)
        if isinstance(error, (OSError, UnicodeError)):
            diagnostic = "unreadable JSON event stream"
        raise SystemExit(f"Pi review failed: {diagnostic}") from None
    if int(status) != 0:
        raise SystemExit("Pi review failed: process exited unsuccessfully")
    Path(output_path).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
