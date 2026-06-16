# NapCat Stream 上传

## 背景

NapCat QQ 机器人的标准 `upload_private_file` 接受两个来源：

1. **URL**：NapCat 服务端下载远端文件
2. **Base64**：直接传文件内容（体积膨胀 33%，受 WS 帧大小限制 ~16MB）

对于数十 MB 的 PDF，直接传 base64 会在 KiraAI-NapCat 间的 WS 连接中丢帧或超时。

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
# CHUNK_SIZE = 512KB — 小于 WS 16MB 帧上限，平衡吞吐和 latency
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
async def progress_cb(pct: int, speed_str: str):
    # pct: 0-100
    # speed_str: "x.x MB/s" 或 "x.x KB/s"
    pass
```

回调频率：约 20 次 + 首尾。每 `max(1, total_chunks // 20)` 片触发一次。

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

## 测试脚本

`test_send.py` 是独立的 WS 直连测试脚本，不依赖 KiraAI：

```bash
# 生成 ~50MB 测试 PDF，上传并发送给指定用户
python test_send.py 2263130787
```

测试流程：
1. 读取 KiraAI 系统配置中的 WS 连接信息
2. 直接连接 NapCat WS
3. 生成测试 PDF（1500 页彩色图片）
4. 分片上传 + `is_complete`
5. 发送到目标用户
6. 输出每个步骤的响应
