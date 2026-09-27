# clm-sync — chatLanguageModels.json 模型列表同步工具

同步 VS Code（Copilot 聊天 BYOK）维护的 `chatLanguageModels.json` 文件中 `vendor=customendpoint` 条目的模型列表，从 OpenAI 兼容的 `/v1/models` 端点拉取最新模型信息。

需要认证的端点会被自动解析 VS Code 加密存储（Secret Storage）里的 API key，并以 `Authorization: Bearer` 头发送。

## 安装与使用

### 1. 安装依赖

```bash
cd copilot-byok-sync
pip install -e .
```

### 2. 使用方法

#### 同步所有 customendpoint（推荐）

```bash
python -m clm_sync.cli --config chatLanguageModels.json --all
```

> 也可用 `python -m clm_sync`（等价，包级入口同样会解析命令行参数），或直接使用安装后的 `clm-sync` 命令。

#### 同步指定 customendpoint

```bash
python -m clm_sync.cli --config chatLanguageModels.json --provider A
python -m clm_sync.cli --config chatLanguageModels.json --provider A --provider B
```

### 3. 命令行参数详解

| 参数 | 含义 |
| - | - |
| `--config` | 配置文件路径（必须） |
| `--all` | 同步**所有** customendpoint（与 `--provider` 二选一，必须指定其一） |
| `--provider NAME` | 同步**指定单个** customendpoint（可多次指定） |
| `--dry-run` | 只显示变更，不实际写入文件 |
| `--no-delete` | 只添加远端发现的新模型，不删除或覆盖本地已有模型 |
| `--sort` | 顺带把每个端点的模型列表按 id 字典序升序（大小写敏感）重排；默认保留原顺序 |
| `--no-credits` | 跳过自动余额查询 |
| `--timeout SECONDS` | 请求超时时间（秒），默认 15 |
| `--version` | 显示版本信息 |

## URL 规范

提供给工具的 `url` 可以是以下任意格式（工具会自动规范化）：

- `https://api.deepseek.com`
- `https://api.deepseek.com/v1`
- `https://api.deepseek.com/v1/chat/completions`（含尾斜杠、任意大小写）
- `https://api.deepseek.com/v1/responses`（Responses-API 网关）
- 以上任意一种带 query string（如 `?limit=100`，会被剥离）

工具会自动把以上 URL 转换为 `https://api.deepseek.com/v1/models` 来请求模型列表。

`--config` 必须指向实际被 VS Code 使用的配置文件。如果你同时维护多个配置副本，请确认编辑的文件与同步命令使用的是同一个路径。

## API key 自动解析

当 provider 的 `apiKey` 字段是形如 `${input:chat.lm.secret.<id>}` 的 VS Code Secret Storage 占位符时，工具会自动解析出真实 key 并随请求发送（`Authorization: Bearer <key>`），整个过程无需手动管理密钥。

**解析链路：**

1. 占位符中的 `<id>` 映射到 `%APPDATA%\Code\User\globalStorage\state.vscdb` 的 `secret://chat.lm.secret.<id>` 条目。
2. 该条目的密文是 `v10` 格式（`v10` + 12 字节 nonce + AES-256-GCM 密文 + 16 字节 tag）。
3. 32 字节 AES 主密钥存于 `%APPDATA%\Code\Local State` 的 `os_crypt.encrypted_key`，由 Windows DPAPI 保护，用 `CryptUnprotectData` 解包。
4. 解密后的 key 只存在于进程内存，**不会打印、记录或写入任何文件**。

**行为矩阵：**

| `apiKey` 值形态 | 请求是否带`Authorization` 头 | 说明 |
| - | - | - |
| 字面 key（如`sk-xxx`） | 是 | 直接作为 Bearer 发送 |
| `${input:chat.lm.secret.<id>}` 占位符 | 是（解析后） | 自动从 VS Code 加密存储解密 |
| 占位符但解析失败 | 否 | 降级为无 key 请求，返回 401 不中断 |
|  缺失 | 否 | 请求不带头，端点自行决定 |

若 `cryptography` 包未安装，占位符解析会静默降级（返回 401），不影响其他 provider 的同步。

> **平台限制：** 占位符解析依赖 Windows 的 DPAPI 与 VS Code 的 Windows 存储路径，**仅在 Windows 上生效**。在 macOS / Linux 上（或 `cryptography` 缺失时）解析自动降级为无 key 请求——字面 key 正常同步，占位符 provider 会得到 401，属预期行为而非故障。

