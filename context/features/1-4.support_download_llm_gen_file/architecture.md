# 实现：支持下载 LLM 生成文件

## 设计原则

**低侵入。** 生成文件 artifact 能力放在 `gateway/platforms/custom/artifacts/`，核心 `api_server.py` 只保留薄集成点，便于同步上游。

**不改 agent/tool。** `write_file` 和 `patch` 已经在 tool result 中返回 `resolved_path`、`files_modified`、`files_created`。`terminal` 常用于执行脚本生成最终文件，artifact 模块会从成功输出中提取仍存在的交付文件路径。所有提取都在 `/v1/runs` 的 `result["messages"]` 后处理完成，不修改工具实现和 LLM 上下文。

**artifact 绑定 tool，不绑定最终消息。** 后端记录 `tool_call_id` 和 `tool_name`，前端决定展示到哪个消息气泡。

**文件服务器为唯一下载源。** 本地路径只作为上传来源，不对外暴露为下载入口。Hermes 对外只返回文件服务器 `publicId`，三方系统直接访问文件服务器下载。

## 模块结构

```text
gateway/platforms/custom/artifacts/
├── __init__.py
├── extractor.py   # 从 run result/messages 提取文件路径和 tool_call_id
├── extension.py   # 上传文件服务器并注册 artifact
└── store.py       # SQLite 持久化 run artifact 元数据
```

## 集成点

### `gateway/platforms/custom/extension_register.py`

当 `file_storage_service_url` 已配置，`ExtensionAggregator.from_config()` 会创建：

```python
FileStorageExtension(storage_components)
RunArtifactsExtension(auth_checker, storage_components.client)
```

新增聚合方法：

```python
await collect_run_artifacts(run_id, session_id, result, request_headers)
list_run_artifacts(run_id)
```

### `gateway/platforms/api_server.py`

`/v1/runs` 完成分支中：

1. 从 agent 获取有效 session：
   ```python
   artifact_session_id = agent.session_id or session_id
   ```
2. 调用 custom extension：
   ```python
   artifacts = await self._custom_extension.collect_run_artifacts(
       run_id,
       artifact_session_id,
       result,
       request_headers=request_headers,
   )
   ```
3. 将 `artifacts` 写入：
   - SSE `run.completed`
   - `_run_statuses[run_id]`

`GET /v1/runs/{run_id}` 若内存 status 不存在，会尝试从 artifact DB 查询：

```python
artifacts = self._custom_extension.list_run_artifacts(run_id)
```

若存在 artifacts，则返回 `status: "unknown"`，用于 gateway 重启后的产物恢复。

### `gateway/platforms/custom/session/handlers.py`

`GET /custom/v1/sessions/{session_id}/messages` 返回历史消息时：

1. 正常读取 `db.get_messages_as_conversation(session_id, include_ancestors=True)`。
2. 收集返回消息中的 `tool_call_id`。
3. 调用 artifact lookup 批量查询 `run_artifacts`。
4. 按 `tool_call_id` 将 artifact 元数据挂到对应 tool message：

```json
{
  "role": "tool",
  "tool_call_id": "call_xxx",
  "tool_name": "terminal",
  "content": "{...}",
  "artifacts": [...]
}
```

无 artifact 的消息保持原结构，避免影响已有前端逻辑。

### `gateway/platforms/custom/file_storage/client.py`

`FileStorageServiceClient.upload()` 新增可选参数：

```python
folder_path: Optional[str] = None
```

用于 run artifact 上传时覆盖默认目录：

```text
/runtime/{YYYY-MM-dd}/{session_id}
```

文件服务器 workspace 仍沿用 `file_storage_workspace`，默认 `hermes-agent`。

## 数据流

```text
Client
  │
  │ POST /v1/runs
  ▼
api_server.py
  │
  │ agent.run_conversation()
  ▼
Agent messages
  │
  │ tool result:
  │ {
  │   "tool_call_id": "call_xxx",
  │   "content": {
  │     "resolved_path": "/abs/report.md",
  │     "files_modified": ["/abs/report.md"]
  │   }
  │ }
  │
  │ or terminal output:
  │ {
  │   "tool_call_id": "call_yyy",
  │   "content": {
  │     "output": "saved: /abs/report.docx",
  │     "exit_code": 0
  │   }
  │ }
  ▼
artifacts.extractor
  │
  │ {path, tool_call_id, tool_name}
  ▼
RunArtifactsExtension
  │
  │ read local file bytes
  │ upload(folderPath="/runtime/{date}/{session_id}")
  ▼
FileStorageService
  │
  │ returns publicId
  ▼
RunArtifactStore
  │
  │ INSERT run_artifacts
  ▼
/v1/runs status + run.completed artifacts[]
```

