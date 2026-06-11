# 查询用户历史会话 — 架构说明

## 涉及的模块

### 1. `hermes_state.py` — SessionDB

**新增参数**: `list_sessions_rich(user_id=None)`

在已有的 `where_clauses` 中追加 `s.user_id = ?` 条件，与 `source`、`exclude_sources` 等过滤器并列。

- 放在参数列表末尾（`order_by_last_active` 之后），保持向后兼容
- `user_id=None`（默认）时不添加过滤，行为不变

### 2. `gateway/platforms/custom/session/handlers.py` — CustomSessionHandlers

**handler**:

- `handle_list_sessions` — 处理 `GET /custom/v1/sessions`
  - 从 query string 提取 `user_id`（必填）、`limit`、`offset`
  - 通过注入的 `session_db_provider()` 获取 SessionDB 实例
  - 调用 `db.list_sessions_rich(user_id=..., limit=..., offset=...)`

- `handle_get_session_messages` — 处理 `GET /custom/v1/sessions/{session_id}/messages`
  - 从 path 提取 `session_id`
  - 调用 `db.get_session()` 校验存在性（不存在返回 404）
  - 调用 `db.get_messages_as_conversation(session_id, include_ancestors=True)` 获取消息
  - 调用 `_attach_artifacts(messages)` 将工具生成附件挂回对应 tool message

**路由注册**:

```python
app.router.add_get("/custom/v1/sessions", self.handle_list_sessions)
app.router.add_get(
    "/custom/v1/sessions/{session_id}/messages",
    self.handle_get_session_messages,
)
```

### 3. `gateway/platforms/custom/session/session_extension.py` — SessionExtension

将 `CustomSessionHandlers` 包装为标准 custom extension：

- `register_routes(app)` 注册 `/custom/v1/sessions*` 路由
- `extend_capabilities(caps)` 声明 `custom_session_api`
- `extend_endpoints(endpoints)` 暴露 `custom_sessions` 和 `custom_session_messages`

### 4. `gateway/platforms/custom/extension_register.py` — ExtensionAggregator

在 `from_config()` 中组装 custom extensions：

```python
storage_components = create_file_storage_components(...)
if storage_components is not None:
    artifacts_extension = RunArtifactsExtension(...)
    artifact_lookup = artifacts_extension.list_tool_call_artifacts

SessionExtension(
    CustomSessionHandlers(
        auth_checker=auth_checker,
        session_db_provider=session_db_provider,
        artifact_lookup=artifact_lookup,
    )
)
```

当文件服务器未配置时，`artifact_lookup=None`，历史消息接口仍正常返回消息，只是不追加 `artifacts` 字段。

### 5. `gateway/platforms/custom/artifacts/store.py` — RunArtifactStore

提供 artifact 元数据查询：

- `list_tool_call_ids(tool_call_ids)`：按一组 `tool_call_id` 批量查询 artifact
- `public_metadata(item)`：转换为可直接返回给 client 的公开字段

artifact 表中保存 `tool_call_id` 和 `tool_name`，因此历史消息接口无需改写 messages 表，只需按工具调用 ID 做后处理 join。

## 数据流

```
GET /custom/v1/sessions?user_id=alice
  → CustomSessionHandlers.handle_list_sessions()
    → SessionDB.list_sessions_rich(user_id="alice")
      → SQL: WHERE s.user_id = 'alice' AND (root_or_branch)
    → 返回会话列表

GET /custom/v1/sessions/{id}/messages
  → CustomSessionHandlers.handle_get_session_messages()
    → SessionDB.get_session(session_id)  → 404 or ok
    → SessionDB.get_messages_as_conversation(session_id, include_ancestors=True)
      → _session_lineage_root_to_tip() 沿 parent_session_id 向上走到 root
      → 反向得到 [root, ..., tip] 链，合并所有 session 的 messages
    → _attach_artifacts(messages)
      → 收集 messages[*].tool_call_id
      → RunArtifactsExtension.list_tool_call_artifacts(tool_call_ids)
      → RunArtifactStore.list_tool_call_ids(tool_call_ids)
      → 按 tool_call_id 分组后写入对应 tool message 的 artifacts 字段
    → 返回消息列表
```

## Artifact 挂载策略

历史消息接口只在响应层追加附件字段，不修改 `messages` 表，也不改变 LLM replay 使用的原始 transcript。

挂载规则：

1. 仅处理包含 `tool_call_id` 的消息。
2. 批量查询 `run_artifacts.tool_call_id IN (...)`，避免逐条查库。
3. 同一个 tool call 可能生成多个 artifact，因此 `artifacts` 是数组。
4. 无 artifact 的消息保持原结构，不返回空数组。
5. artifact 查询失败时记录 warning，并降级为只返回原始消息，避免影响历史会话查看。

## 会话链模型

```
root (parent_session_id=NULL, user_id="alice")
  └── compression child (end_reason='compression')
        └── compression child (end_reason='compression')
```

- 列表接口 (`list_sessions_rich`) 过滤掉 compression 子会话，只暴露 root/branch 作为入口
- 详情接口 (`get_messages_as_conversation`) 拿到任意一个 session_id 后沿链向上走到 root，再反向合并所有消息

## 测试覆盖

| 文件 | 测试类 | 用例数 |
|------|--------|--------|
| `tests/test_hermes_state.py` | `TestUserIDFilter` | 6 |
| `tests/gateway/test_api_server.py` | `TestListSessionsEndpoint` | 8 |
| `tests/gateway/test_api_server.py` | `TestGetSessionMessages` | 5 |
