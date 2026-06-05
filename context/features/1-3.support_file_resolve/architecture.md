# 实现：文件上传与附件解析

## 设计原则

**零侵入注入，纯字符串返回。** 所有上游（`conversation_loop`、`run_agent`、`cli`）不感知文件系统的存在。`FileInjector` 将文件内容或路径引用转为纯文本追加到 `user_message` 字符串末尾，对话引擎无需任何修改。

**可选启用。** 未配置远程存储 URL 时，`FileComponents` 为 `None`，所有文件相关路由不注册，`attachments` 字段被忽略。现有功能零影响。

## 模块架构

```
┌────────────────────────────────────────────────────────────┐
│                    api_server.py                            │
│  POST /v1/runs    POST /v1/chat/completions                │
│  POST /v1/responses                                        │
│       │                                                    │
│       │ attachments: [{file_id}]                            │
│       ▼                                                    │
│  ┌──────────────┐                                          │
│  │ FileInjector │  ← 负责将文件注入到 user_message          │
│  └──────┬───────┘                                          │
│         │                                                   │
│         ├── FileStore        ← SQLite 元数据 + 本地缓存      │
│         ├── FileClassifier   ← 类型分类 + 文档文本提取       │
│         └── RemoteStorageClient ← 远程文件存取抽象          │
│                                                                    
│  POST /v1/files   GET /v1/files   ...                        │
│       │                                                     │
│       ▼                                                     │
│  ┌───────────────┐                                         │
│  │ FileHandlers  │  ← HTTP 端点处理器                       │
│  └───────────────┘                                         │
└────────────────────────────────────────────────────────────┘
```

## 改动文件

### 1. `gateway/platforms/remote_storage.py` — 远程存储抽象 + HTTP 实现

```
RemoteStorageClient (abstract)
  ├── upload(file_id, filename, mime_type, data) → remote_url
  ├── download(file_id) → bytes | None
  ├── delete(file_id) → bool
  ├── health_check() → bool
  └── close()

HttpRemoteStorage(RemoteStorageClient)
  └── HTTP PUT/GET/DELETE 协议，Bearer token 认证，GET 带重试
```

**设计要点**：`RemoteStorageClient` 是抽象接口，替换为 S3/MinIO/WebDAV 只需子类化，不触碰其他模块。

### 2. `gateway/platforms/file_store.py` — 元数据 + 本地缓存

- **存储**：SQLite（`file_store.db`），WAL 模式兼容 NFS/SMB
- **缓存**：`<hermes-home>/file_cache/<file_id>` 本地文件
- **元数据表**：`file_id | filename | mime_type | size_bytes | remote_url | created_at | accessed_at | expires_at`
- **淘汰策略**：LRU（按 `accessed_at`），超过 1000 条时逐出最旧记录
- **过期清理**：`sweep_expired()` 定时任务，删除过期元数据 + 本地缓存 + 远端文件

### 3. `gateway/platforms/file_parsing.py` — 分类 + 文档解析

纯逻辑模块，无状态、无网络、无强制依赖。

**分类优先级**：image > document > text > binary

**文档解析**（惰性导入，库缺失时静默退化）：
| 格式 | 库 |
|------|-----|
| .docx | `python-docx` |
| .pdf | `pypdf` → fallback `PyPDF2` |
| .xlsx | `openpyxl`（read_only, 最多 5000 行） |
| .pptx | `python-pptx` |

### 4. `gateway/platforms/file_injection.py` — 注入策略引擎

**核心方法**：`inject_attachments(user_message: str, attachments: list) → str`

针对每种文件类型的策略：

```
classify(mime, filename)
  ├── "document" → parse_document()
  │   ├── 成功 & ≤100K chars → 内联
  │   ├── 成功 & >100K chars → 路径引用
  │   └── 失败（缺库/损坏） → 路径引用
  │
  ├── "text" → read_content()
  │   ├── ≤100K chars → 内联
  │   └── >100K chars → 路径引用
  │
  ├── "image" → 路径引用 + data URL 长度预览
  │
  └── "binary" → 路径引用
```

**为什么始终返回 `str` 而不是 multimodal list？**

`conversation_loop.py:695` 处有守卫：
```python
if isinstance(_base, str):
    # 注入 memory / plugin 上下文
```
若 `user_message` 是 list，内存和插件上下文注入被静默跳过。保持纯字符串规避此回归。