## Artifact 提取逻辑

`extractor.py` 扫描 `result["messages"]`：

1. 找 `role == "tool"` 的消息。
2. 解析 `content` JSON。
3. 跳过包含 `error` 的 tool result。
4. 按工具类型读取候选路径：
   - `write_file` / `patch`
     - `resolved_path`
     - `files_modified`
     - `files_created`
   - `terminal`
     - 仅处理 `exit_code == 0` 的输出
     - 从 `output` 中提取本地绝对路径或 `~` 路径
5. 仅保留常见交付文件扩展名，如 `.docx`、`.pdf`、`.xlsx`、`.csv`、`.txt`、`.md`、`.zip`、图片等；`.py` 等脚本文件不作为 artifact 上传。
6. 通过 `tool_call_id` 回查前序 assistant `tool_calls`，得到 `tool_name`。
7. 仅保留 `write_file`、`patch` 和 `terminal`。
8. 仅保留当前仍存在的普通文件。

输出：

```python
{
    "path": "/abs/report.md",
    "tool_call_id": "call_xxx",
    "tool_name": "write_file",
}
```

## 文件服务器路径

上传时使用：

```text
workspaceName = "hermes-agent"  # 默认，可配置
folderPath = "/runtime/{YYYY-MM-dd}/{session_id}"
filename = 原始文件名
```

记录到 artifact 元数据中的 `remote_path`：

```text
/hermes-agent/runtime/{YYYY-MM-dd}/{session_id}/{filename}
```

`session_id` 会做路径段清洗，仅保留字母、数字、`_`、`-`，其他字符替换为 `_`。

## SQLite 表

数据库：`<hermes-home>/run_artifacts.db`

表：`run_artifacts`

```sql
CREATE TABLE IF NOT EXISTS run_artifacts (
    artifact_id  TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    session_id   TEXT NOT NULL DEFAULT '',
    tool_call_id TEXT NOT NULL DEFAULT '',
    tool_name    TEXT NOT NULL DEFAULT '',
    public_id    TEXT NOT NULL,
    filename     TEXT NOT NULL,
    mime_type    TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    remote_url   TEXT NOT NULL,
    remote_path  TEXT NOT NULL,
    created_at   REAL NOT NULL
);
```

索引：

```sql
CREATE INDEX IF NOT EXISTS idx_run_artifacts_run_id
    ON run_artifacts(run_id, created_at);

CREATE INDEX IF NOT EXISTS idx_run_artifacts_session_id
    ON run_artifacts(session_id, created_at);
```

兼容旧表：

`RunArtifactStore._ensure_columns()` 会在启动时补齐：

- `session_id`
- `tool_call_id`
- `tool_name`

## 下载职责

Hermes API server 不提供 artifact 代理下载路由。`/v1/runs` 和 `run.completed` 只返回 artifact 元数据：

```text
artifact_id / public_id
filename
mime_type
size
```

三方系统拿到 `artifact_id` / `file_id` 后，直接调用文件服务器下载。`remote_url` 和 `remote_path` 仅作为内部持久化字段，不在公开 artifact metadata 中返回。

## 为什么不把 artifact 写入 messages 表

1. `messages` 是核心上游表，改 schema 侵入更大。
2. 一个 tool call 可能生成多个 artifact，天然是一对多关系。
3. `artifact_id` 只有上传文件服务器后才知道，晚于 message 写入。
4. 把 artifact 元数据写回 `messages.content` 会污染 LLM transcript。

因此采用独立 artifact 表：

```text
tool_call_id -> artifacts[]
```

前端需要展示时，可以按 `tool_call_id` 与消息列表中的 tool message 关联，再挂到最终消息气泡。

## 失败处理

| 阶段 | 行为 |
|------|------|
| tool result 解析失败 | 跳过该消息 |
| 本地文件不存在 | 跳过 |
| 本地文件读取失败 | 记录 warning，跳过 |
| 上传文件服务器失败 | 记录 warning，跳过 |
| 所有 artifact 上传失败 | `/v1/runs` 正常完成，`artifacts: []` |

artifact 上传失败不影响 LLM 最终回复，避免文件后处理阻断主对话。

## 未来演进

- 在 session messages API 返回层按 `tool_call_id` join artifacts。
- 增加 `GET /custom/v1/sessions/{session_id}/artifacts`。
- 支持后台重试上传失败的 artifact。
- 增加 artifact 删除或过期清理策略。
