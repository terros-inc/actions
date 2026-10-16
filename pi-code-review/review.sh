#!/usr/bin/env bash
set -euo pipefail
umask 077

fail() {
  echo "::error::$1" >&2
  exit 1
}

for review_tool in docker gh git jq python3 tar; do
  command -v "$review_tool" >/dev/null || fail "Required command not found: $review_tool"
done

[[ -n "${GH_TOKEN:-}" ]] || fail "github_token is required."
[[ -n "${OPENROUTER_API_KEY:-}" ]] || fail "OPENROUTER_API_KEY must be provided from a GitHub secret."
[[ "${PI_MODEL:-}" == openrouter/* ]] || fail "model must use the openrouter/model format."
openrouter_model=${PI_MODEL#openrouter/}
[[ -n "$openrouter_model" ]] || fail "An OpenRouter model ID is required."
[[ "${PR_NUMBER:-}" =~ ^[1-9][0-9]*$ ]] || fail "pr_number must be a positive pull request number."
[[ "${HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || fail "head_sha must be a full commit SHA."
[[ "${REVIEW_REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "Invalid GitHub repository."
[[ -n "${REVIEW_ACTION_PATH:-}" && -n "${PI_REVIEW_IMAGE:-}" ]] || fail "The action path and image are required."
[[ -n "${RUNNER_TEMP:-}" ]] || fail "RUNNER_TEMP is required."

case "${PI_THINKING:-}" in
  off|minimal|low|medium|high|xhigh|max) ;;
  *) fail "Invalid thinking level." ;;
esac

[[ "$(git rev-parse HEAD)" == "$HEAD_SHA" ]] || fail "Check out head_sha before running Pi Code Review."

review_dir=$(mktemp -d "$RUNNER_TEMP/pi-code-review.XXXXXX")
proxy_pid=
cleanup() {
  if [[ -n "$proxy_pid" ]]; then
    kill "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
  fi
  rm -rf -- "$review_dir"
}
trap cleanup EXIT

mkdir -p "$review_dir/context" "$review_dir/workspace"

# The host gathers GitHub evidence while authenticated. Pi only reads these
# files and a fresh snapshot of committed source, never the runner checkout.
gh pr view "$PR_NUMBER" --repo "$REVIEW_REPOSITORY" \
  --json title,body,baseRefName,baseRefOid,headRefOid,files,isDraft,state,statusCheckRollup \
  > "$review_dir/context/pr.json"
[[ "$(jq -r '.state' "$review_dir/context/pr.json")" == "OPEN" ]] || fail "The pull request is no longer open."
[[ "$(jq -r '.headRefOid' "$review_dir/context/pr.json")" == "$HEAD_SHA" ]] || fail "The pull request head changed; rerun the review."

gh pr diff "$PR_NUMBER" --repo "$REVIEW_REPOSITORY" > "$review_dir/context/diff.patch"
gh api --paginate "repos/$REVIEW_REPOSITORY/pulls/$PR_NUMBER/comments" \
  --jq '.[] | {path, line, body, commit_id, user: .user.login}' \
  > "$review_dir/context/comments.jsonl"
gh api --paginate "repos/$REVIEW_REPOSITORY/pulls/$PR_NUMBER/reviews" \
  --jq '.[] | {state, body, commit_id, user: .user.login}' \
  > "$review_dir/context/reviews.jsonl"

if gh pr checks "$PR_NUMBER" --repo "$REVIEW_REPOSITORY" > "$review_dir/context/checks.txt" 2>&1; then
  printf '\nChecks command exit status: 0\n' >> "$review_dir/context/checks.txt"
else
  checks_status=$?
  printf '\nChecks command exit status: %s\n' "$checks_status" >> "$review_dir/context/checks.txt"
fi

git archive "$HEAD_SHA" | tar -x -C "$review_dir/workspace"

# Only the fixed OpenRouter upstream receives the provider key. The container
# gets an ephemeral local proxy token and has no GitHub or provider credential.
bridge_ip=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
[[ -n "$bridge_ip" ]] || fail "Could not find the Docker bridge gateway."
proxy_token=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
ready_file="$review_dir/proxy-port"
python3 "$REVIEW_ACTION_PATH/model_proxy.py" \
  --bind "$bridge_ip" --ready "$ready_file" --model "$openrouter_model" --token "$proxy_token" \
  > "$review_dir/proxy.log" 2>&1 &
proxy_pid=$!

for _ in {1..50}; do
  [[ -s "$ready_file" ]] && break
  kill -0 "$proxy_pid" 2>/dev/null || fail "The model proxy failed to start."
  sleep 0.1
done
[[ -s "$ready_file" ]] || fail "Timed out starting the model proxy."
proxy_port=$(<"$ready_file")
[[ "$proxy_port" =~ ^[1-9][0-9]*$ ]] || fail "Invalid model proxy port."

PROXY_URL="http://$bridge_ip:$proxy_port/v1" PROXY_TOKEN="$proxy_token" \
  OPENROUTER_MODEL="$openrouter_model" python3 - "$review_dir/context/models.json" <<'PY'
import json
import os
import sys

config = {
    "providers": {
        "review-proxy": {
            "baseUrl": os.environ["PROXY_URL"],
            "api": "openai-completions",
            "apiKey": os.environ["PROXY_TOKEN"],
            "models": [{"id": os.environ["OPENROUTER_MODEL"], "reasoning": True}],
        }
    }
}
with open(sys.argv[1], "w", encoding="utf-8") as output:
    json.dump(config, output)
PY

{
  printf 'Review pull request #%s in %s at commit %s.\n' "$PR_NUMBER" "$REVIEW_REPOSITORY" "$HEAD_SHA"
  printf 'The PR evidence is in /review/context and the committed source is in /workspace.\n'
  if [[ -n "${REVIEW_PROMPT:-}" ]]; then
    printf '\nAdditional review guidance:\n%s\n' "$REVIEW_PROMPT"
  fi
} > "$review_dir/context/request.txt"

# Docker receives only explicitly listed variables and mounts. In particular,
# GH_TOKEN, OPENROUTER_API_KEY, the runner checkout, and Docker's socket stay out.
docker run --rm --read-only --user "$(id -u):$(id -g)" \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 \
  --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
  --mount "type=bind,source=$review_dir/workspace,target=/workspace,readonly" \
  --mount "type=bind,source=$review_dir/context,target=/review/context,readonly" \
  --mount "type=bind,source=$REVIEW_ACTION_PATH,target=/review/action,readonly" \
  --env PI_MODEL_ID="review-proxy/$openrouter_model" \
  --env PI_THINKING="$PI_THINKING" \
  "$PI_REVIEW_IMAGE" /bin/bash /review/action/run-pi.sh \
  > "$review_dir/result.json"

# Validate the agent's untrusted output and fix the target on the host. Pi's
# text never becomes a shell command or an arbitrary GitHub API operation.
python3 "$REVIEW_ACTION_PATH/prepare_review.py" \
  "$review_dir/result.json" "$review_dir/context/pr.json" \
  "$HEAD_SHA" "$review_dir/payload.json"

current_pr=$(gh pr view "$PR_NUMBER" --repo "$REVIEW_REPOSITORY" --json state,headRefOid)
[[ "$(jq -r '.state' <<< "$current_pr")" == "OPEN" ]] || fail "The pull request closed during review."
[[ "$(jq -r '.headRefOid' <<< "$current_pr")" == "$HEAD_SHA" ]] || fail "The pull request head changed during review; no review was posted."

# Each inline finding is a separate write. A failed CLI call may have reached
# GitHub even when its response was lost, so never retry it automatically.
summary_payload="$review_dir/summary.json"
jq '{commit_id, event, body}' "$review_dir/payload.json" > "$summary_payload"
failed_posts="$review_dir/failed-comments.jsonl"
: > "$failed_posts"
comment_count=$(jq -r '.comments | length' "$review_dir/payload.json")
for ((comment_index = 0; comment_index < comment_count; comment_index++)); do
  comment_payload="$review_dir/comment-$comment_index.json"
  comment_response="$review_dir/comment-$comment_index.response"
  comment_error="$review_dir/comment-$comment_index.error"
  jq --argjson index "$comment_index" '{commit_id} + .comments[$index]' \
    "$review_dir/payload.json" > "$comment_payload"

  post_status=0
  if gh api --method POST "repos/$REVIEW_REPOSITORY/pulls/$PR_NUMBER/comments" \
    --input "$comment_payload" > "$comment_response" 2> "$comment_error"; then
    if jq -e '.id | numbers' "$comment_response" >/dev/null 2>&1; then
      printf 'Posted inline finding %s of %s.\n' "$((comment_index + 1))" "$comment_count"
      continue
    fi
    printf 'GitHub CLI returned success without a review-comment ID.\n' >> "$comment_error"
  else
    post_status=$?
  fi

  failure_record="$review_dir/comment-$comment_index.failure.json"
  if jq -nc --argjson index "$comment_index" --argjson exit_status "$post_status" \
    --rawfile stderr "$comment_error" --rawfile response "$comment_response" \
    --slurpfile comment "$comment_payload" \
    '{index: $index, exit_status: $exit_status,
      diagnostic: (if $stderr != "" then $stderr else $response end),
      comment: $comment[0]}' > "$failure_record"; then
    cat "$failure_record" >> "$failed_posts"
    jq -c '{index, exit_status, path: .comment.path, line: .comment.line,
      diagnostic: (.diagnostic | .[0:1000])}' "$failure_record"
  else
    printf 'Could not record details for inline finding %s; continuing to the final review.\n' "$((comment_index + 1))" >&2
  fi
done

if [[ -s "$failed_posts" ]]; then
  if python3 "$REVIEW_ACTION_PATH/prepare_review.py" --summary \
    "$review_dir/payload.json" "$failed_posts" "$review_dir/summary-with-failures.json"; then
    summary_payload="$review_dir/summary-with-failures.json"
  else
    printf 'Could not add inline-post diagnostics to the final review; posting the original summary.\n' >&2
  fi
fi

final_response="$review_dir/final-review.response"
final_error="$review_dir/final-review.error"
if gh api --method POST "repos/$REVIEW_REPOSITORY/pulls/$PR_NUMBER/reviews" \
  --input "$summary_payload" > "$final_response" 2> "$final_error"; then
  if jq -e '.id | numbers' "$final_response" >/dev/null 2>&1; then
    jq '{id, state, html_url}' "$final_response"
  else
    fail "The final review request returned without a review ID. Its publication status is uncertain; no retry was attempted."
  fi
else
  final_status=$?
  jq -Rs '{final_review_error: .}' < "$final_error" >&2
  fail "The final review was not confirmed (GitHub CLI exit $final_status); no retry was attempted."
fi
