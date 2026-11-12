"""Data.O.G 全局 AI 大模型统一接入中枢 (LLMHub).

参考 Cherry Studio / LobeChat 成熟的多服务商管理架构，采用零额外第三方依赖的 Python 标准库原生支持 OpenAI 兼容 REST 协议：
1. 内置三大主流预设（DeepSeek 官方端点、Google Gemini 官方 OpenAI 兼容端点、本地/内网 OpenAI 兼容网关）；
2. API Key 仅持久化于本地 SQLite `data/dataog_state.db`，所有对外接口严格脱敏回显，留空或回传掩码时自动保留原密钥；
3. 支持连通性毫秒级测试、远端模型列表拉取（自动兼容剥离 Gemini `models/` 前缀）及结构化 JSON 生成。
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from core.platform.models import LLMProviderProfile
from core.platform.storage import PlatformStateStore

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


def extract_json_payload(raw_text: str) -> Any:
    """从大模型返回文本中稳健提取 JSON 对象或数组（兼容 ```json 代码块包裹）."""
    text = (raw_text or "").strip()
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
        """汇总全部已启用服务商的候选模型列表（供多邻国右上角模型切换器直接使用）."""
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
                items.append(
                    {
                        "value": f"{p.provider_id}::{mid}",
                        "provider_id": p.provider_id,
                        "provider_name": p.name,
                        "model": mid,
                        "has_api_key": bool(p.api_key),
                        "ready": is_ready,
                        "is_default": bool(p.is_default and mid == p.default_model),
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
            extra=dict(existing.extra if existing else {}),
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
    def _request_openai_json(
        method: str,
        endpoint: str,
        api_key: str,
        body: Optional[Dict[str, Any]],
        timeout_sec: float,
    ) -> Dict[str, Any]:
        safe_url = validate_llm_base_url(endpoint)
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "Data.O.G/2.1",
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
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(
            safe_url,
            data=payload_bytes,
            headers=headers,
            method=method.upper(),
        )
        try:
            with opener.open(req, timeout=max(1.0, float(timeout_sec))) as resp:
                raw = resp.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            err_raw = ""
            try:
                err_raw = exc.read(4096).decode("utf-8", errors="replace")
            except Exception:
                pass
            err_detail = err_raw.strip()[:300]
            try:
                parsed_err = json.loads(err_raw)
                if isinstance(parsed_err, dict) and isinstance(parsed_err.get("error"), dict):
                    err_detail = str(parsed_err["error"].get("message") or err_detail)[:300]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {exc.code}: {err_detail or exc.reason}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"连接 AI 服务端失败: {exc.reason}") from None

        try:
            parsed = json.loads(raw)
        except Exception as exc:
            raise RuntimeError("AI 服务端返回了非 JSON 响应") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("AI 服务端返回的 JSON 顶层结构异常")
        return parsed

    def _http_fallback_chat(
        self,
        profile: LLMProviderProfile,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: Optional[int],
        timeout_sec: float,
    ) -> str:
        endpoint = profile.base_url.rstrip("/") + "/chat/completions"
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        data = self._request_openai_json(
            "POST", endpoint, profile.api_key, body, timeout_sec
        )
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else ""
        if isinstance(content, list):
            parts = [
                str(p.get("text") or "")
                for p in content
                if isinstance(p, dict) and p.get("text")
            ]
            return "".join(parts)
        return str(content or "")

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
    ) -> Dict[str, Any]:
        is_local = any(h in (profile.base_url or "") for h in ("127.0.0.1", "localhost"))
        if not profile.api_key and not is_local:
            raise ValueError(f"请先填写 [{profile.name}] 的 API Key")

        t0 = time.perf_counter()
        client = self._get_sdk_client(profile, timeout_sec=timeout_sec)
        if client is not None:
            kwargs: Dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
            }
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens
            resp = client.chat.completions.create(**kwargs)
            content = str((resp.choices[0].message.content if resp.choices else "") or "")
        else:
            content = self._http_fallback_chat(
                profile, messages, model, temperature, max_tokens, timeout_sec
            )
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return {
            "provider_id": profile.provider_id,
            "provider_name": profile.name,
            "model": model,
            "content": content,
            "elapsed_ms": elapsed_ms,
        }

    def test_provider(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        prof = self.build_profile_from_payload(payload, provider_id=payload.get("provider_id"))
        model = prof.default_model or (prof.models[0] if prof.models else "")
        if not model:
            raise ValueError("请至少填写或选择一个要检测的模型名称")
        res = self._execute_chat_with_profile(
            prof,
            messages=[{"role": "user", "content": "Reply with OK only."}],
            model=model,
            temperature=0.0,
            max_tokens=8,
            timeout_sec=20.0,
        )
        return {
            "ok": True,
            "provider_id": prof.provider_id,
            "provider_name": prof.name,
            "model": model,
            "elapsed_ms": res["elapsed_ms"],
            "reply_preview": res["content"].strip()[:40],
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
