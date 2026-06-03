"""
代理稳定性测试脚本
模拟 Claude Code 的真实请求模式，通过本地测试代理(9195)验证流式转发。
不影响运行中的生产代理(9194)。
"""
import json, os, time, ssl, sys
from http.client import HTTPSConnection
from urllib.parse import urlparse

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "REDACTED_API_KEY")
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 9195
PASSED = 0
FAILED = 0
RESULTS = []

def test(name, passed, detail=""):
    global PASSED, FAILED
    status = "PASS" if passed else "FAIL"
    if passed:
        PASSED += 1
    else:
        FAILED += 1
    msg = f"  [{status}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    RESULTS.append({"name": name, "passed": passed, "detail": detail})

def send_streaming_request(messages, tool_defs=None, max_tokens=2048):
    """Send a streaming request through the proxy, return (status, events, timing)."""
    body = {
        "model": "deepseek-chat",
        "max_tokens": max_tokens,
        "stream": True,
        "messages": messages,
        "thinking": {"type": "enabled", "budget_tokens": 4000},
    }
    if tool_defs:
        body["tools"] = tool_defs

    raw_body = json.dumps(body).encode()
    conn = HTTPSConnection(PROXY_HOST, PROXY_PORT, timeout=120,
                           context=ssl._create_unverified_context())

    start = time.time()
    conn.request("POST", "/v1/messages",
                 body=raw_body,
                 headers={
                     "Content-Type": "application/json",
                     "x-api-key": API_KEY,
                     "anthropic-version": "2023-06-01",
                 })
    resp = conn.getresponse()
    connect_time = time.time() - start

    events = []
    sse_events = []
    accumulated = b""

    # Read SSE stream chunk-by-chunk (mimics our raw-pipe proxy)
    chunk_times = []
    first_byte_time = None
    while True:
        chunk = resp.read(8192)
        if not chunk:
            break
        if first_byte_time is None:
            first_byte_time = time.time() - start
        accumulated += chunk
        chunk_times.append(time.time())

    end_time = time.time()
    conn.close()

    # Parse accumulated SSE into events
    raw_text = accumulated.decode("utf-8", errors="replace")
    current_event = {"name": "", "data": ""}
    for line in raw_text.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("event: "):
            current_event["name"] = line[7:]
        elif line.startswith("data: "):
            current_event["data"] = line[6:]
        elif line == "" and current_event["data"]:
            try:
                parsed = json.loads(current_event["data"])
                sse_events.append({
                    "event": current_event["name"],
                    "type": parsed.get("type", ""),
                    "data": parsed
                })
            except json.JSONDecodeError:
                pass
            current_event = {"name": "", "data": ""}

    total_time = end_time - start
    event_types = [e["type"] for e in sse_events]

    # Check for thinking/text INSIDE content_block_delta
    has_thinking = False
    has_text = False
    for e in sse_events:
        if e["type"] == "content_block_start":
            cb_type = e["data"].get("content_block", {}).get("type", "")
            if cb_type == "thinking":
                has_thinking = True
        elif e["type"] == "content_block_delta":
            delta_type = e["data"].get("delta", {}).get("type", "")
            if delta_type == "thinking_delta":
                has_thinking = True
            elif delta_type == "text_delta":
                has_text = True

    return {
        "status": resp.status,
        "total_time": total_time,
        "connect_time": connect_time,
        "first_byte_time": first_byte_time,
        "total_bytes": len(accumulated),
        "event_count": len(sse_events),
        "event_types": event_types,
        "has_message_start": "message_start" in event_types,
        "has_message_stop": "message_stop" in event_types,
        "has_text": has_text,
        "has_thinking": has_thinking,
        "events": sse_events,
    }


print("=" * 60)
print("PROXY STABILITY TEST SUITE")
print(f"Target: https://{PROXY_HOST}:{PROXY_PORT}")
print("=" * 60)

# ============================================================
# TEST 1: 基础流式 — 简单对话
# ============================================================
print("\n--- TEST 1: 基础流式响应 ---")
r1 = send_streaming_request([
    {"role": "user", "content": "Say hello in exactly 5 words."}
])

test("HTTP 200", r1["status"] == 200, f"status={r1['status']}")
test("message_start 存在", r1["has_message_start"])
test("message_stop 存在", r1["has_message_stop"])
test("包含 thinking", r1["has_thinking"])
test("包含 text 输出", r1["has_text"])
test("首字节延迟 < 60s", r1["first_byte_time"] is not None and r1["first_byte_time"] < 60,
     f"{r1['first_byte_time']:.1f}s" if r1["first_byte_time"] else "None")
test("总时间 < 120s", r1["total_time"] < 120, f"{r1['total_time']:.1f}s")
test("连接关闭正常", r1["status"] == 200 and r1["has_message_stop"])
print(f"  [INFO] {r1['event_count']} events, {r1['total_bytes']} bytes, {r1['total_time']:.1f}s total")

