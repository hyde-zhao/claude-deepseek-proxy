"""
ClaudeCode-DeepSeek-Proxy
Fixes thinking block incompatibility between Claude Code + DeepSeek V4 Pro.

Problem:
  DeepSeek V4 Pro returns thinking blocks in responses. In multi-turn
  conversations, Claude Code doesn't pass them back, causing 400 errors:
  "content[].thinking in the thinking mode must be passed back to the API"
  Also, when reasoning_effort is set, thinking cannot be disabled.

Solution:
  - Response: Cache thinking blocks from DeepSeek responses.
  - Request: When Claude Code doesn't pass thinking blocks back in assistant
    messages, inject the cached thinking blocks automatically.
  - Ensure thinking: enabled is always set (required by reasoning_effort).
  - Convert redacted_thinking blocks back to thinking blocks for DeepSeek
    (DeepSeek only accepts `thinking`, not `redacted_thinking`).
  - Enforce disable_parallel_tool_use: When a tool has disable_parallel_tool_use
    set to true, force the model to return only one tool_use at a time.
    DeepSeek silently ignores this flag, which can cause execution order issues
    in Claude Code (e.g., parallel file writes corrupting each other).
  - Auto-restart: Crash-resistant with auto-recovery loop.

One-time setup: run with --install to trust the self-signed cert.
"""

import base64
import hashlib
import json
import os
import platform
import ssl
import subprocess
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.error
import urllib.request

DEEPSEEK_URL = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/anthropic")
CERT_DIR = os.path.expanduser("~/.claude-deepseek-proxy")
CERT_FILE = os.path.join(CERT_DIR, "cert.pem")

FORWARD_HEADERS = {
    "content-type", "authorization", "x-api-key",
    "anthropic-version", "anthropic-beta",
}

THINKING_BUDGET = int(os.environ.get("THINKING_BUDGET", "10000"))

# --- Thinking block store ---
# We store thinking blocks from responses and inject them back into requests
# when Claude Code doesn't pass them back. Keyed by a hash of the text response.
_thinking_store = {}  # {hash_of_text: [thinking_blocks]}

# --- Parallel tool use tracking ---
# When any tool in the request has disable_parallel_tool_use=true, we need
# to enforce serial tool calls. DeepSeek silently ignores this flag.
# We track which request IDs have this constraint so we can strip extra
# tool_use blocks from the response.
_parallel_disabled = False


def _hash_text(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:16]


