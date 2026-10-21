# Terros reusable GitHub Actions

- [opencode](opencode/action.yml) — Run OpenCode in GitHub Actions
- [open-code-review](open-code-review/action.yml) — AI-powered PR code review using OpenCodeReview
- [pi-code-review](pi-code-review/action.yml) — PR review with Pi, including inline findings and approval through `gh`
- [drive-sync](drive-sync/action.yml) — Publish Markdown files tagged `drive: <folder>` as Google Docs in a shared drive

Pin every action by full commit SHA, never `@main`, so a caller runs only the code it reviewed.
CI (`.github/workflows/ci.yml`) runs each action's tests; its `CI complete` job is the required check.

## Pi code review

The caller checks out the PR head. The action installs Pi in a Docker image,
collects PR evidence with `gh`, and runs Pi against a read-only snapshot of the
head commit. Pi returns a structured review; the host validates it, posts each
inline finding separately with `gh api`, then submits the final summary and
verdict. Failed inline posts are recorded and included in the summary without
an automatic retry. The host checks the PR head before and after the review.
CI results are review context; this action does not require checks to pass
before approving. Repository-required checks continue to gate merging.

```yaml
name: Pi code review
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  pull-requests: write
  checks: read
  statuses: read

jobs:
  review:
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v5
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0
          persist-credentials: false

      - uses: terros-inc/actions/pi-code-review@main
        env:
          OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
          LINEAR_API_KEY: ${{ secrets.LINEAR_API_KEY }} # optional
        with:
          model: openrouter/anthropic/claude-sonnet-4.6
```

Use a Linux runner with Docker, Bash, Git, Python 3, `gh`, and `jq`; GitHub-hosted
Ubuntu runners include them. Pi defaults to version `1.1.0` and Node.js `24`. Set
`pi_version`, `node_version`, `thinking`, or `prompt` to customize the version,
reasoning level, or additional review guidance. `model` currently accepts
OpenRouter models in `openrouter/model` format. Store `OPENROUTER_API_KEY` as a
GitHub repository or organization secret and pass it through the step
environment, as shown above. The model proxy holds that key on the host and
forwards only OpenRouter chat-completion requests. Pi receives a temporary
proxy token and never receives the provider or GitHub token. Its container has
no runner checkout, `.git` directory, or Docker socket.

For optional Linear context, create a personal Linear API key with only the
`Read` permission and limit it to the teams needed for reviews. Store it as a
GitHub repository or organization secret named `LINEAR_API_KEY`, then pass it
through the step environment as shown above. Workspace admins may disable API
key creation; a key can also access private teams available to its owner, so
select only the needed teams. If the secret is unset, the review continues
without Linear. With it set, Pi can submit GraphQL documents and variables to
a host-side proxy whose only upstream destination is Linear's GraphQL API. This
includes Linear's native `semanticSearch` query for natural-language discovery
across issues, projects, initiatives, and documents. Linear enforces read-only
access through the key's permission; the proxy bounds request and response
sizes and the timeout. The Linear key never enters the container or its mounts.

`github_token` defaults to the workflow token. The repository must allow GitHub
Actions to create and approve pull requests, and the token needs
`pull-requests: write`. Supply a GitHub App token or PAT through `github_token`
when repository policy requires a different reviewer identity. Fork PR workflows
typically lack secrets and write permissions, so the example skips them. For
events without a pull request payload, pass `pr_number` and `head_sha` explicitly.

References: [Pi CLI](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/cli.md),
[GitHub inline comment API](https://docs.github.com/en/rest/pulls/comments#create-a-review-comment-for-a-pull-request),
[GitHub review API](https://docs.github.com/en/rest/pulls/reviews#create-a-review-for-a-pull-request),
and [workflow review permissions](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository#preventing-github-actions-from-creating-or-approving-pull-requests).
Linear setup: [API key permissions](https://linear.app/docs/api-and-webhooks),
[GraphQL authentication](https://linear.app/developers/graphql), and
[private-team access](https://linear.app/docs/private-teams). See Linear's
[semantic search schema](https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql)
for query fields and result types.

## Drive sync

Publishes Markdown files whose frontmatter has `drive: <folder>` as Google Docs in that top-level
folder of a Google shared drive, using the Drive API's own Markdown import. One-way, repo to Drive;
it never deletes docs or creates folders. [`drive_sync.py`](drive-sync/drive_sync.py) documents the
rules. Auth is GitHub OIDC through Workload Identity Federation (WIF); there is no key file.

The calling repository adds `.github/workflows/drive-sync.yml` and uses the action directly, pinned
by commit SHA. There is no reusable workflow in between, so nothing in the chain follows a branch:

```yaml
name: Drive Sync
on:
  push:
    branches: [main]
    paths: ['**.md']
  workflow_dispatch:
    inputs:
      all:
        description: Sync every tagged file, not only changed ones
        type: boolean
        default: false
permissions:
  contents: read
  actions: read   # finds the last successful sync with `gh run list`
  id-token: write
jobs:
  drive-sync:
    runs-on: ubuntu-latest
    environment: drive-sync
    timeout-minutes: 10
    concurrency:
      group: drive-sync-${{ github.repository }}
      cancel-in-progress: false
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6
        with:
          fetch-depth: 0   # the sync diffs against the last synced commit
          persist-credentials: false
      - uses: terros-inc/actions/drive-sync@<full commit SHA>
        with:
          drive-id: ${{ vars.DRIVE_SYNC_DRIVE_ID }}
          wif-provider: ${{ vars.DRIVE_SYNC_WIF_PROVIDER }}
          service-account: ${{ vars.DRIVE_SYNC_SERVICE_ACCOUNT }}
          all: ${{ inputs.all == true }}
```

The job needs Bash, Git, Python 3 and `gh`. It exits before authenticating when no tagged file
changed, and fails if one did but an input is empty.

Because the caller's own workflow runs the action, the OIDC token's `job_workflow_ref` is
`<owner>/<repo>/.github/workflows/drive-sync.yml@refs/heads/main`, whatever action SHA it pins. Run
the job in an environment (`drive-sync` above) so its `sub` does not match trust granted to the
branch, and give the Google WIF provider an attribute condition that accepts only that workflow on
the default branch of the listed repositories:

```
assertion.repository_owner == '<owner>' &&
assertion.repository in ['<owner>/<repo-a>', '<owner>/<repo-b>'] &&
assertion.job_workflow_ref == assertion.repository + '/.github/workflows/drive-sync.yml@refs/heads/main' &&
assertion.environment == 'drive-sync'
```

Bumping the action SHA in a caller is a reviewed change to that file on the default branch, so the
condition does not change when the pin moves.
