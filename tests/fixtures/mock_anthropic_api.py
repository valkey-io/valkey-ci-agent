"""Mock Anthropic Messages API for driving the real Claude Code CLI in tests.

Used by tests/test_claude_code_confinement.py.

Scenario file (JSON): {"tool": "Read", "input": {...}}. The first request gets
that tool_use; the next request carries the tool_result, which is appended to
results.jsonl, and gets a final text answer. Supports streaming (SSE) and
non-streaming requests.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.environ.get("MOCK_API_DIR") or os.path.dirname(os.path.abspath(__file__))
SCENARIO = os.path.join(HERE, "scenario.json")
RESULTS = os.path.join(HERE, "results.jsonl")
REQUESTS = os.path.join(HERE, "requests.log")


def _tool_result(body):
    for message in reversed(body.get("messages", [])):
        if message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return block
    return None


def _message(blocks, stop_reason):
    return {
        "id": "msg_mock", "type": "message", "role": "assistant", "model": "claude-mock",
        "content": blocks, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def _sse(blocks, stop_reason):
    out = []

    def event(name, data):
        out.append(f"event: {name}\ndata: {json.dumps(data)}\n\n")

    start = _message([], None)
    start["usage"] = {"input_tokens": 10, "output_tokens": 1}
    event("message_start", {"type": "message_start", "message": start})
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            event("content_block_start", {"type": "content_block_start", "index": index,
                                          "content_block": {"type": "text", "text": ""}})
            event("content_block_delta", {"type": "content_block_delta", "index": index,
                                          "delta": {"type": "text_delta", "text": block["text"]}})
        else:
            event("content_block_start", {"type": "content_block_start", "index": index,
                                          "content_block": {"type": "tool_use", "id": block["id"],
                                                            "name": block["name"], "input": {}}})
            event("content_block_delta", {"type": "content_block_delta", "index": index,
                                          "delta": {"type": "input_json_delta",
                                                    "partial_json": json.dumps(block["input"])}})
        event("content_block_stop", {"type": "content_block_stop", "index": index})
    event("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason,
                                                               "stop_sequence": None},
                            "usage": {"output_tokens": 10}})
    event("message_stop", {"type": "message_stop"})
    return "".join(out).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        with open(REQUESTS, "a") as log:
            log.write(self.path + "\n")
        if "/messages" not in self.path or "count_tokens" in self.path:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"input_tokens": 10}).encode())
            return
        body = json.loads(raw or b"{}")
        result = _tool_result(body)
        tools = [t.get("name") for t in body.get("tools", [])]
        scenario = json.load(open(SCENARIO))
        steps = scenario.get("steps") or [{"tool": scenario["tool"], "input": scenario["input"]}]
        done = sum(
            1 for m in body.get("messages", []) if m.get("role") == "user" and isinstance(m.get("content"), list)
            for block in m["content"] if isinstance(block, dict) and block.get("type") == "tool_result"
        )
        if result is not None:
            with open(RESULTS, "a") as out:
                out.write(json.dumps(result) + "\n")
        if done < len(steps):
            step = steps[done]
            if step["tool"] not in tools:
                blocks = [{"type": "text", "text": f"TOOL-NOT-OFFERED {step['tool']} {tools}"}]
                stop = "end_turn"
            else:
                blocks = [{"type": "tool_use", "id": f"toolu_mock{done}", "name": step["tool"],
                           "input": step["input"]}]
                stop = "tool_use"
        else:
            blocks = [{"type": "text", "text": "done"}]
            stop = "end_turn"
        if body.get("stream"):
            payload = _sse(blocks, stop)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
        else:
            payload = json.dumps(_message(blocks, stop)).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
