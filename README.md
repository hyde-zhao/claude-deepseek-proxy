# ClaudeCode-DeepSeek-Proxy

A local HTTPS proxy that fixes the thinking block incompatibility between **Claude Code** and **DeepSeek V4 Pro** (Anthropic-compatible API).

## The Problem

When using DeepSeek V4 Pro through Claude Code with thinking mode enabled:

1. **Multi-turn 400 error**: DeepSeek returns `thinking` blocks in responses, but Claude Code doesn't pass them back in subsequent requests. DeepSeek requires them: `"content[].thinking in the thinking mode must be passed back to the API"`

2. **Can't disable thinking**: When `reasoning_effort` is set (Claude Code's `effortLevel`), DeepSeek requires thinking to be enabled: `"thinking options type cannot be disabled when reasoning_effort is set"`

This is essentially a compatibility bug in Claude Code — it wasn't designed for DeepSeek's thinking block behavior.

## How It Works

```
Claude Code  ──HTTPS──▶  Proxy (127.0.0.1:9191)  ──HTTPS──▶  DeepSeek API
                           │
                           ├─ Request:  Inject missing thinking blocks into
                           │            assistant messages (from cache)
                           │            Ensure thinking: enabled
                           │
                           └─ Response: Cache thinking blocks for future use
                                        Pass through as-is
```

| Direction | Action | Why |
|-----------|--------|-----|
| **Request** | Inject cached `thinking` blocks into assistant messages | DeepSeek requires them, Claude Code doesn't send them |
| **Request** | Ensure `thinking: enabled` | Required when `reasoning_effort` is set |
| **Request** | Convert `redacted_thinking` → `thinking` | DeepSeek only accepts `thinking` type |
| **Response** | Cache thinking blocks by text hash | For future request injection |
| **Response** | Pass thinking blocks through | Claude Code stores them normally |

## Quick Start

### Prerequisites

- Python 3.8+
- DeepSeek API key

### Install

```bash
pip install cryptography

# Clone
git clone https://github.com/YOUR_USERNAME/claude-deepseek-proxy.git
cd claude-deepseek-proxy

# Generate certificate & install to system trust store (one-time)
python claude_deepseek_proxy.py --install
```

### Run

```bash
# Start the proxy (default port 9191)
python claude_deepseek_proxy.py

# Or specify a port
python claude_deepseek_proxy.py 8443
```

### Configure Claude Code

Edit `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://127.0.0.1:9191",
    "ANTHROPIC_AUTH_TOKEN": "your-deepseek-api-key",
    "ANTHROPIC_MODEL": "DeepSeek-V4-pro[1m]"
  },
  "alwaysThinkingEnabled": true
}
```

Restart Claude Code and you're done!

### Uninstall

```bash
python claude_deepseek_proxy.py --uninstall
```

This removes the CA certificate from the system trust store and deletes all generated cert files.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DEEPSEEK_API_URL` | `https://api.deepseek.com/anthropic` | Upstream API endpoint |
| `THINKING_BUDGET` | `10000` | Token budget for thinking mode |

## How the Certificate Works

The proxy generates a self-signed CA certificate and installs it to your system trust store. This allows Claude Code to connect via HTTPS without SSL errors. The CA signs a server certificate for `127.0.0.1` with proper `IPAddress` SAN (not `DNSName` — a common pitfall that causes hostname mismatch errors).

## Troubleshooting

### SSL certificate hostname mismatch

Make sure the certificate was generated with `IPAddress` SAN, not `DNSName`. If you see this error, regenerate:

```bash
python claude_deepseek_proxy.py --uninstall
python claude_deepseek_proxy.py --install
```

### Proxy not receiving requests

Claude Code only accepts `https://` endpoints. Make sure `ANTHROPIC_BASE_URL` starts with `https://127.0.0.1:PORT`, not `http://`.

### 400: thinking cannot be disabled when reasoning_effort is set

This is exactly what the proxy fixes. Make sure the proxy is running and `ANTHROPIC_BASE_URL` points to it.

## License

MIT
