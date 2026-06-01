# 查询用户历史会话 — 架构说明

## 涉及的模块

### 1. `hermes_state.py` — SessionDB

**新增参数**: `list_sessions_rich(user_id=None)`

在已有的 `where_clauses` 中追加 `s.user_id = ?` 条件，与 `source`、`exclude_sources` 等过滤器并列。

- 放在参数列表末尾（`order_by_last_active` 之后），保持向后兼容
- `user_id=None`（默认）时不添加过滤，行为不变

### 2. `gateway/platforms/api_server.py` — APIServerAdapter

**新增 handler**:

- `_handle_list_sessions` — 处理 `GET /v1/sessions`
  - 从 query string 提取 `user_id`（必填）、`limit`、`offset`
  - 通过 `_ensure_session_db()` 获取 SessionDB 实例
  - 调用 `db.list_sessions_rich(user_id=..., limit=..., offset=...)`

- `_handle_get_session_messages` — 处理 `GET /v1/sessions/{session_id}/messages`
  - 从 path 提取 `session_id`
  - 调用 `db.get_session()` 校验存在性（不存在返回 404）
  - 调用 `db.get_messages_as_conversation(session_id, include_ancestors=True)` 获取消息

**路由注册**: 在 `connect()` 中注册：
```python
app.router.add_get("/v1/sessions", self._handle_list_sessions)
app.router.add_get("/v1/sessions/{session_id}/messages", self._handle_get_session_messages)
```

**capabilities**: `/v1/capabilities` 中声明 `features.session_listing: true` 和 `endpoints.sessions`、`endpoints.session_messages`。

## 数据流

```
GET /v1/sessions?user_id=alice
  → APIServerAdapter._handle_list_sessions()
    → SessionDB.list_sessions_rich(user_id="alice")
      → SQL: WHERE s.user_id = 'alice' AND (root_or_branch)
    → 返回会话列表

GET /v1/sessions/{id}/messages
  → APIServerAdapter._handle_get_session_messages()
    → SessionDB.get_session(session_id)  → 404 or ok
    → SessionDB.get_messages_as_conversation(session_id, include_ancestors=True)
      → _session_lineage_root_to_tip() 沿 parent_session_id 向上走到 root
      → 反向得到 [root, ..., tip] 链，合并所有 session 的 messages
    → 返回消息列表
```

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