### 5. `gateway/platforms/file_handlers.py` — HTTP 端点 + 组件组装

- `FileComponents` — 组件 Bundle（store / injector / handlers / remote）
- `create_file_components(config_extra, auth_checker)` — 工厂函数，无 remote URL 时返回 `None`
- `FileHandlers` — 注册 4 条路由（upload / list / download / delete），不依赖 `APIServerAdapter`

### 6. `gateway/platforms/api_server.py` — 集成点

改动集中在 3 处：

**A. 初始化**（`__init__`）：
```python
self._file_components = create_file_components(extra, self._check_auth)
```

**B. 对话端点入口**（`_handle_runs` / `_handle_chat_completions` / `_handle_responses`）：
```python
attachments = body.get("attachments", [])
if attachments and self._file_components is not None:
    user_message = await self._file_components.injector.inject_attachments(
        user_message, attachments,
    )
```
注入在 session key 解析之前、`conversation_history` 组装之前执行，保证 agent 看到的是已包含文件内容的完整输入。

**C. 路由注册**（`start()`）：
```python
if self._file_components is not None:
    self._file_components.handlers.register_routes(self._app)
```

**D. 清理**（`cleanup()`）：
```python
if self._file_components is not None:
    self._file_components.store.close()
    await self._file_components.remote.close()
```

## 数据流

```
┌─ 上传阶段 ──────────────────────────────────────────┐
│                                                      │
│  Client ──POST /v1/files (multipart)──▶ FileHandlers │
│                                            │         │
│                          ┌─────────────────┘         │
│                          ▼                           │
│               RemoteStorageClient.upload()           │
│                    │                                 │
│                    ▼                                 │
│               FileStore.put() 元数据写入 SQLite       │
│                    │                                 │
│                    ▼                                 │
│           返回 {file_id, filename, ...}              │
└──────────────────────────────────────────────────────┘

┌─ 对话阶段 ────────────────────────────────────────────────────┐
│                                                                │
│  Client ──POST /v1/runs {"input": "...",                      │
│              "attachments": [{"file_id": "file_xxx"}]}         │
│                                          │                     │
│                          api_server.py   │                     │
│                               │          │                     │
│                               ▼          ▼                     │
│                    FileInjector.inject_attachments()           │
│                               │                                │
│                    ┌──────────┼──────────┐                     │
│                    ▼          ▼          ▼                     │
│              FileStore   Classifier   RemoteClient             │
│              .get()      .classify()  .download()              │
│                    │          │          │                     │
│                    ▼          ▼          ▼                     │
│               file_meta   type tag   local cache               │
│                    │          │                                │
│                    └──────────┼────────┐                       │
│                               ▼        ▼                       │
│                       size ≤ limit?   parse doc?               │
│                          │    │                                │
│                    ┌─────┘    └─────┐                          │
│                    ▼                ▼                          │
│              inline content   path reference                   │
│                    │                │                          │
│                    └────────┬───────┘                          │
│                             ▼                                  │
│                  user_message + file block(s)                  │
│                             │                                  │
│                             ▼                                  │
│              run_conversation() ← 无感知                       │
└────────────────────────────────────────────────────────────────┘
```

## 不改的文件

- `conversation_loop.py` — 注入发生在入口之前，loop 看到的是含文件内容的纯字符串
- `run_agent.py` — 无新增参数
- `cli.py` — 不支持文件上传（CLI 无此场景）
- `gateway/run.py` — 不影响网关生命周期

## 关键阈值

| 常量 | 值 | 位置 |
|------|-----|------|
| `_MAX_UPLOAD_BYTES` | 100 MB | `file_handlers.py` |
| `_INLINE_CHAR_LIMIT` | 100K 字符 | `file_injection.py` |
| `_DEFAULT_MAX_FILES` | 1000 | `file_store.py` |
| `_DEFAULT_FILE_TTL` | 86400s (24h) | `file_store.py` |
| `_DEFAULT_CACHE_TTL` | 3600s (1h) | `file_store.py` |
| `_MAX_RETRIES` | 3 | `remote_storage.py` |

## 配置入口

```
环境变量                           gateway config extra 字段
═══════════════════════════════    ═══════════════════════════
REMOTE_STORAGE_URL                 remote_storage_url
REMOTE_STORAGE_TOKEN               remote_storage_token
```

未配置 URL → `create_file_components()` 返回 `None` → 所有文件功能静默禁用。
