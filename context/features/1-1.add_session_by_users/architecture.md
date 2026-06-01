# 实现：user_id → session_id 映射

## 设计原则

**在入口处一次性解析 `user_id → session_id`，之后全部复用现有逻辑。** 不新增参数透传，不改 `run_agent.py`，不改 `conversation_loop.py`。和 Chat Completions 的 `X-Hermes-Session-Id` 加载模式完全一致。

## 改动文件

### 1. `hermes_state.py` — 新增查询方法

```python
def get_active_session_id_for_user(self, user_id: str, source: str = None) -> Optional[str]:
```

- 查询 `sessions` 表中 `user_id` 匹配且 `ended_at IS NULL` 的最新记录
- 可选 `source` 过滤（API server 传 `"api_server"`）
- 无匹配时返回 `None`

插入位置：`get_session()` (line 939) 之后。

### 2. `agent/conversation_compression.py` — 压缩后传播 user_id

```python
agent._session_db.create_session(
    ...
    parent_session_id=old_session_id,
    user_id=getattr(agent, '_user_id', None),  # 新增
)
```

压缩创建 child session 时继承 parent 的 `user_id`。否则 child session 的 `user_id` 为 NULL，下次 `get_active_session_id_for_user()` 找不到它。

### 3. `api_server.py` — 入口处解析 user_id

两个端点（`_handle_responses`、`_handle_runs`）在入口处相同的三步：

**Step A — 提取 user_id：**
```python
user_id = body.get("user_id")
if user_id is not None and not isinstance(user_id, str):
    return 400  # 'user_id' must be a string
if isinstance(user_id, str):
    user_id = user_id.strip() or None
```

**Step B — 在 history 回退链中新增 user_id 分支：**
```python
if not conversation_history and user_id and self._api_key:
    db = self._ensure_session_db()
    if db:
        sid = db.get_active_session_id_for_user(user_id, source="api_server")
        if sid:
            restored = db.get_messages_as_conversation(sid)
            if restored:
                conversation_history = restored
                stored_session_id = sid
```

**Step C — 传给 agent：**

`conversation_history` 已填充，和现有逻辑无缝衔接。`_run_agent()` / `_create_agent()` 零改动。

## 为什么这样做

### 为什么不在 conversation_loop.py 里 auto-restore

CLI 和 Chat Completions 的加载模式是：**调用方加载历史，传入 `run_conversation()`**。

```
CLI:       cli.py → get_messages_as_conversation(sid) → run_conversation(history=...)
Chat API:  api_server.py → get_messages_as_conversation(sid) → _run_agent(history=...)
Responses: api_server.py → get_messages_as_conversation(sid) → _run_agent(history=...)
```

不在 `run_conversation()` 内部自动恢复，保持职责清晰：`run_conversation()` 接收 history，不负责从哪来。

### 为什么不在 API server 重新加载历史的情况下传入空的 conversation_history

CLI 每轮从内存取 `self.conversation_history`，API server 每次从 DB 读。虽然多了一次 DB 读，但：

- SQLite 索引查询是微秒级，LLM 调用是秒级，差 6 个数量级
- `include_ancestors=False`（默认值），只查当前 session 的消息
- 压缩保证消息数不超上下文窗口，不会无限膨胀

## 不改的文件

- `conversation_loop.py` — 不在 loop 内部做 auto-restore
- `run_agent.py` — `_ensure_db_session()` 不变，API server 入口处预创建 session
- `_create_agent()`、`_run_agent()` — 不新增参数

## 数据流

```
POST /v1/responses {"input": "你好", "user_id": "alice"}
  │
  ├─ 提取 user_id = "alice"
  ├─ conversation_history 为空, previous_response_id 为空
  ├─ db.get_active_session_id_for_user("alice", source="api_server") → "sess_abc"
  ├─ db.get_messages_as_conversation("sess_abc") → [12 条历史]
  ├─ conversation_history = [12 条]
  │
  └─ _run_agent(session_id="sess_abc", conversation_history=[12 条])
       └─ run_conversation()
            ├─ messages = list(history) → [12 条]
            ├─ _hydrate_todo_store() ✓
            ├─ 水合 nudge 计数 ✓
            ├─ preflight 压缩检查 ✓
            └─ 正常执行
```

## CLI 对比

CLI 路径                                    API Server 路径（方案）
══════════════════════════════════════════  ══════════════════════════════════════

