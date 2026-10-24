# PR briefing bot

Independent of Cursor. On every non-draft pull request it posts **one GitHub comment**:

- 8-line summary
- risk areas
- missing tests
- files a reviewer should actually look at
- Bugbot-style findings

It never approves, requests reviewers, or merges. If it already commented, it **updates that comment** instead of stacking duplicates.

Anyone can open the PR from GitHub, VS Code, or the CLI. They do not need Cursor. They only need to read the comment on GitHub.

## Use from another project (recommended)

In that repo, add `.github/workflows/pr-brief.yml`:

```yaml
name: PR briefing bot

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  pull-requests: write

jobs:
  brief:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps:
      - uses: Java2Dig/pr-brief-bot@v1
        with:
          github-token: ${{ github.token }}
          llm-api-key: ${{ secrets.LLM_API_KEY }}
```

Then add a repository secret named `LLM_API_KEY` (OpenAI or any OpenAI-compatible key).

Optional inputs: `llm-base-url` (default `https://api.openai.com/v1`) and `llm-model` (default `gpt-4o-mini`).

## Copy the files instead

If you do not want to call this repo, copy `src/pr_brief.py` and `.github/workflows/pr-brief.yml` into the target repo and set the same `LLM_API_KEY` secret. Optional repo variables: `LLM_BASE_URL`, `LLM_MODEL`.

## Local dry run

```bash
export GITHUB_TOKEN=...          # token that can read the PR
export GITHUB_REPOSITORY=Java2Dig/ai-engineering-from-scratch
export PR_NUMBER=123
export LLM_API_KEY=...
python3 src/pr_brief.py --dry-run
```

`--dry-run` prints the comment and does not post it. Omit `--dry-run` to post.

## What it will not do

- Approve or request changes as a review verdict
- Assign reviewers
- Comment on draft PRs until they are marked ready
