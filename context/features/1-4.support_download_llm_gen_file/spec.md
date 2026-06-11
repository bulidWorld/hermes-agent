# 需求：支持下载 LLM 生成文件

## 背景

外部系统通过 `/v1/runs` 调用 Hermes Agent 时，LLM 可能会调用 `write_file`、`patch` 或 `terminal` 等工具生成本地文件。原始工具结果中虽然包含本地路径，但三方系统无法直接访问服务端文件系统，也不适合暴露本地绝对路径作为下载入口。

需要在 API server 层将 LLM 生成文件上传到文件服务器，返回稳定的 artifact 元数据和文件服务器 `publicId`。三方系统拿到 `publicId` 后，直接对接文件服务器下载文件。

## 目标

1. 支持 `/v1/runs` 生成文件后返回 `artifacts`。
2. 生成文件上传到统一文件服务器。
3. 文件服务器返回的 `publicId` 作为 `artifact_id`。
4. artifact 持久化保存，gateway 重启后仍可查询。
5. artifact 绑定到生成它的工具调用，即 `tool_call_id` 和 `tool_name`。
6. 文件在文件服务器中的保存路径按 session 归档：

```text
/hermes-agent/runtime/{YYYY-MM-dd}/{session_id}/{filename}
```

## 非目标

- 不把 artifact 直接写入 `messages` 表。
- 不修改 LLM 上下文或 tool result 内容。
- 不要求后端判断 artifact 应展示在哪个最终 assistant 消息气泡上。
- 不支持未配置文件服务器时的本地文件下载兜底。
- 不在 Hermes API server 中提供 artifact 代理下载接口。

## 外部接口

### `POST /v1/runs`

请求保持现有结构。若本次 run 触发文件工具生成文件，run 完成事件和轮询状态会包含 `artifacts`。

### `GET /v1/runs/{run_id}`

run 完成后的响应新增 `artifacts` 字段：

```json
{
  "object": "hermes.run",
  "run_id": "run_abc",
  "status": "completed",
  "output": "已生成文件。",
  "usage": {
    "input_tokens": 100,
    "output_tokens": 20,
    "total_tokens": 120
  },
  "artifacts": [
    {
      "id": "file_public_id",
      "artifact_id": "file_public_id",
      "run_id": "run_abc",
      "session_id": "session_123",
      "tool_call_id": "call_xyz",
      "tool_name": "write_file",
      "file_id": "file_public_id",
      "filename": "report.md",
      "mime_type": "text/markdown",
      "size": 1234,
      "created_at": 1781059200.0
    }
  ]
}
```

若 gateway 重启后内存中的 run 状态已丢失，但 artifact 元数据仍存在，接口返回：

```json
{
  "object": "hermes.run",
  "run_id": "run_abc",
  "status": "unknown",
  "artifacts": [...]
}
```

### `GET /v1/runs/{run_id}/events`

SSE 的 `run.completed` 事件新增 `artifacts`：

```json
{
  "event": "run.completed",
  "run_id": "run_abc",
  "timestamp": 1781059200.0,
  "output": "已生成文件。",
  "usage": {...},
  "session_id": "session_123",
  "artifacts": [...]
}
```

### `GET /custom/v1/sessions/{session_id}/messages`

历史消息查询会同时查询 artifact 数据库，并按 `tool_call_id` 将工具生成的附件挂到对应 tool message 上：

```json
{
  "role": "tool",
  "tool_call_id": "call_xyz",
  "tool_name": "write_file",
  "content": "{...}",
  "artifacts": [
    {
      "artifact_id": "file_public_id",
      "file_id": "file_public_id",
      "filename": "report.docx"
    }
  ]
}
```

无 artifact 的消息保持原结构，不额外返回空数组。

## 文件下载

Hermes API server 不代理 artifact 下载。三方系统使用 artifact 中的以下字段直接对接文件服务器：

| 字段 | 说明 |
|------|------|
| `artifact_id` | 文件服务器返回的 `publicId` |
| `file_id` | 同 `artifact_id`，便于按文件服务语义使用 |

## 前端展示建议

后端只绑定 artifact 到生成它的工具调用：

```text
artifact.tool_call_id -> messages.tool_call_id
```

前端可以按自己的展示策略，将这些 artifact 展示在：

- 对应 tool 调用区域
- 本轮最终 assistant 消息气泡
- 会话侧边栏的“附件/产物”面板

推荐前端保存或使用：

```text
run_id
session_id
tool_call_id
artifact_id
```

## 配置依赖

该能力依赖文件服务器配置。未配置 `file_storage_service_url` 时：

- run artifact extension 不启用
- `/v1/runs` 不返回 artifacts

沿用文件上传能力的配置：

```yaml
platforms:
  api_server:
    file_storage_service_url: "https://files.example.com"
    file_storage_workspace: "hermes-agent"
    auth_center_user: "..."
    auth_center_pwd: "..."
```

也支持请求头透传：

```text
AuthCenterToken: <jwt>
```

## 限制

| 项目 | 说明 |
|------|------|
| 支持工具 | `write_file`、`patch`、`terminal` 成功输出中的交付文件路径 |
| 支持文件类型 | 常见交付物扩展名，如 `.docx`、`.pdf`、`.xlsx`、`.csv`、`.txt`、`.md`、`.zip`、图片等；`.py` 等脚本文件不上传 |
| artifact 绑定粒度 | tool 调用级别，不直接绑定最终回复消息 |
| artifact_id 来源 | 文件服务器返回的 `publicId` |
| 持久化位置 | Hermes profile 下的 `run_artifacts.db` |
| 文件下载 | 三方系统直接访问文件服务器，不经过 Hermes 代理 |
| 未配置文件服务器 | 不启用该能力 |
