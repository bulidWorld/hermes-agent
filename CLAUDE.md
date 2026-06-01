# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在此仓库中工作时提供指导。

## 项目概述

Hermes Agent 是一个具备内置学习循环的自改进 AI Agent。通过 CLI (`hermes`) 或消息网关（Telegram、Discord、Slack 等）运行对话，支持多种 LLM 提供商（OpenRouter、Nous Portal、OpenAI、Anthropic 等）。

## 常用命令

### 开发环境配置
```bash
# 快速安装（推荐使用 setup-hermes.sh）
./setup-hermes.sh     # 安装 uv、创建 venv、安装 .[all]、创建 hermes 符号链接

# 手动配置
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[all,dev]"
```

### 测试
```bash
scripts/run_tests.sh                     # 全量测试（CI 环境一致性，隔离环境）
scripts/run_tests.sh tests/agent/        # 单个目录
scripts/run_tests.sh tests/agent/test_foo.py::test_x  # 单个测试
scripts/run_tests.sh -v --tb=long        # 透传 pytest 参数
```

**重要：** 必须使用 `scripts/run_tests.sh` — 它确保与 CI 的隔离环境一致性（清除凭证变量、TZ=UTC、LANG=C.UTF-8、4 个 xdist workers）。直接调用 `pytest` 会与 CI 行为不一致。

### TUI 开发
```bash
cd ui-tui
npm install       # 首次安装
npm run dev       # 监听模式
npm run build     # 完整构建
npm run lint      # eslint
npm test          # vitest
```

### 运行
```bash
hermes              # 交互式 CLI
hermes --tui        # Ink 终端 UI
hermes gateway      # 消息网关（Telegram、Discord 等）
hermes doctor       # 诊断问题
```

## 架构

### 核心入口点
- `run_agent.py` — AIAgent 类，核心对话循环（约 12k 行）
- `cli.py` — HermesCLI 类，交互式 prompt_toolkit CLI（约 11k 行）
- `gateway/run.py` — GatewayRunner，消息平台生命周期
- `hermes_cli/main.py` — 入口点，参数解析

### 关键文件
- `model_tools.py` — 工具编排，discover_builtin_tools()
- `toolsets.py` — 工具集定义，_HERMES_CORE_TOOLS
- `hermes_state.py` — SQLite 会话存储，带 FTS5 全文搜索
- `hermes_constants.py` — get_hermes_home()，用于 profile-aware 路径

### 文件依赖链
```
tools/registry.py（无依赖） → tools/*.py（导入时注册） → model_tools.py → run_agent.py/cli.py
```

**自动发现：** 任何包含 `registry.register()` 的 `tools/*.py` 文件都会被自动导入 — 无需维护手动导入列表。但工具仍需添加到 `toolsets.py` 中的工具集才能暴露给 Agent。

### 插件系统
- `plugins/` — 通用插件（hooks、工具、CLI 子命令）
- `plugins/memory/` — 内存后端（honcho、mem0、supermemory 等）
- `plugins/model-providers/` — 推理后端插件

**规则：** 插件不得修改核心文件。应扩展框架接口。

### TUI 架构
```
hermes --tui → Node (Ink) ←stdio JSON-RPC→ Python (tui_gateway) → AIAgent
```
TypeScript 控制屏幕显示。Python 控制会话、工具、模型调用。

## 重要规则

### Prompt 缓存
不要实现会导致以下情况的改动：
- 在对话中途修改历史上下文
- 在对话中途切换工具集
- 在对话中途重新加载内存或重建系统提示

修改系统提示状态的斜杠命令必须默认延迟生效（下一会话），可选使用 `--now` 标志立即生效。

### Profile 安全路径
代码路径必须使用 `hermes_constants` 的 `get_hermes_home()`：
```python
from hermes_constants import get_hermes_home
config_path = get_hermes_home() / "config.yaml"  # 正确

config_path = Path.home() / ".hermes" / "config.yaml"  # 错误 — 会破坏 profiles
```

用户面向的消息使用 `display_hermes_home()`。

### 跨平台兼容性
- 禁止使用 `os.kill(pid, 0)` — 在 Windows 上会静默杀死进程。使用 `psutil.pid_exists(pid)`
- 不要假设 Windows 存在 POSIX 工具（`grep`、`ps`、`kill` 等）
- `termios`/`fcntl` 仅 Unix 可用 — 必须捕获 `ImportError` 和 `NotImplementedError`
- 使用 `pathlib.Path` 进行路径操作

涉及 OS 层代码的 PR 前运行 `scripts/check-windows-footguns.py`。

### 测试规则
- 测试不得写入 `~/.hermes/` — `conftest.py` 会重定向 HERMES_HOME
- 不要写变更检测测试（模型目录、配置版本、枚举计数）
- 写不变量测试（数据间关系，而非数据快照）

## 关键模式

### 添加工具
1. 创建 `tools/your_tool.py`，包含 `registry.register()`
2. 将工具名添加到 `toolsets.py` 中合适的工具集（必需 — 自动发现只导入不暴露）

大多数自定义工具应使用插件：`~/.hermes/plugins/<name>/plugin.yaml`

### 添加斜杠命令
1. 在 `hermes_cli/commands.py` 的 `COMMAND_REGISTRY` 中添加 `CommandDef`
2. 在 `cli.py` 的 `HermesCLI.process_command()` 中添加处理器
3. 如需网关可用，在 `gateway/run.py` 中添加处理器

添加别名只需在 `aliases` 元组中添加 — 调度、帮助、自动补全会自动更新。

### 添加 Skill
Skill 位于 `skills/`（内置）或 `optional-skills/`（官方但不默认激活）。每个都有带 frontmatter 的 `SKILL.md`。

大多数能力应优先使用 Skill 而非 Tool。详见 CONTRIBUTING.md。

### 配置变更
- 新 `config.yaml` 键：添加到 `hermes_cli/config.py` 的 `DEFAULT_CONFIG`
- 新 `.env` 密钥：添加到 `hermes_cli/config.py` 的 `OPTIONAL_ENV_VARS`
- 非密钥（超时、标志、路径）放入 `config.yaml`，而非 `.env`

配置加载器因入口点不同：CLI 用 `load_cli_config()`，大多数子命令用 `load_config()`，网关直接读 YAML。

## 已知陷阱

- **`_last_resolved_tool_names`** 是 `model_tools.py` 中的进程级全局变量 — 子 Agent 执行前后会保存/恢复
- **双重网关消息守卫** — 基础适配器队列 + 运行器拦截；批准命令必须绕过两者
- **过期分支的 squash merge** 会静默回退近期修复 — 合并前确保分支已更新
- **ANSI `\033[K`** 在 `prompt_toolkit.patch_stdout` 下会泄漏 — 使用空格填充替代
- **工具 schema 跨引用** — 不要提及其他工具集名称（可能被禁用）。在 `get_tool_definitions()` 中动态添加跨引用


## 启动命令
cd /opt/apps/hermes-agent && source .venv/bin/activate && python -m hermes_cli.main  
