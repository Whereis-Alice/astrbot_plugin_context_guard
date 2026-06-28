# astrbot_plugin_context_guard

Runtime diagnostics for AstrBot OpenAI-style providers.

This plugin helps track down issues such as:

- context overflow caused by prompt, system prompt, history, dynamic injections, or tools
- provider retry branches that accidentally drop every non-system message
- follow-up upstream errors like `field messages is required`

## What it records

The plugin captures request state at multiple stages:

- `request_early`
- `request_late`
- `agent_begin`
- `provider_prepare`
- `provider_error`

For each request it can summarize:

- `prompt`
- `system_prompt`
- `contexts`
- `extra_user_content_parts`
- request-stage tool object estimates
- mutations made by hooks during `on_llm_request`

It also patches the OpenAI-style provider at runtime so we can see what the final payload looks like right before it is sent upstream.

## Commands

- `context_guard_status`
  Shows the latest captured summary for the current conversation.
- `context_guard_dump`
  Shows the dump directory for the latest captured request.

## Dump layout

Per-request files are written under:

```text
requests/<umo-hash>-<request_id>/events.jsonl
```

The most useful events are:

- `request_late`
  Tells you what hooks changed before provider preparation.
- `provider_prepare`
  Tells you what the final upstream payload looks like.
- `provider_error`
  Tells you what changed after provider overflow/error handling.

## Provider tools summary

When `record_provider_prepare_tools` is enabled, `provider_prepare` also records real statistics for `payloads["tools"]`:

- `tools_summary.count`
  Actual tool count in the prepared provider payload.
- `tools_summary.total_chars`
  Actual serialized character count of the final tools payload.
- `tools_summary.tool_names`
  Full tool name list in payload order.
- `tools_summary.largest_tools`
  Largest tools sorted by actual JSON size.

Each entry in `largest_tools` includes:

- `name`
- `type`
- `chars`
- `description_chars`
- `parameters_chars`
- `property_count`
- `required_count`

This is intentionally separate from the request-stage `tools_est=...` value shown in logs and status:

- `tools_est`
  An estimate based on local tool objects attached to the request before provider serialization.
- `provider_prepare.tools_summary.total_chars`
  The real serialized size of the final tools payload prepared for upstream.

If these two numbers are far apart, the request-stage estimate was a false lead and the actual upstream tools payload is probably not the root cause.

## Recommended workflow

1. Reproduce the problem once.
2. Run `context_guard_status`.
3. Run `context_guard_dump`.
4. Open `events.jsonl`.
5. Compare:
   - `request_early` vs `request_late`
   - `request_late` vs `provider_prepare`
   - `provider_prepare` vs `provider_error`

For tool-related cases, check:

- whether `tools_est` is huge only before provider serialization
- whether `provider_prepare.tools_summary.total_chars` is also huge
- which tools appear at the top of `largest_tools`
- whether `parameters_chars` rather than `description_chars` is driving the size

## Useful config switches

- `record_provider_prepare_tools`
  Enable or disable real payload tool analysis in `provider_prepare`.
- `provider_prepare_tool_top_n`
  Control how many tools are kept in the size ranking.
- `auto_fix_empty_messages_after_overflow`
  Rebuild a minimal safe retry payload when overflow handling empties every non-system message.

## Compatibility

- `astrbot >= 4.16, < 5`
- focused on `ProviderOpenAIOfficial` and compatible subclasses

## Custom plugin source

If you want AstrBot to detect updates for this plugin without publishing to the official market, add this registry URL as a custom plugin source:

- `https://raw.githubusercontent.com/Whereis-Alice/astrbot_plugin_context_guard/main/plugins.json`

If the plugin was previously installed manually, AstrBot may not automatically bind it to the new source. In that case, reinstall it from the custom source, or bind the existing install to that source if your AstrBot build exposes source binding in the plugin manager.

## Notes

This plugin uses lightweight runtime monkey patches for diagnostics and overflow retry repair. The patches are restored when the plugin unloads.
