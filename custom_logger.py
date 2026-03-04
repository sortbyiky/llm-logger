"""
LiteLLM Custom Callback Logger v3
Sends request/response data from each pipeline stage to the llm-logger service.
Adds raw_request event to capture Cursor's original request content.
"""
import asyncio
import datetime
import json
import time
import traceback
import uuid
from typing import Optional

import httpx

LOGGER_URL = "http://llm-logger:8080/log"
_client: Optional[httpx.AsyncClient] = None

def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=3.0)
    return _client

def _safe_json(obj):
    try:
        json.dumps(obj)
        return obj
    except Exception:
        return str(obj)

def _calc_ms(start_time, end_time) -> Optional[float]:
    try:
        if start_time is None or end_time is None:
            return None
        if isinstance(start_time, datetime.datetime):
            return round((end_time - start_time).total_seconds() * 1000, 2)
        return round((float(end_time) - float(start_time)) * 1000, 2)
    except Exception:
        return None

def _extract_usage(response_obj):
    input_tokens = output_tokens = 0
    try:
        usage = getattr(response_obj, "usage", None)
        if usage:
            input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(usage, "completion_tokens", 0) or 0
    except Exception:
        pass
    return input_tokens, output_tokens

def _to_dict(obj):
    try:
        if obj is None:
            return None
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "__dict__"):
            return obj.__dict__
        return str(obj)
    except Exception:
        return str(obj)

async def _send(payload: dict):
    try:
        await _get_client().post(LOGGER_URL, json=payload)
    except Exception as e:
        print(f"[custom_logger] Push failed: {e}")

def _fire(payload: dict):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_send(payload))
        else:
            loop.run_until_complete(_send(payload))
    except Exception:
        pass


try:
    from litellm.integrations.custom_logger import CustomLogger as _Base
    _HAS_BASE = True
except ImportError:
    _Base = object
    _HAS_BASE = False


