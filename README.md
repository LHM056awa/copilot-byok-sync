# clm-sync — chatLanguageModels.json 模型列表同步工具

同步 VS Code（Copilot 聊天 BYOK）维护的 `chatLanguageModels.json` 文件中 `vendor=customendpoint` 条目的模型列表，从 OpenAI 兼容的 `/v1/models` 端点拉取最新模型信息。

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

#### 同步单个 customendpoint（可重复）

```bash
python -m clm_sync.cli --config chatLanguageModels.json --provider A
python -m clm_sync.cli --config chatLanguageModels.json --provider A --provider B
```

### 3. 命令行参数详解

| 参数                  | 含义                                                | 默认值 | 示例                                 |
| --------------------- | --------------------------------------------------- | ------ | ------------------------------------ |
| `--config`          | 配置文件路径（必须）                                | 无     | `--config chatLanguageModels.json` |
| `--all`             | 同步**所有** customendpoint（默认）           | 无     | `--all`                            |
| `--provider NAME`   | 同步**指定单个** customendpoint（可多次指定） | 无     | `--provider A`                     |
| `--dry-run`         | 只显示变更，不实际写入文件                          | 无     | `--dry-run`                        |
| `--no-delete`       | 只添加远端发现的新模型，不删除或覆盖本地已有模型    | 无     | `--no-delete`                      |
| `--timeout SECONDS` | 请求超时时间（秒）                                  | 15     | `--timeout 30`                     |
| `--version`         | 显示版本信息                                        | 无     | `--version`                        |

## URL 规范

你提供给工具的 `url` 可以是以下任意格式（工具会自动规范化）：

- `https://api.deepseek.com`
- `https://api.deepseek.com/v1`
- `https://api.deepseek.com/v1/chat/completions`

工具会自动把以上 URL 转换为 `https://api.deepseek.com/v1/models` 来请求模型列表。

`--config` 必须指向实际被 VS Code 使用的配置文件。如果你同时维护多个配置副本，请确认编辑的文件与同步命令使用的是同一个路径。

当前工具不会解析 VS Code Secret Storage 中的 `${input:...}` 引用，也不会自动读取或发送 API key。因此，只有无需认证即可访问 `/v1/models` 的端点可以直接同步；需要认证的端点会报告请求失败。

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

## 安全性说明

**只改动 customendpoint，内置条目不可变。**

**无法识别 id 的条目会被直接清理。** 模型条目必须提供**非空的字符串** `id` 才有意义并参与同步。缺少 `id`、`id` 为空、或 `id` 非字符串、乃至不是对象的条目，在配置文件中毫无意义，会被**直接丢弃**（所有模式下都会清理，与 `--no-delete` 无关）。运行报告会对每个 provider 列出被丢弃的条目内容（`discarded invalid entries`）。

## 工作区推荐配置

在 `.vscode/tasks.json` 中添加以下任务，让它变成快捷键：

```json
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "Sync Custom Endpoints",
      "type": "shell",
      "command": "python",
      "args": [
        "-m",
        "clm_sync.cli",
        "--config",
        "${workspaceFolder}/chatLanguageModels.json",
        "--all",
        "--no-delete"
      ],
      "group": {
        "kind": "build",
        "isDefault": true
      }
    }
  ]
}
```

## 开发

### 运行测试

```bash
cd copilot-byok-sync
python -m pytest tests/test_sync.py -v
```

### 项目结构

```
copilot-byok-sync/
├── src/clm_sync/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py          # 命令行入口
│   ├── client.py       # HTTP 请求
│   ├── config.py       # 读写 chatLanguageModels.json
│   ├── models.py       # 数据结构
│   └── sync.py         # 核心同步逻辑
├── tests/
│   └── test_sync.py
└── README.md
```

## 后续计划

- 将端点请求优化为多线程并发，减少多个端点依次等待造成的总耗时。
- 支持需要 API key 才能获取 `/v1/models` 的端点。