cli.py                                    api_server.py
  │                                          │
  │  get_messages_as_conversation(sid)       │  get_active_session_id_for_user(uid)
  │  → restored = [msg1, msg2, ...]          │  → session_id (or create new)
  │                                          │
  │  self.conversation_history = restored     │
  │                                          │
  │  run_conversation(                       │  _run_agent(
  │    user_message,                          │    session_id=session_id,
  │    conversation_history=[msg1, msg2]      │    conversation_history=[]    ← 空！
  │  )                                        │  )
  │  ┌─────────────────────────────────┐     │  ┌─────────────────────────────────┐
  │  │                                 │     │  │                                 │
  │  ▼                                 │     │  ▼                                 │
run_agent.py                          │    run_agent.py                          │
  │  run_conversation() forwarder      │     │  run_conversation() forwarder      │
  │  → agent/conversation_loop.py     │     │  → agent/conversation_loop.py     │
  │  ┌───────────────────────────┐     │     │  ┌───────────────────────────┐     │
  │  │                           │     │     │  │                           │     │
  ▼  │ conversation_loop.py      │     │     ▼  │ conversation_loop.py      │     │
     │                           │     │        │                           │     │
L229 │ messages = list(history)   │     │   L229 │ messages = []  ← 空！     │     │
     │ → [msg1, msg2]            │     │        │                           │     │
     │                           │     │        │ ╔═══════════════════════╗ │     │
     │ (auto-restore 跳过,       │     │        │ ║ NEW: auto-restore    ║ │     │
     │  因为 messages 非空)       │     │        │ ║ restored = db.get_   ║ │     │
     │                           │     │        │ ║   messages_as_conv() ║ │     │
     │                           │     │        │ ║ → [msg1, msg2]       ║ │     │
     │                           │     │        │ ║ messages = restored   ║ │     │
     │                           │     │        │ ║ conversation_history  ║ │     │
     │                           │     │        │ ║   = restored    ← Fix B ║│     │
     │                           │     │        │ ╚═══════════════════════╝ │     │
     │                           │     │        │                           │     │
L234 │ if conversation_history:   │     │   L234 │ if conversation_history:   │     │
     │   _hydrate_todo_store()   │     │        │   _hydrate_todo_store()   │     │
     │                           │     │        │                           │     │
L246 │ if conversation_history:   │     │   L246 │ if conversation_history:   │     │
     │   prior_user_turns = ...  │     │        │   prior_user_turns = ...  │     │
     │                           │     │        │                           │     │
     │ ═══════════════════════════╪═    │     │ ═══════════════════════════╪═    │
     │  从这里开始，两条路径      │     │     │  从这里开始，两条路径      │     │
     │  汇入完全相同的代码块      │     │     │  汇入完全相同的代码块      │     │
     │ ═══════════════════════════╪═    │     │ ═══════════════════════════╪═    │
     │                           │     │     │                           │     │
     │ messages.append(user_msg) │     │     │ messages.append(user_msg) │     │
     │ _build_system_prompt()    │     │     │ _build_system_prompt()    │     │
     │ preflight 压缩检查        │     │     │ preflight 压缩检查        │     │
     │   → _compress_context()   │     │     │   → _compress_context()   │     │
     │     ├─ end_session(old)   │     │     │     ├─ end_session(old)   │     │
     │     ├─ create_session(new,│     │     │     ├─ create_session(new,│     │
     │     │   parent=old)       │     │     │     │   parent=old,       │     │
     │     │                     │     │     │     │   user_id=...  ← Fix A)│   │
     │     └─ session_id = new   │     │     │     └─ session_id = new   │     │
     │                           │     │     │                           │     │
     │ agent loop (LLM 调用)     │     │     │ agent loop (LLM 调用)     │     │
     │ tool execution            │     │     │ tool execution            │     │
     │                           │     │     │                           │     │
     │ _persist_session()        │     │     │ _persist_session()        │     │
     │   → flush to session DB   │     │     │   → flush to session DB   │     │
     │                           │     │     │                           │     │
     └─ result ──────────────────┘     │     └─ result ──────────────────┘     │
                                       │                                       │
cli.py (return to caller)              │    api_server.py (return to caller)   │
  │                                    │      │                                │
  │ result["messages"] → 更新         │      │ result (streamed to client)     │
  │ self.conversation_history          │      │                                │

