#!/usr/bin/env python3
"""Mock LLM server for hang/smoke testing.

HTTP server on :9999 that simulates OpenAI-compatible SSE streaming.
Modes controlled by MODE env var:
  ok       — immediate, short response (default)
  slow     — delayed response (3s per chunk)
  hang     — never sends completion (tests timeout)
  reset    — sends some chunks then abruptly closes connection
  auth_fail — returns 401

Usage:
    python scripts/mock_llm_server.py
    MODE=slow python scripts/mock_llm_server.py
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

MODE = os.getenv("MODE", "ok").lower()
PORT = int(os.getenv("MOCK_PORT", "9999"))
DELIMITER = "\r\n"


def sse_event(field: str, value: str) -> str:
    return f"event: {field}\ndata: {value}\n\n"


def make_chatcompletion_chunk(
    content: str, index: int = 0, finish_reason: str | None = None
) -> str:
    payload = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "mock-gpt-4o-mini",
        "choices": [
            {
                "index": index,
                "delta": {"content": content},
                "finish_reason": finish_reason,
            }
        ],
    }
    return json.dumps(payload)


def make_chatcompletion_done(content: str, prompt_tokens: int = 10, completion_tokens: int = 20) -> str:
    payload = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "mock-gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return json.dumps(payload)


class MockLLMHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        print(f"[mock-llm {MODE}] {args[0]}")

    def send_sse(self, chunks: list[str]):
        for chunk in chunks:
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.write(DELIMITER.encode())
            self.wfile.flush()

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length else ""

            if MODE == "auth_fail":
                self.send_error(401, "Unauthorized")
                return

            if MODE == "hang":
                # Send one chunk then never finish — client should timeout
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                chunk = sse_event("message", make_chatcompletion_chunk("Thinking..."))
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.write(DELIMITER.encode())
                self.wfile.flush()
                # Hang forever — client must timeout
                while True:
                    time.sleep(60)

            elif MODE == "reset":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")  # cause reset
                self.end_headers()
                chunk = sse_event("message", make_chatcompletion_chunk("Starting"))
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.write(DELIMITER.encode())
                self.wfile.flush()
                # Abruptly close
                return

            elif MODE == "slow":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                words = ["One ", "two ", "three ", "four ", "five."]
                for w in words:
                    chunk = sse_event("message", make_chatcompletion_chunk(w))
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.write(DELIMITER.encode())
                    self.wfile.flush()
                    time.sleep(0.5)
                done = sse_event("message", make_chatcompletion_chunk("", finish_reason="stop"))
                self.wfile.write(done.encode("utf-8"))
                self.wfile.write(DELIMITER.encode())
                self.wfile.flush()

            else:  # ok
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                words = ["Hello", ", ", "world", "!"]
                for w in words:
                    chunk = sse_event("message", make_chatcompletion_chunk(w))
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.write(DELIMITER.encode())
                    self.wfile.flush()
                    time.sleep(0.05)

                done = sse_event("message", make_chatcompletion_chunk("", finish_reason="stop"))
                self.wfile.write(done.encode("utf-8"))
                self.wfile.write(DELIMITER.encode())
                self.wfile.flush()

        elif self.path == "/v1/models":
            # OpenAI models endpoint
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response = {
                "object": "list",
                "data": [
                    {
                        "id": "mock-gpt-4o-mini",
                        "object": "model",
                        "created": 1712345678,
                        "owned_by": "mock",
                    }
                ],
            }
            self.wfile.write(json.dumps(response).encode("utf-8"))

        else:
            self.send_error(404, f"Unknown path: {self.path}")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"mock-llm running, MODE={MODE}\n".encode())


def main():
    server = HTTPServer(("127.0.0.1", PORT), MockLLMHandler)
    print(f"[mock-llm] Listening on 127.0.0.1:{PORT}, MODE={MODE}")
    server.serve_forever()


if __name__ == "__main__":
    main()
