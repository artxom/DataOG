"""Data.O.G 全局 AI 大模型统一接入中枢 (LLMHub).

参考 Cherry Studio / LobeChat 成熟的多服务商管理架构，采用零额外第三方依赖的 Python 标准库原生支持 OpenAI 兼容 REST 协议：
1. 内置三大主流预设（DeepSeek 官方端点、Google Gemini 官方 OpenAI 兼容端点、本地/内网 OpenAI 兼容网关）；
2. API Key 仅持久化于 Layer 2 本地私有保险箱 SQLite，所有对外接口严格脱敏回显，留空或回传掩码时自动保留原密钥；
3. 支持连通性毫秒级测试、远端模型列表拉取（自动兼容剥离 Gemini `models/` 前缀）及结构化 JSON 生成。
"""

from __future__ import annotations

import datetime
import http.client
import json
import math
import os
import re
import socket
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
    "stream_options",
)

_SMALL_LOCAL_TAG_RE = re.compile(r"(?:^|[:\-_])(?:0\.5b|1\.5b|3b|4b|7b|8b|9b|14b|32b)(?:$|[:\-_])")
_TINY_LOCAL_TAG_RE = re.compile(r"(?:^|[:\-_])(?:0\.5b|1\.5b|3b|4b|7b|8b|9b|14b)(?:$|[:\-_])")
_LIGHTWEIGHT_MODEL_RE = re.compile(
    r"(?:^|[\-_.:/])(?:flash|mini|lite|fast|nano|tiny|haiku|small)(?:$|[\-_.:/0-9])",
    re.IGNORECASE,
)
_TURBO_LIGHTWEIGHT_RE = re.compile(r"(?:^|[\-_.:/])turbo(?:$|[\-_.:/0-9])", re.IGNORECASE)
_FLAGSHIP_MODEL_RE = re.compile(
    r"(?:reasoner|deepseek-r1|(?:^|[\-_.:/])r1(?:$|[\-_.:/])|gemini-(?:2\.5|3)-pro|"
    r"claude-(?:3[.-]7|4|sonnet-4|opus-4|opus)|gpt-5|gpt-4\.1|^(?:o1|o3|o4)(?:$|-|\.|_)|"
    r"qwen-max|qwen-long|qwq|qvq|kimi-k2|glm-5|glm-z1)",
    re.IGNORECASE,
)

MODEL_PROBE_TTL_SEC = 24 * 3600
RETRY_BACKOFF_SCHEDULE: Tuple[float, ...] = (2.0, 5.0, 12.0)
TRANSIENT_HTTP_STATUS_CODES: Tuple[int, ...] = (408, 429, 500, 502, 503, 504)
_PROBE_CALIBRATION_SQL = (
    "SELECT a.cust_id, SUM(a.txn_amt) AS total_amt "
    "FROM dwd.t_txn_daily a JOIN dim.t_branch b ON a.branch_id = b.branch_id "
    "WHERE a.biz_date = '20261124' GROUP BY a.cust_id; -- 标定Token换算比率，请仅回复OK"
)

_NUM_TOKEN_RE = r"(\d{1,3}(?:,\d{3})+|\d{4,8})"

_CTX_LIMIT_STRICT_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"(?i)(?:maximum\s+context\s+length(?:\s+is|\s+exceeded|\s+of)?|"
        rf"maximum(?:\s+number\s+of)?\s+tokens?\s*(?:allowed|is|limit|of)?|"
        rf"max_model_len|context_length|context\s+window|input\s+token\s+limit|"
        rf"token\s+limit|maximum\s+of|limit(?:\s+is|\s+of)?|allowed)\D{{0,18}}{_NUM_TOKEN_RE}\b"
    ),
    re.compile(rf"(?i)\b{_NUM_TOKEN_RE}\s*tokens?\s*(?:maximum|limit|max|allowed)"),
)

_CTX_LIMIT_K_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\((\d{1,4})\s*k(?:\s*tokens?)?\)"),
    re.compile(r"(?i)(?:context|limit|window|max|allowed)\D{0,12}(\d{1,4})\s*k\b"),
)

_CTX_LIMIT_PAREN_FALLBACK_RE = re.compile(rf"(?i)\({_NUM_TOKEN_RE}\s*tokens?\)")


