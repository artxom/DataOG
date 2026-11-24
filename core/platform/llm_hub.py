"""Data.O.G 全局 AI 大模型统一接入中枢 (LLMHub).

参考 Cherry Studio / LobeChat 成熟的多服务商管理架构，采用零额外第三方依赖的 Python 标准库原生支持 OpenAI 兼容 REST 协议：
1. 内置三大主流预设（DeepSeek 官方端点、Google Gemini 官方 OpenAI 兼容端点、本地/内网 OpenAI 兼容网关）；
2. API Key 仅持久化于本地 SQLite `data/dataog_state.db`，所有对外接口严格脱敏回显，留空或回传掩码时自动保留原密钥；
3. 支持连通性毫秒级测试、远端模型列表拉取（自动兼容剥离 Gemini `models/` 前缀）及结构化 JSON 生成。
"""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from core.platform.diagnostics_hub import trace_span
from core.platform.models import LLMProviderProfile
from core.platform.storage import PlatformStateStore

_COMMON_SYSTEM_CA_FILES: Tuple[str, ...] = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/opt/homebrew/etc/openssl@3/cert.pem",
    "/usr/local/etc/openssl@3/cert.pem",
)


def _try_build_truststore_context() -> Optional[ssl.SSLContext]:
    try:
        import truststore  # type: ignore[import-not-found]

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return None