## 余额查询

一次同步运行结束后（含 `--dry-run`），未指定 `--no-credits` 时，工具会自动查询 base URL 属于**已知厂商端点**的 provider 的账户余额，并在报告里对应 provider 下追加一行 `credits:`（示例值）：

```
[changes] DeepSeek
    credits: 12.34 CNY
```

匹配依据是 URL 的**域名**，与你在配置里写的 provider 名无关。当前已知端点：

| 域名 | 余额端点 | 货币 |
| - | - | - |
| `api.deepseek.com` | `/user/balance` | CNY |
| `openrouter.ai` | `/api/v1/credits` | USD |
| `api.moonshot.cn` | `/v1/users/me/balance` | CNY |
| `api.moonshot.ai` | `/v1/users/me/balance` | USD |
| `api.stepfun.com` | `/v1/accounts` | CNY |
| `api.novita.ai` | `/v3/user/balance` | USD |

行为规则：

- **未知域名不查询。**
- **查询失败显示 `unavailable`。** 已知端点的请求失败、或响应里没有可解析的字段时，`credits:` 行显示 `unavailable`；余额是纯展示信息，**不影响模型同步与删除保护逻辑**。
- 需要鉴权的 provider，余额请求与模型同步复用同一把 API key。
- `--no-credits` 可完全跳过余额查询。

## 新增模型的字段

新加入的模型只会写入 VS Code 模型配置字段，并自动补齐：

- `toolCalling`
- `vision`
- `maxInputTokens`
- `maxOutputTokens`
- `supportsReasoningEffort`

远端的 `object`、`created`、`owned_by` 等 API 协议字段不会写入配置。默认值为 `toolCalling: true`、`vision: true`、`maxInputTokens: 1000000`、`maxOutputTokens: 384000`，以及 `supportsReasoningEffort: ["max"]`；如果远端返回这些配置字段，则优先保留远端值。

已存在且仍被远端返回的模型保持本地原有配置不变。

## 删除规则

默认情况下，工具会删除本地存在但远端 `/v1/models` 已不再返回的模型。删除同时会移除该模型在 provider 顶层 `settings` 中对应的配置项。

为避免网络故障导致误删，只有在以下条件全部满足时才会执行删除：

- 未使用 `--no-delete`。
- provider 的所有端点请求都成功。
- 每个成功响应都包含至少一个有效模型 ID。

如果任一端点返回 `401`、请求超时、网络错误或响应格式无效，当前 provider 会跳过删除，并保留本地已有模型。使用 `--no-delete` 时，无论远端结果如何都只新增模型，不删除或覆盖本地已有模型。

**顺序语义。** 默认**保留你手排的模型顺序**，新模型**追加到列表末尾**；没有实质变更的同步不会重写文件。加 `--sort` 参数可顺带把每个端点的模型列表按 id 字典序升序（大小写敏感，`Z` 排在 `a` 前）重排——首次排序会重写一次，此后顺序已稳定，再跑 `--sort` 即 no-op。

**本地重复 id。** 同一个 id 在本地出现多条时，`--no-delete` 下**全部保留**（不静默去重丢失数据）；允许删除且远端已不下发该 id 时，所有重复条目会**一并删除**（只报告一次）。

## 安全性说明

**只改动 customendpoint，内置条目不可变。**

**无法识别 id 的条目会被直接清理。** 模型条目必须提供**非空的字符串** `id` 才有意义并参与同步。缺少 `id`、`id` 为空、或 `id` 非字符串、乃至不是对象的条目，在配置文件中毫无意义，会被**直接丢弃**（所有模式下都会清理，与 `--no-delete` 无关）。运行报告会对每个 provider 列出被丢弃的条目内容（`discarded invalid entries`）。

## 报告着色

在**交互式终端**（如 Windows Terminal、系统终端）中运行时，运行报告会自动着色以便快速定位关键信息；各行颜色含义：

| 报告行 | 颜色 | 含义 |
| - | - | - |
| `[changes]` / provider 名 | 绿 / 加粗 | 该 provider 有变更；名字加粗作定位锚点 |
| `[unchanged]` / `kept ...` | 灰（淡） | 无实质变化，弱化显示 |
| `added (...)` | 绿 | 新加入的模型 |
| `removed (...)` / `error: ...` | 红 | 被删除的模型 / 请求失败 |
| `credits: ...` | 青 | 账户余额（纯展示信息） |
| `deletion skipped ...` / `discarded invalid entries ...` / `warning: no endpoint url ...` | 黄 | 需要注意的提示（删除被跳过、无效条目被清理、端点缺失） |
| `Summary: ...` | 加粗 | 汇总行 |