def _parse_int_commas(raw: str) -> Optional[int]:
    try:
        return int(str(raw or "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_context_limit_from_error(detail: str) -> Optional[int]:
    """从上游 400/413 上下文超限报错文本中提取真实最大上下文窗口数值（兼容千分位逗号与多数字误判防护）."""
    text = str(detail or "").strip()
    if not text:
        return None
    for pat in _CTX_LIMIT_STRICT_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        val = _parse_int_commas(m.group(1))
        if val is not None and 4096 <= val <= 10_000_000:
            return val
    for pat in _CTX_LIMIT_K_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        val = _parse_int_commas(m.group(1))
        if val is not None:
            scaled = val * 1024
            if 4096 <= scaled <= 10_000_000:
                return scaled
    # 兜底匹配 `(32768 tokens)`：若报错同时出现请求数与上限数，取较小值作为窗口上限（因超限必有 input > limit）
    paren_vals: List[int] = []
    for m in _CTX_LIMIT_PAREN_FALLBACK_RE.finditer(text):
        val = _parse_int_commas(m.group(1))
        if val is not None and 4096 <= val <= 10_000_000:
            paren_vals.append(val)
    if paren_vals:
        return min(paren_vals)
    return None


def _estimate_probe_tokens(text: str) -> int:
    """与 attachments.estimate_tokens 同公式的 O(N) 估算（严禁反向导入 modules.*）."""
    if not text:
        return 0
    char_len = len(text)
    utf8_len = len(text.encode("utf-8", errors="ignore"))
    wide_approx = max(0, min(char_len, (utf8_len - char_len) // 2))
    ascii_approx = max(0, char_len - wide_approx)
    return max(1, math.ceil(wide_approx * 1.5 + ascii_approx * 0.28))


def _stitch_continuation_overlap(existing: str, addition: str) -> str:
    """续写拼接去重：剥离模型在续写开头偶然重复的末尾 4~48 字符."""
    if not existing or not addition:
        return addition or ""
    tail = existing[-48:]
    max_k = min(len(tail), len(addition), 48)
    for k in range(max_k, 3, -1):
        if tail.endswith(addition[:k]):
            return addition[k:]
    return addition


def _extract_model_meta_from_item(item: Any) -> Dict[str, Any]:
    """从 `/models` 列表单条记录提取 L1 扩展元数据（兼容 dict 与 SDK 对象）."""
    if item is None:
        return {}
    if isinstance(item, dict):
        d = item
    elif hasattr(item, "model_dump"):
        try:
            d = item.model_dump()
        except Exception:
            d = vars(item) if hasattr(item, "__dict__") else {}
    elif hasattr(item, "__dict__"):
        d = dict(vars(item))
    else:
        d = {}

    if not isinstance(d, dict):
        return {}

    meta: Dict[str, Any] = {}
    for key in (
        "context_length",
        "max_model_len",
        "context_window",
        "input_token_limit",
        "max_input_tokens",
        "n_ctx_train",
        "context_size",
    ):
        raw_v = d.get(key)
        if raw_v is None and isinstance(d.get("top_provider"), dict):
            raw_v = d["top_provider"].get(key)
        if raw_v is not None:
            try:
                iv = int(raw_v)
                if 4096 <= iv <= 10_000_000:
                    meta["context_window"] = iv
                    break
            except (TypeError, ValueError):
                pass

    for key in ("max_output_tokens", "output_token_limit", "max_completion_tokens"):
        raw_v = d.get(key)
        if raw_v is None and isinstance(d.get("top_provider"), dict):
            raw_v = d["top_provider"].get(key)
        if raw_v is not None:
            try:
                iv = int(raw_v)
                if 256 <= iv <= 1_000_000:
                    meta["max_output_tokens"] = iv
                    break
            except (TypeError, ValueError):
                pass

    caps = d.get("capabilities")
    if isinstance(caps, dict):
        if "reasoning" in caps or "thinking" in caps:
            meta["supports_thinking"] = bool(caps.get("reasoning") or caps.get("thinking"))
    elif isinstance(d.get("supported_parameters"), list):
        sp_low = {str(x).lower() for x in d["supported_parameters"]}
        if "reasoning" in sp_low or "include_reasoning" in sp_low:
            meta["supports_thinking"] = True

    return meta


class ContextLengthExceededError(RuntimeError):
    """上游大模型返回输入上下文超出最大窗口限制时抛出的结构化异常."""

    def __init__(self, message: str, *, detected_limit: Optional[int] = None) -> None:
        super().__init__(message)
        self.detected_limit: Optional[int] = (
            detected_limit if detected_limit is not None else parse_context_limit_from_error(message)
        )


class _LLMHttpError(RuntimeError):
    """内部 HTTP 状态异常（用于判断参数不支持自动降级与瞬态重试）."""

    def __init__(self, status_code: int, detail: str, message: str) -> None:
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.detail = detail


class _LLMTransientError(RuntimeError):
    """可安全指数退避重试的瞬态网络/超时/5xx 异常."""

    def __init__(self, message: str, *, reason: str = "timeout", status_code: int = 0) -> None:
        super().__init__(message)
        self.reason = reason
        self.status_code = int(status_code or 0)


def _is_transient_exception(exc: BaseException) -> Tuple[bool, str]:
    if isinstance(exc, ContextLengthExceededError):
        return False, ""
    if isinstance(exc, _LLMTransientError):
        return True, exc.reason
    if isinstance(exc, _LLMHttpError):
        if exc.status_code in TRANSIENT_HTTP_STATUS_CODES:
            return True, f"HTTP {exc.status_code}"
        return False, ""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True, "响应超时"
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, http.client.IncompleteRead, http.client.RemoteDisconnected)):
        return True, "上游连接中断"
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            return False, ""
        if isinstance(exc.reason, (TimeoutError, socket.timeout, ConnectionResetError, BrokenPipeError, OSError)):
            return True, f"网络抖动({exc.reason})"
    return False, ""


def _sleep_interruptible(seconds: float, cancel_check: Optional[Callable[[], bool]] = None) -> bool:
    """可响应取消信号的退避等待，若被取消返回 True."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if cancel_check and cancel_check():
            return True
        rem = deadline - time.monotonic()
        if rem <= 0:
            return False
        time.sleep(min(0.1, rem))


def _lookup_model_probe(extra: Optional[Dict[str, Any]], model: str) -> Optional[Dict[str, Any]]:
    if not isinstance(extra, dict):
        return None
    probes = extra.get("model_probes")
    if not isinstance(probes, dict) or not model:
        return None
    if isinstance(probes.get(model), dict):
        return probes[model]
    low = model.strip().lower()
    for k, v in probes.items():
        if str(k).strip().lower() == low and isinstance(v, dict):
            return v
    return None


def _is_probe_fresh(probe: Optional[Dict[str, Any]], ttl_sec: int = MODEL_PROBE_TTL_SEC) -> bool:
    if not isinstance(probe, dict):
        return False
    if str(probe.get("source") or "") not in ("live_probe", "error_calibrated"):
        return False
    ts = str(probe.get("probed_at") or "").strip()
    if not ts:
        return False
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", ""))
        age = (datetime.datetime.now() - dt).total_seconds()
        return 0 <= age <= ttl_sec
    except Exception:
        return False


def _infer_model_tier(
    m: str,
    m_base: str,
    context_window: int,
    *,
    is_local: bool,
    has_small_tag: bool,
    has_tiny_tag: bool,
    has_colon_small: bool,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    if isinstance(extra, dict):
        override = str(extra.get("model_tier") or "").strip().lower()
        if override in ("flagship", "standard", "lightweight"):
            return override
    # 明确轻量后缀 (-flash / -mini / -lite / -fast / -nano / -tiny / -haiku / -small) 优先判轻量
    if _LIGHTWEIGHT_MODEL_RE.search(m_base) or _LIGHTWEIGHT_MODEL_RE.search(m):
        return "lightweight"
    if _TURBO_LIGHTWEIGHT_RE.search(m_base) and "gpt-4-turbo" not in m_base:
        return "lightweight"
    if "gpt-3.5" in m_base or "deepseek-v2" in m_base or "deepseek-coder-v1" in m_base:
        return "lightweight"
    if (is_local and has_tiny_tag) or (has_colon_small and ":32b" not in m_base):
        return "lightweight"
    if context_window <= 32_768 and not _FLAGSHIP_MODEL_RE.search(m_base):
        return "lightweight"
    if _FLAGSHIP_MODEL_RE.search(m_base) or _FLAGSHIP_MODEL_RE.search(m):
        return "flagship"
    if is_local and has_small_tag and context_window <= 32_768:
        return "lightweight"
    return "standard"


def _compute_tier_budgets(
    context_window: int,
    base_attachment_budget: int,
    model_tier: str,
) -> Tuple[int, int, int]:
    """根据物理窗口与模型分级计算 `(effective_reasoning_budget, attachment_budget, recommended_chunk_tokens)`."""
    cw = max(4096, int(context_window))
    if model_tier == "lightweight":
        if cw >= 200_000:
            eff_budget = 24_000
        elif cw >= 64_000:
            eff_budget = 18_000
        elif cw >= 32_000:
            eff_budget = 16_000
        else:
            eff_budget = max(3_000, int(cw * 0.50))
        att_budget = min(base_attachment_budget, eff_budget)
        rec_chunk = 5_500 if cw >= 32_000 else max(2_000, int(eff_budget * 0.35))
        return eff_budget, att_budget, rec_chunk

    if model_tier == "flagship":
        eff_budget = min(240_000, max(base_attachment_budget, int(cw * 0.65)))
        att_budget = min(base_attachment_budget, eff_budget)
        rec_chunk = 12_000 if cw >= 64_000 else max(4_000, int(cw * 0.25))
        return eff_budget, att_budget, rec_chunk

    # standard
    eff_budget = min(180_000, max(base_attachment_budget, int(cw * 0.62)))
    att_budget = min(base_attachment_budget, eff_budget)
    rec_chunk = 8_000 if cw >= 64_000 else max(4_000, int(cw * 0.25))
    return eff_budget, att_budget, rec_chunk


def compute_adaptive_timeout(
    caps: Optional[Dict[str, Any]],
    requested_timeout: float,
    *,
    stream: bool = True,
) -> float:
    """结合探测 TTFT 动态计算流式/非流式读超时（保留单测显式小超时不强行拉长）."""
    req = max(1.0, float(requested_timeout or (120.0 if stream else 45.0)))
    if req < 25.0:
        return req
    ttft_ms = float((caps or {}).get("ttft_ms") or 0.0)
    base = max(req, 120.0) if stream else req
    if ttft_ms >= 3000.0:
        # 慢供应商按实测首字延迟弹性抬高至 180s~240s
        scaled = max(base, 120.0 + (ttft_ms / 100.0))
        return min(240.0, max(180.0 if ttft_ms >= 6000.0 else base, scaled))
    return base


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
    has_tiny_tag = bool(_TINY_LOCAL_TAG_RE.search(m_base))
    has_colon_small = any(t in m_base for t in (":0.5b", ":1.5b", ":3b", ":4b", ":7b", ":8b", ":9b", ":14b", ":32b"))

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
        if _LIGHTWEIGHT_MODEL_RE.search(m_base):
            # 轻量快速版如 deepseek-v4-flash：物理窗口保守 64K，有效推理预算由 lightweight 档收敛
            context_window = 64_000
            attachment_budget = 40_000
            max_output_tokens = 8192
            supports_thinking = False
            thinking_mode = "none"
            thinking_param_style = "none"
        else:
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

    source = "heuristic"
    token_ratio = 1.0
    ttft_ms = 0.0
    tokens_per_sec = 0.0
    stream_supported = True
    probed_at = ""
    probe_tier_override = ""

    # 合并持久化四级探针缓存 (L1 models_meta / L2 live_probe / L3 error_calibrated)
    probe = _lookup_model_probe(extra, model)
    if isinstance(probe, dict):
        if probe.get("context_window"):
            try:
                p_cw = int(probe["context_window"])
                if 4096 <= p_cw <= 10_000_000:
                    context_window = p_cw
                    attachment_budget = max(4000, min(240_000, int(p_cw * 0.62)))
            except (TypeError, ValueError):
                pass
        if probe.get("max_output_tokens"):
            try:
                p_out = int(probe["max_output_tokens"])
                if 256 <= p_out <= 1_000_000:
                    max_output_tokens = p_out
            except (TypeError, ValueError):
                pass
        if "supports_thinking" in probe and probe.get("supports_thinking") is not None:
            supports_thinking = bool(probe["supports_thinking"])
            if supports_thinking and thinking_mode == "none":
                thinking_mode = "toggleable"
        if probe.get("token_ratio"):
            try:
                tr = float(probe["token_ratio"])
                if 0.4 <= tr <= 3.0:
                    token_ratio = round(tr, 3)
            except (TypeError, ValueError):
                pass
        if probe.get("ttft_ms"):
            try:
                ttft_ms = max(0.0, round(float(probe["ttft_ms"]), 1))
            except (TypeError, ValueError):
                pass
        if probe.get("tokens_per_sec"):
            try:
                tokens_per_sec = max(0.0, round(float(probe["tokens_per_sec"]), 1))
            except (TypeError, ValueError):
                pass
        if "stream_supported" in probe and probe.get("stream_supported") is not None:
            stream_supported = bool(probe["stream_supported"])
        if probe.get("source"):
            source = str(probe["source"])
        if probe.get("probed_at"):
            probed_at = str(probe["probed_at"])
        if str(probe.get("detected_model_tier") or "").strip().lower() in ("flagship", "standard", "lightweight"):
            probe_tier_override = str(probe["detected_model_tier"]).strip().lower()

    # 服务商显式配置的自定义上下文窗口覆盖（>= 4096；若存在更小的实测 error_calibrated 物理上限则保护不越界）
    if isinstance(extra, dict) and extra.get("context_window"):
        try:
            custom_ctx = int(extra["context_window"])
            if custom_ctx >= 4096:
                if source == "error_calibrated" and context_window < custom_ctx:
                    pass
                else:
                    context_window = custom_ctx
                    attachment_budget = max(4000, min(240_000, int(custom_ctx * 0.62)))
                    if source == "heuristic":
                        source = "custom"
        except (TypeError, ValueError):
            pass

    model_tier = _infer_model_tier(
        m,
        m_base,
        context_window,
        is_local=is_local,
        has_small_tag=has_small_tag,
        has_tiny_tag=has_tiny_tag,
        has_colon_small=has_colon_small,
        extra=extra,
    )
    if probe_tier_override and not (isinstance(extra, dict) and extra.get("model_tier")):
        model_tier = probe_tier_override

    effective_reasoning_budget, attachment_budget, recommended_chunk_tokens = _compute_tier_budgets(
        context_window, attachment_budget, model_tier
    )

    return {
        "context_window": int(context_window),
        "effective_reasoning_budget": int(effective_reasoning_budget),
        "attachment_budget": int(attachment_budget),
        "max_output_tokens": int(max_output_tokens),
        "recommended_chunk_tokens": int(recommended_chunk_tokens),
        "model_tier": model_tier,
        "supports_thinking": bool(supports_thinking),
        "thinking_mode": thinking_mode,
        "omit_temperature": bool(omit_temperature),
        "use_max_completion_tokens": bool(use_max_completion_tokens),
        "thinking_param_style": thinking_param_style,
        "token_ratio": float(token_ratio),
        "ttft_ms": float(ttft_ms),
        "tokens_per_sec": float(tokens_per_sec),
        "stream_supported": bool(stream_supported),
        "source": source,
        "probed_at": probed_at,
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
                        "effective_reasoning_budget": caps["effective_reasoning_budget"],
                        "attachment_budget": caps["attachment_budget"],
                        "max_output_tokens": caps["max_output_tokens"],
                        "recommended_chunk_tokens": caps["recommended_chunk_tokens"],
                        "model_tier": caps["model_tier"],
                        "supports_thinking": caps["supports_thinking"],
                        "thinking_mode": caps["thinking_mode"],
                        "token_ratio": caps["token_ratio"],
                        "ttft_ms": caps["ttft_ms"],
                        "stream_supported": caps["stream_supported"],
                        "source": caps["source"],
                        "probed_at": caps["probed_at"],
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
        if "model_tier" in payload:
            raw_tier = str(payload.get("model_tier") or "").strip().lower()
            if not raw_tier or raw_tier == "auto":
                extra.pop("model_tier", None)
            elif raw_tier in ("flagship", "standard", "lightweight"):
                extra["model_tier"] = raw_tier
            else:
                raise ValueError("模型分级仅支持 auto / flagship / standard / lightweight")

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
            "User-Agent": "Data.O.G/3.4",
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
            raise ContextLengthExceededError(
                msg, detected_limit=parse_context_limit_from_error(err_detail)
            ) from None
        raise _LLMHttpError(exc.code, err_detail or str(exc.reason or ""), msg) from None

    @staticmethod
    def _raise_for_url_error(exc: urllib.error.URLError) -> None:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise RuntimeError(
                f"连接 AI 服务端失败: {exc.reason}"
                "（TLS 证书校验未通过：如处于企业代理或私有自签网关环境，"
                "请将根证书加入系统受信任根证书，或通过环境变量 SSL_CERT_FILE 指定 CA 证书文件）"
            ) from None
        if isinstance(exc.reason, (TimeoutError, socket.timeout, ConnectionResetError, BrokenPipeError, OSError)):
            raise _LLMTransientError(
                f"连接 AI 服务端失败: {exc.reason}",
                reason=f"网络抖动({exc.reason})",
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
            except (TimeoutError, socket.timeout) as exc:
                raise _LLMTransientError(f"AI 服务端响应超时: {exc}", reason="响应超时") from None
            except (ConnectionResetError, BrokenPipeError, http.client.IncompleteRead, http.client.RemoteDisconnected) as exc:
                raise _LLMTransientError(f"AI 服务端连接中断: {exc}", reason="连接中断") from None

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
        include_usage: bool = False,
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
        injected = False
        if stream:
            body["stream"] = True
            if include_usage:
                body["stream_options"] = {"include_usage": True}
                injected = True

        tmode = (thinking_mode or "auto").strip().lower()
        style = caps["thinking_param_style"]
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
        for k in ("thinking", "reasoning_effort", "enable_thinking", "stream_options"):
            cleaned.pop(k, None)
        return cleaned

    def _persist_model_probe(
        self,
        provider_id: str,
        model: str,
        updates: Dict[str, Any],
        *,
        preserve_stronger_source: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """将模型探查/校准档案安全落盘至 Layer 2 私有库 `llm_providers.extra_json['model_probes']`."""
        pid = (provider_id or "").strip().lower()
        mdl = (model or "").strip()
        if not pid or not mdl:
            return None
        existing = self.store.get_llm_provider(pid)
        if existing is None:
            return None

        extra = dict(existing.extra or {})
        probes = dict(extra.get("model_probes") or {}) if isinstance(extra.get("model_probes"), dict) else {}
        cur = dict(probes.get(mdl) or {}) if isinstance(probes.get(mdl), dict) else {}

        source_rank = {
            "heuristic": 0,
            "models_meta": 1,
            "live_probe": 2,
            "error_calibrated": 3,
            "custom": 3,
        }
        cur_src = str(cur.get("source") or "heuristic")
        new_src = str(updates.get("source") or cur_src)
        if preserve_stronger_source and source_rank.get(cur_src, 0) > source_rank.get(new_src, 0):
            # 仅补充缺失字段，不降级更强来源（例如已 error_calibrated / live_probe）
            merged = dict(cur)
            for k, v in updates.items():
                if k == "source":
                    continue
                if k not in merged or merged[k] in (None, "", 0, 0.0):
                    merged[k] = v
        else:
            merged = {**cur, **{k: v for k, v in updates.items() if v is not None}}
            merged["source"] = new_src

        merged.setdefault("probed_at", datetime.datetime.now().isoformat(timespec="seconds"))
        # 预填派生预算以便外部只读查看
        temp_extra = {**extra, "model_probes": {**probes, mdl: merged}}
        caps = infer_model_capabilities(mdl, existing.provider_type, existing.base_url, temp_extra)
        merged["context_window"] = caps["context_window"]
        merged["effective_reasoning_budget"] = caps["effective_reasoning_budget"]
        merged["max_output_tokens"] = caps["max_output_tokens"]
        merged["recommended_chunk_tokens"] = caps["recommended_chunk_tokens"]
        merged["model_tier"] = caps["model_tier"]
        merged["supports_thinking"] = caps["supports_thinking"]
        merged["token_ratio"] = caps["token_ratio"]
        merged["ttft_ms"] = caps["ttft_ms"]
        merged["stream_supported"] = caps["stream_supported"]

        probes[mdl] = merged
        extra["model_probes"] = probes
        existing.extra = extra
        self.store.upsert_llm_provider(existing)
        return merged

    def record_calibrated_context_limit(
        self,
        provider_id: str,
        model: str,
        detected_limit: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        """L3 运行期 400 超限报错自校准：立即回写实测物理窗口上限."""
        if not detected_limit or detected_limit < 4096 or detected_limit > 10_000_000:
            return None
        return self._persist_model_probe(
            provider_id,
            model,
            {
                "context_window": int(detected_limit),
                "source": "error_calibrated",
                "probed_at": datetime.datetime.now().isoformat(timespec="seconds"),
            },
            preserve_stronger_source=False,
        )

    def _probe_ollama_show_meta(
        self, base_url: str, model_id: str, timeout_sec: float = 3.0
    ) -> Dict[str, Any]:
        """L1 扩展：针对本地 Ollama 端点 (`127.0.0.1:11434` / `localhost:11434`) 按需读取 `/api/show`."""
        parsed = urlparse((base_url or "").strip())
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if host not in ("127.0.0.1", "localhost") or port != 11434:
            return {}
        root_url = f"{parsed.scheme}://{parsed.netloc}/api/show"
        try:
            data = self._request_openai_json(
                "POST", root_url, "", {"name": model_id}, min(3.0, max(1.0, float(timeout_sec)))
            )
        except Exception:
            return {}

        meta: Dict[str, Any] = {}
        # 1. 从 parameters 文本提取 num_ctx
        params_txt = str(data.get("parameters") or "")
        m_ctx = re.search(r"(?im)^\s*num_ctx\s+(\d{4,7})\b", params_txt)
        if m_ctx:
            try:
                v = int(m_ctx.group(1))
                if 4096 <= v <= 10_000_000:
                    meta["context_window"] = v
            except ValueError:
                pass
        # 2. 从 model_info 提取 *.context_length
        minfo = data.get("model_info")
        if isinstance(minfo, dict) and "context_window" not in meta:
            for k, v in minfo.items():
                if str(k).endswith("context_length"):
                    try:
                        iv = int(v)
                        if 4096 <= iv <= 10_000_000:
                            meta["context_window"] = iv
                            break
                    except (TypeError, ValueError):
                        pass
        # 3. 解析参数规模判断轻量档 (<= 14B)
        details = data.get("details")
        if isinstance(details, dict):
            psize = str(details.get("parameter_size") or "").strip().upper()
            m_b = re.match(r"^(\d+(?:\.\d+)?)\s*B$", psize)
            if m_b:
                try:
                    if float(m_b.group(1)) <= 14.5:
                        meta["detected_model_tier"] = "lightweight"
                except ValueError:
                    pass
        return meta

    def _http_fallback_chat_full(
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
    ) -> Tuple[str, str, str, Dict[str, Any]]:
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
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return "", "", "", usage
        finish_reason = str(choices[0].get("finish_reason") or "")
        msg = choices[0].get("message") or {}
        r_txt, c_txt = self._extract_message_reasoning_and_content(msg)
        return r_txt, c_txt, finish_reason, usage

    def _http_fallback_models_with_meta(
        self, profile: LLMProviderProfile, timeout_sec: float
    ) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
        endpoint = profile.base_url.rstrip("/") + "/models"
        data = self._request_openai_json(
            "GET", endpoint, profile.api_key, None, timeout_sec
        )
        items = data.get("data") or []
        ids: List[str] = []
        meta_by_id: Dict[str, Dict[str, Any]] = {}
        for it in items:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            raw_id = str(it.get("id") or "")
            ids.append(raw_id)
            norm_id = _normalize_model_id(raw_id, profile.provider_type, profile.base_url)
            meta = _extract_model_meta_from_item(it)
            if meta and norm_id:
                meta_by_id[norm_id] = meta
        return ids, meta_by_id

    @staticmethod
    def _build_continuation_messages(
        orig_messages: List[Dict[str, str]],
        accumulated_content: str,
    ) -> List[Dict[str, str]]:
        anchor = accumulated_content[-2500:] if len(accumulated_content) > 2500 else accumulated_content
        return list(orig_messages) + [
            {"role": "assistant", "content": anchor},
            {
                "role": "user",
                "content": (
                    "你上一轮的输出因长度上限或连接中断被截断。"
                    "请严格从上述截断位置紧接着继续输出剩余内容，切勿重复已经输出的部分，也不要添加多余开场白。"
                ),
            },
        ]

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
        retry_on_transient: bool = True,
        max_retries: int = 3,
        retry_delays: Optional[Tuple[float, ...]] = None,
        on_retry: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        auto_continue_on_length: bool = True,
        max_continuations: int = 3,
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
            retry_on_transient=retry_on_transient,
            max_retries=max_retries,
            retry_delays=retry_delays,
            on_retry=on_retry,
            cancel_check=cancel_check,
            auto_continue_on_length=auto_continue_on_length,
            max_continuations=max_continuations,
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
        retry_on_transient: bool = True,
        max_retries: int = 3,
        retry_delays: Optional[Tuple[float, ...]] = None,
        on_retry: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        auto_continue_on_length: bool = True,
        max_continuations: int = 3,
    ) -> Dict[str, Any]:
        is_local = any(h in (profile.base_url or "") for h in ("127.0.0.1", "localhost"))
        if not profile.api_key and not is_local:
            raise ValueError(f"请先填写 [{profile.name}] 的 API Key")

        caps = infer_model_capabilities(model, profile.provider_type, profile.base_url, profile.extra)
        eff_timeout = compute_adaptive_timeout(caps, timeout_sec, stream=False)
        delays = retry_delays if retry_delays is not None else RETRY_BACKOFF_SCHEDULE
        eff_max_retries = max(0, int(max_retries)) if retry_on_transient else 0

        t0 = time.perf_counter()

        def _invoke_once(
            req_msgs: List[Dict[str, str]],
            req_max_tok: Optional[int],
            req_tmode: str,
        ) -> Tuple[str, str, str, Dict[str, Any]]:
            attempt = 0
            cur_max_tok = req_max_tok
            cur_tmode = req_tmode
            while True:
                try:
                    client = self._get_sdk_client(profile, timeout_sec=eff_timeout)
                    if client is not None:
                        sdk_kwargs, _, injected = self._build_chat_body(
                            profile,
                            messages=req_msgs,
                            model=model,
                            temperature=temperature,
                            max_tokens=cur_max_tok,
                            stream=False,
                            thinking_mode=cur_tmode,
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
                        r_txt, c_txt = "", ""
                        fr = ""
                        usg: Dict[str, Any] = {}
                        if getattr(resp, "choices", None):
                            ch0 = resp.choices[0]
                            fr = str(getattr(ch0, "finish_reason", "") or "")
                            r_txt, c_txt = self._extract_message_reasoning_and_content(ch0.message)
                        raw_u = getattr(resp, "usage", None)
                        if isinstance(raw_u, dict):
                            usg = raw_u
                        return r_txt, c_txt, fr, usg
                    return self._http_fallback_chat_full(
                        profile,
                        req_msgs,
                        model,
                        temperature,
                        cur_max_tok,
                        eff_timeout,
                        thinking_mode=cur_tmode,
                        reasoning_effort=reasoning_effort,
                    )
                except ContextLengthExceededError as ctx_exc:
                    if ctx_exc.detected_limit:
                        self.record_calibrated_context_limit(
                            profile.provider_id, model, ctx_exc.detected_limit
                        )
                    raise
                except Exception as exc:
                    is_trans, reason = _is_transient_exception(exc)
                    if not is_trans or attempt >= eff_max_retries:
                        raise
                    attempt += 1
                    wait_sec = float(delays[min(attempt - 1, len(delays) - 1)]) if delays else 0.0
                    shed_load = attempt >= 2
                    if shed_load:
                        if cur_max_tok is not None:
                            cur_max_tok = max(1024, min(4096, cur_max_tok // 2))
                        else:
                            cur_max_tok = min(4096, max(2048, int(caps.get("max_output_tokens") or 4096) // 2))
                        cur_tmode = "disabled"
                    if on_retry:
                        on_retry(
                            {
                                "type": "retry_wait",
                                "attempt": attempt,
                                "max_retries": eff_max_retries,
                                "wait_sec": wait_sec,
                                "reason": reason,
                                "shed_load": shed_load,
                                "detail": (
                                    f"供应商{reason}，正在第 {attempt}/{eff_max_retries} 次自动退避重连"
                                    + ("（已启用减载降级）" if shed_load else "")
                                    + "..."
                                ),
                            }
                        )
                    if _sleep_interruptible(wait_sec, cancel_check):
                        raise RuntimeError("用户已取消当前大模型调用") from None

        reasoning, content, finish_reason, usage = _invoke_once(messages, max_tokens, thinking_mode)
        continuations = 0
        eff_max_cont = max(0, min(5, int(max_continuations))) if auto_continue_on_length else 0
        while (
            finish_reason == "length"
            and content.strip()
            and continuations < eff_max_cont
            and not (cancel_check and cancel_check())
        ):
            continuations += 1
            cont_msgs = self._build_continuation_messages(messages, content)
            c_r, c_c, finish_reason, c_usage = _invoke_once(cont_msgs, max_tokens, thinking_mode)
            if c_r:
                reasoning = (reasoning + "\n" + c_r).strip() if reasoning else c_r
            if not c_c:
                break
            stitched = _stitch_continuation_overlap(content, c_c)
            if not stitched:
                break
            content += stitched
            if c_usage:
                usage = c_usage

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return {
            "provider_id": profile.provider_id,
            "provider_name": profile.name,
            "model": model,
            "content": content,
            "reasoning": reasoning,
            "finish_reason": finish_reason,
            "usage": usage,
            "continuations": continuations,
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
        retry_on_transient: bool = True,
        max_retries: int = 3,
        retry_delays: Optional[Tuple[float, ...]] = None,
        auto_continue_on_length: bool = True,
        max_continuations: int = 3,
        include_usage: bool = False,
    ) -> Iterator[Dict[str, Any]]:
        """零依赖流式调用 OpenAI 兼容 `/chat/completions`，支持弹性静默超时、瞬态指数退避重试与 length 自动续写."""
        resolved = self.resolve_provider_and_model(
            provider_id=provider_id, model=model, require_ready=True
        )
        assert resolved is not None
        profile, eff_model = resolved
        yield from self._stream_chat_with_profile(
            profile,
            messages=messages,
            model=eff_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
            cancel_check=cancel_check,
            retry_on_transient=retry_on_transient,
            max_retries=max_retries,
            retry_delays=retry_delays,
            auto_continue_on_length=auto_continue_on_length,
            max_continuations=max_continuations,
            include_usage=include_usage,
        )

    def _stream_chat_with_profile(
        self,
        profile: LLMProviderProfile,
        *,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        timeout_sec: float = 120.0,
        thinking_mode: str = "auto",
        reasoning_effort: str = "",
        cancel_check: Optional[Callable[[], bool]] = None,
        retry_on_transient: bool = True,
        max_retries: int = 3,
        retry_delays: Optional[Tuple[float, ...]] = None,
        auto_continue_on_length: bool = True,
        max_continuations: int = 3,
        include_usage: bool = False,
    ) -> Iterator[Dict[str, Any]]:
        eff_model = model
        is_local = any(h in (profile.base_url or "") for h in ("127.0.0.1", "localhost"))
        if not profile.api_key and not is_local:
            raise ValueError(f"请先填写 [{profile.name}] 的 API Key")

        caps = infer_model_capabilities(eff_model, profile.provider_type, profile.base_url, profile.extra)
        eff_timeout = compute_adaptive_timeout(caps, timeout_sec, stream=True)
        delays = retry_delays if retry_delays is not None else RETRY_BACKOFF_SCHEDULE
        eff_max_retries = max(0, int(max_retries)) if retry_on_transient else 0
        eff_max_cont = max(0, min(5, int(max_continuations))) if auto_continue_on_length else 0

        t0 = time.perf_counter()
        reasoning_buf: List[str] = []
        content_buf: List[str] = []
        finish_reason = ""
        usage: Dict[str, Any] = {}
        continuations = 0
        retries_used = 0
        stream_used_sse = False

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
                "continuations": continuations,
                "retries": retries_used,
                "stream_supported": stream_used_sse,
                "cancelled": bool(cancelled),
                "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }

        client = self._get_sdk_client(profile, timeout_sec=eff_timeout)
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
            except ContextLengthExceededError as ctx_exc:
                if ctx_exc.detected_limit:
                    self.record_calibrated_context_limit(profile.provider_id, eff_model, ctx_exc.detected_limit)
                raise

            # 兼容返回单对象（非流式 Mock 客户端）
            if hasattr(resp, "choices"):
                ch0 = resp.choices[0] if resp.choices else None
                if ch0 is not None and getattr(ch0, "finish_reason", None):
                    finish_reason = str(ch0.finish_reason)
                r_all, c_all = self._extract_message_reasoning_and_content(
                    ch0.message if ch0 is not None else None
                )
                if r_all:
                    reasoning_buf.append(r_all)
                    yield {"type": "reasoning", "delta": r_all}
                if c_all:
                    content_buf.append(c_all)
                    yield {"type": "content", "delta": c_all}
                yield _make_done_event(False)
                return

            stream_used_sse = True
            parser = ThinkingStreamParser()
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

        def _open_stream(req_body: Dict[str, Any], read_timeout: float) -> Tuple[Any, str]:
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
                    resp_obj = opener.open(req, timeout=max(1.0, float(read_timeout)))
                    # 确保流式逐行读取按单次静默间隔计时，防止总时长 > 120s 被中途掐断
                    try:
                        raw_sock = getattr(getattr(resp_obj, "fp", None), "raw", None)
                        if raw_sock is not None and hasattr(raw_sock, "_sock") and raw_sock._sock is not None:
                            raw_sock._sock.settimeout(max(1.0, float(read_timeout)))
                    except Exception:
                        pass
                    return resp_obj, safe_url
                except urllib.error.HTTPError as exc:
                    self._raise_for_http_error(exc)
                except urllib.error.URLError as exc:
                    self._raise_for_url_error(exc)
                except (TimeoutError, socket.timeout) as exc:
                    raise _LLMTransientError(f"AI 流式连接超时: {exc}", reason="响应超时") from None
                except (ConnectionResetError, BrokenPipeError, http.client.IncompleteRead, http.client.RemoteDisconnected) as exc:
                    raise _LLMTransientError(f"AI 流式连接中断: {exc}", reason="连接中断") from None
            raise RuntimeError("无法建立 AI 流式连接")

        def _stream_single_segment(
            seg_msgs: List[Dict[str, str]],
            seg_max_tokens: Optional[int],
            seg_tmode: str,
            *,
            stitch_prefix: bool = False,
        ) -> Iterator[Dict[str, Any]]:
            nonlocal finish_reason, usage, stream_used_sse
            body, _, injected = self._build_chat_body(
                profile,
                messages=seg_msgs,
                model=eff_model,
                temperature=temperature,
                max_tokens=seg_max_tokens,
                stream=True,
                thinking_mode=seg_tmode,
                reasoning_effort=reasoning_effort,
                include_usage=include_usage,
            )
            try:
                resp_obj, _ = _open_stream(body, eff_timeout)
            except _LLMHttpError as exc:
                if injected and _is_unsupported_thinking_param_error(exc.status_code, exc.detail):
                    resp_obj, _ = _open_stream(self._strip_optional_thinking_params(body), eff_timeout)
                else:
                    raise

            parser = ThinkingStreamParser()
            stitch_buf = ""
            stitched_done = not stitch_prefix
            seg_finish = ""
            seg_cancelled = False

            def _emit_content_delta(chunk_txt: str) -> Optional[Dict[str, Any]]:
                nonlocal stitch_buf, stitched_done
                if not chunk_txt:
                    return None
                if not stitched_done:
                    stitch_buf += chunk_txt
                    if len(stitch_buf) < 48:
                        return None
                    stitched_done = True
                    cleaned = _stitch_continuation_overlap("".join(content_buf), stitch_buf)
                    stitch_buf = ""
                    if cleaned:
                        content_buf.append(cleaned)
                        return {"type": "content", "delta": cleaned}
                    return None
                content_buf.append(chunk_txt)
                return {"type": "content", "delta": chunk_txt}

            with resp_obj:
                ctype = (resp_obj.headers.get("Content-Type") or "").lower()
                if "application/json" in ctype:
                    try:
                        raw_json = resp_obj.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
                    except (TimeoutError, socket.timeout, http.client.IncompleteRead, ConnectionResetError) as exc:
                        raise _LLMTransientError(f"读取 AI 响应超时/中断: {exc}", reason="响应超时") from None
                    try:
                        parsed = json.loads(raw_json)
                    except Exception as exc:
                        raise RuntimeError("AI 服务端返回了非 JSON 响应") from exc
                    if not isinstance(parsed, dict):
                        raise RuntimeError("AI 服务端返回的 JSON 顶层结构异常")
                    if isinstance(parsed.get("error"), dict):
                        err_msg = str(parsed["error"].get("message") or parsed["error"])[:400]
                        if _is_context_limit_error(err_msg):
                            raise ContextLengthExceededError(
                                err_msg, detected_limit=parse_context_limit_from_error(err_msg)
                            )
                        raise RuntimeError(err_msg)
                    if isinstance(parsed.get("usage"), dict):
                        usage = parsed["usage"]
                    choices = parsed.get("choices") or []
                    if choices and isinstance(choices[0], dict):
                        seg_finish = str(choices[0].get("finish_reason") or "")
                        r_all, c_all = self._extract_message_reasoning_and_content(
                            choices[0].get("message") or {}
                        )
                        if r_all:
                            reasoning_buf.append(r_all)
                            yield {"type": "reasoning", "delta": r_all}
                        if c_all:
                            ev = _emit_content_delta(c_all)
                            if ev:
                                yield ev
                    if not stitched_done and stitch_buf:
                        stitched_done = True
                        cleaned = _stitch_continuation_overlap("".join(content_buf), stitch_buf)
                        stitch_buf = ""
                        if cleaned:
                            content_buf.append(cleaned)
                            yield {"type": "content", "delta": cleaned}
                    finish_reason = seg_finish
                    yield {"type": "_seg_end", "cancelled": False, "finish_reason": seg_finish}
                    return

                stream_used_sse = True
                while True:
                    if cancel_check and cancel_check():
                        seg_cancelled = True
                        break
                    try:
                        raw_line = resp_obj.readline()
                    except (TimeoutError, socket.timeout) as exc:
                        raise _LLMTransientError(f"流式读取静默超时: {exc}", reason="流式静默超时") from None
                    except (ConnectionResetError, BrokenPipeError, http.client.IncompleteRead, http.client.RemoteDisconnected) as exc:
                        raise _LLMTransientError(f"流式传输中断: {exc}", reason="流式连接中断") from None
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
                            raise ContextLengthExceededError(
                                err_msg, detected_limit=parse_context_limit_from_error(err_msg)
                            )
                        raise RuntimeError(err_msg)
                    if isinstance(chunk_obj.get("usage"), dict):
                        usage = chunk_obj["usage"]
                    choices = chunk_obj.get("choices") or []
                    if not choices or not isinstance(choices[0], dict):
                        continue
                    ch0 = choices[0]
                    if ch0.get("finish_reason"):
                        seg_finish = str(ch0["finish_reason"])
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
                        ev = _emit_content_delta(out_c)
                        if ev:
                            yield ev

            tail_r, tail_c = parser.flush()
            if tail_r:
                reasoning_buf.append(tail_r)
                yield {"type": "reasoning", "delta": tail_r}
            if tail_c:
                ev = _emit_content_delta(tail_c)
                if ev:
                    yield ev
            if not stitched_done and stitch_buf:
                stitched_done = True
                cleaned = _stitch_continuation_overlap("".join(content_buf), stitch_buf)
                stitch_buf = ""
                if cleaned:
                    content_buf.append(cleaned)
                    yield {"type": "content", "delta": cleaned}

            finish_reason = seg_finish
            yield {"type": "_seg_end", "cancelled": seg_cancelled, "finish_reason": seg_finish}

        cancelled = False
        cur_msgs = list(messages)
        cur_max_tok = max_tokens
        cur_tmode = thinking_mode
        stitch_next = False

        while True:
            attempt = 0
            seg_done_ok = False
            while not seg_done_ok:
                try:
                    for ev in _stream_single_segment(
                        cur_msgs, cur_max_tok, cur_tmode, stitch_prefix=stitch_next
                    ):
                        if ev.get("type") == "_seg_end":
                            cancelled = bool(ev.get("cancelled"))
                            seg_done_ok = True
                            break
                        yield ev
                except ContextLengthExceededError as ctx_exc:
                    if ctx_exc.detected_limit:
                        self.record_calibrated_context_limit(
                            profile.provider_id, eff_model, ctx_exc.detected_limit
                        )
                    raise
                except Exception as exc:
                    is_trans, reason = _is_transient_exception(exc)
                    if not is_trans or attempt >= eff_max_retries:
                        raise
                    attempt += 1
                    retries_used += 1
                    wait_sec = float(delays[min(attempt - 1, len(delays) - 1)]) if delays else 0.0
                    shed_load = attempt >= 2
                    if shed_load:
                        if cur_max_tok is not None:
                            cur_max_tok = max(1024, min(4096, cur_max_tok // 2))
                        else:
                            cur_max_tok = min(4096, max(2048, int(caps.get("max_output_tokens") or 4096) // 2))
                        cur_tmode = "disabled"
                    # 若流式已输出局部正文后再中断，续接上下文无缝续写避免重复开头
                    existing_c = "".join(content_buf)
                    if existing_c.strip():
                        cur_msgs = self._build_continuation_messages(messages, existing_c)
                        stitch_next = True
                    yield {
                        "type": "retry_wait",
                        "attempt": attempt,
                        "max_retries": eff_max_retries,
                        "wait_sec": wait_sec,
                        "reason": reason,
                        "shed_load": shed_load,
                        "detail": (
                            f"供应商{reason}，正在第 {attempt}/{eff_max_retries} 次自动退避重连"
                            + ("并启用减载降级" if shed_load else "")
                            + "..."
                        ),
                    }
                    if _sleep_interruptible(wait_sec, cancel_check):
                        cancelled = True
                        seg_done_ok = True
                        break

            if cancelled:
                break
            if (
                finish_reason == "length"
                and "".join(content_buf).strip()
                and continuations < eff_max_cont
                and not (cancel_check and cancel_check())
            ):
                continuations += 1
                yield {
                    "type": "status",
                    "stage": "自动续写衔接",
                    "detail": f"输出触及单次长度上限，正在无缝自动续写 ({continuations}/{eff_max_cont})...",
                }
                cur_msgs = self._build_continuation_messages(messages, "".join(content_buf))
                stitch_next = True
                continue
            break

        yield _make_done_event(cancelled)

    def probe_model_capabilities(
        self,
        *,
        provider_id: str = "",
        model: str = "",
        force_refresh: bool = False,
        timeout_sec: float = 20.0,
        allow_fallback: bool = True,
        payload_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """四级分层动态模型能力探查器：L1 元数据 + L2 轻量活体探针 + L3 校准合并 + L4 分级预算."""
        if payload_override:
            prof = self.build_profile_from_payload(
                payload_override, provider_id=provider_id or payload_override.get("provider_id")
            )
            eff_model = _normalize_model_id(
                model or prof.default_model or (prof.models[0] if prof.models else ""),
                prof.provider_type,
                prof.base_url,
            )
            if not eff_model:
                raise ValueError("请指定要探查的模型名称")
        else:
            resolved = self.resolve_provider_and_model(
                provider_id=provider_id, model=model, require_ready=True
            )
            assert resolved is not None
            prof, eff_model = resolved

        # 检查 24h TTL 缓存命中（若草稿修改了 base_url 则跳过旧端点缓存）
        saved_for_cache = self.store.get_llm_provider(prof.provider_id) if payload_override else None
        base_url_changed = bool(
            payload_override
            and saved_for_cache is not None
            and saved_for_cache.base_url != prof.base_url
        )
        cached_probe = None if base_url_changed else _lookup_model_probe(prof.extra, eff_model)
        if not force_refresh and _is_probe_fresh(cached_probe):
            caps = infer_model_capabilities(eff_model, prof.provider_type, prof.base_url, prof.extra)
            return {
                "ok": True,
                "cached": True,
                "probe_ok": True,
                "provider_id": prof.provider_id,
                "provider_name": prof.name,
                "model": eff_model,
                "probe": cached_probe,
                "capabilities": caps,
                "message": (
                    f"{eff_model} 使用已缓存探查档案（窗口 {caps['context_window']:,} · "
                    f"甜蜜区 {caps['effective_reasoning_budget']:,} · {caps['model_tier']}）"
                ),
            }

        l1_meta: Dict[str, Any] = {}
        # L1a: 尝试低开销拉取 /models 元数据
        try:
            client = self._get_sdk_client(prof, timeout_sec=min(6.0, timeout_sec))
            if client is not None:
                listing = client.models.list()
                data_items = getattr(listing, "data", None)
                if data_items is None:
                    data_items = list(listing)
                for it in data_items:
                    raw_id = getattr(it, "id", None) or (it.get("id") if isinstance(it, dict) else "")
                    nm = _normalize_model_id(str(raw_id or ""), prof.provider_type, prof.base_url)
                    if nm.lower() == eff_model.lower():
                        l1_meta.update(_extract_model_meta_from_item(it))
                        break
            else:
                _, meta_map = self._http_fallback_models_with_meta(prof, min(6.0, timeout_sec))
                for k, v in meta_map.items():
                    if k.lower() == eff_model.lower():
                        l1_meta.update(v)
                        break
        except Exception:
            pass

        # L1b: 本地 Ollama /api/show 探测
        ollama_meta = self._probe_ollama_show_meta(prof.base_url, eff_model, timeout_sec=3.0)
        if ollama_meta:
            for k, v in ollama_meta.items():
                l1_meta.setdefault(k, v)

        # L2: 轻量活体探针（固定 SQL 标定短文本，max_tokens=32/64，测 TTFT、token_ratio、Thinking、SSE）
        base_caps = infer_model_capabilities(eff_model, prof.provider_type, prof.base_url, prof.extra)
        probe_max_tok = 64 if base_caps.get("thinking_mode") == "native_always" else 32
        probe_msgs = [{"role": "user", "content": _PROBE_CALIBRATION_SQL}]
        est_prompt_tok = _estimate_probe_tokens(_PROBE_CALIBRATION_SQL)

        live_ok = False
        probe_error = ""
        ttft_ms = 0.0
        tokens_per_sec = 0.0
        token_ratio = 1.0
        stream_supported = True
        observed_thinking: Optional[bool] = None
        calibrated_limit: Optional[int] = None

        t_start = time.perf_counter()
        first_chunk_t: Optional[float] = None
        usage_obj: Dict[str, Any] = {}
        out_text_len = 0

        try:
            for ev in self._stream_probe_with_profile(
                prof,
                messages=probe_msgs,
                model=eff_model,
                max_tokens=probe_max_tok,
                timeout_sec=min(20.0, float(timeout_sec)),
            ):
                et = ev.get("type")
                if et in ("reasoning", "content"):
                    delta = str(ev.get("delta") or "")
                    if delta:
                        if first_chunk_t is None:
                            first_chunk_t = time.perf_counter()
                        out_text_len += len(delta)
                        if et == "reasoning":
                            observed_thinking = True
                elif et == "done":
                    stream_supported = bool(ev.get("stream_supported", True))
                    if isinstance(ev.get("usage"), dict):
                        usage_obj = ev["usage"]
                    if first_chunk_t is None:
                        first_chunk_t = time.perf_counter()
            live_ok = True
        except ContextLengthExceededError as ctx_exc:
            probe_error = str(ctx_exc)
            calibrated_limit = ctx_exc.detected_limit
            if calibrated_limit:
                self.record_calibrated_context_limit(prof.provider_id, eff_model, calibrated_limit)
        except Exception as exc:
            probe_error = str(exc)
            if not allow_fallback:
                raise

        total_sec = max(0.001, time.perf_counter() - t_start)
        if live_ok:
            ttft_ms = round(((first_chunk_t or time.perf_counter()) - t_start) * 1000.0, 1)
            prompt_tokens_srv = 0
            comp_tokens_srv = 0
            try:
                prompt_tokens_srv = int(usage_obj.get("prompt_tokens") or 0)
                comp_tokens_srv = int(usage_obj.get("completion_tokens") or 0)
            except (TypeError, ValueError):
                pass
            if prompt_tokens_srv > 12:
                # 扣除 ChatML 角色封套开销（约 10 tokens）后估算中英混杂 SQL 的真实比率
                net_prompt = max(1, prompt_tokens_srv - 10)
                token_ratio = round(max(0.55, min(2.4, net_prompt / max(1, est_prompt_tok))), 2)
            gen_sec = max(0.05, total_sec - (ttft_ms / 1000.0))
            eff_comp = comp_tokens_srv if comp_tokens_srv > 0 else max(1, out_text_len // 4)
            tokens_per_sec = round(eff_comp / gen_sec, 1)

        probe_record: Dict[str, Any] = dict(l1_meta)
        if calibrated_limit:
            probe_record["context_window"] = calibrated_limit
            probe_record["source"] = "error_calibrated"
        elif live_ok:
            probe_record["ttft_ms"] = ttft_ms
            probe_record["tokens_per_sec"] = tokens_per_sec
            probe_record["token_ratio"] = token_ratio
            probe_record["stream_supported"] = stream_supported
            if observed_thinking is True:
                probe_record["supports_thinking"] = True
            probe_record["source"] = "live_probe"
        elif l1_meta:
            probe_record["source"] = "models_meta"
        else:
            probe_record["source"] = "heuristic"
        probe_record["probed_at"] = datetime.datetime.now().isoformat(timespec="seconds")

        saved_existing = self.store.get_llm_provider(prof.provider_id)
        can_persist_probe = (
            saved_existing is not None
            and (not payload_override or saved_existing.base_url == prof.base_url)
        )
        saved_probe: Optional[Dict[str, Any]] = None
        if can_persist_probe:
            saved_probe = self._persist_model_probe(
                prof.provider_id,
                eff_model,
                probe_record,
                preserve_stronger_source=not live_ok and not bool(calibrated_limit),
            )

        # 重新加载计算最终能力档案；若存在未落盘草稿（如 context_window / model_tier / base_url），保留草稿覆盖
        if payload_override:
            eff_extra = dict((saved_existing.extra if saved_existing else {}) or {})
            if saved_probe is not None:
                probes_tmp = dict(eff_extra.get("model_probes") or {})
                probes_tmp[eff_model] = saved_probe
                eff_extra["model_probes"] = probes_tmp
            else:
                probes_tmp = dict(eff_extra.get("model_probes") or {})
                probes_tmp[eff_model] = probe_record
                eff_extra["model_probes"] = probes_tmp
            if "context_window" in payload_override:
                if "context_window" in prof.extra:
                    eff_extra["context_window"] = prof.extra["context_window"]
                else:
                    eff_extra.pop("context_window", None)
            if "model_tier" in payload_override:
                if "model_tier" in prof.extra:
                    eff_extra["model_tier"] = prof.extra["model_tier"]
                else:
                    eff_extra.pop("model_tier", None)
            caps = infer_model_capabilities(eff_model, prof.provider_type, prof.base_url, eff_extra)
        else:
            refreshed_prof = self.store.get_llm_provider(prof.provider_id) or prof
            eff_extra = dict(refreshed_prof.extra or {})
            if saved_probe is None:
                probes_tmp = dict(eff_extra.get("model_probes") or {})
                probes_tmp[eff_model] = probe_record
                eff_extra["model_probes"] = probes_tmp
            caps = infer_model_capabilities(
                eff_model, refreshed_prof.provider_type, refreshed_prof.base_url, eff_extra
            )
        final_probe = {**(saved_probe or probe_record), **caps}

        return {
            "ok": True,
            "cached": False,
            "probe_ok": live_ok,
            "probe_error": probe_error,
            "provider_id": prof.provider_id,
            "provider_name": prof.name,
            "model": eff_model,
            "probe": final_probe,
            "capabilities": caps,
            "message": (
                f"已探查 {eff_model}：窗口 {caps['context_window']:,} · 甜蜜区 {caps['effective_reasoning_budget']:,} · "
                f"分级 {caps['model_tier']} · TTFT {caps['ttft_ms']:.0f}ms · 比率 {caps['token_ratio']}x"
                if live_ok
                else f"活体探针未达（{probe_error[:80]}），已优雅降级为 {caps['source']} 档案（窗口 {caps['context_window']:,} · 甜蜜区 {caps['effective_reasoning_budget']:,}）"
            ),
        }

    def _stream_probe_with_profile(
        self,
        profile: LLMProviderProfile,
        *,
        messages: List[Dict[str, str]],
        model: str,
        max_tokens: int,
        timeout_sec: float,
    ) -> Iterator[Dict[str, Any]]:
        """供 probe_model_capabilities 使用的轻量流式调用（支持未落盘草稿配置，开启 include_usage，禁用重试与续写）."""
        yield from self._stream_chat_with_profile(
            profile,
            messages=messages,
            model=model,
            temperature=0.0,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            retry_on_transient=False,
            max_retries=0,
            auto_continue_on_length=False,
            include_usage=True,
        )

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
            retry_on_transient=False,
            max_retries=0,
            auto_continue_on_length=False,
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
        meta_by_model: Dict[str, Dict[str, Any]] = {}
        if client is not None:
            listing = client.models.list()
            data_items = getattr(listing, "data", None)
            if data_items is None:
                data_items = list(listing)
            for item in data_items:
                mid = getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else "")
                if mid:
                    raw_ids.append(str(mid))
                    norm_mid = _normalize_model_id(str(mid), prof.provider_type, prof.base_url)
                    ex_meta = _extract_model_meta_from_item(item)
                    if ex_meta and norm_mid:
                        meta_by_model[norm_mid] = ex_meta
        else:
            raw_ids, meta_by_model = self._http_fallback_models_with_meta(prof, timeout_sec=20.0)

        cleaned: List[str] = []
        for mid in raw_ids:
            nm = _normalize_model_id(mid, prof.provider_type, prof.base_url)
            if nm and nm not in cleaned:
                cleaned.append(nm)

        if not cleaned:
            raise ValueError("远端接口未返回任何可用模型列表")

        # 本地 Ollama 端点按需补充 `/api/show` 上下文与参数量元数据（最多探测前 8 个，单次 <= 2s）
        if is_local and ":11434" in (prof.base_url or ""):
            for mid in cleaned[:8]:
                if "context_window" not in (meta_by_model.get(mid) or {}):
                    om = self._probe_ollama_show_meta(prof.base_url, mid, timeout_sec=2.0)
                    if om:
                        meta_by_model.setdefault(mid, {}).update(om)

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        # 若该服务商已存在于数据库，同步更新其模型缓存列表与 L1 元数据（不覆写更强来源）
        saved_existing = self.store.get_llm_provider(prof.provider_id)
        if saved_existing is not None:
            saved_existing.models = cleaned
            if not saved_existing.default_model or saved_existing.default_model not in cleaned:
                saved_existing.default_model = cleaned[0]
            self.store.upsert_llm_provider(saved_existing)
            now_iso = datetime.datetime.now().isoformat(timespec="seconds")
            for mid, mdata in meta_by_model.items():
                if mdata:
                    self._persist_model_probe(
                        prof.provider_id,
                        mid,
                        {**mdata, "source": "models_meta", "probed_at": now_iso},
                        preserve_stronger_source=True,
                    )

        return {
            "ok": True,
            "provider_id": prof.provider_id,
            "models": cleaned,
            "models_meta": meta_by_model,
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
