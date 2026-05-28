# 🧠 Claude Code + DeepSeek Thinking Mode Proxy

![CI](https://img.shields.io/github/actions/workflow/status/HiChat-fog/claude-deepseek-proxy/ci.yml?branch=master&style=flat-square&label=CI)
![Python](https://img.shields.io/badge/Python-3.9%2B-blue?style=flat-square)
![License](https://img.shields.io/github/license/HiChat-fog/claude-deepseek-proxy?style=flat-square)

**Enable thinking mode for Claude Code with DeepSeek V4 Pro** — a crash-resistant HTTPS proxy that fixes the "thinking block must be passed back" error. Set it up once, forget about it.

**让 Claude Code + DeepSeek V4 Pro 的 thinking 模式真正能用** — 防崩+自动重启的 HTTPS proxy，解决 "thinking block must be passed back" 报错。设一次，再也不用管。

---

## What's the problem? / 啥问题？

When using DeepSeek V4 Pro with Claude Code and thinking mode enabled, multi-turn conversations blow up:

用 DeepSeek V4 Pro 跑 Claude Code，开着 thinking 模式，多轮对话直接炸：

1. **400: content[].thinking must be passed back** — DeepSeek returns thinking blocks in responses, but Claude Code doesn't pass them back in the next request. DeepSeek then rejects it. This is essentially a Claude Code bug — it wasn't designed for DeepSeek.

   **400: content[].thinking must be passed back** — DeepSeek 返回了 thinking 块，Claude Code 下一轮没传回去，DeepSeek 就报错了。本质是 Claude Code 的 bug，它不是给 DeepSeek 设计的。

2. **400: thinking cannot be disabled when reasoning_effort is set** — Can you just disable thinking? Nope. Claude Code's `effortLevel` sends `reasoning_effort`, and DeepSeek mandates thinking must be enabled when reasoning_effort is set.

   **400: thinking cannot be disabled when reasoning_effort is set** — 你说那我关 thinking 不行吗？不行。Claude Code 的 `effortLevel` 会发 `reasoning_effort`，DeepSeek 规定设了 reasoning_effort 就必须开 thinking。

Can't enable it, can't disable it — you're stuck. This proxy fixes that.

开也不行关也不行，卡死了。这个 proxy 就是来解决这个的。

---

## How it works / 怎么解决的

```
Claude Code  ──HTTPS──▶  Proxy (127.0.0.1:9191)  ──HTTPS──▶  DeepSeek API
                           │
                           ├─ Request: assistant msg missing thinking blocks?
                           │           Inject them from cache!
                           │           Ensure thinking: enabled
                           │           Detect disable_parallel_tool_use
                           │
                           ├─ Response: cache thinking blocks
                           │            Pass through to Claude Code as-is
                           │            Enforce serial tool calls if requested
                           │
                           └─ Crash-proof: try/catch per request + auto-restart
```

| Direction | What it does | Why |
|-----------|-------------|-----|
| Request | Inject cached thinking blocks into assistant messages that are missing them | DeepSeek requires them, Claude Code doesn't send them |
| Request | Ensure `thinking: enabled` | Required by reasoning_effort |
| Request | Convert `redacted_thinking` back to `thinking` | DeepSeek only accepts `thinking` type |
| Request | Detect `disable_parallel_tool_use` flag in tools | DeepSeek silently ignores this flag |
| Response | Cache thinking blocks | Needed for future request injection |
| Response | Strip extra tool_use blocks if `disable_parallel_tool_use` was set | DeepSeek ignores the flag, we enforce it |
| Response | Pass through as-is | Claude Code stores them normally |

TL;DR: **Claude Code doesn't pass back thinking blocks? No worries — the proxy remembers them and auto-injects every request. DeepSeek ignores `disable_parallel_tool_use`? The proxy enforces it.**

简单说就是：**Claude Code 不传 thinking 块？没关系，proxy 帮你记着，每次请求自动补上。DeepSeek 忽略 `disable_parallel_tool_use`？proxy 帮你强制执行。**

---

## Crash Resistance / 防崩机制

Nobody wants their proxy dying silently in the middle of a session. This proxy has two layers of protection:

谁也不想 proxy 跑着跑着静悄悄挂了。这个 proxy 有两层防护：

1. **Request-level error handling / 请求级防崩** — Each request is wrapped in a try/except. If something goes wrong with a single request (bad JSON, timeout, weird API response), the proxy returns a 502 error for that request but **keeps running**. No more one bad request killing the whole server.

   每个请求都包了 try/except。单个请求出了问题（JSON 解析失败、超时、API 返回奇怪的东西），proxy 只会返回 502，**不会崩**。不会再出现一个请求搞死整个 server 的情况。

2. **Auto-restart loop / 进程级自愈** — If the server somehow crashes entirely, it automatically restarts within 3 seconds. Port already in use? It retries every 5 seconds. Only `Ctrl+C` will actually stop it.

   如果 server 还是不幸崩了，3 秒内自动重启。端口被占了？每 5 秒重试。只有 `Ctrl+C` 才能真正停掉它。

### Auto-start on boot / 开机自启 (Windows)

Put this VBS file in your Startup folder (`Win+R` → `shell:startup`):

把下面的 VBS 文件放到启动文件夹（`Win+R` → `shell:startup`）：

```vbs
Set objShell = CreateObject("WScript.Shell")
objShell.Run "python ""C:\path\to\claude_deepseek_proxy.py"" 9191", 0, False
```

This launches the proxy silently on login. Set it and forget it.

开机自动后台启动 proxy，设一次就不用管了。

---

## Quick Start / 快速开始

### Install dependencies / 装依赖

```bash
pip install cryptography
```

### Clone & install cert / clone & 安装证书

```bash
git clone https://github.com/HiChat-fog/claude-deepseek-proxy.git
cd claude-deepseek-proxy

# Generate cert + install to system trust store (one-time setup)
# 生成证书 + 装到系统信任区（只需跑一次）
python claude_deepseek_proxy.py --install
```

### Start the proxy / 启动 proxy

```bash
# Default port 9191 / 默认 9191 端口
python claude_deepseek_proxy.py

# Or specify a port / 或者指定端口
python claude_deepseek_proxy.py 8443
```

The proxy auto-restarts on crash. If you see `Auto-restart enabled` in the log, you're covered.

proxy 崩了会自动重启。日志里看到 `Auto-restart enabled` 就说明防护已开启。

### Configure Claude Code / 配置 Claude Code

Edit `~/.claude/settings.json`:

编辑 `~/.claude/settings.json`：

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

Restart Claude Code and you're good to go. 重启 Claude Code，搞定。

### Uninstall / 卸载

```bash
python claude_deepseek_proxy.py --uninstall
```

Removes the CA cert from trust store and deletes cert files. Clean exit.

删证书、清文件，干干净净。

---

## Environment Variables / 环境变量

| Variable | Default | Description |
|----------|---------|-------------|
| `DEEPSEEK_API_URL` | `https://api.deepseek.com/anthropic` | Upstream API URL / 上游 API 地址 |
| `THINKING_BUDGET` | `10000` | Thinking token budget / thinking token 预算 |

---

## About the Certificate / 关于证书

The proxy generates a self-signed CA certificate and installs it to your system trust store, so Claude Code won't complain about SSL errors over HTTPS. The CA-issued server cert is for `127.0.0.1`, using `IPAddress` in SAN (not `DNSName` — using DNSName for an IP causes hostname mismatch, that's a gotcha).

proxy 会生成一个自签名 CA 证书装到你系统信任区，这样 Claude Code 走 HTTPS 就不会报 SSL 错误。CA 签发的 server 证书给 `127.0.0.1` 用，SAN 用的是 `IPAddress`（不是 `DNSName`，用 DNSName 会 hostname mismatch，这是个坑）。

---

## DeepSeek Anthropic API Compatibility Report / 兼容性清单

We systematically tested DeepSeek's Anthropic-compatible endpoint against the real Anthropic API spec. Here's what we found — including issues that few people in the community have documented.

我们系统性地测试了 DeepSeek Anthropic 兼容端点与真实 Anthropic API 规范的差异。以下是我们发现的问题——包括社区中少有人记录的。

### ✅ Fixed by this proxy / 本 proxy 已修复

| # | Issue / 问题 | Severity | Status |
|---|---|---|---|
| 1 | **`thinking` block must be passed back** — DeepSeek 400 if missing in multi-turn | 🔴 Critical | ✅ Fixed — cached + injected |
| 2 | **`redacted_thinking` rejected** — DeepSeek doesn't accept this type | 🟠 High | ✅ Fixed — converted to `thinking` |
| 3 | **`disable_parallel_tool_use` ignored** — DeepSeek returns multiple tool_calls even when tools request serial execution | 🟠 High | ✅ Fixed — proxy strips extra tool_use blocks |

### ⚠️ Known limitations (not fixable by proxy) / 已知限制（proxy 无法修复）

| # | Issue / 问题 | Severity | Impact / 影响 |
|---|---|---|---|
| 4 | **`cache_control` silently ignored** — `cache_read_input_tokens` always returns 0 | 🔴 Critical | Every request reprocesses the entire system prompt from scratch. Combined with Claude Code's billing header, this can cause **~10x cost increase**. Workaround: set `CLAUDE_CODE_ATTRIBUTION_HEADER=false` |
| 5 | **`thinking.budget_tokens` ignored** — `--effort max` has no effect | 🟠 High | Thinking depth is always the same regardless of budget. `--effort` is effectively useless with DeepSeek |
| 6 | **`disable_parallel_tool_use` ignored** (original issue) | 🟠 High | Can cause file operation order issues in Claude Code (parallel writes corrupting each other). Proxy now enforces this server-side |
| 7 | **`is_error` in `tool_result` silently ignored** | 🟡 Medium | DeepSeek treats error tool results as successful. If a tool returns `is_error: true` with empty content, the model won't know it failed. In practice, most errors include descriptive text so the model can infer failure from content |
| 8 | **`thinking.type=adaptive` silently downgraded** — no error, but behaves as `enabled` | 🟡 Low | No 400 error, but behavior differs from native Anthropic API |
| 9 | **Model name mapping** — `claude-opus-*` → `deepseek-v4-pro`, `claude-sonnet-*`/`claude-haiku-*` → `deepseek-v4-flash` | 🟡 Medium | Claude Code's SubAgent may expect a cheap model (haiku) but gets flash. Users may not notice the model difference |

### 📊 Test methodology / 测试方法

All findings were verified by sending real API requests to `https://api.deepseek.com/anthropic/v1/messages` and comparing behavior against the Anthropic API specification. Tests included:

- `is_error: true` with empty content vs. descriptive content
- `disable_parallel_tool_use: true` with multi-tool prompts
- `thinking.type: adaptive` vs `enabled`
- `budget_tokens` from 100 to 50000
- `cache_control` breakpoints in system prompts
- Model name resolution for `claude-opus-*`, `claude-sonnet-*`, `claude-haiku-*`

### 💡 Why isn't this a "security vulnerability"? / 为什么这不是"安全漏洞"？

These are **API protocol compatibility defects**, not security vulnerabilities. They don't leak data, bypass access controls, or allow privilege escalation. However, they can cause **silent logic errors** — the most dangerous kind, because the user gets wrong results without any error message.

这些都是 **API 协议兼容性缺陷**，不是安全漏洞。不会泄露数据、不会绕过权限。但它们会导致**静默逻辑错误**——最危险的那种，因为用户拿到错误结果却看不到任何报错。

---

## Troubleshooting / 踩坑记录

### SSL certificate hostname mismatch

The certificate SAN must use `IPAddress` for IP addresses, not `DNSName`. If you hit this, regenerate:

证书 SAN 里 IP 地址必须用 `IPAddress`，不能用 `DNSName`。遇到这个就重新生成：

```bash
python claude_deepseek_proxy.py --uninstall
python claude_deepseek_proxy.py --install
```

### Proxy not receiving requests / Proxy 收不到请求

Claude Code only connects to `https://` endpoints — it ignores `http://`. Make sure `ANTHROPIC_BASE_URL` is `https://127.0.0.1:PORT`.

Claude Code 只走 `https://`，`http://` 的 endpoint 它不理。确保 `ANTHROPIC_BASE_URL` 是 `https://127.0.0.1:PORT`。

### 400: thinking cannot be disabled when reasoning_effort is set

This is exactly what the proxy fixes. Make sure the proxy is running and `ANTHROPIC_BASE_URL` points to it.

这正是这个 proxy 解决的问题。确保 proxy 在跑，`ANTHROPIC_BASE_URL` 指向它。

### ConnectionRefused / 连接被拒绝

The proxy process might have died. With the auto-restart feature this should be rare, but if it happens, just restart it manually. Also make sure the port isn't blocked by a firewall.

proxy 进程可能挂了。有自动重启功能后应该很少出现，但如果碰到了手动重启就行。另外确认端口没被防火墙拦。

---

## License

MIT