**着色启用条件：**

- 仅当标准输出（stdout）是**交互式 TTY**（终端直接显示）时输出 ANSI 颜色。
- 当 stdout 被**重定向或管道**（如 `> log`、`| grep`）时自动输出纯文本，日志保持干净。
- 设置环境变量 **`NO_COLOR`**（任意值，如 `NO_COLOR=1`）可全局强制关闭颜色。
- 打印到标准错误（stderr）的机器可读副本**始终是纯文本**（不带色码），供脚本 / CI 抓取。

颜色**默认自动探测**：交互式终端直接显示时上色，重定向 / 管道 / 设置了 `NO_COLOR` 时自动输出纯文本，无需任何手动操作。`NO_COLOR`（如 `NO_COLOR=1`）只是可选开关，仅当你的终端**支持 TTY 但无法渲染 ANSI**（老版 conhost、部分远程会话）而看到裸转义码时才需手动设置；现代 Windows Terminal 无需理会。

## 工作区推荐配置

在 `.vscode/tasks.json` 中添加以下任务，让它变成快捷键。该任务同步的是**全局配置**（用户数据目录下 VS Code 实际生效的文件，`%APPDATA%\Code\User\chatLanguageModels.json`）：

```json
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "Sync Custom Endpoints",
      "command": "python",
      "args": [
        "-m",
        "clm_sync.cli",
        "--config",
        "${env:APPDATA}/Code/User/chatLanguageModels.json",
        "--all"
      ],
      "group": {
        "kind": "build",
        "isDefault": true
      }
    },
    {
      "label": "Sync Custom Endpoints in WT",
      "command": "wt",
      "args": [
        "-w",
        "0",
        "cmd",
        "/c",
        "python -m clm_sync.cli --config \"%APPDATA%\\Code\\User\\chatLanguageModels.json\" --all & echo. & pause"
      ],
      "presentation": {
        "reveal": "silent",
        "close": true
      }
    }
  ]
}
```

**任务说明：**

- `Sync Custom Endpoints`：在 VS Code 集成终端里同步全局配置。
- `Sync Custom Endpoints in WT`：通过 `wt`（Windows Terminal）弹出一个新窗口运行同步，报告直接显示在窗口里（交互终端下自动着色，见「报告着色」），结尾 `pause` 停住便于查看。

**双击运行的批处理脚本。** 仓库根目录还提供：

- `sync-global-models.bat` — 双击即在 cmd 窗口对全局配置执行与上述任务等价的同步。
- `deploy-user-tasks.bat`（调用 `deploy_user_tasks.py`）— 把工作区 `.vscode/tasks.json` 里的任务**合并**到用户级 `%APPDATA%\Code\User\tasks.json`，使任务对**所有** VS Code 窗口生效。合并语义按 `label` 匹配：同名任务原地更新、新任务追加、用户级已有的其他任务与字段（`dependencies` 等）**原样保留**；文件损坏时中止且不动原文件，无备份、原子写入。用法：`python deploy_user_tasks.py`（或双击 `deploy-user-tasks.bat`）。

## 开发

### 运行测试

```bash
cd copilot-byok-sync
python -m pytest tests/test_sync.py -v
```

### 项目结构

```
copilot-byok-sync/
├── .vscode/
│   └── tasks.json            # 工作区级同步任务（含 WT 窗口版）
├── src/clm_sync/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py          # 命令行入口（报告终端自动着色）
│   ├── client.py       # HTTP 请求（含 Authorization 头）
│   ├── config.py       # 读写 chatLanguageModels.json
│   ├── models.py       # 数据结构
│   ├── secrets.py      # VS Code 加密存储占位符解析（DPAPI + AES-GCM）
│   └── sync.py         # 核心同步逻辑
├── tests/
│   └── test_sync.py
├── sync-global-models.bat   # 双击同步全局配置（cmd 窗口）
├── deploy_user_tasks.py    # 把工作区任务合并到用户级 tasks.json（label 级合并）
├── deploy-user-tasks.bat   # 上一条的双击入口
├── CHANGELOG.md
└── README.md
```

### 后续计划

- 将端点请求优化为多线程并发，减少多个端点依次等待造成的总耗时。
- 支持参数简写。
