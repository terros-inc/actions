#!/usr/bin/env python3
"""Publish tagged Markdown files to Google Docs in a shared drive.

Run by the drive-sync composite action (action.yml next to this file).
One-way, repo to Drive. A Markdown file opts in with frontmatter:

  ---
  drive: runbooks            # a top-level folder of the shared drive, any case
  driveDocId: <doc id>       # optional: update this existing doc in place
  ---

For each tagged file changed between --before and --sha, the file is uploaded
through the Drive API's native Markdown import, with a banner on top and
relative links rewritten to absolute GitHub URLs. A doc without driveDocId is
found again on later runs by the gitSyncSource app property set on it.

When a file is deleted or its tag is removed, its doc is kept and its banner
says it is no longer synced. This script never deletes, trashes or moves a doc,
and never creates a folder.

Usage:
  drive_sync.py --repo owner/name --sha SHA --before SHA --drive-id ID [--all] [--plan-only]

DRIVE_ACCESS_TOKEN must hold an OAuth access token with the Drive scope,
unless --plan-only is given.
"""

import argparse
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"
SOURCE_PROP = "gitSyncSource"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"

FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE = re.compile(r"(`+)(?:(?!\1).)+?\1")
# ](target "title") of an inline link or image; <target> allows spaces.
INLINE_LINK_RE = re.compile(r"(\]\(\s*)(<[^>]+>|[^)\s]+)")
# [id]: target   (a reference-style link definition)
REF_DEF_RE = re.compile(r"^(\s{0,3}\[[^\]]+\]:\s*)(<[^>]+>|\S+)")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
ZERO_SHA_RE = re.compile(r"^0+$")
DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class SyncError(Exception):
    pass


# --- Markdown ---------------------------------------------------------------


