# Terros reusable GitHub Actions

- [opencode](opencode/action.yml) — Run OpenCode in GitHub Actions
- [open-code-review](open-code-review/action.yml) — AI-powered PR code review using OpenCodeReview
- [pi-code-review](pi-code-review/action.yml) — PR review with Pi, including inline findings and approval through `gh`

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
