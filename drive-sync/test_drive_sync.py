"""Tests for drive_sync.py, with the Google Drive API replaced by an in-memory fake.

Run: python3 -m unittest discover -s drive-sync
"""

import json
import os
import re
import subprocess
import tempfile
import unittest
import urllib.parse

import drive_sync

REPO = "example-org/example"
DRIVE_ID = "0Ashared"


class FakeDrive:
    """Just enough of Drive v3 over HTTP: list, multipart create and update."""

    def __init__(self, folders):
        self.files = {fid: {"name": name, "folder": True} for fid, name in folders.items()}
        self.requests = []
        self.next_id = 1

    def send(self, method, url, headers, body):
        self.requests.append((method, url))
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if method == "GET" and parsed.path.endswith("/files"):
            return 200, json.dumps({"files": self._query(params["q"])}).encode()
        if method in ("POST", "PATCH") and params.get("uploadType") == "multipart":
            metadata, content = self._parts(headers["Content-Type"], body)
            if method == "POST":
                fid = f"doc{self.next_id}"
                self.next_id += 1
                self.files[fid] = {"name": metadata["name"], "parents": metadata["parents"]}
            else:
                fid = parsed.path.rsplit("/", 1)[1]
                if fid not in self.files:
                    return 404, b'{"error": "not found"}'
                assert "name" not in metadata and "parents" not in metadata
            self.files[fid].update(content=content, props=metadata.get("appProperties", {}))
            return 200, json.dumps({"id": fid, "name": self.files[fid]["name"]}).encode()
        return 400, f"unexpected {method} {url}".encode()

    def _query(self, q):
        prop = re.search(r"key='(\w+)' and value='(\w+)'", q)
        if prop:
            key, value = prop.groups()
            return [{"id": i, "name": f["name"]} for i, f in self.files.items()
                    if f.get("props", {}).get(key) == value]
        return [{"id": i, "name": f["name"]} for i, f in self.files.items() if f.get("folder")]

    @staticmethod
    def _parts(content_type, body):
        boundary = content_type.split("boundary=")[1]
        parts = body.decode().split(f"--{boundary}")[1:-1]
        meta, content = (p.split("\r\n\r\n", 1)[1][: -len("\r\n")] for p in parts)
        return json.loads(meta), content

    def docs(self):
        return {i: f for i, f in self.files.items() if not f.get("folder")}


class DriveSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.test")
        self.git("config", "user.name", "Test")
        self.fake = FakeDrive({"fRunbooks": "Runbooks", "fMarketing": "Marketing"})
        self.drive = drive_sync.Drive("token", DRIVE_ID, send=self.fake.send)
        self.head = None
        self.log = []

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, files):
        """Write (or, for None, delete) files, commit, and return (before, sha)."""
        for path, text in files.items():
            if text is None:
                os.remove(path)
                continue
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "change")
        before, self.head = self.head or "0" * 40, self.git("rev-parse", "HEAD")
        return before, self.head

    def push(self, files):
        before, sha = self.commit(files)
        publish, retire = drive_sync.plan(sha, before, full=False)
        return drive_sync.sync(self.drive, REPO, sha, before, publish, retire, log=self.log.append)

    def test_publishes_tagged_file_with_banner_and_absolute_links_once(self):
        runbook = (
            "---\ndrive: runbooks\n---\n# Deploy\n\n"
            "See [the workflow](../../.github/workflows/deploy.yml#L10), [root](/README.md),\n"
            "[a section](#rollback), [docs](https://example.test/x) and `[code](skip.md)`.\n\n"
            "[ref]: ./other.md\n\n```\n[fenced](skip.md)\n```\n"
        )
        errors = self.push({"docs/runbooks/deploy.md": runbook, "docs/notes.md": "# Untagged\n"})
        sha = self.head

        self.assertEqual(errors, [])
        (doc_id, doc), = self.fake.docs().items()  # The untagged file is not published.
        self.assertEqual(doc["name"], "Deploy")
        self.assertEqual(doc["parents"], ["fRunbooks"])
        blob = f"https://github.com/{REPO}/blob/{sha}"
        self.assertTrue(doc["content"].startswith(
            f"> Generated from [{REPO}/docs/runbooks/deploy.md]({blob}/docs/runbooks/deploy.md)"
            f" at [{sha[:12]}](https://github.com/{REPO}/commit/{sha})."
            " Edit it in Git; changes here are overwritten.\n\n# Deploy\n"
        ))
        self.assertNotIn("drive: runbooks", doc["content"])
        self.assertIn(f"[the workflow]({blob}/.github/workflows/deploy.yml#L10)", doc["content"])
        self.assertIn(f"[root]({blob}/README.md)", doc["content"])
        self.assertIn(f"[ref]: {blob}/docs/runbooks/other.md", doc["content"])
        self.assertIn("[a section](#rollback)", doc["content"])
        self.assertIn("[docs](https://example.test/x)", doc["content"])
        self.assertIn("`[code](skip.md)`", doc["content"])
        self.assertIn("[fenced](skip.md)", doc["content"])

        # A later change without driveDocId finds the same doc instead of creating a duplicate.
        self.assertEqual(self.push({"docs/runbooks/deploy.md": runbook + "\nMore.\n"}), [])
        self.assertEqual(list(self.fake.docs()), [doc_id])
        self.assertIn("More.", self.fake.docs()[doc_id]["content"])

    def test_updates_doc_in_place_by_drive_doc_id(self):
        self.fake.files["existingDoc"] = {"name": "Dual Region Failover Runbook", "parents": ["fRunbooks"]}

        errors = self.push({"failover.md": "---\ndrive: runbooks\ndriveDocId: existingDoc\n---\n# Region failover\n"})

        self.assertEqual(errors, [])
        self.assertEqual(list(self.fake.docs()), ["existingDoc"])
        doc = self.fake.docs()["existingDoc"]
        self.assertEqual(doc["name"], "Dual Region Failover Runbook")  # Not renamed or moved.
        self.assertIn("# Region failover", doc["content"])
        self.assertNotIn("POST", [m for m, _ in self.fake.requests])

    def test_matches_folder_case_insensitively(self):
        self.assertEqual(self.push({"a.md": "---\ndrive: RunBooks\n---\nBody\n"}), [])
        (doc,) = self.fake.docs().values()
        self.assertEqual(doc["parents"], ["fRunbooks"])

    def test_fails_clearly_when_no_folder_matches_and_creates_nothing(self):
        errors = self.push({
            "a.md": "---\ndrive: playbooks\n---\nBody\n",
            "b.md": "---\ndrive: marketing\n---\nBody\n",
        })

        self.assertEqual(len(errors), 1)
        self.assertIn("a.md: no top-level folder in the shared drive is named 'playbooks'", errors[0])
        self.assertIn("Marketing, Runbooks", errors[0])
        # The other file still publishes; no folder is ever created.
        self.assertEqual([d["parents"] for d in self.fake.docs().values()], [["fMarketing"]])
        self.assertEqual(sum(f.get("folder", False) for f in self.fake.files.values()), 2)

    def test_never_deletes_a_doc_when_file_is_deleted_or_untagged(self):
        self.push({
            "gone.md": "---\ndrive: runbooks\n---\n# Gone\n",
            "untagged.md": "---\ndrive: runbooks\n---\n# Untagged\n",
        })
        before_removal = self.head
        docs_before = set(self.fake.docs())

        errors = self.push({"gone.md": None, "untagged.md": "# Untagged\nNew text.\n"})

        self.assertEqual(errors, [])
        self.assertEqual(set(self.fake.docs()), docs_before)
        self.assertEqual({m for m, _ in self.fake.requests}, {"GET", "POST", "PATCH"})
        contents = {d["name"]: d["content"] for d in self.fake.docs().values()}
        self.assertIn("**No longer synced:** the file was deleted", contents["Gone"])
        self.assertIn(f"blob/{before_removal}/gone.md", contents["Gone"])
        self.assertIn("**No longer synced:** the `drive` tag was removed", contents["Untagged"])
        self.assertNotIn("New text.", contents["Untagged"])
        self.assertFalse(hasattr(self.drive, "delete") or hasattr(self.drive, "trash"))


if __name__ == "__main__":
    unittest.main()