def parse_frontmatter(text):
    """Return (top-level scalar keys, body). Only `key: value` lines are read."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$", line)
        if not kv:
            continue
        value = re.sub(r"\s+#.*$", "", kv.group(2))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[kv.group(1)] = value
    return meta, text[m.end():]


def drive_tag(meta):
    return (meta.get("drive") or "").strip()


def doc_title(meta, body, path):
    if meta.get("title"):
        return meta["title"]
    for line in body.splitlines():
        h1 = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
        if h1:
            return h1.group(1)
    return posixpath.splitext(posixpath.basename(path))[0]


def absolute_link(target, repo, sha, path):
    """Rewrite one link target relative to `path` into a GitHub URL, or return it unchanged."""
    bracketed = target.startswith("<") and target.endswith(">")
    raw = target[1:-1] if bracketed else target
    if not raw or raw.startswith(("#", "//")) or SCHEME_RE.match(raw):
        return target
    split = re.match(r"^([^?#]*)(.*)$", raw)
    rel, suffix = split.group(1), split.group(2)
    if rel.startswith("/"):
        resolved = posixpath.normpath(rel.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), rel))
    if resolved == ".." or resolved.startswith("../"):
        return target  # Points outside the repository; leave it alone.
    if resolved == ".":
        resolved = ""
    url = f"https://github.com/{repo}/blob/{sha}/{resolved}{suffix}"
    return f"<{url}>" if bracketed else url


def rewrite_links(body, repo, sha, path):
    out = []
    in_fence = False
    for line in body.split("\n"):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue

        def fix(m):
            return m.group(1) + absolute_link(m.group(2), repo, sha, path)

        line = REF_DEF_RE.sub(fix, line)
        # Rewrite only outside inline code spans.
        pieces, last = [], 0
        for code in INLINE_CODE_RE.finditer(line):
            pieces.append(INLINE_LINK_RE.sub(fix, line[last:code.start()]))
            pieces.append(code.group(0))
            last = code.end()
        pieces.append(INLINE_LINK_RE.sub(fix, line[last:]))
        out.append("".join(pieces))
    return "\n".join(out)


def commit_link(repo, sha):
    return f"[{sha[:12]}](https://github.com/{repo}/commit/{sha})"


def banner(repo, path, sha, retired=None):
    """retired is (reason, sha) when the file stopped being synced at that commit."""
    source = f"[{repo}/{path}](https://github.com/{repo}/blob/{sha}/{path})"
    text = (
        f"> Generated from {source} at {commit_link(repo, sha)}."
        " Edit it in Git; changes here are overwritten."
    )
    if retired:
        reason, at = retired
        text += (
            f"\n>\n> **No longer synced:** {reason} at {commit_link(repo, at)}."
            " This doc is kept but is no longer updated from Git."
        )
    return text + "\n\n"


def render(text, repo, sha, path, retired=None):
    _, body = parse_frontmatter(text)
    return banner(repo, path, sha, retired) + rewrite_links(body.lstrip("\r\n"), repo, sha, path)


def source_key(repo, path):
    # App property key + value must fit in 124 bytes, so store a hash of the source.
    return hashlib.sha1(f"{repo}:{path}".encode()).hexdigest()


# --- Git --------------------------------------------------------------------


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def git_show(rev, path):
    try:
        return subprocess.run(
            ["git", "show", f"{rev}:{path}"], check=True, capture_output=True
        ).stdout.decode("utf-8")
    except subprocess.CalledProcessError:
        return None


def commit_exists(rev):
    return subprocess.run(
        ["git", "cat-file", "-e", f"{rev}^{{commit}}"], capture_output=True
    ).returncode == 0


def plan(sha, before, full):
    """Return (publish, retire) for the push before..sha.

    publish: (path, text) of tagged files at sha. retire: (path, text, reason)
    of files tagged at before that are deleted or untagged at sha; text is
    their last tagged version.
    """
    incremental = not full and before and not ZERO_SHA_RE.match(before) and commit_exists(before)
    if incremental:
        out = git("diff", "--no-renames", "--name-only", "-z", before, sha)
    else:
        out = git("ls-tree", "-r", "--name-only", "-z", sha)
    publish, retire = [], []
    for path in sorted(p for p in out.split("\0") if p.endswith(".md")):
        new = git_show(sha, path)
        if new is not None and drive_tag(parse_frontmatter(new)[0]):
            publish.append((path, new))
            continue
        if incremental:
            old = git_show(before, path)
            if old is not None and drive_tag(parse_frontmatter(old)[0]):
                reason = "the file was deleted" if new is None else "the `drive` tag was removed"
                retire.append((path, old, reason))
    return publish, retire


# --- Drive ------------------------------------------------------------------


def http_send(method, url, headers, body):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Drive:
    """The few Drive v3 calls the sync needs. There is deliberately no delete."""

    def __init__(self, token, drive_id, send=http_send):
        self.token = token
        self.drive_id = drive_id
        self.send = send

    def _call(self, method, url, params, body=None, content_type=None):
        url = f"{url}?{urllib.parse.urlencode({**params, 'supportsAllDrives': 'true'})}"
        headers = {"Authorization": f"Bearer {self.token}"}
        if content_type:
            headers["Content-Type"] = content_type
        status, data = self.send(method, url, headers, body)
        if status >= 300:
            detail = data.decode("utf-8", "replace")[:500]
            raise SyncError(f"Drive API {method} {url.split('?')[0]} failed with {status}: {detail}")
        return json.loads(data or b"{}")

    def _list(self, q):
        files, token = [], None
        while True:
            params = {
                "q": q,
                "corpora": "drive",
                "driveId": self.drive_id,
                "includeItemsFromAllDrives": "true",
                "fields": "nextPageToken,files(id,name)",
                "pageSize": "1000",
            }
            if token:
                params["pageToken"] = token
            page = self._call("GET", f"{API}/files", params)
            files += page.get("files", [])
            token = page.get("nextPageToken")
            if not token:
                return files

    def top_level_folders(self):
        return self._list(
            f"'{self.drive_id}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false"
        )

    def find_by_source(self, key):
        return self._list(
            f"appProperties has {{ key='{SOURCE_PROP}' and value='{key}' }} and trashed = false"
        )

    def _upload(self, method, url, metadata, markdown):
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\nContent-Type: text/markdown; charset=UTF-8\r\n\r\n"
            f"{markdown}\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        return self._call(
            method, url, {"uploadType": "multipart", "fields": "id,name"}, body,
            f"multipart/related; boundary={boundary}",
        )

    def create_doc(self, name, folder_id, markdown, key):
        metadata = {
            "name": name,
            "mimeType": DOC_MIME,
            "parents": [folder_id],
            "appProperties": {SOURCE_PROP: key},
        }
        return self._upload("POST", f"{UPLOAD_API}/files", metadata, markdown)

    def update_doc(self, doc_id, markdown, key):
        """Replace the doc's content. Its name, folder and ID stay the same."""
        metadata = {"appProperties": {SOURCE_PROP: key}}
        return self._upload("PATCH", f"{UPLOAD_API}/files/{doc_id}", metadata, markdown)


