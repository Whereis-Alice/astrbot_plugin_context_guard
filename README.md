# astrbot_plugin_context_guard

用于排查 AstrBot 的 OpenAI 风格 Provider 上下文爆掉问题，并在 overflow 重试把 `messages` 删空时做兜底修复。

这个插件主要帮你定位下面几类问题：

- `prompt`、`system_prompt`、历史消息、动态注入内容、tools 中到底是谁把上下文撑爆了
- overflow 重试分支误删所有非 system 消息，最终触发 `field messages is required`
- hook 链路里是谁改了请求内容，改了多少

## 插件会记录什么

插件会在多个阶段记录同一请求的状态：

- `request_early`
- `request_late`
- `agent_begin`
- `provider_prepare`
- `provider_error`

每次请求会统计这些内容：

- `prompt`
- `system_prompt`
- `contexts`
- `extra_user_content_parts`
- 请求阶段的 tool 体积估算值
- `on_llm_request` 阶段发生的变更记录

它还会在运行时给 OpenAI 风格 Provider 打轻量补丁，用来观察真正发给上游之前的最终 payload 长什么样。

## 命令

- `context_guard_status`
  查看当前会话最近一次抓到的摘要
- `context_guard_dump`
  查看最近一次请求对应的 dump 目录

## Dump 文件位置

每次请求的记录会写到：

```text
requests/<umo-hash>-<request_id>/events.jsonl
```

最值得优先看的事件：

- `request_late`
  看 hook 链路在 provider_prepare 之前改了什么
- `provider_prepare`
  看最终发往上游的 payload 长什么样
- `provider_error`
  看 overflow 或报错后，请求又发生了什么变化

## provider_prepare 的 tools 真实体积统计

当 `record_provider_prepare_tools` 开启时，`provider_prepare` 会额外记录 `payloads["tools"]` 的真实统计结果：

- `tools_summary.count`
  最终 payload 里的真实工具数量
- `tools_summary.total_chars`
  最终 tools JSON 序列化后的真实总字符数
- `tools_summary.tool_names`
  按实际 payload 顺序记录的工具名列表
- `tools_summary.largest_tools`
  按真实 JSON 体积从大到小排序的工具列表

`largest_tools` 里的每一项会带这些字段：

- `name`
- `type`
- `chars`
- `description_chars`
- `parameters_chars`
- `property_count`
- `required_count`

这部分和日志/状态里的 `tools_est=...` 是分开的，含义不一样：

- `tools_est`
  请求对象在 provider 序列化之前，对本地 tool 对象做出的估算值
- `provider_prepare.tools_summary.total_chars`
  真正准备发给上游时，最终 tools payload 的实际字符数

如果这两个数字差很大，说明“tool 很大”可能只是前期估算假象，真正的根因未必在最终 tools payload。

从 `0.1.2` 开始，如果 `log_provider_prepare_summary` 开启，插件还会直接把 `provider_prepare` 的真实摘要打印到日志里，包括：

- `messages` 的真实总字符数
- `tools` 的真实总字符数
- `tools_est` 和真实 tools payload 的差值
- `top_tools` 预览
- 一句简短结论，告诉你 tools 到底像不像根因

## 推荐排查流程

1. 先稳定复现一次问题
2. 执行 `context_guard_status`
3. 执行 `context_guard_dump`
4. 打开 `events.jsonl`
5. 重点对比下面几组阶段：

- `request_early` 和 `request_late`
- `request_late` 和 `provider_prepare`
- `provider_prepare` 和 `provider_error`

如果怀疑是 tools 引发的问题，重点看：

- `tools_est` 是不是只在 provider 序列化前看起来很大
- `provider_prepare.tools_summary.total_chars` 是不是真的也很大
- `largest_tools` 里到底是哪几个 tool 最肥
- 体积主要来自 `parameters_chars` 还是 `description_chars`

## 常用配置项

- `record_provider_prepare_tools`
  是否记录 `provider_prepare` 阶段真实的 tools payload 统计
- `log_provider_prepare_summary`
  是否把 `provider_prepare` 的真实摘要直接输出到日志
- `provider_prepare_tool_top_n`
  `largest_tools` 最多保留多少个 tool
- `provider_prepare_log_top_n`
  日志里的 `top_tools` 最多显示多少个 tool
- `auto_fix_empty_messages_after_overflow`
  当 overflow 重试把所有非 system 消息删空时，自动构造一个最小安全重试请求

## 兼容性

- `astrbot >= 4.16, < 5`
- 重点适配 `ProviderOpenAIOfficial` 及其兼容子类

## 自定义插件源

如果你没有把插件发到官方市场，但又想让 AstrBot 识别更新，可以在 AstrBot 的“添加插件源”里填这个地址：

- `https://raw.githubusercontent.com/Whereis-Alice/astrbot_plugin_context_guard/main/plugins.json`

如果这个插件之前是手动安装的，AstrBot 不一定会自动把它绑定到这个新源。遇到这种情况，通常有两种处理方式：

- 直接从这个自定义源重新安装一次
- 如果你的 AstrBot 版本支持“绑定插件源”，就把现有安装绑定到这个源

## 说明

这个插件使用的是轻量级运行时 monkey patch，目标是尽量少侵入地抓取诊断信息，并修复 overflow 后 `messages` 被删空导致的重试异常。插件卸载时会恢复补丁。