def build_llm_ssl_context() -> ssl.SSLContext:
    """构建跨平台 HTTPS TLS 校验上下文，解决 macOS 官方 Python 空白默认 CA 仓导致合法证书失败问题."""
    with trace_span("llm", "ssl_context"):
        if os.environ.get("SSL_CERT_FILE", "").strip() or os.environ.get("SSL_CERT_DIR", "").strip():
            return ssl.create_default_context()

        # macOS 优先接入系统钥匙串（同时兼容苹果系统根证书与企业/代理自签根证书）
        if sys.platform == "darwin":
            ts_ctx = _try_build_truststore_context()
            if ts_ctx is not None:
                return ts_ctx

        ctx = ssl.create_default_context()
        try:
            ca_count = int((ctx.cert_store_stats() or {}).get("x509_ca", 0))
        except Exception:
            ca_count = 0
        if ca_count > 0:
            return ctx

        # 若默认 OpenSSL CA 仓为空，依次回退系统信任库、常见系统 CA Bundle、certifi 及 macOS security 导出
        ts_ctx = _try_build_truststore_context()
        if ts_ctx is not None:
            return ts_ctx

        for ca_path in _COMMON_SYSTEM_CA_FILES:
            if os.path.isfile(ca_path):
                try:
                    ctx.load_verify_locations(cafile=ca_path)
                    if int((ctx.cert_store_stats() or {}).get("x509_ca", 0)) > 0:
                        return ctx
                except Exception:
                    continue

        try:
            import certifi  # type: ignore[import-not-found]

            certifi_ca = certifi.where()
            if certifi_ca and os.path.isfile(certifi_ca):
                ctx.load_verify_locations(cafile=certifi_ca)
                return ctx
        except Exception:
            pass

        if sys.platform == "darwin":
            try:
                proc = subprocess.run(
                    [
                        "/usr/bin/security",
                        "find-certificate",
                        "-a",
                        "-p",
                        "/System/Library/Keychains/SystemRootCertificates.keychain",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if proc.returncode == 0 and "BEGIN CERTIFICATE" in (proc.stdout or ""):
                    ctx.load_verify_locations(cadata=proc.stdout)
                    return ctx
            except Exception:
                pass

        return ctx

BUILTIN_PROVIDER_PRESETS: Tuple[Dict[str, Any], ...] = (
    {
        "provider_id": "deepseek",
        "name": "DeepSeek",
        "provider_type": "DEEPSEEK",
        "enabled": True,
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "is_default": True,
    },
    {
        "provider_id": "gemini",
        "name": "Google Gemini",
        "provider_type": "GEMINI",
        "enabled": True,
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-2.5-flash",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro"],
        "is_default": False,
    },
    {
        "provider_id": "custom",
        "name": "OpenAI 兼容网关",
        "provider_type": "OPENAI_COMPATIBLE",
        "enabled": False,
        "base_url": "http://127.0.0.1:11434/v1",
        "default_model": "qwen2.5-coder:32b",
        "models": ["qwen2.5-coder:32b"],
        "is_default": False,
    },
)

BUILTIN_PROVIDER_IDS = {p["provider_id"] for p in BUILTIN_PROVIDER_PRESETS}


def validate_llm_base_url(raw_url: str) -> str:
    """校验并标准化 OpenAI 兼容 API Base URL（仅允许 http/https，禁止内嵌凭据）."""
    url = (raw_url or "").strip()
    if not url:
        raise ValueError("API 地址 (Base URL) 不能为空")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("API 地址仅允许使用 http:// 或 https:// 协议")
    if not parsed.hostname:
        raise ValueError("API 地址格式无效，缺少主机域名或 IP")
    if parsed.username or parsed.password:
        raise ValueError("禁止在 API 地址 URL 中直接内嵌账号密码凭据")
    # 保留末尾斜杠对 Gemini `/v1beta/openai/` 兼容友好，去除多余空白
    return url


def is_masked_secret_input(val: Optional[str]) -> bool:
    """判断前端提交的密钥字符串是否为未修改的脱敏掩码."""
    if val is None:
        return True
    s = val.strip()
    if not s:
        return True
    return "••••" in s or s == "******" or s == "********"


_CONTEXT_LIMIT_PATTERNS: Tuple[str, ...] = (
    "context_length_exceeded",
    "maximum context length",
    "context length",
    "too many tokens",
    "token limit",
    "prompt is too long",
    "exceeds the maximum",
)

_UNSUPPORTED_THINKING_PATTERNS: Tuple[str, ...] = (
    "unknown",
    "extra_forbidden",
    "unrecognized",
    "invalid",
    "unsupported",
    "not allowed",
    "reasoning_effort",
    "enable_thinking",
    "thinking",
)

_SMALL_LOCAL_TAG_RE = re.compile(r"(?:^|[:\-_])(?:0\.5b|1\.5b|3b|4b|7b|8b|9b|14b|32b)(?:$|[:\-_])")


class ContextLengthExceededError(RuntimeError):
    """上游大模型返回输入上下文超出最大窗口限制时抛出的结构化异常."""


class _LLMHttpError(RuntimeError):
    """内部 HTTP 状态异常（用于判断参数不支持自动降级）."""

    def __init__(self, status_code: int, detail: str, message: str) -> None:
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.detail = detail


def _is_context_limit_error(detail: str) -> bool:
    low = (detail or "").lower()
    return any(pat in low for pat in _CONTEXT_LIMIT_PATTERNS)


def _is_unsupported_thinking_param_error(status_code: int, detail: str) -> bool:
    if status_code not in (400, 422):
        return False
    if _is_context_limit_error(detail):
        return False
    low = (detail or "").lower()
    return any(pat in low for pat in _UNSUPPORTED_THINKING_PATTERNS)


class ThinkingStreamParser:
    """流式增量分离模型思考过程与最终回答内容的状态机.

    同时兼容：
    1. OpenAI/DeepSeek/vLLM 结构化增量字段 (`reasoning_content` / `reasoning` / `thinking`)；
    2. 正文内嵌 `<think>...</think>` / `<thought>...</thought>` 标签（严格处理跨分片截断）。
    普通 SQL `<` 比较运算符绝不缓冲丢失。
    """

    _OPEN_TAGS: Tuple[Tuple[str, str], ...] = (
        ("<think>", "</think>"),
        ("<thought>", "</thought>"),
    )

    def __init__(self) -> None:
        self._in_think: bool = False
        self._active_close_tag: str = ""
        self._buf: str = ""
        self._trim_leading_ws_after_think: bool = False

    def _emit_content(self, text: str, content_parts: List[str]) -> None:
        if not text:
            return
        if self._trim_leading_ws_after_think:
            stripped = text.lstrip("\r\n")
            if not stripped:
                return
            self._trim_leading_ws_after_think = False
            content_parts.append(stripped)
            return
        content_parts.append(text)

    def feed(self, reasoning_delta: str = "", content_delta: str = "") -> Tuple[str, str]:
        """喂入单步增量文本，返回 `(reasoning_out, content_out)`."""
        reasoning_parts: List[str] = []
        content_parts: List[str] = []

        if reasoning_delta:
            reasoning_parts.append(reasoning_delta)

        if content_delta:
            data = self._buf + content_delta
            self._buf = ""
            pos = 0
            n = len(data)
            while pos < n:
                idx = data.find("<", pos)
                if idx == -1:
                    chunk = data[pos:]
                    if self._in_think:
                        reasoning_parts.append(chunk)
                    else:
                        self._emit_content(chunk, content_parts)
                    break

                if idx > pos:
                    prefix = data[pos:idx]
                    if self._in_think:
                        reasoning_parts.append(prefix)
                    else:
                        self._emit_content(prefix, content_parts)

                tail = data[idx:]
                tail_low = tail.lower()

                if not self._in_think:
                    matched_open = False
                    partial_open = False
                    for open_tag, close_tag in self._OPEN_TAGS:
                        if tail_low.startswith(open_tag):
                            self._in_think = True
                            self._active_close_tag = close_tag
                            pos = idx + len(open_tag)
                            matched_open = True
                            break
                        if len(tail_low) < len(open_tag) and open_tag.startswith(tail_low):
                            partial_open = True
                    if matched_open:
                        continue
                    if partial_open:
                        self._buf = tail
                        break
                    self._emit_content("<", content_parts)
                    pos = idx + 1
                else:
                    close_tag = self._active_close_tag or "</think>"
                    if tail_low.startswith(close_tag):
                        self._in_think = False
                        self._active_close_tag = ""
                        self._trim_leading_ws_after_think = True
                        pos = idx + len(close_tag)
                        continue
                    if len(tail_low) < len(close_tag) and close_tag.startswith(tail_low):
                        self._buf = tail
                        break
                    reasoning_parts.append("<")
                    pos = idx + 1

        return "".join(reasoning_parts), "".join(content_parts)

    def flush(self) -> Tuple[str, str]:
        """刷新尾部残余缓冲."""
        if not self._buf:
            return "", ""
        rem = self._buf
        self._buf = ""
        if self._in_think:
            return rem, ""
        parts: List[str] = []
        self._emit_content(rem, parts)
        return "", "".join(parts)


def strip_think_tags(raw_text: str) -> Tuple[str, str]:
    """从完整文本中剥离 `<think>` / `<thought>` 块，返回 `(reasoning, clean_content)`."""
    parser = ThinkingStreamParser()
    r1, c1 = parser.feed("", raw_text or "")
    r2, c2 = parser.flush()
    return (r1 + r2).strip(), (c1 + c2).strip()


def infer_model_capabilities(
    model: str,
    provider_type: str = "",
    base_url: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """根据模型标识与网关配置推断现代化上下文窗口、附件预算及 Thinking 能力规格."""
    m = (model or "").strip().lower()
    m_base = m.rsplit("/", 1)[-1]
    ptype = (provider_type or "").strip().upper()
    burl = (base_url or "").strip().lower()
    is_local = any(h in burl for h in ("127.0.0.1", "localhost"))
    has_small_tag = bool(_SMALL_LOCAL_TAG_RE.search(m_base))
    has_colon_small = any(t in m_base for t in (":7b", ":8b", ":14b", ":32b"))

    context_window = 64_000
    attachment_budget = 40_000
    max_output_tokens = 8192
    supports_thinking = False
    thinking_mode = "none"  # native_always | toggleable | none
    omit_temperature = False
    use_max_completion_tokens = False
    thinking_param_style = "none"  # deepseek | openai_effort | qwen_enable | none

    # 1. Google Gemini 系列
    if "gemini" in m_base or ptype == "GEMINI":
        if any(v in m_base for v in ("gemini-2.5", "gemini-3", "gemini-2.0", "gemini-1.5")):
            context_window = 1_000_000
            attachment_budget = 240_000
            max_output_tokens = 16384
        elif "gemini" in m_base:
            context_window = 1_000_000
            attachment_budget = 240_000
            max_output_tokens = 16384
        if any(v in m_base for v in ("gemini-2.5", "gemini-3", "thinking")):
            supports_thinking = True
            thinking_mode = "toggleable"
            thinking_param_style = "openai_effort"

    # 2. DeepSeek 系列
    elif "deepseek" in m_base or "deepseek" in m:
        context_window = 128_000
        attachment_budget = 80_000
        if "reasoner" in m_base or "deepseek-r1" in m or m_base.startswith("r1"):
            max_output_tokens = 16384
            supports_thinking = True
            thinking_mode = "native_always"
            omit_temperature = True
            thinking_param_style = "none"
        else:
            # deepseek-chat / deepseek-v3 / deepseek-coder
            max_output_tokens = 8192
            supports_thinking = True
            thinking_mode = "toggleable"
            thinking_param_style = "deepseek"

    # 3. OpenAI o1 / o3 / o4 / GPT 系列
    elif re.match(r"^(?:o1|o3|o4)(?:$|-|\.|_)", m_base):
        context_window = 200_000
        attachment_budget = 128_000
        max_output_tokens = 16384
        supports_thinking = True
        thinking_mode = "native_always"
        omit_temperature = True
        use_max_completion_tokens = True
        thinking_param_style = "openai_effort"
    elif "gpt-4.1" in m_base:
        context_window = 1_000_000
        attachment_budget = 240_000
        max_output_tokens = 16384
    elif "gpt-5" in m_base:
        context_window = 400_000
        attachment_budget = 180_000
        max_output_tokens = 16384
        supports_thinking = True
        thinking_mode = "toggleable"
        use_max_completion_tokens = True
        thinking_param_style = "openai_effort"
    elif "gpt-4o" in m_base:
        context_window = 128_000
        attachment_budget = 80_000
        max_output_tokens = 16384

    # 4. Anthropic Claude 系列
    elif "claude" in m_base:
        context_window = 200_000
        attachment_budget = 130_000
        if re.search(r"claude-(?:3[.-]7|4|sonnet-4|opus-4|haiku-4)", m_base):
            max_output_tokens = 16384
            supports_thinking = True
            thinking_mode = "toggleable"
            thinking_param_style = "openai_effort"

    # 5. Qwen / QwQ / QvQ 系列
    elif m_base.startswith(("qwq", "qvq")):
        context_window = 128_000
        attachment_budget = 80_000
        max_output_tokens = 16384
        supports_thinking = True
        thinking_mode = "native_always"
        thinking_param_style = "none"
    elif m_base.startswith("qwen-long"):
        context_window = 1_000_000
        attachment_budget = 240_000
        max_output_tokens = 16384
    elif m_base.startswith("qwen3"):
        if (is_local and has_small_tag) or has_colon_small:
            context_window = 32_768
            attachment_budget = 20_000
        else:
            context_window = 128_000
            attachment_budget = 80_000
        max_output_tokens = 16384
        supports_thinking = True
        thinking_mode = "toggleable"
        thinking_param_style = "qwen_enable"
    elif "qwen" in m_base:
        if (is_local and has_small_tag) or has_colon_small:
            context_window = 32_768
            attachment_budget = 20_000
        else:
            context_window = 128_000
            attachment_budget = 80_000

    # 6. Kimi / Moonshot / GLM / Doubao / MiniMax / Hunyuan
    elif any(k in m_base for k in ("kimi", "moonshot", "glm-4", "glm-5", "glm-z1", "doubao", "minimax", "hunyuan")):
        context_window = 128_000
        attachment_budget = 80_000
        if any(k in m_base for k in ("thinking", "z1", "t1", "r1", "reasoner")):
            max_output_tokens = 16384
            supports_thinking = True
            thinking_mode = "native_always"

    # 7. 其他本地小参数或未知名模型兜底
    else:
        if (is_local and has_small_tag) or has_colon_small:
            context_window = 32_768
            attachment_budget = 20_000
        else:
            context_window = 64_000
            attachment_budget = 40_000
        if any(k in m_base for k in ("reasoner", "-r1", "_r1", ":r1", "thinking")):
            max_output_tokens = 16384
            supports_thinking = True
            thinking_mode = "native_always"

    # 服务商显式配置的自定义上下文窗口覆盖（>= 4096）
    if isinstance(extra, dict) and extra.get("context_window"):
        try:
            custom_ctx = int(extra["context_window"])
            if custom_ctx >= 4096:
                context_window = custom_ctx
                attachment_budget = max(4000, min(240_000, int(custom_ctx * 0.62)))
        except (TypeError, ValueError):
            pass

    return {
        "context_window": int(context_window),
        "attachment_budget": int(attachment_budget),
        "max_output_tokens": int(max_output_tokens),
        "supports_thinking": bool(supports_thinking),
        "thinking_mode": thinking_mode,
        "omit_temperature": bool(omit_temperature),
        "use_max_completion_tokens": bool(use_max_completion_tokens),
        "thinking_param_style": thinking_param_style,
    }


def extract_json_payload(raw_text: str) -> Any:
    """从大模型返回文本中稳健提取 JSON 对象或数组（自动剥离 `<think>` 块并兼容 ```json 代码块包裹）."""
    raw = (raw_text or "").strip()
    if "<think" in raw.lower() or "<thought" in raw.lower():
        _, stripped = strip_think_tags(raw)
        text = stripped
    else:
        text = raw
    if not text:
        raise ValueError("AI 模型返回内容为空")

    # 1. 直接解析
    try:
        return json.loads(text)
    except Exception:
        pass

    # 2. 提取 Markdown 代码围栏
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    if fence_match:
        inner = fence_match.group(1).strip()
        try:
            return json.loads(inner)
        except Exception:
            pass

    # 3. 定位最外层 {...} 或 [...]
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            snippet = text[start : end + 1]
            try:
                return json.loads(snippet)
            except Exception:
                continue

    raise ValueError("未能从模型输出中解析出合法的 JSON 结构")


def _normalize_model_id(raw_id: str, provider_type: str = "", base_url: str = "") -> str:
    mid = (raw_id or "").strip()
    if not mid:
        return ""
    if (
        provider_type.upper() == "GEMINI"
        or "generativelanguage.googleapis.com" in (base_url or "").lower()
    ) and mid.startswith("models/"):
        return mid[len("models/") :]
    return mid


class LLMHub:
    """全局 AI 大模型服务商管理与统一协议调用中心."""

    def __init__(
        self,
        store: PlatformStateStore,
        client_factory: Optional[Callable[[LLMProviderProfile, float], Any]] = None,
    ) -> None:
        self.store = store
        self._client_factory = client_factory
        self._ensure_builtin_presets()

    def _ensure_builtin_presets(self) -> None:
        existing_ids = {p.provider_id for p in self.store.list_llm_providers()}
        has_default = any(p.is_default for p in self.store.list_llm_providers())
        for spec in BUILTIN_PROVIDER_PRESETS:
            if spec["provider_id"] in existing_ids:
                continue
            prof = LLMProviderProfile(
                provider_id=spec["provider_id"],
                name=spec["name"],
                provider_type=spec["provider_type"],
                enabled=bool(spec["enabled"]),
                base_url=spec["base_url"],
                api_key="",
                default_model=spec["default_model"],
                models=list(spec["models"]),
                is_default=bool(spec["is_default"] and not has_default),
            )
            self.store.upsert_llm_provider(prof)
            if prof.is_default:
                has_default = True

    def list_providers(self) -> List[Dict[str, Any]]:
        return [p.to_public_dict() for p in self.store.list_llm_providers()]

    def list_available_models(self) -> List[Dict[str, Any]]:
        """汇总全部已启用服务商的候选模型列表（附带上下文窗口与 Thinking 能力元数据）."""
        providers = self.store.list_llm_providers()
        items: List[Dict[str, Any]] = []
        for p in providers:
            if not p.enabled and not p.is_default:
                continue
            is_local = any(h in (p.base_url or "") for h in ("127.0.0.1", "localhost"))
            is_ready = bool(p.enabled and (p.api_key or is_local))
            model_ids = list(p.models or [])
            if p.default_model and p.default_model not in model_ids:
                model_ids.insert(0, p.default_model)
            for mid in model_ids:
                caps = infer_model_capabilities(mid, p.provider_type, p.base_url, p.extra)
                items.append(
                    {
                        "value": f"{p.provider_id}::{mid}",
                        "provider_id": p.provider_id,
                        "provider_name": p.name,
                        "model": mid,
                        "has_api_key": bool(p.api_key),
                        "ready": is_ready,
                        "is_default": bool(p.is_default and mid == p.default_model),
                        "context_window": caps["context_window"],
                        "attachment_budget": caps["attachment_budget"],
                        "supports_thinking": caps["supports_thinking"],
                        "thinking_mode": caps["thinking_mode"],
                    }
                )
        return items

    def build_profile_from_payload(
        self,
        payload: Dict[str, Any],
        provider_id: Optional[str] = None,
    ) -> LLMProviderProfile:
        pid = (provider_id or payload.get("provider_id") or "").strip().lower()
        if not pid:
            pid = f"prov_{uuid.uuid4().hex[:8]}"
        if not re.match(r"^[a-z0-9_-]{2,48}$", pid):
            raise ValueError("服务商标识仅允许包含小写字母、数字、下划线或连字符")

        existing = self.store.get_llm_provider(pid)

        name = str(payload.get("name") if "name" in payload else (existing.name if existing else pid)).strip()
        if not name:
            name = pid

        ptype = str(
            payload.get("provider_type")
            if "provider_type" in payload and payload.get("provider_type")
            else (existing.provider_type if existing else "OPENAI_COMPATIBLE")
        ).strip().upper()

        raw_url = (
            str(payload.get("base_url") or "").strip()
            if "base_url" in payload and payload.get("base_url") is not None
            else (existing.base_url if existing else "")
        )
        base_url = validate_llm_base_url(raw_url)

        clear_key = bool(payload.get("clear_api_key", False))
        incoming_key = payload.get("api_key")
        if clear_key:
            api_key = ""
        elif incoming_key is not None and not is_masked_secret_input(str(incoming_key)):
            api_key = str(incoming_key).strip()
        else:
            api_key = existing.api_key if existing else ""

        raw_models = payload.get("models")
        if isinstance(raw_models, list):
            models = []
            for m in raw_models:
                nm = _normalize_model_id(str(m), ptype, base_url)
                if nm and nm not in models:
                    models.append(nm)
        else:
            models = list(existing.models) if existing else []

        default_model = _normalize_model_id(
            str(
                payload.get("default_model")
                if "default_model" in payload and payload.get("default_model") is not None
                else (existing.default_model if existing else (models[0] if models else ""))
            ),
            ptype,
            base_url,
        )
        if default_model and default_model not in models:
            models.insert(0, default_model)
        if not default_model and models:
            default_model = models[0]

        enabled = (
            bool(payload["enabled"])
            if "enabled" in payload and payload["enabled"] is not None
            else (existing.enabled if existing else True)
        )
        is_default = (
            bool(payload["is_default"])
            if "is_default" in payload and payload["is_default"] is not None
            else (existing.is_default if existing else False)
        )

        extra = dict(existing.extra if existing else {})
        if "context_window" in payload:
            raw_cw = payload.get("context_window")
            if raw_cw is None or raw_cw == "" or (isinstance(raw_cw, (int, float)) and int(raw_cw) == 0):
                extra.pop("context_window", None)
            else:
                try:
                    cw_val = int(raw_cw)
                except (TypeError, ValueError) as exc:
                    raise ValueError("自定义上下文窗口必须为整数（或留空/填 0 恢复自动识别）") from exc
                if cw_val < 4096:
                    raise ValueError("自定义上下文窗口需 >= 4096 tokens（填 0 或留空则按模型自动识别）")
                extra["context_window"] = cw_val

        return LLMProviderProfile(
            provider_id=pid,
            name=name,
            provider_type=ptype,
            enabled=enabled,
            base_url=base_url,
            api_key=api_key,
            default_model=default_model,
            models=models,
            is_default=is_default,
            extra=extra,
        )

    def save_provider(
        self, payload: Dict[str, Any], provider_id: Optional[str] = None
    ) -> LLMProviderProfile:
        prof = self.build_profile_from_payload(payload, provider_id=provider_id)
        saved = self.store.upsert_llm_provider(prof)
        self.store.add_log(
            "INFO",
            "AI模型配置",
            f"已保存 AI 服务商 [{saved.name}] ({saved.base_url} · 默认模型 {saved.default_model or '-'})",
        )
        return saved

    def delete_provider(self, provider_id: str) -> bool:
        pid = (provider_id or "").strip().lower()
        if pid in BUILTIN_PROVIDER_IDS:
            raise ValueError("内置核心服务商预设不支持删除，可将其关闭或重置密钥")
        return self.store.delete_llm_provider(pid)

    def resolve_provider_and_model(
        self,
        provider_id: str = "",
        model: str = "",
        require_ready: bool = False,
    ) -> Optional[Tuple[LLMProviderProfile, str]]:
        """解析调用目标服务商与模型名称（支持 'deepseek::deepseek-chat' 复合写法）."""
        pid = (provider_id or "").strip().lower()
        mdl = (model or "").strip()
        if "::" in mdl and not pid:
            pid_part, mdl_part = mdl.split("::", 1)
            pid = pid_part.strip().lower()
            mdl = mdl_part.strip()

        providers = self.store.list_llm_providers()
        chosen: Optional[LLMProviderProfile] = None

        if pid:
            chosen = next((p for p in providers if p.provider_id == pid), None)
        if chosen is None and mdl:
            # 按模型名匹配拥有密钥且已启用的服务商，再回退任意拥有该模型的服务商
            chosen = next(
                (p for p in providers if p.enabled and p.api_key and mdl in (p.models or [])),
                None,
            ) or next((p for p in providers if mdl in (p.models or [])), None)
        if chosen is None:
            # 优先选已配置密钥且启用的默认服务商
            chosen = (
                next((p for p in providers if p.is_default and p.enabled and p.api_key), None)
                or next((p for p in providers if p.enabled and p.api_key), None)
                or next(
                    (
                        p
                        for p in providers
                        if p.enabled and p.provider_type == "OPENAI_COMPATIBLE" and "127.0.0.1" in p.base_url
                    ),
                    None,
                )
                or next((p for p in providers if p.is_default), None)
                or (providers[0] if providers else None)
            )

        if chosen is None:
            if require_ready:
                raise ValueError("尚未配置任何 AI 模型服务商")
            return None

        is_local_no_auth = any(
            h in (chosen.base_url or "") for h in ("127.0.0.1", "localhost")
        )
        if require_ready and not chosen.api_key and not is_local_no_auth:
            raise ValueError(
                f"服务商 [{chosen.name}] 尚未填写 API Key，请先在「系统配置 → AI 模型」中保存密钥"
            )
        if not chosen.api_key and not is_local_no_auth:
            return None

        eff_model = _normalize_model_id(
            mdl or chosen.default_model or (chosen.models[0] if chosen.models else ""),
            chosen.provider_type,
            chosen.base_url,
        )
        if not eff_model:
            if require_ready:
                raise ValueError(f"服务商 [{chosen.name}] 未指定可用模型名称")
            return None
        return chosen, eff_model

    def _get_sdk_client(self, profile: LLMProviderProfile, timeout_sec: float = 30.0) -> Any:
        if self._client_factory is not None:
            return self._client_factory(profile, timeout_sec)
        return None

    @staticmethod
    def _build_openai_opener_and_request(
        method: str,
        endpoint: str,
        api_key: str,
        body: Optional[Dict[str, Any]],
        *,
        accept: str = "application/json",
    ) -> Tuple[Any, urllib.request.Request, str]:
        safe_url = validate_llm_base_url(endpoint)
        headers: Dict[str, str] = {
            "Accept": accept,
            "User-Agent": "Data.O.G/3.3",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload_bytes: Optional[bytes] = None
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

        class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):  # type: ignore[override]
                validate_llm_base_url(newurl)
                return super().redirect_request(req, fp, code, msg, hdrs, newurl)

        handlers: List[Any] = [_SafeRedirectHandler()]
        if safe_url.lower().startswith("https://"):
            handlers.append(urllib.request.HTTPSHandler(context=build_llm_ssl_context()))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(
            safe_url,
            data=payload_bytes,
            headers=headers,
            method=method.upper(),
        )
        return opener, req, safe_url

    @staticmethod
    def _raise_for_http_error(exc: urllib.error.HTTPError) -> None:
        err_raw = ""
        try:
            err_raw = exc.read(4096).decode("utf-8", errors="replace")
        except Exception:
            pass
        finally:
            try:
                exc.close()
            except Exception:
                pass
        err_detail = err_raw.strip()[:400]
        try:
            parsed_err = json.loads(err_raw)
            if isinstance(parsed_err, dict):
                if isinstance(parsed_err.get("error"), dict):
                    err_detail = str(parsed_err["error"].get("message") or err_detail)[:400]
                elif parsed_err.get("message"):
                    err_detail = str(parsed_err.get("message") or err_detail)[:400]
        except Exception:
            pass
        msg = f"HTTP {exc.code}: {err_detail or exc.reason}"
        if exc.code in (400, 413) and _is_context_limit_error(err_detail):
            raise ContextLengthExceededError(msg) from None
        raise _LLMHttpError(exc.code, err_detail or str(exc.reason or ""), msg) from None

    @staticmethod
    def _raise_for_url_error(exc: urllib.error.URLError) -> None:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise RuntimeError(
                f"连接 AI 服务端失败: {exc.reason}"
                "（TLS 证书校验未通过：如处于企业代理或私有自签网关环境，"
                "请将根证书加入系统受信任根证书，或通过环境变量 SSL_CERT_FILE 指定 CA 证书文件）"
            ) from None
        raise RuntimeError(f"连接 AI 服务端失败: {exc.reason}") from None

    @staticmethod
    def _request_openai_json(
        method: str,
        endpoint: str,
        api_key: str,
        body: Optional[Dict[str, Any]],
        timeout_sec: float,
    ) -> Dict[str, Any]:
        opener, req, safe_url = LLMHub._build_openai_opener_and_request(
            method, endpoint, api_key, body, accept="application/json"
        )
        parsed_ep = urlparse(safe_url)
        with trace_span(
            "llm",
            "http_request",
            {
                "method": method.upper(),
                "host": parsed_ep.hostname or "",
                "path": parsed_ep.path or "/",
            },
        ):
            try:
                with opener.open(req, timeout=max(1.0, float(timeout_sec))) as resp:
                    raw = resp.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                LLMHub._raise_for_http_error(exc)
            except urllib.error.URLError as exc:
                LLMHub._raise_for_url_error(exc)

        try:
            parsed = json.loads(raw)
        except Exception as exc:
            raise RuntimeError("AI 服务端返回了非 JSON 响应") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("AI 服务端返回的 JSON 顶层结构异常")
        return parsed

    @staticmethod
    def _extract_reasoning_and_content_parts(raw_content: Any) -> Tuple[str, str]:
        if isinstance(raw_content, list):
            r_parts: List[str] = []
            c_parts: List[str] = []
            for p in raw_content:
                if not isinstance(p, dict):
                    if p:
                        c_parts.append(str(p))
                    continue
                ptype = str(p.get("type") or "").strip().lower()
                if ptype in ("thinking", "reasoning", "thought"):
                    val = p.get("thinking") or p.get("reasoning") or p.get("text") or ""
                    if val:
                        r_parts.append(str(val))
                else:
                    val = p.get("text") or p.get("content") or ""
                    if val:
                        c_parts.append(str(val))
            return "".join(r_parts), "".join(c_parts)
        if raw_content is None:
            return "", ""
        return "", str(raw_content)

    @classmethod
    def _extract_message_reasoning_and_content(cls, msg_obj: Any) -> Tuple[str, str]:
        if msg_obj is None:
            return "", ""
        if isinstance(msg_obj, dict):
            raw_reasoning = str(
                msg_obj.get("reasoning_content")
                or msg_obj.get("reasoning")
                or msg_obj.get("thinking")
                or ""
            )
            raw_content = msg_obj.get("content")
        else:
            raw_reasoning = str(
                getattr(msg_obj, "reasoning_content", None)
                or getattr(msg_obj, "reasoning", None)
                or getattr(msg_obj, "thinking", None)
                or ""
            )
            raw_content = getattr(msg_obj, "content", "")

        blk_reasoning, content_str = cls._extract_reasoning_and_content_parts(raw_content)
        parser = ThinkingStreamParser()
        out_r1, out_c1 = parser.feed(raw_reasoning + blk_reasoning, content_str)
        out_r2, out_c2 = parser.flush()
        return (out_r1 + out_r2), (out_c1 + out_c2)

    @staticmethod
    def _build_chat_body(
        profile: LLMProviderProfile,
        *,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: Optional[int],
        stream: bool,
        thinking_mode: str = "auto",
        reasoning_effort: str = "",
    ) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
        caps = infer_model_capabilities(model, profile.provider_type, profile.base_url, profile.extra)
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if not caps["omit_temperature"]:
            body["temperature"] = temperature
        if max_tokens is not None:
            tok_key = "max_completion_tokens" if caps["use_max_completion_tokens"] else "max_tokens"
            body[tok_key] = max_tokens
        if stream:
            body["stream"] = True

        tmode = (thinking_mode or "auto").strip().lower()
        style = caps["thinking_param_style"]
        injected = False
        if tmode in ("enabled", "disabled"):
            if style == "deepseek":
                body["thinking"] = {"type": "enabled" if tmode == "enabled" else "disabled"}
                injected = True
            elif style == "openai_effort":
                if tmode == "enabled":
                    body["reasoning_effort"] = (reasoning_effort or "medium").strip().lower()
                    injected = True
                elif tmode == "disabled" and caps["thinking_mode"] == "toggleable":
                    body["reasoning_effort"] = "none"
                    injected = True
            elif style == "qwen_enable":
                body["enable_thinking"] = tmode == "enabled"
                injected = True

        return body, caps, injected

    @staticmethod
    def _strip_optional_thinking_params(body: Dict[str, Any]) -> Dict[str, Any]:
        cleaned = dict(body)
        for k in ("thinking", "reasoning_effort", "enable_thinking"):
            cleaned.pop(k, None)
        return cleaned

    def _http_fallback_chat(
        self,
        profile: LLMProviderProfile,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: Optional[int],
        timeout_sec: float,
        *,
        thinking_mode: str = "auto",
        reasoning_effort: str = "",
    ) -> Tuple[str, str]:
        endpoint = profile.base_url.rstrip("/") + "/chat/completions"
        body, _, injected = self._build_chat_body(
            profile,
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
        )
        try:
            data = self._request_openai_json(
                "POST", endpoint, profile.api_key, body, timeout_sec
            )
        except _LLMHttpError as exc:
            if injected and _is_unsupported_thinking_param_error(exc.status_code, exc.detail):
                data = self._request_openai_json(
                    "POST",
                    endpoint,
                    profile.api_key,
                    self._strip_optional_thinking_params(body),
                    timeout_sec,
                )
            else:
                raise
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return "", ""
        msg = choices[0].get("message") or {}
        return self._extract_message_reasoning_and_content(msg)

    def _http_fallback_models(
        self, profile: LLMProviderProfile, timeout_sec: float
    ) -> List[str]:
        endpoint = profile.base_url.rstrip("/") + "/models"
        data = self._request_openai_json(
            "GET", endpoint, profile.api_key, None, timeout_sec
        )
        items = data.get("data") or []
        return [str(it.get("id") or "") for it in items if isinstance(it, dict) and it.get("id")]

    def chat_completion(
        self,
        *,
        messages: List[Dict[str, str]],
        provider_id: str = "",
        model: str = "",
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        timeout_sec: float = 45.0,
        thinking_mode: str = "auto",
        reasoning_effort: str = "",
    ) -> Dict[str, Any]:
        resolved = self.resolve_provider_and_model(
            provider_id=provider_id, model=model, require_ready=True
        )
        assert resolved is not None
        prof, eff_model = resolved
        return self._execute_chat_with_profile(
            prof,
            messages=messages,
            model=eff_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
        )

    def _execute_chat_with_profile(
        self,
        profile: LLMProviderProfile,
        *,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        timeout_sec: float = 30.0,
        thinking_mode: str = "auto",
        reasoning_effort: str = "",
    ) -> Dict[str, Any]:
        is_local = any(h in (profile.base_url or "") for h in ("127.0.0.1", "localhost"))
        if not profile.api_key and not is_local:
            raise ValueError(f"请先填写 [{profile.name}] 的 API Key")

        t0 = time.perf_counter()
        client = self._get_sdk_client(profile, timeout_sec=timeout_sec)
        reasoning = ""
        content = ""
        if client is not None:
            sdk_kwargs, _, injected = self._build_chat_body(
                profile,
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                thinking_mode=thinking_mode,
                reasoning_effort=reasoning_effort,
            )
            try:
                resp = client.chat.completions.create(**sdk_kwargs)
            except TypeError:
                if injected:
                    resp = client.chat.completions.create(
                        **self._strip_optional_thinking_params(sdk_kwargs)
                    )
                else:
                    raise
            if getattr(resp, "choices", None):
                reasoning, content = self._extract_message_reasoning_and_content(
                    resp.choices[0].message
                )
        else:
            reasoning, content = self._http_fallback_chat(
                profile,
                messages,
                model,
                temperature,
                max_tokens,
                timeout_sec,
                thinking_mode=thinking_mode,
                reasoning_effort=reasoning_effort,
            )
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return {
            "provider_id": profile.provider_id,
            "provider_name": profile.name,
            "model": model,
            "content": content,
            "reasoning": reasoning,
            "elapsed_ms": elapsed_ms,
        }

    def chat_completion_stream(
        self,
        *,
        messages: List[Dict[str, str]],
        provider_id: str = "",
        model: str = "",
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        timeout_sec: float = 120.0,
        thinking_mode: str = "auto",
        reasoning_effort: str = "",
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Iterator[Dict[str, Any]]:
        """零依赖流式调用 OpenAI 兼容 `/chat/completions` 接口，实时增量拆分思考过程与回答正文."""
        resolved = self.resolve_provider_and_model(
            provider_id=provider_id, model=model, require_ready=True
        )
        assert resolved is not None
        profile, eff_model = resolved
        is_local = any(h in (profile.base_url or "") for h in ("127.0.0.1", "localhost"))
        if not profile.api_key and not is_local:
            raise ValueError(f"请先填写 [{profile.name}] 的 API Key")

        t0 = time.perf_counter()
        parser = ThinkingStreamParser()
        reasoning_buf: List[str] = []
        content_buf: List[str] = []
        finish_reason = ""
        usage: Dict[str, Any] = {}

        def _make_done_event(cancelled: bool = False) -> Dict[str, Any]:
            return {
                "type": "done",
                "provider_id": profile.provider_id,
                "provider_name": profile.name,
                "model": eff_model,
                "content": "".join(content_buf),
                "reasoning": "".join(reasoning_buf),
                "finish_reason": finish_reason,
                "usage": usage,
                "cancelled": bool(cancelled),
                "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }

        client = self._get_sdk_client(profile, timeout_sec=timeout_sec)
        if client is not None:
            sdk_kwargs, _, injected = self._build_chat_body(
                profile,
                messages=messages,
                model=eff_model,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                thinking_mode=thinking_mode,
                reasoning_effort=reasoning_effort,
            )
            try:
                resp = client.chat.completions.create(**sdk_kwargs)
            except TypeError:
                clean_kwargs = self._strip_optional_thinking_params(sdk_kwargs) if injected else dict(sdk_kwargs)
                try:
                    resp = client.chat.completions.create(**clean_kwargs)
                except TypeError:
                    clean_kwargs.pop("stream", None)
                    resp = client.chat.completions.create(**clean_kwargs)

            # 兼容返回单对象（非流式 Mock 客户端）
            if hasattr(resp, "choices"):
                r_all, c_all = self._extract_message_reasoning_and_content(
                    resp.choices[0].message if resp.choices else None
                )
                if r_all:
                    reasoning_buf.append(r_all)
                    yield {"type": "reasoning", "delta": r_all}
                if c_all:
                    content_buf.append(c_all)
                    yield {"type": "content", "delta": c_all}
                yield _make_done_event(False)
                return

            cancelled = False
            for chunk in resp:
                if cancel_check and cancel_check():
                    cancelled = True
                    break
                ch_usage = getattr(chunk, "usage", None)
                if isinstance(ch_usage, dict):
                    usage = ch_usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                ch0 = choices[0]
                fr = getattr(ch0, "finish_reason", None)
                if fr:
                    finish_reason = str(fr)
                delta = getattr(ch0, "delta", None) or getattr(ch0, "message", None)
                if delta is None:
                    continue
                if isinstance(delta, dict):
                    raw_r = str(
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or delta.get("thinking")
                        or ""
                    )
                    raw_c = delta.get("content")
                else:
                    raw_r = str(
                        getattr(delta, "reasoning_content", None)
                        or getattr(delta, "reasoning", None)
                        or getattr(delta, "thinking", None)
                        or ""
                    )
                    raw_c = getattr(delta, "content", "")
                blk_r, c_str = self._extract_reasoning_and_content_parts(raw_c)
                out_r, out_c = parser.feed(raw_r + blk_r, c_str)
                if out_r:
                    reasoning_buf.append(out_r)
                    yield {"type": "reasoning", "delta": out_r}
                if out_c:
                    content_buf.append(out_c)
                    yield {"type": "content", "delta": out_c}

            tail_r, tail_c = parser.flush()
            if tail_r:
                reasoning_buf.append(tail_r)
                yield {"type": "reasoning", "delta": tail_r}
            if tail_c:
                content_buf.append(tail_c)
                yield {"type": "content", "delta": tail_c}
            yield _make_done_event(cancelled)
            return

        endpoint = profile.base_url.rstrip("/") + "/chat/completions"
        body, _, injected = self._build_chat_body(
            profile,
            messages=messages,
            model=eff_model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
        )

        def _open_stream(req_body: Dict[str, Any]) -> Tuple[Any, str]:
            opener, req, safe_url = self._build_openai_opener_and_request(
                "POST",
                endpoint,
                profile.api_key,
                req_body,
                accept="text/event-stream, application/json",
            )
            parsed_ep = urlparse(safe_url)
            with trace_span(
                "llm",
                "http_stream_open",
                {
                    "method": "POST",
                    "host": parsed_ep.hostname or "",
                    "path": parsed_ep.path or "/",
                },
            ):
                try:
                    resp_obj = opener.open(req, timeout=max(1.0, float(timeout_sec)))
                    return resp_obj, safe_url
                except urllib.error.HTTPError as exc:
                    self._raise_for_http_error(exc)
                except urllib.error.URLError as exc:
                    self._raise_for_url_error(exc)
            raise RuntimeError("无法建立 AI 流式连接")

        try:
            resp_obj, _ = _open_stream(body)
        except _LLMHttpError as exc:
            if injected and _is_unsupported_thinking_param_error(exc.status_code, exc.detail):
                resp_obj, _ = _open_stream(self._strip_optional_thinking_params(body))
            else:
                raise

        cancelled = False
        with resp_obj:
            ctype = (resp_obj.headers.get("Content-Type") or "").lower()
            if "application/json" in ctype:
                raw_json = resp_obj.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(raw_json)
                except Exception as exc:
                    raise RuntimeError("AI 服务端返回了非 JSON 响应") from exc
                if not isinstance(parsed, dict):
                    raise RuntimeError("AI 服务端返回的 JSON 顶层结构异常")
                if isinstance(parsed.get("error"), dict):
                    err_msg = str(parsed["error"].get("message") or parsed["error"])[:400]
                    if _is_context_limit_error(err_msg):
                        raise ContextLengthExceededError(err_msg)
                    raise RuntimeError(err_msg)
                if isinstance(parsed.get("usage"), dict):
                    usage = parsed["usage"]
                choices = parsed.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    finish_reason = str(choices[0].get("finish_reason") or "")
                    r_all, c_all = self._extract_message_reasoning_and_content(
                        choices[0].get("message") or {}
                    )
                    if r_all:
                        reasoning_buf.append(r_all)
                        yield {"type": "reasoning", "delta": r_all}
                    if c_all:
                        content_buf.append(c_all)
                        yield {"type": "content", "delta": c_all}
                yield _make_done_event(False)
                return

            while True:
                if cancel_check and cancel_check():
                    cancelled = True
                    break
                raw_line = resp_obj.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                if data_str == "[DONE]":
                    break
                try:
                    chunk_obj = json.loads(data_str)
                except Exception:
                    continue
                if not isinstance(chunk_obj, dict):
                    continue
                if isinstance(chunk_obj.get("error"), dict):
                    err_msg = str(chunk_obj["error"].get("message") or chunk_obj["error"])[:400]
                    if _is_context_limit_error(err_msg):
                        raise ContextLengthExceededError(err_msg)
                    raise RuntimeError(err_msg)
                if isinstance(chunk_obj.get("usage"), dict):
                    usage = chunk_obj["usage"]
                choices = chunk_obj.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    continue
                ch0 = choices[0]
                if ch0.get("finish_reason"):
                    finish_reason = str(ch0["finish_reason"])
                delta = ch0.get("delta")
                if not isinstance(delta, dict):
                    delta = ch0.get("message") if isinstance(ch0.get("message"), dict) else {}
                raw_r = str(
                    delta.get("reasoning_content")
                    or delta.get("reasoning")
                    or delta.get("thinking")
                    or ""
                )
                blk_r, c_str = self._extract_reasoning_and_content_parts(delta.get("content"))
                out_r, out_c = parser.feed(raw_r + blk_r, c_str)
                if out_r:
                    reasoning_buf.append(out_r)
                    yield {"type": "reasoning", "delta": out_r}
                if out_c:
                    content_buf.append(out_c)
                    yield {"type": "content", "delta": out_c}

        tail_r, tail_c = parser.flush()
        if tail_r:
            reasoning_buf.append(tail_r)
            yield {"type": "reasoning", "delta": tail_r}
        if tail_c:
            content_buf.append(tail_c)
            yield {"type": "content", "delta": tail_c}
        yield _make_done_event(cancelled)

    def test_provider(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        prof = self.build_profile_from_payload(payload, provider_id=payload.get("provider_id"))
        model = prof.default_model or (prof.models[0] if prof.models else "")
        if not model:
            raise ValueError("请至少填写或选择一个要检测的模型名称")
        caps = infer_model_capabilities(model, prof.provider_type, prof.base_url, prof.extra)
        probe_max_tokens = 256 if caps["supports_thinking"] else 32
        res = self._execute_chat_with_profile(
            prof,
            messages=[{"role": "user", "content": "Reply with OK only."}],
            model=model,
            temperature=0.0,
            max_tokens=probe_max_tokens,
            timeout_sec=20.0,
        )
        preview = (res.get("content") or "").strip()
        if not preview and res.get("reasoning"):
            preview = f"[思考] {str(res['reasoning']).strip()}"
        return {
            "ok": True,
            "provider_id": prof.provider_id,
            "provider_name": prof.name,
            "model": model,
            "elapsed_ms": res["elapsed_ms"],
            "reply_preview": preview[:40],
            "message": f"{prof.name} ({model}) 连通正常 · {res['elapsed_ms']} ms",
        }

    def fetch_models(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        prof = self.build_profile_from_payload(payload, provider_id=payload.get("provider_id"))
        is_local = any(h in (prof.base_url or "") for h in ("127.0.0.1", "localhost"))
        if not prof.api_key and not is_local:
            raise ValueError(f"拉取模型列表前请先填写 [{prof.name}] 的 API Key")

        t0 = time.perf_counter()
        client = self._get_sdk_client(prof, timeout_sec=20.0)
        raw_ids: List[str] = []
        if client is not None:
            listing = client.models.list()
            data_items = getattr(listing, "data", None)
            if data_items is None:
                data_items = list(listing)
            for item in data_items:
                mid = getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else "")
                if mid:
                    raw_ids.append(str(mid))
        else:
            raw_ids = self._http_fallback_models(prof, timeout_sec=20.0)

        cleaned: List[str] = []
        for mid in raw_ids:
            nm = _normalize_model_id(mid, prof.provider_type, prof.base_url)
            if nm and nm not in cleaned:
                cleaned.append(nm)

        if not cleaned:
            raise ValueError("远端接口未返回任何可用模型列表")

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        # 若该服务商已存在于数据库，同步更新其模型缓存列表（不覆盖未提交的其它字段）
        saved_existing = self.store.get_llm_provider(prof.provider_id)
        if saved_existing is not None:
            saved_existing.models = cleaned
            if not saved_existing.default_model or saved_existing.default_model not in cleaned:
                saved_existing.default_model = cleaned[0]
            self.store.upsert_llm_provider(saved_existing)

        return {
            "ok": True,
            "provider_id": prof.provider_id,
            "models": cleaned,
            "default_model": (
                prof.default_model if prof.default_model in cleaned else cleaned[0]
            ),
            "elapsed_ms": elapsed_ms,
            "providers": self.list_providers(),
            "available_models": self.list_available_models(),
            "message": f"已同步 {len(cleaned)} 个可用模型 ({elapsed_ms} ms)",
        }

    def create_embeddings(
        self,
        texts: List[str],
        *,
        provider_id: str = "",
        model: str = "",
        base_url: str = "",
        api_key: str = "",
        timeout_sec: float = 15.0,
    ) -> List[List[float]]:
        """零依赖调用 OpenAI 兼容 `/embeddings` 接口生成文本向量（离线/本地网关可免 Key）."""
        clean_inputs = [str(t or "") for t in (texts or [])]
        if not clean_inputs:
            return []

        eff_base_url = (base_url or "").strip()
        eff_api_key = (api_key or "").strip()
        eff_model = (model or "").strip()

        if not eff_base_url:
            resolved = self.resolve_provider_and_model(
                provider_id=provider_id,
                model=eff_model,
                require_ready=True,
            )
            assert resolved is not None
            prof, resolved_model = resolved
            eff_base_url = prof.base_url
            if not eff_api_key:
                eff_api_key = prof.api_key
            if not eff_model:
                eff_model = resolved_model
        else:
            eff_base_url = validate_llm_base_url(eff_base_url)
            if "::" in eff_model and not provider_id:
                _, eff_model = eff_model.split("::", 1)
                eff_model = eff_model.strip()

        if not eff_model:
            raise ValueError("未指定 Embedding 模型名称")

        is_local = any(h in eff_base_url.lower() for h in ("127.0.0.1", "localhost"))
        if not eff_api_key and not is_local:
            raise ValueError("调用远端 Embedding 接口前请先填写 API Key")

        endpoint = eff_base_url.rstrip("/") + "/embeddings"
        body: Dict[str, Any] = {
            "model": eff_model,
            "input": clean_inputs,
        }
        data = self._request_openai_json(
            "POST", endpoint, eff_api_key, body, max(1.0, float(timeout_sec))
        )
        items = data.get("data")
        if not isinstance(items, list):
            raise RuntimeError("Embedding 服务端返回的 data 字段格式异常")

        ordered = sorted(
            (it for it in items if isinstance(it, dict)),
            key=lambda it: int(it.get("index", 0) or 0),
        )
        vectors: List[List[float]] = []
        for item in ordered:
            emb = item.get("embedding")
            if not isinstance(emb, list) or not emb:
                raise RuntimeError("Embedding 服务端返回的向量条目无效")
            vec = [float(x) for x in emb]
            vectors.append(vec)
        if len(vectors) != len(clean_inputs):
            raise RuntimeError(
                f"Embedding 返回向量数量 ({len(vectors)}) 与输入文本数 ({len(clean_inputs)}) 不一致"
            )
        return vectors
