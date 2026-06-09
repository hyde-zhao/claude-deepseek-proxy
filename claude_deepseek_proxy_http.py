"""
HTTP (non-SSL) version of the proxy.
Eliminates local SSL deadlock entirely.
Claude Code connects via HTTP, proxy forwards to the configured upstream.
"""
import base64
import hashlib
import http.client
import json
import os
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse


def load_dotenv(path: str = ".env"):
    """Load environment variables from a .env file without external deps.

    Existing OS environment variables are kept and take precedence.
    Supported lines:
      KEY=value
      KEY="value"
      KEY='value'
    Lines starting with # are ignored.
    """
    dotenv_path = os.path.abspath(path)
    if not os.path.isfile(dotenv_path):
        return
    with open(dotenv_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_dotenv()

DEEPSEEK_URL = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/anthropic")
THINKING_BUDGET = int(os.environ.get("THINKING_BUDGET", "4000"))

FORWARD_HEADERS = {
    "content-type", "authorization", "x-api-key",
    "anthropic-version", "anthropic-beta",
}

_thinking_store = {}
_parallel_disabled = False

def _upstream_url(path):
    return DEEPSEEK_URL.rstrip("/") + path

def _hash_text(text):
    return hashlib.md5(text.encode()).hexdigest()[:16]

def patch_request(data):
    global _parallel_disabled
    _parallel_disabled = False
    tools = data.get("tools", [])
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.get("disable_parallel_tool_use", False):
                _parallel_disabled = True
                break

    # DeepSeek V4 Pro can reject thinking: disabled even when Claude Code omits
    # reasoning_effort, which happens with some sub-agent requests.
    thinking = data.get("thinking", {})
    if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
        data["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}

    messages = data.get("messages", [])
    injected_count = 0
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        has_thinking = any(
            isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
            for b in content
        )
        if has_thinking:
            new_content = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "redacted_thinking":
                    try:
                        thinking_text = base64.b64decode(b.get("data", "")).decode()
                    except Exception:
                        thinking_text = "[thinking redacted]"
                    new_content.append({"type": "thinking", "thinking": thinking_text})
                else:
                    new_content.append(b)
            msg["content"] = new_content
        else:
            text_parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            text_hash = _hash_text("".join(text_parts))
            if text_hash in _thinking_store:
                stored = _thinking_store[text_hash]
                thinking_blocks = []
                for tb in stored:
                    if tb.get("type") == "thinking":
                        thinking_blocks.append(tb)
                    elif tb.get("type") == "redacted_thinking":
                        try:
                            thinking_text = base64.b64decode(tb.get("data", "")).decode()
                        except Exception:
                            thinking_text = "[thinking redacted]"
                        thinking_blocks.append({"type": "thinking", "thinking": thinking_text})
                msg["content"] = thinking_blocks + list(content)
                injected_count += 1
            else:
                placeholder = {"type": "thinking", "thinking": "[proxy-injected thinking]"}
                msg["content"] = [placeholder] + list(content)
                injected_count += 1
    return data, injected_count

def patch_response(data):
    global _parallel_disabled
    content = data.get("content", [])
    if not isinstance(content, list):
        return data, False
    if _parallel_disabled:
        tool_use_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if len(tool_use_blocks) > 1:
            first_tool = True
            new_content = []
            removed = 0
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    if first_tool:
                        new_content.append(b)
                        first_tool = False
                    else:
                        removed += 1
                else:
                    new_content.append(b)
            content = new_content
            data["content"] = content
            if removed > 0:
                print(f"[{time.strftime('%H:%M:%S')}] PARALLEL-FIX: removed {removed} tool_use", flush=True)
        _parallel_disabled = False
    thinking_blocks = [b for b in content if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")]
    if not thinking_blocks:
        return data, False
    text_parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    if text_parts:
        text_hash = _hash_text("".join(text_parts))
        _thinking_store[text_hash] = thinking_blocks
    return data, True


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()

    def _send_upstream_error(self, status, raw):
        try:
            json.loads(raw)
            body = raw
        except Exception:
            text = raw.decode("utf-8", errors="replace")
            body = json.dumps({
                "error": {
                    "type": "upstream_error",
                    "message": text[:2000] or f"upstream returned HTTP {status}"
                }
            }).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _forward(self):
        try:
            self._forward_inner()
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] ERR: {type(e).__name__}: {e}", flush=True)
            try:
                self._send_json(502, {
                    "error": {
                        "type": "proxy_error",
                        "message": str(e)
                    }
                })
            except Exception:
                pass

    def _forward_inner(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        injected = 0
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                data, injected = patch_request(data)
                body = json.dumps(data).encode()
                ts = time.strftime("%H:%M:%S")
                msgs = len(data.get("messages", []))
                print(f"[{ts}] REQ {len(body)}b {msgs}msgs inject={injected}", flush=True)
        except (json.JSONDecodeError, TypeError) as e:
            print(f"[{time.strftime('%H:%M:%S')}] PARSE ERR: {e}", flush=True)

        headers = {k: v for k, v in self.headers.items() if k.lower() in FORWARD_HEADERS}
        headers["Host"] = urlparse(DEEPSEEK_URL).hostname

        # Only use SSE when the client explicitly requested stream=true.
        # Forcing /messages into SSE makes non-stream Claude Code calls fail
        # with "Failed to parse JSON".
        is_stream = isinstance(data, dict) and data.get("stream", False)

        if is_stream:
            self._handle_stream(body, headers)
            return

        parsed = urlparse(_upstream_url(self.path))
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=300)
        else:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=300)
        try:
            conn.request("POST", parsed.path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status >= 400:
                print(f"[{time.strftime('%H:%M:%S')}] ERR {resp.status}: {raw[:500]}", flush=True)
                self._send_upstream_error(resp.status, raw)
                return
            try:
                j = json.loads(raw)
                j, _ = patch_response(j)
                raw = json.dumps(j).encode()
            except Exception:
                pass
            self.send_response(resp.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()
            print(f"[{time.strftime('%H:%M:%S')}] RESP {resp.status} {len(raw)}b", flush=True)
        finally:
            conn.close()

    def _handle_stream(self, body, forward_headers):
        parsed = urlparse(_upstream_url(self.path))
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=300)
        else:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=300)
        try:
            conn.request("POST", parsed.path, body=body, headers=forward_headers)
            resp = conn.getresponse()
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] STREAM {resp.status}", flush=True)

            if resp.status != 200:
                err = resp.read()
                self._send_upstream_error(resp.status, err)
                return

            self.send_response(200)
            self.send_header("Content-Type", resp.getheader("Content-Type", "text/event-stream"))
            self.send_header("Cache-Control", "no-cache")
            self.close_connection = True
            self.end_headers()

            accumulated = bytearray()
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                accumulated.extend(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
            self.wfile.flush()

            # Cache thinking blocks from accumulated data
            if accumulated:
                text = accumulated.decode("utf-8", errors="replace")
                thinking_blocks = []
                text_parts = []
                for line in text.split("\n"):
                    s = line.strip()
                    if not s.startswith("data: "):
                        continue
                    try:
                        event = json.loads(s[6:])
                        etype = event.get("type", "")
                        if etype == "content_block_start":
                            cb = event.get("content_block", {})
                            if isinstance(cb, dict) and cb.get("type") in ("thinking", "redacted_thinking"):
                                thinking_blocks.append(cb)
                        elif etype == "content_block_delta":
                            delta = event.get("delta", {})
                            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                                text_parts.append(delta.get("text", ""))
                    except Exception:
                        pass
                if thinking_blocks and text_parts:
                    text_hash = _hash_text("".join(text_parts))
                    _thinking_store[text_hash] = thinking_blocks
                    print(f"[{ts}] cached {len(thinking_blocks)} thinking", flush=True)

            print(f"[{ts}] STREAM done {len(accumulated)}b", flush=True)
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] STREAM broke: {type(e).__name__}: {e}", flush=True)
        finally:
            conn.close()

    do_POST = _forward
    do_PATCH = _forward
    do_PUT = _forward

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"claude-deepseek-proxy-http ok")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9191
    while True:
        try:
            server = HTTPServer(("127.0.0.1", port), ProxyHandler)
            print(f"[proxy-http] http://127.0.0.1:{port} -> {DEEPSEEK_URL}", flush=True)
            print(f"[proxy-http] Set ANTHROPIC_BASE_URL=http://127.0.0.1:{port}", flush=True)
            print("[proxy-http] Auto-restart enabled - will recover from crashes", flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[proxy-http] Shutting down.")
            try:
                server.shutdown()
            except Exception:
                pass
            break
        except OSError as e:
            if "10048" in str(e) or "Address already in use" in str(e):
                print(f"[proxy-http] Port {port} already in use, retrying in 5s...", flush=True)
                time.sleep(5)
                continue
            print(f"[proxy-http] OSError: {e}, restarting in 3s...", flush=True)
            time.sleep(3)
        except Exception as e:
            print(f"[proxy-http] Crash: {type(e).__name__}: {e}, restarting in 3s...", flush=True)
            time.sleep(3)

if __name__ == "__main__":
    main()