class CustomLogger(_Base):
    def __init__(self, *args, **kwargs):
        if _HAS_BASE:
            try:
                super().__init__(*args, **kwargs)
            except Exception:
                pass

    # 0. Raw request from Cursor - captured before LiteLLM processes it
    def log_pre_api_call(self, model, messages, kwargs):
        self._emit_raw_request(model, messages, kwargs)
        self._emit_pre(model, messages, kwargs)

    async def async_log_pre_api_call(self, model, messages, kwargs):
        await _send(self._build_raw_request_payload(model, messages, kwargs))
        await _send(self._build_pre_payload(model, messages, kwargs))

    def _build_raw_request_payload(self, model, messages, kwargs):
        request_id = kwargs.get("litellm_call_id", str(uuid.uuid4()))
        # Extract message summary (first 100 chars of last user message)
        msg_summary = ""
        try:
            for msg in reversed(messages or []):
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                content = part.get("text", "")
                                break
                    msg_summary = str(content)[:100]
                    break
        except Exception:
            pass
        return {
            "request_id": request_id,
            "event_type": "raw_request",
            "model": model,
            "request_body": _safe_json({
                "model": model,
                "messages": messages,
                "stream": kwargs.get("stream", False),
                "max_tokens": kwargs.get("max_tokens"),
                "temperature": kwargs.get("temperature"),
            }),
            "chunk_data": msg_summary,  # message preview for list display
            "metadata": {
                "api_base": str(kwargs.get("api_base", "")),
                "stream": kwargs.get("stream", False),
                "litellm_call_id": request_id,
                "num_messages": len(messages) if messages else 0,
            },
        }

    def _emit_raw_request(self, model, messages, kwargs):
        _fire(self._build_raw_request_payload(model, messages, kwargs))

    # 1. Request enters LiteLLM
    def _emit_pre(self, model, messages, kwargs):
        _fire(self._build_pre_payload(model, messages, kwargs))

    def _build_pre_payload(self, model, messages, kwargs):
        request_id = kwargs.get("litellm_call_id", str(uuid.uuid4()))
        return {
            "request_id": request_id,
            "event_type": "pre_call",
            "model": model,
            "request_body": _safe_json({
                "model": model,
                "messages": messages,
                "stream": kwargs.get("stream", False),
                "max_tokens": kwargs.get("max_tokens"),
                "temperature": kwargs.get("temperature"),
            }),
            "metadata": {
                "api_base": str(kwargs.get("api_base", "")),
                "stream": kwargs.get("stream", False),
                "litellm_call_id": request_id,
            },
        }

    # 2. Raw upstream response received
    def log_post_api_call(self, kwargs, response_obj, start_time, end_time):
        self._emit_post(kwargs, response_obj, start_time, end_time)

    async def async_log_post_api_call(self, kwargs, response_obj, start_time, end_time):
        self._emit_post(kwargs, response_obj, start_time, end_time)

    def _emit_post(self, kwargs, response_obj, start_time, end_time):
        is_stream = kwargs.get("stream", False)
        if is_stream:
            return
        request_id = kwargs.get("litellm_call_id", "")
        _fire({
            "request_id": request_id,
            "event_type": "post_call",
            "model": kwargs.get("model", ""),
            "duration_ms": _calc_ms(start_time, end_time),
            "response_body": _safe_json(_to_dict(response_obj)),
            "metadata": {
                "api_base": str(kwargs.get("api_base", "")),
                "stream": kwargs.get("stream", False),
            },
        })

    # 3. Success returned to Cursor
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._emit_success(kwargs, response_obj, start_time, end_time)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._emit_success(kwargs, response_obj, start_time, end_time)

    def _emit_success(self, kwargs, response_obj, start_time, end_time):
        request_id = kwargs.get("litellm_call_id", "")
        input_tokens, output_tokens = _extract_usage(response_obj)

        reply_text = None
        try:
            choices = getattr(response_obj, "choices", None)
            if choices and len(choices) > 0:
                msg = getattr(choices[0], "message", None)
                if msg:
                    reply_text = getattr(msg, "content", None)
                    if reply_text and len(reply_text) > 200:
                        reply_text = reply_text[:200] + "…"
        except Exception:
            pass

        _fire({
            "request_id": request_id,
            "event_type": "success",
            "model": kwargs.get("model", ""),
            "duration_ms": _calc_ms(start_time, end_time),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "response_body": _safe_json(_to_dict(response_obj)),
            "chunk_data": reply_text,
            "metadata": {
                "stream": kwargs.get("stream", False),
                "api_base": str(kwargs.get("api_base", "")),
                "response_cost": kwargs.get("response_cost"),
            },
        })

    # 4. Failure
    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._emit_failure(kwargs, response_obj, start_time, end_time)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._emit_failure(kwargs, response_obj, start_time, end_time)

    def _emit_failure(self, kwargs, response_obj, start_time, end_time):
        request_id = kwargs.get("litellm_call_id", "")
        try:
            error_msg = traceback.format_exc() if isinstance(response_obj, Exception) else str(response_obj)
        except Exception:
            error_msg = "unknown error"
        _fire({
            "request_id": request_id,
            "event_type": "failure",
            "model": kwargs.get("model", ""),
            "duration_ms": _calc_ms(start_time, end_time),
            "error": error_msg,
            "metadata": {
                "api_base": str(kwargs.get("api_base", "")),
                "stream": kwargs.get("stream", False),
            },
        })

    # 5. Streaming SSE chunk
    async def async_log_stream_event(self, kwargs, response_obj, start_time, end_time):
        request_id = kwargs.get("litellm_call_id", "")
        chunk_text = ""
        try:
            choices = getattr(response_obj, "choices", None)
            if choices:
                delta = getattr(choices[0], "delta", None)
                if delta:
                    chunk_text = (
                        getattr(delta, "content", "") or
                        getattr(delta, "thinking", "") or
                        ""
                    )
        except Exception:
            pass
        if not chunk_text:
            return
        await _send({
            "request_id": request_id,
            "event_type": "chunk",
            "model": kwargs.get("model", ""),
            "chunk_data": chunk_text,
        })


# LiteLLM registers callbacks via a module-level instance
customHandler = CustomLogger()

def _register():
    try:
        import litellm
        if customHandler not in litellm.callbacks:
            litellm.callbacks.append(customHandler)
        if customHandler not in litellm.input_callback:
            litellm.input_callback.append(customHandler)
        if customHandler not in litellm.success_callback:
            litellm.success_callback.append(customHandler)
        if customHandler not in litellm.failure_callback:
            litellm.failure_callback.append(customHandler)
        print("[custom_logger] Registered to all events (callbacks/input/success/failure)")
    except Exception as e:
        print(f"[custom_logger] Registration failed: {e}")

_register()
