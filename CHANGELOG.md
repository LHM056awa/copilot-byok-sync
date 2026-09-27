# Changelog

All notable changes to `clm-sync` are documented in this file.

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
