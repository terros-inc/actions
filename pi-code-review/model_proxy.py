#!/usr/bin/env python3
"""Forward fixed-destination model and Linear requests with host-held keys."""

import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

UPSTREAM_URL = "https://openrouter.ai/api/v1/chat/completions"
LINEAR_UPSTREAM_URL = "https://api.linear.app/graphql"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_LINEAR_REQUEST_BYTES = 256 * 1024
MAX_LINEAR_RESPONSE_BYTES = 2 * 1024 * 1024
LINEAR_TIMEOUT_SECONDS = 30


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def redact_secret(value, secret):
    """Keep a provider key out of even an unexpected upstream error payload."""
    if isinstance(value, str):
        return value.replace(secret, "[redacted]")
    if isinstance(value, list):
        return [redact_secret(item, secret) for item in value]
    if isinstance(value, dict):
        return {redact_secret(key, secret): redact_secret(item, secret) for key, item in value.items()}
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "PiReviewProxy/1"

    def log_message(self, format_string, *args):
        # Never write credentials, request bodies, or model responses to logs.
        pass

    def respond(self, status, message):
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.proxy_token
        return hmac.compare_digest(supplied, expected)

    def forward_linear(self):
        if not self.server.linear_key:
            return self.respond(404, "Linear unavailable")
        if not self.authorized():
            return self.respond(403, "Forbidden")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.respond(400, "Invalid content length")
        if length < 1 or length > MAX_LINEAR_REQUEST_BYTES:
            return self.respond(413, "Invalid Linear request size")
        self.connection.settimeout(10)
        try:
            body = self.rfile.read(length)
        except (TimeoutError, OSError):
            return self.respond(408, "Linear request timed out")
        if len(body) != length:
            return self.respond(400, "Incomplete Linear request")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self.respond(400, "Invalid JSON request")
        if not isinstance(payload, dict) or not isinstance(payload.get("query"), str) or not payload["query"].strip():
            return self.respond(400, "A GraphQL document is required")
        if "variables" in payload and payload["variables"] is not None and not isinstance(payload["variables"], dict):
            return self.respond(400, "Invalid GraphQL variables")
        if "operationName" in payload and payload["operationName"] is not None and not isinstance(payload["operationName"], str):
            return self.respond(400, "Invalid operation name")

        # The document and variables are agent-selected. Linear's read-only
        # token enforces operation permissions; this proxy fixes only the
        # destination and keeps the credential on the host.
        request = Request(
            LINEAR_UPSTREAM_URL,
            data=body,
            headers={
                "Authorization": self.server.linear_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            upstream = self.server.opener.open(request, timeout=LINEAR_TIMEOUT_SECONDS)
        except HTTPError as error:
            upstream = error
        except (URLError, TimeoutError, OSError):
            return self.respond(502, "Linear transport failed")

        try:
            with upstream:
                status = upstream.status
                if 300 <= status < 400:
                    return self.respond(502, "Linear redirect rejected")
                raw = upstream.read(MAX_LINEAR_RESPONSE_BYTES + 1)
        except (URLError, TimeoutError, OSError):
            return self.respond(502, "Linear transport failed")
        if len(raw) > MAX_LINEAR_RESPONSE_BYTES:
            return self.respond(502, "Linear response exceeded limit")
        try:
            result = redact_secret(json.loads(raw), self.server.linear_key)
        except (UnicodeDecodeError, json.JSONDecodeError):
            reported_status = status if 400 <= status < 600 else 502
            return self.respond(reported_status, f"Linear returned HTTP {status} without JSON")
        if not isinstance(result, dict):
            return self.respond(502, "Invalid Linear GraphQL response")
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_LINEAR_RESPONSE_BYTES:
            return self.respond(502, "Linear response exceeded limit")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        if self.path == "/linear/graphql":
            return self.forward_linear()
        if self.path != "/v1/chat/completions":
            return self.respond(404, "Unsupported model endpoint")

        if not self.authorized():
            return self.respond(403, "Forbidden")

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.respond(400, "Invalid content length")
        if length < 1 or length > MAX_REQUEST_BYTES:
            return self.respond(413, "Invalid request size")

        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self.respond(400, "Invalid JSON request")
        if not isinstance(payload, dict) or payload.get("model") != self.server.model_id:
            return self.respond(400, "Unexpected model")

        upstream_request = Request(
            UPSTREAM_URL,
            data=body,
            headers={
                "Authorization": "Bearer " + self.server.provider_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            upstream_response = self.server.opener.open(upstream_request, timeout=180)
        except HTTPError as error:
            upstream_response = error
        except URLError:
            return self.respond(502, "OpenRouter could not be reached")

        with upstream_response:
            self.send_response(upstream_response.status)
            self.send_header(
                "Content-Type",
                upstream_response.headers.get("Content-Type", "application/json"),
            )
            self.end_headers()
            try:
                while chunk := upstream_response.read(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()

    provider_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not provider_key:
        parser.error("OPENROUTER_API_KEY is required")

    server = ThreadingHTTPServer((args.bind, 0), Handler)
    server.proxy_token = args.token
    server.provider_key = provider_key
    server.linear_key = os.environ.get("LINEAR_API_KEY", "")
    server.model_id = args.model
    server.opener = build_opener(NoRedirect)

    with open(args.ready, "w", encoding="ascii") as ready:
        ready.write(str(server.server_port))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
