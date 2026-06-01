# 需求：通过 user_id 实现服务端会话持久化

## 背景

当前 API server 的 `/v1/responses` 和 `/v1/runs` 端点依赖客户端管理会话历史。客户端必须通过以下方式之一传递历史：

1. **`conversation_history`** — 请求体中显式传入完整历史
2. **`previous_response_id`** — 服务端 `_response_store`（SQLite，容量 100 条）链式追踪

而 CLI 通过 `state.db` 实现了完整的会话持久化——历史自动保存、恢复、压缩。Chat Completions 端点也支持通过 `X-Hermes-Session-Id` 头部恢复历史。但 Responses/Runs 端点缺少等价的能力。

## 需求

用户通过 `user_id` 标识自己，服务端自动维护会话状态：

1. 客户端传 `user_id` 即可，不需要传 `conversation_history` 或 `previous_response_id`
2. 服务端在 `state.db` 中维护 `user_id → session_id` 的映射
3. 后续请求自动从 DB 恢复历史，复用 CLI 的压缩能力
4. `/v1/responses` 和 `/v1/runs` 两个端点都支持

## 场景

### 首次请求（/v1/responses）
```
POST /v1/responses
Authorization: Bearer <key>
{
  "input": "你好，我叫张三",
  "user_id": "alice"
}
→ 服务端创建新 session，记录 user_id=alice
→ 正常执行，结果写入 session DB
```

### 后续请求（/v1/responses）
```
POST /v1/responses
Authorization: Bearer <key>
{
  "input": "我叫什么名字？",
  "user_id": "alice"
}
→ 服务端查出 session_id，从 DB 恢复历史
→ agent 看到之前的对话，正确回答"张三"
```

### /v1/runs 同样支持
```
POST /v1/runs
Authorization: Bearer <key>
{
  "input": "还记得我的名字吗？",
  "user_id": "alice"
}
→ 返回 run_id，轮询或 SSE 获取结果
→ 历史同样从 DB 恢复
```

### 压缩后
```
会话过长触发压缩 → child session 继承 user_id（agent/conversation_compression.py）
→ 下次请求仍然能正确恢复
```

## 安全要求

- `user_id` 会话恢复需要 API key 认证（与 `X-Hermes-Session-Id` 策略一致）
- 未配置 `API_SERVER_KEY` 时，`user_id` 功能静默跳过

## 优先级（回退链）

**`/v1/responses`：**
显式 `conversation_history` > `previous_response_id` > `user_id`（DB 恢复）> 多段 `input` 数组

**`/v1/runs`：**
显式 `session_id` > `previous_response_id` > `user_id`（DB 恢复）> `run_id`
