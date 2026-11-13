# NapCat Stream 上传

## 背景

NapCat QQ 机器人的标准 `upload_private_file` 接受两个来源：

1. **URL**：NapCat 服务端下载远端文件
2. **Base64**：直接传文件内容（体积膨胀 33%，受实现端 WS 帧上限约束）

2026-11-12 在线实测 NapCat.Core 4.18.14：`maxPayload=50MiB`，`base64://` 文件
37 MB 成功、38 MB 触发 `Client Error: Max payload size exceeded`（50 × 3/4 ≈ 37.5 MiB）。
旧文档「~16MB」的说法已被证伪。插件不据此设置本地闸门，上限交给实现端决定。
超过该边界时实现端可能直接丢帧不回包，客户端只看到超时，需查实现端日志确认。

## Stream API 分片策略

NapCat 提供 `upload_file_stream` 动作，支持分片上传：

```
分片 1: { stream_id, chunk_data, chunk_index, total_chunks, file_size, expected_sha256, filename }
分片 2: { stream_id, chunk_data, chunk_index, total_chunks, file_size, expected_sha256, filename }
...
分片 N: { stream_id, chunk_data, chunk_index, total_chunks, file_size, expected_sha256, filename }
完成  : { stream_id, is_complete: true }    ← 必须！否则 NapCat 不组装文件
```

关键参数：

| 参数 | 说明 |
|------|------|
| `stream_id` | 同一文件的 upload session，所有分片相同 |
| `chunk_data` | base64 编码的分片数据 |
| `expected_sha256` | **完整文件的 SHA256**，不是分片的！每个分片传同一个值 |
| `is_complete` | 最后一帧设为 `true`，NapCat 返回远端 `file_path` |

## 实现细节

```python
# CHUNK_SIZE = 512KB — 远小于 WS 50MiB 帧上限，平衡吞吐和 latency
CHUNK_SIZE = 512 * 1024

# 必须先计算完整文件 hash
full_sha256 = hashlib.sha256(open(file_path, "rb").read()).hexdigest()

# 每片传同样的 expected_sha256
params = {
    "expected_sha256": full_sha256,
    # ... 其他参数
}
```

### 进度回调

```python
async def progress_cb(pct: int | None, note: str):
    # pct: 分片期间为 0-100 的真实百分比；实现端发送阶段为 None（进度未知）
    # note: 分片期间是 "x.x MB/s" 速度串，None 时是阶段说明
    pass
```

回调频率：约 20 次 + 首尾。每 `max(1, total_chunks // 20)` 片触发一次。
分片全部到达实现端后、调用 `upload_private_file` / `upload_group_file` 之前，还会额外
上报一次 `None`：这一段由实现端内部转存，协议不回报进度，不能继续挂在 100%。
只有 `upload_method=stream` 才走这条链路；标准方案全程显示「进度未知」。

### 超时模型：无进展看门狗，不给总时长设闸门

分片链路的总时长 = `分片数 × 每片往返 + 实现端转存`，无法预估：100MB = 200 片，
只要平均每片往返超过 2.1s（约 250KB/s 上行）就超过 7 分钟。用固定总时长当外层闸门，
会把进度一路正常的传输判成「在 420s 内未收到实现端响应」——这是我们自己制造的假话。

因此 `stream` 不用 `asyncio.wait_for` 卡总时长，改用 `StallGuard`：

| 阶段 | 阈值 | 依据 |
|------|------|------|
| 分片上传 | `max(60, 单片超时 × 2)` 秒无任何 ack | 单片本身已有超时，这里只兜客户端不按超时返回的卡死 |
| 分片完成确认 | 同上 | 组装 `file_path` |
| 实现端发送 | `send_timeout + 60` 秒无 ack | 这一段没有中间 ack，阈值必须覆盖转存耗时 |

每次实现端确认都 `guard.touch()`（与 `progress_cb` 是否上报无关）。触发时抛出
`format_stalled`：写明「连续 N 秒没有收到任何实现端确认（阈值 X 秒）」，并明确
**「不是体积超限，也不是实现端明确拒绝」**。`path` / `base64` 仍是单次动作，保留原总时长兜底。

## 完整上传链路

```python
# 1. 分片上传到 NapCat temp
remote_path = await stream_upload_file(client, local_pdf_path, timeout=300)

# 2. 发给用户（只传远端路径，文件已在 NapCat 服务器上）
await client.send_action("upload_private_file", {
    "user_id": user_id,
    "file": remote_path,    # 如 /app/.config/QQ/NapCat/temp/xxx.pdf
    "name": "1248643.pdf",
})
```

## 注意事项

1. **`expected_sha256` 必须是全文件 hash**：每个 chunk 传同样的值，不是分片 hash
2. **`is_complete` 不可省略**：没有这个信号 NapCat 不会组装文件，也不返回 `file_path`
3. **timeout 要设够**：大文件上传建议 300s+
4. **WS 连接复用**：通过 `adapter.get_client()` 获取已有连接，不开新连接
5. **文件格式**：NapCat 会根据 filename 后缀决定文件类型，保持 `.pdf`
