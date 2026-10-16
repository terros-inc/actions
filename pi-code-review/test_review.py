"""Credential-boundary and publication tests for the Pi review action."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

ACTION_DIR = Path(__file__).resolve().parent


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, ACTION_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


model_proxy = load_module("model_proxy")
prepare_review = load_module("prepare_review")


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.pr = {"isDraft": False, "files": [{"path": "src/main.py"}]}
        self.head = "a" * 40

    def test_valid_inline_review_has_fixed_commit(self):
        result = {
            "event": "REQUEST_CHANGES",
            "body": "One introduced bug",
            "comments": [{"path": "src/main.py", "line": 4, "side": "RIGHT", "body": "[P1] Bad result"}],
        }
        payload = prepare_review.prepare(result, self.pr, self.head)
        self.assertEqual(payload["commit_id"], self.head)
        self.assertEqual(payload["comments"], result["comments"])

    def test_agent_cannot_choose_arbitrary_api_target_or_file(self):
        base = {"event": "COMMENT", "body": "Reviewed", "comments": []}
        with self.assertRaises(SystemExit):
            prepare_review.prepare(dict(base, api_url="https://elsewhere.example"), self.pr, self.head)
        with self.assertRaises(SystemExit):
            prepare_review.prepare(
                dict(base, comments=[{"path": "other.py", "line": 1, "side": "RIGHT", "body": "Bad"}]),
                self.pr,
                self.head,
            )

    def test_draft_cannot_be_approved(self):
        with self.assertRaises(SystemExit):
            prepare_review.prepare(
                {"event": "APPROVE", "body": "Looks good", "comments": []},
                dict(self.pr, isDraft=True),
                self.head,
            )

    def test_ci_status_does_not_gate_approval(self):
        payload = prepare_review.prepare(
            {"event": "APPROVE", "body": "Looks good", "comments": []},
            dict(self.pr, statusCheckRollup=[{"conclusion": "FAILURE"}]),
            self.head,
        )
        self.assertEqual(payload["event"], "APPROVE")

    def test_failed_inline_finding_is_retained_in_final_summary(self):
        payload = {
            "commit_id": self.head,
            "event": "REQUEST_CHANGES",
            "body": "Review summary",
            "comments": [{"path": "src/main.py", "line": 4, "side": "RIGHT", "body": "[P1] Introduced bug"}],
        }
        failure = {"index": 0, "exit_status": 17, "diagnostic": "HTTP 422: invalid line", "comment": payload["comments"][0]}
        summary = prepare_review.final_summary(payload, [failure])
        self.assertNotIn("comments", summary)
        self.assertIn("[P1] Introduced bug", summary["body"])
        self.assertIn("HTTP 422: invalid line", summary["body"])
        self.assertEqual(summary["event"], "REQUEST_CHANGES")


class ProxyTests(unittest.TestCase):
    def test_proxy_adds_provider_key_only_to_fixed_upstream(self):
        received = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append((self.path, self.headers.get("Authorization")))
                body = b'{"choices":[]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), model_proxy.Handler)
        proxy.proxy_token = "scoped-token"
        proxy.provider_key = "real-provider-secret"
        proxy.model_id = "anthropic/claude-sonnet-4.6"
        proxy.opener = model_proxy.build_opener(model_proxy.NoRedirect)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        upstream_thread.start()
        proxy_thread.start()
        try:
            upstream_url = f"http://127.0.0.1:{upstream.server_port}/api/v1/chat/completions"
            proxy_url = f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions"
            with patch.object(model_proxy, "UPSTREAM_URL", upstream_url):
                payload = json.dumps({"model": proxy.model_id, "messages": []}).encode()
                request = Request(proxy_url, data=payload, headers={"Authorization": "Bearer scoped-token"})
                with urlopen(request) as response:
                    self.assertEqual(response.read(), b'{"choices":[]}')
                self.assertEqual(received, [("/api/v1/chat/completions", "Bearer real-provider-secret")])

                for path, authorization, body in [
                    ("/v1/other", "Bearer scoped-token", payload),
                    ("/v1/chat/completions", "Bearer wrong", payload),
                    ("/v1/chat/completions", "Bearer scoped-token", b'{"model":"other"}'),
                ]:
                    with self.assertRaises(HTTPError):
                        urlopen(Request(
                            f"http://127.0.0.1:{proxy.server_port}{path}",
                            data=body,
                            headers={"Authorization": authorization},
                        ))
                self.assertEqual(len(received), 1)
        finally:
            proxy.shutdown()
            upstream.shutdown()
            proxy.server_close()
            upstream.server_close()


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pi-review-boundary-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "src").mkdir()
        (self.repo / "src/main.py").write_text("answer = 42\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "src/main.py"], check=True)
        subprocess.run([
            "git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-qm", "fixture",
        ], check=True)
        self.head = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        (self.repo / "runner-secret.txt").write_text("checkout-only-credential")

        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.capture = self.root / "capture"
        self.capture.mkdir()
        self.stub("gh", '''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
capture = pathlib.Path(os.environ['FAKE_CAPTURE'])
head = os.environ['FAKE_HEAD']
if args[:2] == ['pr', 'view']:
    counter = capture / 'views'
    views = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(views))
    if views > 1 and os.environ.get('FAKE_STALE'):
        head = 'b' * 40
    print(json.dumps({'title':'Fixture','body':'','state':'OPEN','headRefOid':head,'isDraft':False,'files':[{'path':'src/main.py'}]}))
elif args[:2] == ['pr', 'diff']:
    print('diff --git a/src/main.py b/src/main.py')
elif args[:2] == ['pr', 'checks']:
    print('checks passed')
elif args[:2] == ['api', '--paginate']:
    pass
elif args[:3] == ['api', '--method', 'POST']:
    input_path = pathlib.Path(args[args.index('--input') + 1])
    target = args[3]
    payload = json.loads(input_path.read_text())
    attempts_path = capture / 'post-attempts.json'
    attempts = json.loads(attempts_path.read_text()) if attempts_path.exists() else []
    inline_index = sum(1 for attempt in attempts if attempt['target'].endswith('/comments'))
    attempts.append({'target':target, 'payload':payload})
    attempts_path.write_text(json.dumps(attempts))
    if target.endswith('/comments'):
        failed = {int(value) for value in os.environ.get('FAKE_INLINE_FAILURES','').split(',') if value}
        if inline_index in failed:
            print(f'HTTP 422: synthetic failure for inline finding {inline_index + 1}', file=sys.stderr)
            raise SystemExit(17)
        print(json.dumps({'id':1000 + inline_index,'html_url':'https://example.invalid/comment'}))
    elif target.endswith('/reviews'):
        if os.environ.get('FAKE_FINAL_FAIL'):
            print('Synthetic final review failure', file=sys.stderr)
            raise SystemExit(23)
        (capture / 'posted.json').write_text(json.dumps(payload))
        (capture / 'posted-target').write_text(target)
        print(json.dumps({'id':123,'state':'APPROVED','html_url':'https://example.invalid/review'}))
    else:
        raise SystemExit('Unexpected POST target: ' + target)
else:
    raise SystemExit('Unexpected gh call: ' + repr(args))
''')
        self.stub("docker", '''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
if args[:2] == ['network', 'inspect']:
    print('127.0.0.1')
elif args[0] == 'run':
    capture = pathlib.Path(os.environ['FAKE_CAPTURE'])
    (capture / 'docker-args.json').write_text(json.dumps(args))
    mounts = [args[index + 1] for index, arg in enumerate(args[:-1]) if arg == '--mount']
    workspace = next(m for m in mounts if 'target=/workspace' in m)
    snapshot = pathlib.Path(workspace.split(',')[1].removeprefix('source='))
    (capture / 'snapshot.json').write_text(json.dumps({
        'has_source': (snapshot / 'src/main.py').exists(),
        'has_git': (snapshot / '.git').exists(),
        'has_runner_secret': (snapshot / 'runner-secret.txt').exists(),
    }))
    print(os.environ['FAKE_RESULT'])
else:
    raise SystemExit('Unexpected docker call: ' + repr(args))
''')
        self.environment = dict(os.environ)
        self.environment.update({
            "PATH": str(self.bin_dir) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(self.root),
            "GH_TOKEN": "fake-github-secret",
            "OPENROUTER_API_KEY": "fake-provider-secret",
            "PI_REVIEW_IMAGE": "pi-review:test",
            "PI_MODEL": "openrouter/anthropic/claude-sonnet-4.6",
            "PI_THINKING": "high",
            "PR_NUMBER": "123",
            "HEAD_SHA": self.head,
            "REVIEW_REPOSITORY": "example/repo",
            "REVIEW_ACTION_PATH": str(ACTION_DIR),
            "FAKE_CAPTURE": str(self.capture),
            "FAKE_HEAD": self.head,
            "FAKE_RESULT": json.dumps({"event": "APPROVE", "body": "Reviewed; no issues found.", "comments": []}),
        })

    def stub(self, name, source):
        path = self.bin_dir / name
        path.write_text(source)
        path.chmod(0o755)

    def run_launcher(self, **overrides):
        return subprocess.run(
            ["bash", str(ACTION_DIR / "review.sh")], cwd=self.repo,
            env=dict(self.environment, **overrides), text=True, capture_output=True,
        )

    def test_container_receives_snapshot_and_scoped_model_access(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.capture / "docker-args.json").read_text())
        self.assertIn("--read-only", args)
        self.assertIn("--cap-drop", args)
        self.assertIn("--security-opt", args)
        self.assertNotIn("fake-github-secret", json.dumps(args))
        self.assertNotIn("fake-provider-secret", json.dumps(args))
        self.assertFalse(any("GH_TOKEN" in arg or "OPENROUTER_API_KEY" in arg for arg in args))
        self.assertEqual(json.loads((self.capture / "snapshot.json").read_text()), {
            "has_source": True, "has_git": False, "has_runner_secret": False,
        })
        self.assertEqual(json.loads((self.capture / "posted.json").read_text())["commit_id"], self.head)
        self.assertEqual((self.capture / "posted-target").read_text(), "repos/example/repo/pulls/123/reviews")

    def test_changed_head_prevents_post(self):
        result = self.run_launcher(FAKE_STALE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.capture / "posted.json").exists())

    def test_agent_cannot_select_a_different_post_target(self):
        malicious = json.dumps({"event": "APPROVE", "body": "Reviewed", "comments": [], "api_url": "https://elsewhere.invalid"})
        result = self.run_launcher(FAKE_RESULT=malicious)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.capture / "posted.json").exists())

    def three_findings(self):
        return json.dumps({
            "event": "REQUEST_CHANGES",
            "body": "Three introduced bugs",
            "comments": [
                {"path": "src/main.py", "line": index + 1, "side": "RIGHT", "body": f"[P1] Finding {index + 1}"}
                for index in range(3)
            ],
        })

    def assert_inline_failures_still_submit_summary(self, failing_indexes):
        result = self.run_launcher(
            FAKE_RESULT=self.three_findings(),
            FAKE_INLINE_FAILURES=",".join(str(index) for index in failing_indexes),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        attempts = json.loads((self.capture / "post-attempts.json").read_text())
        self.assertEqual(len(attempts), 4)
        self.assertEqual([attempt["target"].rsplit("/", 1)[-1] for attempt in attempts],
                         ["comments", "comments", "comments", "reviews"])
        for index, attempt in enumerate(attempts[:3]):
            self.assertEqual(attempt["payload"]["body"], f"[P1] Finding {index + 1}")
            self.assertEqual(attempt["payload"]["commit_id"], self.head)
        final = attempts[-1]["payload"]
        self.assertEqual(final["event"], "REQUEST_CHANGES")
        self.assertNotIn("comments", final)
        for index in failing_indexes:
            self.assertIn(f"[P1] Finding {index + 1}", final["body"])
            self.assertIn(f"synthetic failure for inline finding {index + 1}", final["body"])
        self.assertEqual((self.capture / "posted-target").read_text(),
                         "repos/example/repo/pulls/123/reviews")

    def test_first_inline_failure_does_not_skip_later_findings_or_summary(self):
        self.assert_inline_failures_still_submit_summary([0])

    def test_middle_inline_failure_does_not_skip_later_findings_or_summary(self):
        self.assert_inline_failures_still_submit_summary([1])

    def test_all_inline_failures_still_submit_summary(self):
        self.assert_inline_failures_still_submit_summary([0, 1, 2])

    def test_final_summary_failure_is_not_retried(self):
        result = self.run_launcher(
            FAKE_RESULT=self.three_findings(),
            FAKE_INLINE_FAILURES="0",
            FAKE_FINAL_FAIL="1",
        )
        self.assertNotEqual(result.returncode, 0)
        attempts = json.loads((self.capture / "post-attempts.json").read_text())
        self.assertEqual(len(attempts), 4)
        self.assertEqual(attempts[-1]["target"], "repos/example/repo/pulls/123/reviews")


if __name__ == "__main__":
    unittest.main()
