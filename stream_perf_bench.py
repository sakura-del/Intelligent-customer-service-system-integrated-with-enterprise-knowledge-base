"""流式响应首 Token 时间压测脚本。

测试 10 条查询的首 Token 时间（meta 事件到达时间），验证：
- 闲聊/转人工 < 200ms（快通道）
- 知识问答 < 1s（含意图识别+检索）
"""
import time
import json
import httpx

QUERIES = [
    ("你好，请问有什么服务", "闲聊-快通道"),
    ("谢谢你的帮助", "闲聊-快通道"),
    ("如何联系人工客服", "转人工-快通道"),
    ("产品有哪些功能", "知识问答"),
    ("忘记登录密码怎么办", "知识问答"),
    ("退货流程是什么", "知识问答"),
    ("你们的工作时间是几点到几点", "知识问答"),
    ("会员等级有哪些权益", "知识问答"),
    ("产品支持哪些支付方式", "知识问答"),
    ("售后服务政策是怎样的", "知识问答"),
]


def measure_first_token(client: httpx.Client, message: str) -> tuple[float, str]:
    """调用 /api/v1/chat/stream，返回 (首Token耗时秒, 首事件类型)。

    收到首个 meta/token 事件后立即关闭流，不等完整生成。
    """
    start = time.perf_counter()
    # 用独立 client 连接，便于收到首 token 后直接 close 避免等待完整流
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": message, "session_id": f"stream-perf-{int(start)}"},
        headers={"Accept": "text/event-stream"},
        timeout=60,
    ) as r:
        first_event_type = ""
        buffer = ""
        for chunk in r.iter_text():
            buffer += chunk
            # 解析 SSE 事件：event: xxx\ndata: {...}\n\n
            while "\n\n" in buffer:
                event_block, buffer = buffer.split("\n\n", 1)
                event_type = ""
                for line in event_block.split("\n"):
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:") and event_type in ("meta", "token", "done", "error"):
                        if event_type in ("meta", "token"):
                            elapsed = time.perf_counter() - start
                            return elapsed, event_type
        return 30.0, "timeout"


def main() -> None:
    client = httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=60)

    print("=" * 75)
    print("流式响应首 Token 时间压测（目标：闲聊<200ms，知识问答<1s）")
    print("=" * 75)
    print(f"{'查询':<28} {'类型':<16} {'首Token':<10} {'首事件':<8} {'达标':<6}")
    print("-" * 75)

    fast_latencies = []  # 快通道查询
    rag_latencies = []   # 知识问答查询

    for q, qtype in QUERIES:
        elapsed, etype = measure_first_token(client, q)
        if "快通道" in qtype:
            fast_latencies.append(elapsed)
            target = 0.2
        else:
            rag_latencies.append(elapsed)
            target = 1.0
        ok = "✓" if elapsed < target else "✗"
        print(f"{q[:26]:<28} {qtype:<16} {elapsed*1000:7.0f}ms  {etype:<8} {ok}")

    print("-" * 75)
    if fast_latencies:
        avg_fast = sum(fast_latencies) / len(fast_latencies) * 1000
        print(f"快通道（闲聊/转人工）avg={avg_fast:.0f}ms  目标<200ms  {'达标' if avg_fast < 200 else '未达标'}")
    if rag_latencies:
        avg_rag = sum(rag_latencies) / len(rag_latencies) * 1000
        max_rag = max(rag_latencies) * 1000
        print(f"知识问答            avg={avg_rag:.0f}ms  max={max_rag:.0f}ms  目标<1000ms  {'达标' if avg_rag < 1000 else '未达标'}")

    # 查询首 Token 监控指标
    try:
        r = client.get("/api/v1/performance/metrics", timeout=10)
        if r.status_code == 200:
            data = r.json()
            print(f"\n性能指标接口 stream_first_token_ms_avg={data.get('stream_first_token_ms_avg')}")
            print(f"性能指标接口 stream_first_token_ms_p95={data.get('stream_first_token_ms_p95')}")
    except Exception as exc:
        print(f"获取性能指标失败：{exc}")

    client.close()


if __name__ == "__main__":
    main()
