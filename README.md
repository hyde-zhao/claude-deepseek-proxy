# ClaudeCode-DeepSeek-Proxy

一个本地 HTTPS proxy，解决 **Claude Code + DeepSeek V4 Pro** 的 thinking block 兼容性问题。

## 啥问题？

用 DeepSeek V4 Pro 跑 Claude Code，开着 thinking 模式，多轮对话直接炸：

1. **400: content[].thinking must be passed back** — DeepSeek 返回了 thinking 块，Claude Code 下一轮没传回去，DeepSeek 就报错了。本质是 Claude Code 的 bug，它不是给 DeepSeek 设计的。

2. **400: thinking cannot be disabled when reasoning_effort is set** — 你说那我关 thinking 不行吗？不行。Claude Code 的 `effortLevel` 会发 `reasoning_effort`，DeepSeek 规定设了 reasoning_effort 就必须开 thinking。

开也不行关也不行，卡死了。这个 proxy 就是来解决这个的。

## 怎么解决的

```
Claude Code  ──HTTPS──▶  Proxy (127.0.0.1:9191)  ──HTTPS──▶  DeepSeek API
                           │
                           ├─ 请求：assistant 消息缺 thinking 块？
                           │         从缓存里补回去！
                           │         确保 thinking: enabled
                           │
                           └─ 响应：把 thinking 块缓存下来
                                     原样透传给 Claude Code
```

| 方向 | 干了啥 | 为啥 |
|------|--------|------|
| Request | 缺 thinking 块的 assistant 消息，从缓存补回去 | DeepSeek 要求必须传，Claude Code 不传 |
| Request | 确保 `thinking: enabled` | reasoning_effort 要求的 |
| Request | `redacted_thinking` 转回 `thinking` | DeepSeek 只认 `thinking` |
| Response | 缓存 thinking 块 | 下次请求要用 |
| Response | 原样透传 | Claude Code 正常存就行 |

简单说就是：**Claude Code 不传 thinking 块？没关系，proxy 帮你记着，每次请求自动补上。**

## 快速开始

### 装依赖

```bash
pip install cryptography
```

### clone & 安装证书

```bash
git clone https://github.com/2138299057-lang/claude-deepseek-proxy.git
cd claude-deepseek-proxy

# 生成证书 + 装到系统信任区（只需跑一次）
python claude_deepseek_proxy.py --install
```

### 启动 proxy

```bash
# 默认 9191 端口
python claude_deepseek_proxy.py

# 或者指定端口
python claude_deepseek_proxy.py 8443
```

### 配置 Claude Code

编辑 `~/.claude/settings.json`：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://127.0.0.1:9191",
    "ANTHROPIC_AUTH_TOKEN": "你的-deepseek-api-key",
    "ANTHROPIC_MODEL": "DeepSeek-V4-pro[1m]"
  },
  "alwaysThinkingEnabled": true
}
```

重启 Claude Code，搞定。

### 卸载

```bash
python claude_deepseek_proxy.py --uninstall
```

删证书、清文件，干干净净。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_URL` | `https://api.deepseek.com/anthropic` | 上游 API 地址 |
| `THINKING_BUDGET` | `10000` | thinking token 预算 |

## 关于证书

proxy 会生成一个自签名 CA 证书装到你系统信任区，这样 Claude Code 走 HTTPS 就不会报 SSL 错误。CA 签发的 server 证书给 `127.0.0.1` 用，SAN 用的是 `IPAddress`（不是 `DNSName`，用 DNSName 会 hostname mismatch，这是个坑）。

## 踩坑记录

### SSL certificate hostname mismatch

证书 SAN 里 IP 地址必须用 `IPAddress`，不能用 `DNSName`。遇到这个就重新生成：

```bash
python claude_deepseek_proxy.py --uninstall
python claude_deepseek_proxy.py --install
```

### Proxy 收不到请求

Claude Code 只走 `https://`，`http://` 的 endpoint 它不理。确保 `ANTHROPIC_BASE_URL` 是 `https://127.0.0.1:PORT`。

### 400: thinking cannot be disabled when reasoning_effort is set

这正是这个 proxy 解决的问题。确保 proxy 在跑，`ANTHROPIC_BASE_URL` 指向它。

## License

MIT
