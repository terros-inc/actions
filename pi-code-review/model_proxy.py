#!/usr/bin/env python3
"""Forward only OpenRouter chat completions, adding the host-held API key."""

import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

UPSTREAM_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_REQUEST_BYTES = 16 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


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

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self.respond(404, "Unsupported model endpoint")

        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.proxy_token
        if not hmac.compare_digest(supplied, expected):
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