def patch_request(data):
    """Fix outgoing request to DeepSeek.

    1. Ensure thinking is ENABLED (required when reasoning_effort is set)
    2. For every assistant message missing thinking blocks,
       inject cached thinking blocks so DeepSeek doesn't 400.
    3. If Claude Code passed back redacted_thinking blocks,
       convert them to thinking blocks (DeepSeek only accepts thinking).
    4. Detect disable_parallel_tool_use flag so we can enforce it in
       the response (DeepSeek silently ignores this flag).
    """
    global _parallel_disabled

    # --- Detect disable_parallel_tool_use ---
    # Check if any tool definition has disable_parallel_tool_use=true
    _parallel_disabled = False
    tools = data.get("tools", [])
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.get("disable_parallel_tool_use", False):
                _parallel_disabled = True
                break

    # --- Strip dangerous stop_sequences ---
    # DeepSeek's stop_sequences can be abused to truncate safety refusals
    # mid-sentence (e.g., "I cannot" → truncated, looks like compliance).
    # We remove stop_sequences that match common refusal phrases.
    SAFETY_REFUSAL_STOPS = {
        "i cannot", "i can't", "i'm sorry", "i must", "however",
        "i don't", "i will not", "not able", "against", "policy",
        "illegal", "unethical", "instead", "alternatively",
    }
    stop_seqs = data.get("stop_sequences", [])
    if isinstance(stop_seqs, list) and stop_seqs:
        filtered = [s for s in stop_seqs if s.lower().strip() not in SAFETY_REFUSAL_STOPS]
        removed_count = len(stop_seqs) - len(filtered)
        if removed_count > 0:
            data["stop_sequences"] = filtered
            print(f"[{time.strftime('%H:%M:%S')}] SAFETY-FIX: Removed {removed_count} dangerous stop_sequences (safety refusal truncation prevention)", flush=True)
    # --- Fix thinking mode ---
    has_reasoning = "reasoning_effort" in data
    thinking = data.get("thinking", {})

    if has_reasoning:
        if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
            data["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}
    elif isinstance(thinking, dict) and thinking.get("type") == "disabled":
        pass
    elif not thinking or (isinstance(thinking, dict) and thinking.get("type") != "enabled"):
        data["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}

    # --- Fix assistant messages ---
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
            # Convert any redacted_thinking blocks back to thinking blocks
            # (DeepSeek only accepts thinking, not redacted_thinking)
            new_content = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "redacted_thinking":
                    try:
                        thinking_text = base64.b64decode(b.get("data", "")).decode()
                    except Exception:
                        thinking_text = "[thinking redacted by proxy]"
                    new_content.append({
                        "type": "thinking",
                        "thinking": thinking_text
                    })
                else:
                    new_content.append(b)
            msg["content"] = new_content
        else:
            # No thinking blocks at all -- inject cached or placeholder thinking blocks.
            # This is the KEY fix: DeepSeek requires thinking blocks in assistant
            # messages when thinking mode is enabled, but Claude Code drops them.

            text_parts = [
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            text_hash = _hash_text("".join(text_parts))

            if text_hash in _thinking_store:
                # Use the original thinking blocks from our cache
                stored = _thinking_store[text_hash]
                thinking_blocks = []
                for tb in stored:
                    if tb.get("type") == "thinking":
                        thinking_blocks.append(tb)
                    elif tb.get("type") == "redacted_thinking":
                        try:
                            thinking_text = base64.b64decode(tb.get("data", "")).decode()
                        except Exception:
                            thinking_text = "[thinking redacted by proxy]"
                        thinking_blocks.append({
                            "type": "thinking",
                            "thinking": thinking_text
                        })
                msg["content"] = thinking_blocks + list(content)
                injected_count += 1
            else:
                # No stored thinking -- inject a generic thinking placeholder
                placeholder = {
                    "type": "thinking",
                    "thinking": "[This thinking block was injected by the proxy because Claude Code did not pass it back]"
                }
                msg["content"] = [placeholder] + list(content)
                injected_count += 1

    return data, injected_count


def patch_response(data):
    """Fix incoming response from DeepSeek.

    1. Cache thinking blocks for future request injection.
       Keep thinking blocks as-is in the response so Claude Code can store them.
       If Claude Code passes them back, great. If not, we inject from cache.
    2. Enforce disable_parallel_tool_use: If the request had this flag set
       and DeepSeek returned multiple tool_use blocks, keep only the first one.
       DeepSeek silently ignores this flag, so we enforce it on our side.
    """
    global _parallel_disabled

    content = data.get("content", [])
    if not isinstance(content, list):
        return data, False

    # --- Enforce disable_parallel_tool_use ---
    if _parallel_disabled:
        tool_use_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if len(tool_use_blocks) > 1:
            # Keep only the first tool_use block, remove the rest
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
                print(f"[{time.strftime('%H:%M:%S')}] PARALLEL-FIX: Removed {removed} extra tool_use blocks (disable_parallel_tool_use was set)", flush=True)
        _parallel_disabled = False  # Reset for next request

    # --- Cache thinking blocks ---
    thinking_blocks = [
        b for b in content
        if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
    ]
    if not thinking_blocks:
        return data, False

    # Store thinking blocks keyed by text content hash (for later retrieval)
    text_parts = [
        b.get("text", "") for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    if text_parts:
        text_hash = _hash_text("".join(text_parts))
        _thinking_store[text_hash] = thinking_blocks

    return data, True


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Suppress default logging; we do our own

    def _forward(self):
        try:
            self._forward_inner()
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] UNHANDLED: {type(e).__name__}: {e}", flush=True)
            try:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"proxy error: {e}"}).encode())
            except Exception:
                pass

    def _forward_inner(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        # --- Request patching ---
        injected = 0
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                data, injected = patch_request(data)
                body = json.dumps(data).encode()
                ts = time.strftime("%H:%M:%S")
                msgs = len(data.get("messages", []))
                thinking_mode = data.get("thinking", {})
                print(f"[{ts}] REQ {len(body)}b {msgs}msgs thinking={thinking_mode} inject={injected}", flush=True)
        except (json.JSONDecodeError, TypeError) as e:
            print(f"[{time.strftime('%H:%M:%S')}] REQ parse error: {e}", flush=True)

        headers = {k: v for k, v in self.headers.items() if k.lower() in FORWARD_HEADERS}
        from urllib.parse import urlparse
        headers["Host"] = urlparse(DEEPSEEK_URL).hostname

        req = urllib.request.Request(DEEPSEEK_URL + self.path, data=body,
                                      headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            raw = resp.read()

            # --- Response patching ---
            cached = False
            try:
                j = json.loads(raw)
                j, cached = patch_response(j)
                raw = json.dumps(j).encode()
            except (json.JSONDecodeError, TypeError):
                pass

            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", len(raw))
            self.end_headers()
            self.wfile.write(raw)

            ts = time.strftime("%H:%M:%S")
            tag = " [cached-thinking]" if cached else ""
            print(f"[{ts}] RESP {resp.status} {len(raw)}b{tag}", flush=True)

        except urllib.error.HTTPError as e:
            err = e.read()
            print(f"[{time.strftime('%H:%M:%S')}] ERR {e.code}: {err[:500]}", flush=True)
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(err)

    do_POST = _forward
    do_PATCH = _forward
    do_PUT = _forward

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"claude-deepseek-proxy ok")


def generate_cert():
    """Generate a CA cert + server cert signed by it."""
    os.makedirs(CERT_DIR, exist_ok=True)

    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime
    import ipaddress

    # CA key + cert
    ca_key = rsa.generate_private_key(65537, 2048)
    ca_subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "ClaudeCode DeepSeek Proxy CA"),
    ])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject).issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            key_cert_sign=True, crl_sign=True, digital_signature=False,
            content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, encipher_only=False, decipher_only=False,
        ), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    # Server key + cert signed by CA
    srv_key = rsa.generate_private_key(65537, 2048)
    srv_subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1"),
    ])
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(srv_subject)
        .issuer_name(ca_cert.subject)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            x509.DNSName("localhost"),
        ]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    # Write server cert+key (concatenated for SSLContext)
    with open(CERT_FILE, "wb") as f:
        f.write(srv_cert.public_bytes(serialization.Encoding.PEM))
        f.write(srv_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()
        ))

    # Write CA cert separately for trust store import
    ca_path = os.path.join(CERT_DIR, "ca.pem")
    with open(ca_path, "wb") as f:
        f.write(ca_cert.public_bytes(serialization.Encoding.PEM))

    return ca_path