# --- Sync -------------------------------------------------------------------


def resolve_folder(folders, tag):
    matches = [f for f in folders if f["name"].casefold() == tag.casefold()]
    if len(matches) == 1:
        return matches[0]["id"]
    names = ", ".join(sorted(f["name"] for f in folders)) or "(none)"
    if not matches:
        raise SyncError(
            f"no top-level folder in the shared drive is named '{tag}' (case-insensitive). "
            f"Folders the sync can see: {names}. The sync never creates folders."
        )
    raise SyncError(f"more than one top-level folder matches '{tag}': {names}")


def existing_doc(drive, meta, key):
    doc_id = meta.get("driveDocId")
    if doc_id:
        if not DOC_ID_RE.match(doc_id):
            raise SyncError(f"driveDocId '{doc_id}' is not a Google Drive file ID")
        return doc_id
    found = drive.find_by_source(key)
    return found[0]["id"] if found else None


def sync(drive, repo, sha, before, publish, retire, log=print):
    """Apply the plan. Returns a list of error messages; one bad file does not stop the rest."""
    errors, published_ids = [], set()
    folders = drive.top_level_folders() if publish else []
    for path, text in publish:
        try:
            meta, body = parse_frontmatter(text)
            folder_id = resolve_folder(folders, drive_tag(meta))
            key = source_key(repo, path)
            markdown = render(text, repo, sha, path)
            doc_id = existing_doc(drive, meta, key)
            if doc_id:
                drive.update_doc(doc_id, markdown, key)
                log(f"updated {path} -> https://docs.google.com/document/d/{doc_id}/edit")
            else:
                doc_id = drive.create_doc(doc_title(meta, body, path), folder_id, markdown, key)["id"]
                log(
                    f"created {path} -> https://docs.google.com/document/d/{doc_id}/edit "
                    f"(add `driveDocId: {doc_id}` to its frontmatter to pin it)"
                )
            published_ids.add(doc_id)
        except SyncError as e:
            errors.append(f"{path}: {e}")
    for path, text, reason in retire:
        try:
            meta, _ = parse_frontmatter(text)
            key = source_key(repo, path)
            doc_id = existing_doc(drive, meta, key)
            if not doc_id:
                log(f"skipped {path}: no doc was ever published for it")
                continue
            if doc_id in published_ids:
                continue  # Another file now publishes to this doc (for example after a rename).
            # The content is the last tagged version, so the banner cites the commit it came from.
            markdown = render(text, repo, before, path, retired=(f"{reason} in {repo}", sha))
            drive.update_doc(doc_id, markdown, key)
            log(f"marked {path} as no longer synced in https://docs.google.com/document/d/{doc_id}/edit")
        except SyncError as e:
            errors.append(f"{path}: {e}")
    return errors


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", required=True, help="owner/name of the repository being synced")
    p.add_argument("--sha", required=True, help="commit to publish")
    p.add_argument("--before", default="", help="commit before the push; empty syncs every tagged file")
    p.add_argument("--drive-id", required=True, help="ID of the shared drive")
    p.add_argument("--all", action="store_true", help="sync every tagged file, not only changed ones")
    p.add_argument("--plan-only", action="store_true", help="print the plan and write has_work to GITHUB_OUTPUT")
    args = p.parse_args(argv)

    publish, retire = plan(args.sha, args.before, args.all)
    for path, _ in publish:
        print(f"publish: {path}")
    for path, _, reason in retire:
        print(f"retire: {path} ({reason})")
    if args.plan_only:
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"has_work={'true' if publish or retire else 'false'}\n")
        return 0
    if not publish and not retire:
        print("No tagged Markdown files changed.")
        return 0

    token = os.environ.get("DRIVE_ACCESS_TOKEN")
    if not token:
        print("error: DRIVE_ACCESS_TOKEN is not set", file=sys.stderr)
        return 2
    errors = sync(Drive(token, args.drive_id), args.repo, args.sha, args.before, publish, retire)
    for e in errors:
        print(f"::error::{e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
