# 需求：文件上传与附件解析

## 背景

当前 API server 支持 inline 图片（`image_url` / `image_base64`），但不支持文件上传。用户必须将文件内容编码为 base64 data URL 嵌入请求体，这在以下场景不可行：

1. **大文件** — 请求体大小限制，传输效率低
2. **非图片文件** — 文档（PDF/DOCX/XLSX/PPTX）、代码、文本等无法内联
3. **跨轮复用** — 同一文件在多次对话中引用，需要每次重新传输

## 需求

提供标准的文件上传 API，客户端上传文件后获得 `file_id`，在对话请求中通过 `attachments` 字段引用，服务端根据文件大小和类型自动决定**内联注入到上下文**或**路径引用（让 agent 调用 read_file 按需读取）**。

## 端点

### `POST /v1/files` — 上传

```
Content-Type: multipart/form-data
Authorization: Bearer <api_key>

字段：
  file        文件内容（可重复提交多个文件）
  ttl         可选，过期秒数（正整数），默认 86400（24h）
```

**成功响应** `201`：
```json
{
  "object": "list",
  "data": [
    {
      "file_id": "file_a1b2c3d4e5f6...",
      "filename": "report.pdf",
      "mime_type": "application/pdf",
      "size_bytes": 245000,
      "remote_url": "https://files.example.com/files/file_a1b2...",
      "created_at": 1717200000.0,
      "expires_at": 1717286400.0
    }
  ],
  "warnings": ["empty.txt: empty file"]
}
```

**错误响应**：
| 状态码 | code | 说明 |
|--------|------|------|
| 400 | `invalid_content_type` | 非 multipart/form-data |
| 400 | `no_file_provided` | 请求中无 file 字段 |
| 413 | `file_too_large` | 单文件超过 100 MB |
| 502 | `remote_upload_failed` | 远程存储不可用 |

### `GET /v1/files` — 列表

```
GET /v1/files
Authorization: Bearer <api_key>
```

**响应** `200`：`{"object": "list", "data": [...]}`，按创建时间倒序。

### `GET /v1/files/{file_id}` — 下载

```
GET /v1/files/{file_id}
Authorization: Bearer <api_key>
```

**响应** `200`：文件二进制内容，附带 `Content-Disposition`、`Content-Type`、`X-File-Id` 头。

**错误** `404`：文件不存在。

### `DELETE /v1/files/{file_id}` — 删除

```
DELETE /v1/files/{file_id}
Authorization: Bearer <api_key>
```

**响应** `200`：`{"id": "file_...", "object": "file", "deleted": true}`

## 在对话中引用文件

### `/v1/runs`、`/v1/chat/completions`、`/v1/responses`

三个对话端点均在请求体中新增 `attachments` 字段：

```json
{
  "input": "请分析这份报告",
  "user_id": "alice",
  "attachments": [
    {"file_id": "file_a1b2c3d4..."},
    {"file_id": "file_e5f6g7h8..."}
  ]
}
```

服务端按 `attachments` 顺序，将文件内容注入到 `input` 字符串之后。注入后的消息对各端点后续逻辑完全透明。

## 注入策略（大小分治）

服务端根据文件类型和大小，自动选择注入方式：

| 类型 | 条件 | 行为 |
|------|------|------|
| **文本** (.py/.js/.md/.yaml 等) | ≤ 100K 字符 | 内联完整内容到上下文 |
| **文本** | > 100K 字符 | 路径引用 + 提示 agent 使用 `read_file` |
| **文档** (.pdf/.docx/.xlsx/.pptx) | 解析后 ≤ 100K 字符 | 内联解析出的文本 |
| **文档** | 解析后 > 100K 字符 | 路径引用 |
| **文档** | 解析失败（缺少库） | 路径引用 |
| **图片** | 任意 | 路径引用 + data URL 长度预览 |
| **二进制** | 任意 | 路径引用 |

### 内联格式

```
[Attached file: report.txt (1.2KB, text/plain)]
Path: /home/user/.hermes/file_cache/file_a1b2...
Content:
...完整文件内容...
[End: report.txt]
```

### 路径引用格式

```
[Attached file: large.csv (15.3MB, text/csv)]
Path: /home/user/.hermes/file_cache/file_e5f6...
Use the read_file tool to read this file.
```

## 配置

文件上传功能通过以下方式启用：

```bash
# 环境变量
REMOTE_STORAGE_URL=https://files.example.com
REMOTE_STORAGE_TOKEN=<bearer_token>

# 或在 gateway config extra 字段
remote_storage_url: "https://files.example.com"
remote_storage_token: "<bearer_token>"
```

未配置 `REMOTE_STORAGE_URL` 时，`/v1/files/*` 路由不会被注册，`/v1/health` 中 `file_upload` / `file_attachments` 为 `false`。对话端点中的 `attachments` 字段被忽略。

## 认证

所有 `/v1/files/*` 端点均要求 Bearer token 认证，与 API server 其他端点使用相同的 `API_SERVER_KEY` 校验。

## 限制

| 项目 | 值 |
|------|-----|
| 单文件最大 | 100 MB |
| 内联阈值 | 100K 字符 |
| 默认 TTL | 24 小时 |
| 最大文件数 | 1000（LRU 淘汰） |
| 远程存储协议 | HTTP PUT/GET/DELETE（可替换为 S3 等） |