def uninstall_cert():
    """Remove CA cert from system trust store and delete cert files."""
    import shutil
    system = platform.system()

    if system == "Windows":
        subprocess.run(
            ["certutil", "-delstore", "-user", "Root", "ClaudeCode DeepSeek Proxy CA"],
            capture_output=True, text=True
        )
    elif system == "Darwin":
        subprocess.run(
            ["sudo", "security", "delete-certificate", "-c", "ClaudeCode DeepSeek Proxy CA"],
            capture_output=True, text=True
        )

    if os.path.exists(CERT_DIR):
        shutil.rmtree(CERT_DIR)
    print("[proxy] Certificates removed.")


def install_cert(ca_path: str):
    """Install CA cert to system trust store."""
    system = platform.system()
    if system == "Windows":
        cmd = ["certutil", "-addstore", "-user", "Root", ca_path]
    elif system == "Darwin":
        cmd = ["sudo", "security", "add-trusted-cert", "-d", "-r", "trustRoot",
               "-k", "/Library/Keychains/System.keychain", ca_path]
    else:
        cmd = ["sudo", "cp", ca_path,
               "/usr/local/share/ca-certificates/claude-deepseek-proxy.crt"]
        subprocess.run(cmd, check=False)
        cmd = ["sudo", "update-ca-certificates"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print("[proxy] CA certificate installed to system trust store.")
    else:
        print(f"[proxy] Failed to install cert: {result.stderr}")


def main():
    if "--uninstall" in sys.argv:
        uninstall_cert()
        return

    if "--install" in sys.argv:
        ca = generate_cert()
        install_cert(ca)
        print("[proxy] Cert installed. Run without --install to start proxy.")
        return

    if not os.path.exists(CERT_FILE):
        print("[proxy] No certificate found. Generating and installing...")
        ca = generate_cert()
        install_cert(ca)

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9191

    # Auto-restart loop: if the server crashes, restart it automatically
    while True:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(CERT_FILE)

            server = HTTPServer(("127.0.0.1", port), ProxyHandler)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)

            print(f"[proxy] https://127.0.0.1:{port} -> {DEEPSEEK_URL}", flush=True)
            print(f"[proxy] Set ANTHROPIC_BASE_URL=https://127.0.0.1:{port}", flush=True)
            print("[proxy] Auto-restart enabled — will recover from crashes", flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[proxy] Shutting down.")
            try:
                server.shutdown()
            except Exception:
                pass
            break
        except OSError as e:
            if "10048" in str(e) or "Address already in use" in str(e):
                print(f"[proxy] Port {port} already in use, retrying in 5s...", flush=True)
                time.sleep(5)
                continue
            print(f"[proxy] OSError: {e}, restarting in 3s...", flush=True)
            time.sleep(3)
        except Exception as e:
            print(f"[proxy] Crash: {type(e).__name__}: {e}, restarting in 3s...", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