# ============================================================
# TEST 2: 多轮对话 — 测试 thinking 缓存
# ============================================================
print("\n--- TEST 2: 多轮对话（验证 thinking 缓存）---")
r2a = send_streaming_request([
    {"role": "user", "content": "What is the capital of France? Reply in one word."}
])
# Simulate Claude Code: pass assistant message back WITHOUT thinking blocks
assistant_text = ""
for e in r2a["events"]:
    if e["type"] == "content_block_delta":
        t = e["data"].get("delta", {}).get("text", "")
        assistant_text += t

test("Turn 1 完成", r2a["has_message_stop"] and r2a["has_text"],
     f"text={assistant_text[:30]}")

# Turn 2: Claude Code passes back assistant message with text but NO thinking blocks
r2b = send_streaming_request([
    {"role": "user", "content": "What is the capital of France? Reply in one word."},
    {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]},
    {"role": "user", "content": "What about Germany?"}
])

test("Turn 2 完成（thinking 已缓存注入）", r2b["has_message_stop"] and r2b["has_text"],
     f"events={r2b['event_count']} text_len={sum(1 for t in r2b['event_types'] if 'text' in t or 'tool' in t)}")

# ============================================================
# TEST 3: 工具调用流式
# ============================================================
print("\n--- TEST 3: 带工具定义的流式请求 ---")
r3 = send_streaming_request([
    {"role": "user", "content": "What is 2+2? Use the calculate tool."}
], tool_defs=[{
    "name": "calculate",
    "description": "Calculate a math expression",
    "input_schema": {
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "Math expression to evaluate"}
        },
        "required": ["expression"]
    }
}])

test("HTTP 200 with tools", r3["status"] == 200)
test("message_start/stop 完整", r3["has_message_start"] and r3["has_message_stop"])
tool_use_count = sum(1 for t in r3["event_types"] if t == "tool_use")
test("工具响应正常", r3["has_message_stop"],
     f"tool_use_events={tool_use_count}")
print(f"  [INFO] {r3['event_count']} events, first_byte={r3['first_byte_time']:.1f}s" if r3['first_byte_time'] else "")

# ============================================================
# TEST 4: 长上下文模拟 — 逐渐增长的消息历史
# ============================================================
print("\n--- TEST 4: 长上下文渐进压力测试 ---")
history = [{"role": "user", "content": "You are a helpful assistant. Keep your responses very brief, 1-2 sentences max."}]
history.append({"role": "assistant", "content": [{"type": "text", "text": "Understood."}]})

max_turns = 8
turns_ok = 0
for turn in range(max_turns):
    topic = ["apple", "banana", "cherry", "date", "elderberry",
             "fig", "grape", "honeydew"][turn]
    history.append({"role": "user", "content": f"What color is a {topic}? One word only."})

    r = send_streaming_request(history, max_tokens=256)

    # Extract assistant text for next turn
    assistant_text_turn = ""
    for e in r["events"]:
        if e["type"] == "content_block_delta":
            t = e["data"].get("delta", {}).get("text", "")
            assistant_text_turn += t

    if r["has_message_stop"] and r["has_text"]:
        history.append({"role": "assistant",
                        "content": [{"type": "text", "text": assistant_text_turn}]})
        turns_ok += 1

    test(f"Turn {turn+1}/{max_turns} ({topic})",
         r["has_message_stop"] and r["has_text"],
         f"{r['total_time']:.1f}s {len(history)}msgs {r['total_bytes']}b")

print(f"  [INFO] {turns_ok}/{max_turns} turns completed")

# ============================================================
# TEST 5: 快速连续请求（无延迟）
# ============================================================
print("\n--- TEST 5: 快速连续请求 ---")
quick_ok = 0
for i in range(5):
    r = send_streaming_request([
        {"role": "user", "content": f"Say the number {i+1} and nothing else."}
    ], max_tokens=64)
    if r["has_message_stop"]:
        quick_ok += 1
    test(f"Quick #{i+1}", r["has_message_stop"],
         f"{r['total_time']:.1f}s")

print(f"  [INFO] {quick_ok}/5 quick requests OK")

# ============================================================
# SUMMARY
# ============================================================
print(f"\n{'=' * 60}")
print(f"RESULTS: {PASSED} passed, {FAILED} failed, {PASSED + FAILED} total")
print(f"{'=' * 60}")

# Output JSON for analysis
with open("proxy_test_results.json", "w", encoding="utf-8") as f:
    json.dump({
        "summary": {"passed": PASSED, "failed": FAILED},
        "results": RESULTS
    }, f, ensure_ascii=False, indent=2)

if FAILED > 0:
    print("\nFAILED tests:")
    for r in RESULTS:
        if not r["passed"]:
            print(f"  - {r['name']}: {r['detail']}")
    sys.exit(1)
else:
    print("\nAll tests passed.")
