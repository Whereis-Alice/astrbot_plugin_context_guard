# astrbot_plugin_context_guard

用于排查 AstrBot 在 OpenAI / OpenAI 兼容 Provider 上出现的这类问题：

- `上下文长度超过限制`
- 随后的重试把 `messages` 删空
- 上游返回 `field messages is required`

插件做两件事：

1. 诊断

- 在 `on_llm_request` 早晚各抓一次 `ProviderRequest`
- 记录 `prompt`、`system_prompt`、`contexts`、`extra_user_content_parts` 的字符量变化
- 跟踪 `request.contexts` / `request.extra_user_content_parts` 在 hook 阶段被谁追加过内容
- 在 OpenAI provider 的 `_prepare_chat_payload`、`_handle_api_error` 阶段继续写 dump，看到真实 `messages` 是怎么变化的

2. 兜底修复

- 当 provider 因 `context length` 进入重试分支，并把 `messages` 删到只剩 system 或完全为空时：
  - 优先裁剪过长的 system 消息
  - 保留最后一条非 system 消息
  - 必要时补一条最小 user 占位消息

这能直接拦住后续的 `field messages is required`。

## 目录结构

```text
astrbot_plugin_context_guard/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── README.md
├── LICENSE
└── .gitignore
```

## 使用方式

把整个 `astrbot_plugin_context_guard` 目录放进 AstrBot 插件目录并启用。

启用后：

- 看控制台日志里的 `[ContextGuard] ...`
- 用命令 `context_guard_status` 查看当前会话最近一次捕获摘要
- 用命令 `context_guard_dump` 查看 dump 路径

## Dump 内容

插件数据目录下会生成：

- `requests/<hash>-<request_id>/events.jsonl`

里面会按时间顺序记录：

- `request_early`
- `request_late`
- `agent_begin`
- `provider_prepare`
- `provider_error`

重点看这两个阶段：

- `provider_prepare`
  这里能看到最终要发给上游的 `messages` 数量、角色分布和字符量
- `provider_error`
  这里能看到 context overflow 处理前后的变化，以及插件是否执行了修复动作

## 推荐排查顺序

1. 先复现一次问题
2. 运行 `context_guard_status`
3. 打开 `context_guard_dump` 给出的目录，查看 `events.jsonl`
4. 对比：
   - `request_early` 到 `request_late` 是否出现了异常大的增长
   - `provider_prepare` 的 `messages` 是否已经极大
   - `provider_error` 里是不是在 overflow 后丢光了非 system 消息

## 兼容性

- 目标版本：`astrbot >= 4.16, < 5`
- 重点覆盖 OpenAI 风格 Provider（`ProviderOpenAIOfficial` 及其子类）

## 说明

这是一个运行时诊断插件，会对 OpenAI provider 做轻量 monkey patch。
如果你停用或卸载插件，patch 会在插件卸载时恢复。
