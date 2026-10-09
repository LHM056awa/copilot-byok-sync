# Changelog

All notable changes to `clm-sync` are documented in this file.

## 0.6.1 (2026-10-09)

### Fixed

- **交互式终端下不再重复打印报告**：此前当 provider 有失败时，报告会打印两遍——stdout 一份带色、stderr 一份无色。原因是 stderr 副本的判断条件 `failed and plain_report` 把"是否有失败"与"用户能否看到 stdout"两个正交维度混在一起。现改为**仅在 stdout 不是交互式 TTY**（重定向 / 管道 / CI）时镜像 stderr 副本，且恒为纯文本；交互式终端下只打一份。正向副作用：零失败运行的 CI 现在也能从 stderr 拿到完整报告。

---

## 0.6.0 (2026-10-02)

### Added

- **获取时自动忽略非文本模型**：同步 `/v1/models` 时只看远端条目的 `id` 与 `name`（大小写不敏感），命中即跳过、不新增入库；`vision` 为文本对话模型能力标记，不在过滤范围。报告新增黄色 `filtered non-text` 行。
- **无文本信号端点不触发删除**：远端只返回非文本模型时视为无文本模型信号，跳过删除以保护本地文本模型（与请求失败同等保护）；报告新增黄色 `no text signal` 行。

### Fixed

- **关键词匹配边界**：短词根 `voice`/`dall`/`sora`/`imagen`/`music`/`image`/`embed` 改为独立 token 匹配（`embed` 另补 `embedding`/`embeddings` 派生形式，`text-embedding-3-large` 仍被过滤），不再误杀 `invoice-parser`、`medallion-7b`、`sorami-7b`、`imagenet-classifier`、`musical-theory-llm`、`imagery-chat`、`embedded-reasoning` 等文本模型；`gpt-image-1`、`image-gen`、`dall-e-3`、`sora-2` 等仍正常过滤。
- **`diffusion` 词根改 token 匹配**：`diffusiongemma-26b-a4b-it` 这类用离散扩散方法训练的文本输出模型不再被误杀；`stable-diffusion-xl`、`text-to-image-diffusion`、`diffusion-3` 等真正图像生成模型仍过滤。
- **token 匹配支持数字后缀**：边界由 `[a-z0-9]` 放宽为 `[a-z]`，`tts1`/`asr1`/`stt2`/`sora2`/`imagen4`/`dalle3` 现在能正常过滤；`matt`/`stts`/`asrock`/`ttsx`/`xtts` 仍不误杀。
- **中文 VLM 对话模型不再误杀**：`name` 含「理解」时中文词根（视频/图像/图片/音频/语音/音乐）豁免，`图像理解`/`视频理解`/`图片理解` 保留；`图像生成器`/`视频解说` 仍过滤。豁免只作用于中文词根，不影响英文词根。
- **手写的关键词模型不再被静默删除**：本地已存在且远端仍返回的模型（即使命中关键词被过滤）保留，不再连同 `settings` 一起删。副作用：已同步入库的媒体模型在远端仍返回时也不再被清理。
- **多端点删除保护改为 endpoint 级**：一个端点的文本信号不再授权删除另一个端点的本地模型；无文本信号的端点通过 `no_signal_endpoints` 报告。
- **重复 id 一文本一非文本时不再污染删除闸门**：同一 id 只要有一条文本条目即视为文本（与"远端仍返回即保留"一致），报告不再自相矛盾。

---

## 0.5.3 (2026-09-30)

### Fixed

- Windows 真控制台（conhost）默认不开 VT processing，之前只看 isatty() 就上色，导致 cmd 里出现裸转义码。现在 Windows 下先用 GetConsoleMode 探测、必要时 SetConsoleMode 开启 ENABLE_VIRTUAL_TERMINAL_PROCESSING；开启失败则回退纯文本。VS Code 集成终端等非真控制台句柄保持信任。

### Changed

- `Sync Custom Endpoints in WT` 任务改名为 `Sync Custom Endpoints (Console)`：通过 `start cmd` 启动独立 conhost 窗口，不再依赖 wt.exe（wt 是 COM 派发 stub，会导致 VS Code 任务完成信号丢失、下次运行弹“选择要终止的实例”）。

---

## 0.5.2 (2026-09-28)

### Fixed

- **免安装运行找不到模块**：`src` 布局下未执行 `pip install -e .` 时，`python -m clm_sync.cli` 报 `ModuleNotFoundError: No module named 'clm_sync'`。`sync-global-models.bat` 与 `.vscode/tasks.json` 两个任务现默认把 `src` 加入 `PYTHONPATH`，双击 / 任务直跑即可；`client.py` 的 `User-Agent` 硬编码版本号改为跟随 `__version__`，避免下次发版遗漏。

---

## 0.5.1 (2026-09-27)

### Fixed

- **跨 provider settings 残留清理**：同步删除某 customendpoint 模型时，其他 vendor（如 `agent-host-copilotcli`）`settings` 里以 `<vendor>/<provider>/<model_id>` 形式引用该模型的条目此前无人清理，导致模型已删但配置残留。新增 `prune_cross_settings` 在 `sync_config` 末尾对整个配置做第二遍剪枝，删除任意 provider `settings` 中精确匹配 `customendpoint/<name>/<id>` 的 key（只碰 `settings`，绝不动其他 vendor 的 `models` 与受保护字段，且幂等）；报告在源 provider 下打印 `cross-provider settings removed` 行。

