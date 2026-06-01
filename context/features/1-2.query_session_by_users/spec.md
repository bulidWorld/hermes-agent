# 查询用户的历史会话

## 概述
为 API 网关新增两个接口，支持外部系统通过 user_id 查询用户的历史会话列表和指定会话的完整对话记录。

## 业务场景
- 前端应用需要展示某个用户的会话历史列表
- 用户点击某条历史会话后，需要加载该会话的完整对话记录
- 会话可能经过压缩（compression）产生父子链，查询详情时应自动合并祖先消息

## 功能点

### 1. 查询会话列表

**`GET /v1/sessions?user_id={user_id}`**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_id` | string | 是 | 用户标识 |
| `limit` | int | 否 | 每页条数，默认 20，最大 100 |
| `offset` | int | 否 | 分页偏移，默认 0 |

行为:
- 仅返回"顶层会话"——根会话（parent_session_id 为空）和分支会话（parent 以 end_reason='branched' 结束），压缩链子会话不重复出现
- 需要 API key 认证（与 X-Hermes-Session-Id 策略一致）
- 返回 `{"object": "list", "data": [{session}, ...]}`，每个 session 对象结构：

  | 字段 | 类型 | 说明 |
  |------|------|------|
  | `id` | string | 会话唯一标识 |
  | `title` | string \| null | 会话标题（最长 100 字符） |
  | `source` | string | 会话来源：`cli`、`telegram`、`discord`、`acp`、`tui` 等 |
  | `user_id` | string \| null | 用户标识 |
  | `model` | string \| null | 使用的模型名称 |
  | `parent_session_id` | string \| null | 父会话 ID（压缩链 / 分支） |
  | `started_at` | number | 会话创建时间（Unix 时间戳） |
  | `ended_at` | number \| null | 会话结束时间，活跃会话为 null |
  | `end_reason` | string \| null | 结束原因：`compression`、`session_reset`、`branched` 等 |
  | `message_count` | int | 消息数量 |
  | `tool_call_count` | int | 工具调用次数 |
  | `input_tokens` | int | 输入 token 总量 |
  | `output_tokens` | int | 输出 token 总量 |
  | `cache_read_tokens` | int | 缓存读取 token 量 |
  | `cache_write_tokens` | int | 缓存写入 token 量 |
  | `estimated_cost_usd` | number \| null | 预估费用（USD） |
  | `actual_cost_usd` | number \| null | 实际费用（USD） |
  | `handoff_state` | string \| null | 跨平台切换状态：`pending`、`running`、`completed`、`failed` |

### 2. 查询会话消息

**`GET /v1/sessions/{session_id}/messages`**

行为:
- 始终沿 parent_session_id 链从根节点走到当前节点，合并所有祖先消息，确保压缩链会话也能获取完整上下文
- 会话不存在时返回 404
- 需要 API key 认证
- 返回 `{"object": "session.messages", "session_id": "...", "data": [{role, content}, ...]}`

## 认证策略
两个接口均遵循与 X-Hermes-Session-Id header 相同的安全策略：配置了 API key 时强制 Bearer token 验证，未配置 key 时允许本地无认证访问。
