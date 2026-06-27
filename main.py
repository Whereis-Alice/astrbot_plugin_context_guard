"""AstrBot plugin: diagnose context overflow and guard empty-message retries."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register


PLUGIN_ID = "astrbot_plugin_context_guard"
PLUGIN_VERSION = "0.1.0"
PLUGIN_DESC = "诊断上下文过长根因，记录 payload 演变，并修复 overflow 后 messages 被删空的重试问题"
PLUGIN_REPO = ""

STATE_EXTRA_KEY = f"{PLUGIN_ID}.state"
EARLY_PRIORITY = 10000
LATE_PRIORITY = -10000
OVERFLOW_MARKER = "\n[ContextGuard truncated after context overflow]\n"
PREVIEW_FALLBACK = "-"

_ACTIVE_PLUGIN: "ContextGuardPlugin | None" = None
_PATCHED = False
_PATCH_ORIGINALS: dict[str, Any] = {}
_PATCH_CLASSES: dict[str, Any] = {}


@dataclass
class MutationRecord:
    channel: str
    action: str
    source: str
    count: int
    preview: str
    when: float = field(default_factory=time.time)


@dataclass
class RequestSummary:
    prompt_chars: int
    system_prompt_chars: int
    context_message_count: int
    context_non_system_count: int
    context_chars: int
    extra_parts_count: int
    extra_parts_chars: int
    tool_count: int
    roles: dict[str, int]
    longest_message_chars: int
    total_chars: int


@dataclass
class FixRecord:
    phase: str
    reason: str
    actions: list[str]
    before_messages: int
    after_messages: int
    note: str = ""
    when: float = field(default_factory=time.time)


@dataclass
class RequestAuditState:
    request_id: str
    started_at: float
    session_id: str
    umo: str
    dump_dir: str
    initial_summary: RequestSummary
    initial_prompt_preview: str
    initial_system_preview: str
    final_summary: RequestSummary | None = None
    final_prompt_preview: str = ""
    final_system_preview: str = ""
    mutations: list[MutationRecord] = field(default_factory=list)
    diagnoses: list[str] = field(default_factory=list)
    fixes: list[FixRecord] = field(default_factory=list)


class TrackedRequestList(list[Any]):
    """Track in-place request list mutations across hook execution."""

    def __init__(
        self,
        values: Iterable[Any],
        *,
        channel: str,
        owner: "ContextGuardPlugin",
        state: RequestAuditState,
    ) -> None:
        super().__init__(values)
        self._channel = channel
        self._owner = owner
        self._state = state

    def append(self, item: Any) -> None:  # type: ignore[override]
        super().append(item)
        self._record("append", [item])

    def extend(self, values: Iterable[Any]) -> None:  # type: ignore[override]
        values_list = list(values)
        super().extend(values_list)
        self._record("extend", values_list)

    def insert(self, index: int, item: Any) -> None:  # type: ignore[override]
        super().insert(index, item)
        self._record("insert", [item])

    def pop(self, index: int = -1) -> Any:  # type: ignore[override]
        item = super().pop(index)
        self._record("pop", [item])
        return item

    def clear(self) -> None:  # type: ignore[override]
        removed = list(self)
        super().clear()
        self._record("clear", removed)

    def __iadd__(self, values: Iterable[Any]) -> "TrackedRequestList":
        self.extend(values)
        return self

    def __setitem__(self, index: Any, value: Any) -> None:
        if isinstance(index, slice):
            values = list(value)
            super().__setitem__(index, values)
        else:
            values = [value]
            super().__setitem__(index, value)
        self._record("setitem", values)

    def _record(self, action: str, values: list[Any]) -> None:
        self._owner.record_mutation(self._state, self._channel, action, values)


@register(PLUGIN_ID, "Codex", PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class ContextGuardPlugin(Star):
    """Diagnose context overflow causes and repair empty-message retries."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict[str, Any] | None = None,
    ) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self._data_dir = Path(StarTools.get_data_dir(PLUGIN_ID))
        self._requests_dir = self._data_dir / "requests"
        self._last_state_by_umo: dict[str, RequestAuditState] = {}
        self._last_state_by_session: dict[str, RequestAuditState] = {}
        self._session_to_umo: dict[str, str] = {}
        self._plugin_file = Path(__file__).resolve().as_posix().lower()

    async def initialize(self) -> None:
        self._requests_dir.mkdir(parents=True, exist_ok=True)
        self._set_active()
        self._apply_provider_patches()
        logger.info("[%s] plugin initialized", PLUGIN_ID)

    async def terminate(self) -> None:
        self._restore_provider_patches()
        self._clear_active()
        logger.info("[%s] plugin terminated", PLUGIN_ID)

    @filter.on_llm_request(priority=EARLY_PRIORITY)
    async def capture_request_start(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self._cfg_bool("enabled", True):
            return

        session_key = self._session_key(getattr(req, "session_id", "") or "")
        request_id = f"{int(time.time() * 1000)}-{id(req)}"
        dump_dir = self._request_dump_dir(event.unified_msg_origin, request_id)
        dump_dir.mkdir(parents=True, exist_ok=True)

        state = RequestAuditState(
            request_id=request_id,
            started_at=time.time(),
            session_id=session_key,
            umo=event.unified_msg_origin,
            dump_dir=str(dump_dir),
            initial_summary=self._summarize_request(req),
            initial_prompt_preview=self._preview_text(getattr(req, "prompt", None)),
            initial_system_preview=self._preview_text(getattr(req, "system_prompt", "")),
        )
        self._remember_state(state)
        self._set_state_refs(event, req, state)

        req.contexts = TrackedRequestList(
            getattr(req, "contexts", []) or [],
            channel="request.contexts",
            owner=self,
            state=state,
        )
        req.extra_user_content_parts = TrackedRequestList(
            getattr(req, "extra_user_content_parts", []) or [],
            channel="request.extra_user_content_parts",
            owner=self,
            state=state,
        )

        self._append_dump_event(
            state,
            "request_early",
            {
                "summary": asdict(state.initial_summary),
                "prompt_preview": state.initial_prompt_preview,
                "system_prompt_preview": state.initial_system_preview,
                "session_id": session_key,
                "umo": event.unified_msg_origin,
            },
        )

    @filter.on_llm_request(priority=LATE_PRIORITY)
    async def capture_request_end(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self._cfg_bool("enabled", True):
            return

        state = self._get_state(event, req)
        if state is None:
            return

        state.final_summary = self._summarize_request(req)
        state.final_prompt_preview = self._preview_text(getattr(req, "prompt", None))
        state.final_system_preview = self._preview_text(getattr(req, "system_prompt", ""))
        state.diagnoses = self._diagnose_request(state)
        self._remember_state(state)

        self._append_dump_event(
            state,
            "request_late",
            {
                "summary": asdict(state.final_summary),
                "prompt_preview": state.final_prompt_preview,
                "system_prompt_preview": state.final_system_preview,
                "mutations": [asdict(item) for item in state.mutations],
                "diagnoses": state.diagnoses,
            },
        )

        total_chars = state.final_summary.total_chars
        warn_limit = self._cfg_int("warn_total_chars", 20000)
        log_fn = logger.warning if warn_limit > 0 and total_chars >= warn_limit else logger.info
        log_fn(
            "[ContextGuard] umo=%s session=%s total_chars=%s prompt=%s system=%s contexts=%s/%s extra=%s tools=%s",
            state.umo,
            state.session_id or PREVIEW_FALLBACK,
            total_chars,
            state.final_summary.prompt_chars,
            state.final_summary.system_prompt_chars,
            state.final_summary.context_message_count,
            state.final_summary.context_chars,
            state.final_summary.extra_parts_chars,
            state.final_summary.tool_count,
        )
        if state.diagnoses:
            log_fn("[ContextGuard] diagnoses: %s", " | ".join(state.diagnoses))
        if state.mutations:
            preview = " | ".join(
                f"{item.source} {item.channel}.{item.action} +{item.count}"
                for item in state.mutations[: self._cfg_int("mutation_log_limit", 8)]
            )
            log_fn("[ContextGuard] tracked mutations: %s", preview)

    @filter.on_agent_begin(priority=LATE_PRIORITY)
    async def capture_agent_context(
        self,
        event: AstrMessageEvent,
        run_context: Any,
    ) -> None:
        if not self._cfg_bool("enabled", True):
            return
        if not self._cfg_bool("log_agent_context", True):
            return

        state = self._last_state_by_umo.get(event.unified_msg_origin)
        if state is None:
            return

        summary = self._summarize_payload_messages(getattr(run_context, "messages", []) or [])
        self._append_dump_event(state, "agent_begin", {"summary": summary})

    @filter.command("context_guard_status")
    async def context_guard_status(self, event: AstrMessageEvent):
        """Show the latest ContextGuard summary for this conversation."""
        state = self._last_state_by_umo.get(event.unified_msg_origin)
        if state is None:
            yield event.plain_result("ContextGuard: 当前会话还没有捕获到请求记录。")
            return
        yield event.plain_result(self._format_status(state))

    @filter.command("context_guard_dump")
    async def context_guard_dump(self, event: AstrMessageEvent):
        """Show the dump directory for the latest captured request."""
        state = self._last_state_by_umo.get(event.unified_msg_origin)
        if state is None:
            yield event.plain_result("ContextGuard: 当前会话还没有 dump。")
            return
        yield event.plain_result(f"ContextGuard dump: {state.dump_dir}")

    def record_mutation(
        self,
        state: RequestAuditState,
        channel: str,
        action: str,
        values: list[Any],
    ) -> None:
        record = MutationRecord(
            channel=channel,
            action=action,
            source=self._infer_mutation_source(),
            count=len(values),
            preview=self._preview_value(values[0] if values else None),
        )
        state.mutations.append(record)

    def on_provider_payload_prepared(
        self,
        provider: Any,
        payloads: dict,
        context_query: list[Any],
    ) -> None:
        state = self._state_for_provider(provider)
        if state is None:
            return
        summary = self._summarize_payload_messages(payloads.get("messages", []) or [])
        self._append_dump_event(
            state,
            "provider_prepare",
            {
                "summary": summary,
                "model": payloads.get("model"),
            },
        )

    def on_provider_api_error_handled(
        self,
        provider: Any,
        error: Exception,
        before_payloads: dict[str, Any],
        before_context_query: list[Any],
        result: tuple[Any, ...],
    ) -> tuple[Any, ...]:
        state = self._state_for_provider(provider)
        if len(result) != 7:
            return result

        (
            success,
            chosen_key,
            available_api_keys,
            payloads,
            context_query,
            func_tool,
            image_fallback_used,
        ) = result

        before_summary = self._summarize_payload_messages(before_payloads.get("messages", []) or [])
        after_summary = self._summarize_payload_messages(payloads.get("messages", []) or [])
        error_text = self._preview_text(str(error), limit=320)

        actions: list[str] = []
        if (
            self._cfg_bool("auto_fix_empty_messages_after_overflow", True)
            and self._is_context_overflow_error(error)
        ):
            repaired_messages, actions = self._repair_after_context_overflow(
                before_context_query=before_context_query,
                after_messages=payloads.get("messages", []) or [],
            )
            if repaired_messages is not None:
                payloads["messages"] = repaired_messages
                if isinstance(context_query, list):
                    context_query[:] = copy.deepcopy(repaired_messages)
                after_summary = self._summarize_payload_messages(repaired_messages)
                if state is not None:
                    fix = FixRecord(
                        phase="provider_overflow_fix",
                        reason="context overflow fallback removed all non-system messages",
                        actions=actions,
                        before_messages=before_summary.get("message_count", 0),
                        after_messages=after_summary.get("message_count", 0),
                        note="Applied after provider overflow handling returned an unsafe payload.",
                    )
                    state.fixes.append(fix)
                    self._remember_state(state)
                logger.warning(
                    "[ContextGuard] repaired overflow retry payload: session=%s before=%s after=%s actions=%s",
                    getattr(provider, "_context_guard_current_session_id", "") or PREVIEW_FALLBACK,
                    before_summary.get("message_count", 0),
                    after_summary.get("message_count", 0),
                    " | ".join(actions) if actions else PREVIEW_FALLBACK,
                )

        if state is not None:
            self._append_dump_event(
                state,
                "provider_error",
                {
                    "error": error_text,
                    "before_summary": before_summary,
                    "after_summary": after_summary,
                    "actions": actions,
                    "success": bool(success),
                    "image_fallback_used": bool(image_fallback_used),
                },
            )

        return (
            success,
            chosen_key,
            available_api_keys,
            payloads,
            context_query,
            func_tool,
            image_fallback_used,
        )

    def _set_active(self) -> None:
        global _ACTIVE_PLUGIN
        _ACTIVE_PLUGIN = self

    def _clear_active(self) -> None:
        global _ACTIVE_PLUGIN
        if _ACTIVE_PLUGIN is self:
            _ACTIVE_PLUGIN = None

    def _apply_provider_patches(self) -> None:
        global _PATCHED
        if _PATCHED:
            return

        try:
            from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        except Exception as exc:
            logger.warning("[%s] failed to import openai provider for patching: %s", PLUGIN_ID, exc)
            return

        _PATCH_CLASSES["ProviderOpenAIOfficial"] = ProviderOpenAIOfficial
        _PATCH_ORIGINALS["text_chat"] = ProviderOpenAIOfficial.text_chat
        _PATCH_ORIGINALS["text_chat_stream"] = ProviderOpenAIOfficial.text_chat_stream
        _PATCH_ORIGINALS["_prepare_chat_payload"] = ProviderOpenAIOfficial._prepare_chat_payload
        _PATCH_ORIGINALS["_handle_api_error"] = ProviderOpenAIOfficial._handle_api_error

        async def text_chat_wrapper(provider: Any, *args: Any, **kwargs: Any) -> Any:
            session_id = kwargs.get("session_id")
            if session_id is None and len(args) > 1:
                session_id = args[1]
            setattr(provider, "_context_guard_current_session_id", session_id or "")
            try:
                return await _PATCH_ORIGINALS["text_chat"](provider, *args, **kwargs)
            finally:
                setattr(provider, "_context_guard_current_session_id", "")

        async def text_chat_stream_wrapper(provider: Any, *args: Any, **kwargs: Any):
            session_id = kwargs.get("session_id")
            if session_id is None and len(args) > 1:
                session_id = args[1]
            setattr(provider, "_context_guard_current_session_id", session_id or "")
            try:
                async for chunk in _PATCH_ORIGINALS["text_chat_stream"](provider, *args, **kwargs):
                    yield chunk
            finally:
                setattr(provider, "_context_guard_current_session_id", "")

        async def prepare_chat_payload_wrapper(provider: Any, *args: Any, **kwargs: Any) -> tuple[Any, Any]:
            payloads, context_query = await _PATCH_ORIGINALS["_prepare_chat_payload"](
                provider,
                *args,
                **kwargs,
            )
            plugin = _ACTIVE_PLUGIN
            if plugin is not None:
                plugin.on_provider_payload_prepared(provider, payloads, context_query)
            return payloads, context_query

        async def handle_api_error_wrapper(
            provider: Any,
            error: Exception,
            payloads: dict,
            context_query: list,
            func_tool: Any,
            chosen_key: str,
            available_api_keys: list[str],
            retry_cnt: int,
            max_retries: int,
            image_fallback_used: bool = False,
        ) -> tuple[Any, ...]:
            before_payloads = copy.deepcopy(payloads)
            before_context_query = copy.deepcopy(context_query)
            result = await _PATCH_ORIGINALS["_handle_api_error"](
                provider,
                error,
                payloads,
                context_query,
                func_tool,
                chosen_key,
                available_api_keys,
                retry_cnt,
                max_retries,
                image_fallback_used=image_fallback_used,
            )
            plugin = _ACTIVE_PLUGIN
            if plugin is None:
                return result
            return plugin.on_provider_api_error_handled(
                provider=provider,
                error=error,
                before_payloads=before_payloads,
                before_context_query=before_context_query,
                result=result,
            )

        ProviderOpenAIOfficial.text_chat = text_chat_wrapper
        ProviderOpenAIOfficial.text_chat_stream = text_chat_stream_wrapper
        ProviderOpenAIOfficial._prepare_chat_payload = prepare_chat_payload_wrapper
        ProviderOpenAIOfficial._handle_api_error = handle_api_error_wrapper
        _PATCHED = True

    def _restore_provider_patches(self) -> None:
        global _PATCHED
        if not _PATCHED:
            return
        provider_cls = _PATCH_CLASSES.get("ProviderOpenAIOfficial")
        if provider_cls is None:
            return

        for name in ("text_chat", "text_chat_stream", "_prepare_chat_payload", "_handle_api_error"):
            original = _PATCH_ORIGINALS.get(name)
            if original is not None:
                setattr(provider_cls, name, original)
        _PATCHED = False

    def _remember_state(self, state: RequestAuditState) -> None:
        self._last_state_by_umo[state.umo] = state
        if state.session_id:
            self._last_state_by_session[state.session_id] = state
            self._session_to_umo[state.session_id] = state.umo
        self._trim_state_cache()

    def _trim_state_cache(self) -> None:
        max_items = self._cfg_int("remember_last_sessions", 50)
        if max_items <= 0:
            self._last_state_by_umo.clear()
            self._last_state_by_session.clear()
            self._session_to_umo.clear()
            return

        while len(self._last_state_by_umo) > max_items:
            first_key = next(iter(self._last_state_by_umo))
            removed = self._last_state_by_umo.pop(first_key)
            if removed.session_id:
                self._last_state_by_session.pop(removed.session_id, None)
                self._session_to_umo.pop(removed.session_id, None)

    def _set_state_refs(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        state: RequestAuditState,
    ) -> None:
        try:
            event.set_extra(STATE_EXTRA_KEY, state)
        except Exception:
            pass
        setattr(req, STATE_EXTRA_KEY.replace(".", "_"), state)

    def _get_state(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest | None,
    ) -> RequestAuditState | None:
        try:
            state = event.get_extra(STATE_EXTRA_KEY, None)
            if isinstance(state, RequestAuditState):
                return state
        except Exception:
            pass

        if req is not None:
            state = getattr(req, STATE_EXTRA_KEY.replace(".", "_"), None)
            if isinstance(state, RequestAuditState):
                return state
            session_key = self._session_key(getattr(req, "session_id", "") or "")
            if session_key:
                return self._last_state_by_session.get(session_key)
        return self._last_state_by_umo.get(event.unified_msg_origin)

    def _state_for_provider(self, provider: Any) -> RequestAuditState | None:
        session_key = self._session_key(getattr(provider, "_context_guard_current_session_id", "") or "")
        if session_key:
            state = self._last_state_by_session.get(session_key)
            if state is not None:
                return state
        if self._last_state_by_umo:
            last_key = next(reversed(self._last_state_by_umo))
            return self._last_state_by_umo.get(last_key)
        return None

    def _request_dump_dir(self, umo: str, request_id: str) -> Path:
        digest = hashlib.sha1(umo.encode("utf-8")).hexdigest()[:10]
        return self._requests_dir / f"{digest}-{request_id}"

    def _append_dump_event(
        self,
        state: RequestAuditState,
        phase: str,
        payload: dict[str, Any],
    ) -> None:
        dump_dir = Path(state.dump_dir)
        dump_dir.mkdir(parents=True, exist_ok=True)
        event_path = dump_dir / "events.jsonl"
        entry = {
            "phase": phase,
            "at": time.time(),
            "request_id": state.request_id,
            **payload,
        }
        with event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def _summarize_request(self, req: ProviderRequest) -> RequestSummary:
        contexts = getattr(req, "contexts", []) or []
        roles = Counter(self._message_role(item) for item in contexts)
        context_lengths = [self._message_char_len(item) for item in contexts]
        extra_parts = getattr(req, "extra_user_content_parts", []) or []

        prompt_chars = self._content_char_len(getattr(req, "prompt", None))
        system_prompt_chars = self._content_char_len(getattr(req, "system_prompt", ""))
        context_chars = sum(context_lengths)
        extra_parts_chars = sum(self._content_char_len(item) for item in extra_parts)
        longest_message_chars = max(context_lengths, default=0)
        tool_count = len(self._extract_tools(getattr(req, "func_tool", None)))

        return RequestSummary(
            prompt_chars=prompt_chars,
            system_prompt_chars=system_prompt_chars,
            context_message_count=len(list(contexts)),
            context_non_system_count=sum(1 for role in roles.elements() if role != "system"),
            context_chars=context_chars,
            extra_parts_count=len(list(extra_parts)),
            extra_parts_chars=extra_parts_chars,
            tool_count=tool_count,
            roles=dict(roles),
            longest_message_chars=longest_message_chars,
            total_chars=prompt_chars + system_prompt_chars + context_chars + extra_parts_chars,
        )

    def _summarize_payload_messages(self, messages: list[Any]) -> dict[str, Any]:
        normalized = [self._ensure_message_dict(item) for item in messages]
        roles = Counter(str(item.get("role", "unknown")) for item in normalized)
        lengths = [self._message_char_len(item) for item in normalized]
        previews = [
            f"{item.get('role', 'unknown')}:{self._preview_value(item.get('content'))}"
            for item in normalized[: self._cfg_int("detail_preview_limit", 6)]
        ]
        return {
            "message_count": len(normalized),
            "non_system_count": sum(1 for item in normalized if item.get("role") != "system"),
            "roles": dict(roles),
            "total_chars": sum(lengths),
            "longest_message_chars": max(lengths, default=0),
            "previews": previews,
        }

    def _diagnose_request(self, state: RequestAuditState) -> list[str]:
        if state.final_summary is None:
            return []

        final = state.final_summary
        initial = state.initial_summary
        diagnoses: list[str] = []
        dominant_threshold = max(self._cfg_int("dominant_chars_threshold", 12000), final.total_chars // 2)

        if final.system_prompt_chars >= dominant_threshold:
            diagnoses.append(
                f"system_prompt 体积最大（{final.system_prompt_chars} chars），优先检查人格提示词或 on_llm_request 注入。"
            )
        if final.context_chars >= dominant_threshold and final.context_message_count > 0:
            diagnoses.append(
                f"context history 体积很大（{final.context_chars} chars / {final.context_message_count} messages），优先检查会话历史累积。"
            )
        if final.extra_parts_chars >= self._cfg_int("extra_parts_warn_chars", 8000):
            diagnoses.append(
                f"extra_user_content_parts 很大（{final.extra_parts_chars} chars），很可能有插件在请求阶段追加了大块动态上下文。"
            )
        if final.prompt_chars >= self._cfg_int("single_prompt_warn_chars", 12000) and final.context_message_count <= 1:
            diagnoses.append(
                f"当前用户输入本身很长（{final.prompt_chars} chars），单条 prompt 就可能触发 context overflow。"
            )
        if final.system_prompt_chars != initial.system_prompt_chars:
            diagnoses.append(
                f"system_prompt 在 hook 阶段发生变化（{initial.system_prompt_chars} -> {final.system_prompt_chars} chars）。"
            )
        if state.mutations:
            source_counts = Counter(item.source for item in state.mutations)
            preview = ", ".join(
                f"{source} x{count}"
                for source, count in source_counts.most_common(self._cfg_int("mutation_source_limit", 4))
            )
            diagnoses.append(f"检测到 request 列表在 hook 阶段被修改：{preview}")
        if not diagnoses:
            diagnoses.append("未发现明显异常注入，若仍报错，请重点看 provider_prepare / provider_error dump。")
        return diagnoses

    def _repair_after_context_overflow(
        self,
        *,
        before_context_query: list[Any],
        after_messages: list[Any],
    ) -> tuple[list[dict[str, Any]] | None, list[str]]:
        original_messages = [self._ensure_message_dict(item) for item in before_context_query]
        current_messages = [self._ensure_message_dict(item) for item in after_messages]
        actions: list[str] = []

        current_non_system = [item for item in current_messages if item.get("role") != "system"]
        if current_non_system:
            return None, actions

        system_messages = [copy.deepcopy(item) for item in original_messages if item.get("role") == "system"]
        non_system_messages = [copy.deepcopy(item) for item in original_messages if item.get("role") != "system"]

        trimmed_system = system_messages
        if self._cfg_bool("auto_trim_system_messages_after_overflow", True):
            trimmed_system, trimmed_actions = self._trim_system_messages(
                system_messages,
                max_chars=max(256, self._cfg_int("max_system_chars_after_overflow", 4000)),
            )
            actions.extend(trimmed_actions)

        if non_system_messages:
            kept = non_system_messages[-1]
            kept, trim_actions = self._trim_message(
                kept,
                max_chars=max(256, self._cfg_int("max_non_system_chars_after_overflow", 6000)),
                label="last_non_system_message",
            )
            actions.append("restored the last non-system message after provider truncation removed every user/tool message")
            actions.extend(trim_actions)
            return trimmed_system + [kept], actions

        if trimmed_system and self._cfg_bool("inject_placeholder_user_on_system_only_overflow", True):
            placeholder = {
                "role": "user",
                "content": self._cfg_str(
                    "overflow_placeholder_user_message",
                    "[ContextGuard] Continue with reduced context.",
                ),
            }
            actions.append("injected a placeholder user message because only system messages remained after overflow handling")
            return trimmed_system + [placeholder], actions

        return None, actions

    def _trim_system_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        max_chars: int,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if not messages:
            return [], []
        actions: list[str] = []
        remaining = max_chars
        trimmed_messages: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            messages_left = max(1, len(messages) - index)
            per_limit = max(128, remaining // messages_left)
            trimmed, message_actions = self._trim_message(
                copy.deepcopy(message),
                max_chars=per_limit,
                label=f"system[{index}]",
            )
            trimmed_messages.append(trimmed)
            actions.extend(message_actions)
            remaining = max(0, remaining - self._message_char_len(trimmed))
        return trimmed_messages, actions

    def _trim_message(
        self,
        message: dict[str, Any],
        *,
        max_chars: int,
        label: str,
    ) -> tuple[dict[str, Any], list[str]]:
        actions: list[str] = []
        original_chars = self._message_char_len(message)
        if original_chars <= max_chars:
            return message, actions

        content = message.get("content")
        trimmed_content, changed = self._trim_content(content, max_chars=max_chars)
        if changed:
            message["content"] = trimmed_content
            actions.append(f"trimmed {label} from {original_chars} to {self._message_char_len(message)} chars")
        return message, actions

    def _trim_content(self, content: Any, *, max_chars: int) -> tuple[Any, bool]:
        if isinstance(content, str):
            if len(content) <= max_chars:
                return content, False
            return self._truncate_text(content, max_chars=max_chars), True

        if isinstance(content, list):
            remaining = max_chars
            changed = False
            trimmed_items: list[Any] = []
            for item in content:
                if isinstance(item, dict):
                    new_item = copy.deepcopy(item)
                    for key in ("text", "think"):
                        value = new_item.get(key)
                        if not isinstance(value, str):
                            continue
                        if remaining <= 0:
                            new_item[key] = ""
                            changed = True
                            continue
                        if len(value) > remaining:
                            new_item[key] = self._truncate_text(value, max_chars=remaining)
                            changed = True
                        remaining = max(0, remaining - len(str(new_item.get(key, ""))))
                    trimmed_items.append(new_item)
                    continue
                trimmed_items.append(item)
            return trimmed_items, changed

        return content, False

    def _truncate_text(self, value: str, *, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        if max_chars <= len(OVERFLOW_MARKER) + 32:
            return value[:max_chars]
        head = max(16, (max_chars - len(OVERFLOW_MARKER)) // 2)
        tail = max(16, max_chars - len(OVERFLOW_MARKER) - head)
        return f"{value[:head]}{OVERFLOW_MARKER}{value[-tail:]}"

    def _is_context_overflow_error(self, error: Exception) -> bool:
        lowered = str(error).lower()
        return "maximum context length" in lowered or "context length" in lowered

    def _session_key(self, value: str) -> str:
        return str(value or "").strip()

    def _extract_tools(self, tool_set: Any) -> list[Any]:
        if tool_set is None:
            return []
        for attr in ("tools", "_tools", "func_tools"):
            value = getattr(tool_set, attr, None)
            if isinstance(value, list):
                return value
        for method_name in ("get_tools", "list_tools"):
            method = getattr(tool_set, method_name, None)
            if callable(method):
                try:
                    value = method()
                    if isinstance(value, list):
                        return value
                except Exception:
                    continue
        try:
            return list(tool_set)
        except Exception:
            return []

    def _ensure_message_dict(self, message: Any) -> dict[str, Any]:
        if isinstance(message, dict):
            return message
        if hasattr(message, "model_dump"):
            try:
                dumped = message.model_dump()
                if isinstance(dumped, dict):
                    return dumped
            except Exception:
                pass
        if hasattr(message, "dict"):
            try:
                dumped = message.dict()
                if isinstance(dumped, dict):
                    return dumped
            except Exception:
                pass
        return {
            "role": getattr(message, "role", "unknown"),
            "content": getattr(message, "content", str(message)),
        }

    def _message_role(self, message: Any) -> str:
        if isinstance(message, dict):
            return str(message.get("role", "unknown"))
        return str(getattr(message, "role", "unknown"))

    def _message_char_len(self, message: Any) -> int:
        item = self._ensure_message_dict(message)
        total = self._content_char_len(item.get("content"))
        total += self._content_char_len(item.get("reasoning_content"))
        tool_calls = item.get("tool_calls")
        if tool_calls:
            total += len(self._safe_json(tool_calls))
        return total

    def _content_char_len(self, value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, str):
            return len(value)
        if isinstance(value, list):
            return sum(self._content_char_len(item) for item in value)
        if isinstance(value, dict):
            if isinstance(value.get("text"), str):
                return len(value.get("text") or "")
            if isinstance(value.get("think"), str):
                return len(value.get("think") or "")
            return len(self._safe_json(value))
        if hasattr(value, "model_dump_for_context"):
            try:
                dumped = value.model_dump_for_context()
                return self._content_char_len(dumped)
            except Exception:
                return len(str(value))
        if hasattr(value, "model_dump"):
            try:
                return self._content_char_len(value.model_dump())
            except Exception:
                return len(str(value))
        return len(str(value))

    def _preview_text(self, value: Any, *, limit: int | None = None) -> str:
        if value is None:
            return PREVIEW_FALLBACK
        text = str(value)
        if not text:
            return PREVIEW_FALLBACK
        max_chars = limit or self._cfg_int("preview_chars", 160)
        if len(text) <= max_chars:
            return text
        head = max(16, max_chars // 2)
        tail = max(16, max_chars - head - 3)
        return f"{text[:head]}...{text[-tail:]}"

    def _preview_value(self, value: Any) -> str:
        if value is None:
            return PREVIEW_FALLBACK
        if isinstance(value, dict):
            if "role" in value:
                content = value.get("content")
                return f"{value.get('role')}:{self._preview_value(content)}"
            if "text" in value:
                return self._preview_text(value.get("text"))
            return self._preview_text(self._safe_json(value))
        if isinstance(value, list):
            if not value:
                return "[]"
            return "[" + ", ".join(self._preview_value(item) for item in value[:3]) + ("..." if len(value) > 3 else "") + "]"
        return self._preview_text(value)

    def _infer_mutation_source(self) -> str:
        for frame_info in inspect.stack()[2:12]:
            filename = Path(frame_info.filename).resolve().as_posix().lower()
            if filename == self._plugin_file:
                continue
            if filename.endswith("inspect.py"):
                continue
            relative = Path(frame_info.filename).name
            return f"{relative}:{frame_info.lineno}:{frame_info.function}"
        return "unknown"

    def _format_status(self, state: RequestAuditState) -> str:
        final = state.final_summary or state.initial_summary
        lines = [
            f"ContextGuard request_id: {state.request_id}",
            f"session: {state.session_id or PREVIEW_FALLBACK}",
            f"dump: {state.dump_dir}",
            f"total_chars: {final.total_chars}",
            (
                "sizes: "
                f"prompt={final.prompt_chars}, "
                f"system={final.system_prompt_chars}, "
                f"context={final.context_chars} ({final.context_message_count} msgs), "
                f"extra={final.extra_parts_chars}, "
                f"tools={final.tool_count}"
            ),
        ]
        if state.diagnoses:
            lines.append("diagnoses: " + " | ".join(state.diagnoses))
        if state.fixes:
            last_fix = state.fixes[-1]
            lines.append(
                "last_fix: "
                f"{last_fix.reason} | "
                f"{' | '.join(last_fix.actions) if last_fix.actions else PREVIEW_FALLBACK}"
            )
        return "\n".join(lines)

    def _safe_json(self, value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)

    def _cfg(self, key: str, default: Any) -> Any:
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        return default

    def _cfg_int(self, key: str, default: int) -> int:
        value = self._cfg(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, key: str, default: str) -> str:
        value = self._cfg(key, default)
        if value is None:
            return default
        return str(value)
