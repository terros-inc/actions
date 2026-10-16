#!/usr/bin/env python3
"""Validate Pi's untrusted JSON and construct one fixed-target review payload."""

import json
import sys


def invalid(message):
    raise SystemExit(f"Invalid Pi review result: {message}")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as source:
            return json.load(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        invalid(f"cannot read JSON from {path}: {error}")


def prepare(result, pr, head_sha):
    if not isinstance(result, dict) or set(result) != {"event", "body", "comments"}:
        invalid("expected event, body, and comments")

    event = result["event"]
    body = result["body"]
    comments = result["comments"]
    if event not in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}:
        invalid("unsupported review event")
    if not isinstance(body, str) or not body.strip() or len(body) > 60000:
        invalid("body must be nonempty and at most 60000 characters")
    if not isinstance(comments, list) or len(comments) > 100:
        invalid("comments must be a list of at most 100 items")
    if event == "APPROVE":
        if pr.get("isDraft") is True or comments:
            invalid("cannot approve a draft or a review with findings")

    files = pr.get("files")
    if not isinstance(files, list):
        invalid("PR file list is unavailable")
    changed_paths = {entry.get("path") for entry in files if isinstance(entry, dict)}
    checked_comments = []
    for item in comments:
        if not isinstance(item, dict) or set(item) not in (
            {"path", "line", "side", "body"},
            {"path", "line", "side", "start_line", "start_side", "body"},
        ):
            invalid("inline comment has unsupported fields")
        if not isinstance(item["path"], str) or item["path"] not in changed_paths:
            invalid("inline comment path is not in this PR")
        if type(item["line"]) is not int or item["line"] < 1:
            invalid("inline comment line must be positive")
        if item["side"] not in {"LEFT", "RIGHT"}:
            invalid("inline comment side must be LEFT or RIGHT")
        if not isinstance(item["body"], str) or not item["body"].strip() or len(item["body"]) > 10000:
            invalid("inline comment body is invalid")
        if "start_line" in item:
            if type(item["start_line"]) is not int or not 1 <= item["start_line"] <= item["line"]:
                invalid("inline comment start_line is invalid")
            if item["start_side"] not in {"LEFT", "RIGHT"}:
                invalid("inline comment start_side is invalid")
        checked_comments.append(item)

    return {"commit_id": head_sha, "event": event, "body": body, "comments": checked_comments}


def final_summary(payload, failures):
    """Make a summary-only review and preserve findings with uncertain posts."""
    summary = {key: payload[key] for key in ("commit_id", "event", "body")}
    if not failures:
        return summary

    # Reserve space for failed findings even when Pi's original summary is long.
    summary["body"] = summary["body"][:30000]
    summary["body"] += (
        f"\n\n### Inline publication\n{len(failures)} inline finding(s) could not be confirmed. "
        "They were not retried because the result of a failed request may be ambiguous. "
        "The findings are repeated here so they remain visible.\n"
    )
    omitted = 0
    for failure in failures:
        comment = failure["comment"]
        diagnostic = failure["diagnostic"].strip()[:500] or "No GitHub CLI diagnostic was returned."
        detail = (
            f"\n**Finding {failure['index'] + 1} — `{comment['path']}:{comment['line']}`** "
            f"(CLI exit {failure['exit_status']})\n\n"
            f"{comment['body'][:1500]}\n\n"
            f"Posting diagnostic: {diagnostic}\n"
        )
        if len(summary["body"]) + len(detail) + 200 > 60000:
            omitted += 1
        else:
            summary["body"] += detail
    if omitted:
        summary["body"] += f"\n{omitted} further failed finding(s) are recorded in the action log."
    return summary


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--summary":
        original = read_json(sys.argv[2])
        try:
            with open(sys.argv[3], encoding="utf-8") as source:
                failures = [json.loads(line) for line in source if line.strip()]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            invalid(f"cannot read failed inline posts: {error}")
        payload = final_summary(original, failures)
        destination = sys.argv[4]
    elif len(sys.argv) == 5:
        payload = prepare(read_json(sys.argv[1]), read_json(sys.argv[2]), sys.argv[3])
        destination = sys.argv[4]
    else:
        raise SystemExit("Usage: prepare_review.py RESULT PR_METADATA HEAD_SHA OUTPUT | --summary PAYLOAD FAILURES OUTPUT")
    with open(destination, "w", encoding="utf-8") as output:
        json.dump(payload, output)
