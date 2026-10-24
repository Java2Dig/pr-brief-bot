#!/usr/bin/env python3
"""Post a reviewer brief on a GitHub pull request. Comments only — never approves."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

COMMENT_MARKER = "<!-- pr-brief-bot -->"
MAX_DIFF_CHARS = 80_000
MAX_COMMENT_CHARS = 65_000
SKIP_NAME_PARTS = (
    "lock.json",
    "lock.yaml",
    ".lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "uv.lock",
    "go.sum",
    ".min.js",
    ".map",
)


SYSTEM_PROMPT = """You are a PR briefing bot. Your only job is to help a human reviewer decide what to read.
You never approve, merge, request reviewers, or change the PR.

Write Markdown for a GitHub pull request comment using exactly these sections:

## Summary
Exactly 8 short lines. Cover intent, what changed, and user-visible impact. No filler.

## Risk areas
Bullet the real failure modes: auth, data, concurrency, migrations, API contract, security, backwards compatibility.
If risk is low, say so and why.

## Missing tests
Name concrete cases that are not covered (file or behavior). If coverage looks adequate, say what is covered and what you did not verify.

## Files a reviewer should actually look at
List only the files that need human judgment. Skip lockfiles, generated code, formatting-only churn, and obvious boilerplate.

## Bugbot-style findings
A compact list of likely bugs, each as: Severity | file:line | finding. Highest severity first.
If none, write: no bugs found.

Be specific to this diff. Cite paths and symbols. Do not lecture about process.
If the diff is empty or you cannot read it, say that and stop.
"""


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        if default is not None:
            return default
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def http_json(
    url: str,
    *,
    token: str,
    method: str = "GET",
    body: dict | None = None,
    accept: str = "application/vnd.github+json",
) -> object:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-brief-bot",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {err.code} {method} {url}\n{detail}") from err


def llm_complete(*, base_url: str, api_key: str, model: str, user_prompt: str) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "pr-brief-bot",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        raise SystemExit(f"LLM HTTP {err.code} POST {url}\n{detail}") from err

    try:
        text = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as err:
        raise SystemExit(f"Unexpected LLM response:\n{json.dumps(result)[:2000]}") from err
    if not isinstance(text, str) or not text.strip():
        raise SystemExit("LLM returned an empty brief")
    return text.strip()


def resolve_pr_number() -> int:
    if os.environ.get("PR_NUMBER"):
        return int(os.environ["PR_NUMBER"])
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise SystemExit("Set PR_NUMBER or run this from GitHub Actions")
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    pr = event.get("pull_request") or {}
    number = pr.get("number")
    if not number:
        raise SystemExit("GitHub event is not a pull request")
    if pr.get("draft"):
        print("Skipping draft pull request", file=sys.stderr)
        raise SystemExit(0)
    return int(number)


def list_pr_files(api: str, repo: str, pr: int, token: str) -> list[dict]:
    files: list[dict] = []
    page = 1
    while True:
        batch = http_json(
            f"{api}/repos/{repo}/pulls/{pr}/files?per_page=100&page={page}",
            token=token,
        )
        if not isinstance(batch, list) or not batch:
            break
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return files


def is_noise_file(filename: str) -> bool:
    lower = filename.lower()
    return any(part in lower for part in SKIP_NAME_PARTS)


def build_user_prompt(pr: dict, files: list[dict]) -> str:
    lines = [
        f"Repository: {pr.get('base', {}).get('repo', {}).get('full_name', '')}",
        f"PR #{pr.get('number')}: {pr.get('title')}",
        f"Author: {pr.get('user', {}).get('login')}",
        f"Base: {pr.get('base', {}).get('ref')}  Head: {pr.get('head', {}).get('ref')}",
        "",
        "PR description:",
        (pr.get("body") or "(empty)").strip(),
        "",
        "Changed files:",
    ]
    patches: list[str] = []
    used = 0
    for item in files:
        name = item.get("filename", "")
        status = item.get("status", "")
        additions = item.get("additions", 0)
        deletions = item.get("deletions", 0)
        noise = " (likely noise)" if is_noise_file(name) else ""
        lines.append(f"- {status} {name} +{additions}/-{deletions}{noise}")
        patch = item.get("patch")
        if not patch or is_noise_file(name):
            continue
        chunk = f"\n### {name}\n```diff\n{patch}\n```\n"
        if used + len(chunk) > MAX_DIFF_CHARS:
            patches.append("\n[diff truncated for size]\n")
            break
        patches.append(chunk)
        used += len(chunk)

    lines.append("")
    lines.append("Diffs:")
    lines.extend(patches or ["(no patches available — files may be too large or binary)"])
    return "\n".join(lines)


def find_existing_comment(api: str, repo: str, pr: int, token: str) -> int | None:
    page = 1
    while True:
        comments = http_json(
            f"{api}/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}",
            token=token,
        )
        if not isinstance(comments, list) or not comments:
            return None
        for comment in comments:
            body = comment.get("body") or ""
            if COMMENT_MARKER in body:
                return int(comment["id"])
        if len(comments) < 100:
            return None
        page += 1


def upsert_comment(api: str, repo: str, pr: int, token: str, body: str) -> None:
    existing = find_existing_comment(api, repo, pr, token)
    if existing:
        http_json(
            f"{api}/repos/{repo}/issues/comments/{existing}",
            token=token,
            method="PATCH",
            body={"body": body},
        )
        print(f"Updated comment {existing} on PR #{pr}")
        return
    http_json(
        f"{api}/repos/{repo}/issues/{pr}/comments",
        token=token,
        method="POST",
        body={"body": body},
    )
    print(f"Posted new comment on PR #{pr}")


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    api = env("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    repo = env("GITHUB_REPOSITORY")
    token = env("GITHUB_TOKEN")
    pr_number = resolve_pr_number()

    pr = http_json(f"{api}/repos/{repo}/pulls/{pr_number}", token=token)
    if not isinstance(pr, dict):
        raise SystemExit("Unexpected pull request payload")
    if pr.get("draft"):
        print("Skipping draft pull request")
        return 0

    files = list_pr_files(api, repo, pr_number, token)
    user_prompt = build_user_prompt(pr, files)

    if dry_run and os.environ.get("LLM_API_KEY") in (None, ""):
        print(user_prompt)
        return 0

    brief = llm_complete(
        base_url=env("LLM_BASE_URL", "https://api.openai.com/v1"),
        api_key=env("LLM_API_KEY"),
        model=env("LLM_MODEL", "gpt-4o-mini"),
        user_prompt=user_prompt,
    )
    body = f"{COMMENT_MARKER}\n{brief}"
    if len(body) > MAX_COMMENT_CHARS:
        body = body[: MAX_COMMENT_CHARS - 20] + "\n\n[truncated]"

    if dry_run:
        print(body)
        return 0

    upsert_comment(api, repo, pr_number, token, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