---

## 0.5.0 (2026-09-27)

### Added

- **报告终端自动着色**：stdout 为交互式 TTY 时运行报告带 ANSI 颜色（`[changes]`/`added` 绿、`removed`/`error` 红、`credits` 青、各类 warning 黄、汇总行加粗）；重定向 / 管道 / 设置 `NO_COLOR` 时自动回退纯文本；stderr 机器可读副本恒无色。
- **`deploy_user_tasks.py` + `deploy-user-tasks.bat`**：把工作区 `.vscode/tasks.json` 按 `label` 合并进用户级 `%APPDATA%\Code\User\tasks.json`（同名更新、新任务追加、用户级其他任务/字段原样保留）；无备份、原子写入、目标文件损坏时中止不动原文件。
- 工作区 `.vscode/tasks.json` 新增 `Sync Custom Endpoints in WT` 任务：经 `wt`（Windows Terminal）弹窗运行同步，报告直接显示在窗口内（交互终端自动着色）。

---

## 0.4.0 (2026-09-27)

### Fixed

- **`base_display_name` 保留版本号/日期中的点**：`.` 不再当作单词分隔符，`agnes-2.0-flash` 渲染为 `Agnes 2.0 Flash`（此前被拆成 `Agnes 2 0 Flash`）；`2024.08.06`、`v4.5` 等日期/版本 token 同理。

### Changed

- **新加入模型条目的字段顺序固定**：新条目统一按 `id → name → url → toolCalling → vision → maxInputTokens → maxOutputTokens → supportsReasoningEffort` 写盘（`id` 恒在前），不再随远端是否返回 `name` 而变化；已存在条目仍原样保留不受影响。

### Added

- **无端点 provider 告警 + 端点指针保留**：`models` 里没有任何可请求 `url` 的 provider 不再被静默忽略，报告打印 `warning: no endpoint url ...`（配置提示，不影响退出码与删除保护）；url-only 指针条目在请求失败时保留为 resync hook，避免端点信息永久丢失。

---

## 0.3.0 (2026-09-27)

### Added

- **自动余额查询**：同步运行结束后，自动查询已知厂商端点（`api.deepseek.com`、`openrouter.ai`、`api.moonshot.cn`、`api.moonshot.ai`、`api.stepfun.com`、`api.novita.ai`）的账户余额，在报告对应 provider 下追加 `credits:` 行。
- `--no-credits` 参数：跳过余额查询。
- `sync-global-models.bat`：双击运行的全局同步辅助脚本（等价于 `.vscode/tasks.json` 任务）。

### Changed

- `enrich_with_credits`：余额 key 解析改走 `resolve_provider_key`（与模型同步路径等价）；`--all` 模式下按 `config_index` 精确配对 provider（不再依赖列表顺序）。
- `_json_path`：索引部分收紧为显式 ASCII 十进制（`[0-9]+`），拒绝 Unicode 数字（如 `²`）等非法格式。
- `parse_credits_payload`：非 JSON 响应（HTML 错误页）返回 `None` 而非回退展示原始 body；字符串数字自动 `float()` 再应用 scale。

### Fixed

- 字面 `apiKey` 在余额查询路径被错误丢弃（现与模型同步一致穿透）。
- `apiKey` 缺失时 `resolve_placeholder(None)` 触发 `AttributeError` 崩溃（现降级为无 key）。
- OpenRouter 多字段 spec 缺失字段时 label 张冠李戴（现按 spec 声明的字段数决定是否加标签）。

---

## 0.2.0 (2026-09-26)

### Added

- **API key 自动解析**：provider 的 `apiKey` 为 `${input:chat.lm.secret.<id>}` 占位符时，自动从 VS Code 加密存储（`state.vscdb` + DPAPI + AES-256-GCM）解密出真实 key，以 `Authorization: Bearer` 头发送。
- `--sort` 参数：把每个端点的模型列表按 id 字典序升序（大小写敏感）重排。

### Changed

- 默认任务（`.vscode/tasks.json`）改为同步全局配置（`%APPDATA%\Code\User\chatLanguageModels.json`），去掉 `--no-delete`。

### Fixed

- 占位符解析失败时不再把 `${input:...}` 文本作为 Bearer token 发出（降级为无 key）。
- `fetcher` 带 `*args`（VAR_POSITIONAL）时不再被误判为不支持第三参导致 key 被静默丢弃。
- 远端响应 `data` 非 list（如 dict/string/scalar）时不再静默迭代出垃圾模型 id，改报 `TransportError`。
- 本地重复 id 在 `--no-delete` 下全部保留；允许删除且远端已不下发时一并删除（报告一次）。

---

## 0.1.0 (2026-09-26)

### Added

- 初始版本：从 OpenAI 兼容 `/v1/models` 端点同步 `vendor=customendpoint` 条目的模型列表。
- `--config`、`--all`、`--provider`、`--dry-run`、`--no-delete`、`--timeout` 参数。
- 删除保护：任一端点失败时跳过删除。
- 新模型自动补齐 `toolCalling`、`vision`、`maxInputTokens`、`maxOutputTokens`、`supportsReasoningEffort`。
- 无法识别 id 的条目直接清理（`discarded_invalid` 报告）。
