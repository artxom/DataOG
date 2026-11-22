"""管理员总控台认证与会话管理 (`PBKDF2-HMAC-SHA256` + `HttpOnly` 本地会话)."""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
DEFAULT_AUTH_FILE = os.path.join(
    PROJECT_ROOT, ".keygen_secrets", "admin_console_auth.json"
)

PBKDF2_ITERATIONS = 600_000
SESSION_COOKIE_NAME = "dataog_admin_session"
SESSION_TTL_SECONDS = 12 * 3600
MAX_FAILED_ATTEMPTS = 5
RATE_LIMIT_WINDOW_SEC = 60.0

_DUMMY_SALT = b"dataog-admin-timing-pad"


def _pbkdf2_hash(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        int(iterations),
        dklen=32,
    )
    return dk.hex()


def _safe_chmod(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class AdminAuthManager:
    """管理本地控制台管理员凭据与服务端会话令牌."""

    def __init__(self, auth_file: Optional[str] = None) -> None:
        self.auth_file = os.path.abspath(auth_file or DEFAULT_AUTH_FILE)
        self._lock = threading.RLock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._failed_attempts: Dict[str, Deque[float]] = {}

    def _load_record(self) -> Optional[Dict[str, Any]]:
        if not os.path.isfile(self.auth_file):
            return None
        try:
            with open(self.auth_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if (
                isinstance(data, dict)
                and isinstance(data.get("username"), str)
                and isinstance(data.get("salt"), str)
                and isinstance(data.get("password_hash"), str)
            ):
                return data
        except Exception:
            return None
        return None

    def is_initialized(self) -> bool:
        with self._lock:
            if self._load_record() is not None:
                return True
            env_user = (os.environ.get("DATAOG_ADMIN_USER") or "").strip()
            env_pass = os.environ.get("DATAOG_ADMIN_PASSWORD") or ""
            return bool(env_user and env_pass)

    @staticmethod
    def _validate_username(username: str) -> str:
        clean = (username or "").strip()
        if len(clean) < 2 or len(clean) > 64:
            raise ValueError("管理员账号长度须在 2 ~ 64 个字符之间")
        if any(ord(ch) < 32 for ch in clean):
            raise ValueError("管理员账号包含非法控制字符")
        return clean

    @staticmethod
    def _validate_password(password: str) -> str:
        if not isinstance(password, str) or len(password) < 6 or len(password) > 256:
            raise ValueError("管理员密码长度须在 6 ~ 256 个字符之间")
        return password

    def set_credentials(self, username: str, password: str) -> Dict[str, Any]:
        clean_user = self._validate_username(username)
        clean_pass = self._validate_password(password)

        salt_bytes = secrets.token_bytes(16)
        pw_hash = _pbkdf2_hash(clean_pass, salt_bytes, PBKDF2_ITERATIONS)
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        payload = {
            "version": 1,
            "username": clean_user,
            "salt": salt_bytes.hex(),
            "iterations": PBKDF2_ITERATIONS,
            "password_hash": pw_hash,
            "updated_at": now_iso,
        }

        parent_dir = os.path.dirname(self.auth_file)
        os.makedirs(parent_dir, exist_ok=True)
        _safe_chmod(parent_dir, 0o700)

        with self._lock:
            fd, tmp_path = tempfile.mkstemp(
                prefix=".admin_auth_", suffix=".tmp", dir=parent_dir
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                    fh.flush()
                _safe_chmod(tmp_path, 0o600)
                os.replace(tmp_path, self.auth_file)
                _safe_chmod(self.auth_file, 0o600)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            self._sessions.clear()
            self._failed_attempts.clear()

        return {"username": clean_user, "updated_at": now_iso}

    def _check_rate_limit_locked(self, client_key: str, now_mono: float) -> bool:
        bucket = self._failed_attempts.setdefault(client_key or "local", deque())
        while bucket and (now_mono - bucket[0]) > RATE_LIMIT_WINDOW_SEC:
            bucket.popleft()
        return len(bucket) >= MAX_FAILED_ATTEMPTS

    def _record_failure_locked(self, client_key: str, now_mono: float) -> None:
        bucket = self._failed_attempts.setdefault(client_key or "local", deque())
        while bucket and (now_mono - bucket[0]) > RATE_LIMIT_WINDOW_SEC:
            bucket.popleft()
        bucket.append(now_mono)

    def _verify_password_against_store_locked(
        self, username: str, password: str
    ) -> Tuple[bool, str]:
        rec = self._load_record()
        clean_user = (username or "").strip()
        if rec is not None:
            stored_user = str(rec.get("username") or "")
            stored_hash = str(rec.get("password_hash") or "")
            iterations = int(rec.get("iterations") or PBKDF2_ITERATIONS)
            try:
                salt_bytes = bytes.fromhex(str(rec.get("salt") or ""))
            except ValueError:
                salt_bytes = _DUMMY_SALT
            candidate_hash = _pbkdf2_hash(password or "", salt_bytes, iterations)
            user_ok = hmac.compare_digest(
                clean_user.encode("utf-8"), stored_user.encode("utf-8")
            )
            hash_ok = hmac.compare_digest(candidate_hash, stored_hash)
            return bool(user_ok and hash_ok), stored_user

        env_user = (os.environ.get("DATAOG_ADMIN_USER") or "").strip()
        env_pass = os.environ.get("DATAOG_ADMIN_PASSWORD") or ""
        if env_user and env_pass:
            user_ok = hmac.compare_digest(
                clean_user.encode("utf-8"), env_user.encode("utf-8")
            )
            pass_ok = hmac.compare_digest(
                (password or "").encode("utf-8"), env_pass.encode("utf-8")
            )
            return bool(user_ok and pass_ok), env_user

        _pbkdf2_hash(password or "", _DUMMY_SALT, 10_000)
        return False, ""

    def _issue_session_locked(self, username: str) -> str:
        now = time.time()
        expired = [
            tok for tok, meta in self._sessions.items() if meta.get("expires_at", 0) <= now
        ]
        for tok in expired:
            self._sessions.pop(tok, None)

        token = secrets.token_urlsafe(32)
        self._sessions[token] = {
            "username": username,
            "created_at": now,
            "expires_at": now + SESSION_TTL_SECONDS,
        }
        return token

    def verify_login(
        self, username: str, password: str, *, client_key: str = "local"
    ) -> Tuple[bool, str, Optional[str], int]:
        now_mono = time.monotonic()
        with self._lock:
            if not self.is_initialized():
                return False, "管理员账号尚未初始化，请先设置账号密码", None, 403

            if self._check_rate_limit_locked(client_key, now_mono):
                return (
                    False,
                    "登录连续失败次数过多，为防暴力破解已锁定 60 秒",
                    None,
                    429,
                )

            ok, matched_user = self._verify_password_against_store_locked(
                username, password
            )
            if not ok:
                self._record_failure_locked(client_key, now_mono)
                return False, "管理员账号或密码错误", None, 401

            self._failed_attempts.pop(client_key or "local", None)
            token = self._issue_session_locked(matched_user)
            return True, "登录成功", token, 200

    def validate_session(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        now = time.time()
        with self._lock:
            sess = self._sessions.get(token)
            if not sess:
                return None
            if float(sess.get("expires_at") or 0) <= now:
                self._sessions.pop(token, None)
                return None
            sess["expires_at"] = now + SESSION_TTL_SECONDS
            return {"username": sess["username"], "expires_at": sess["expires_at"]}

    def logout(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def change_password(
        self, username: str, old_password: str, new_password: str
    ) -> str:
        with self._lock:
            ok, matched_user = self._verify_password_against_store_locked(
                username, old_password
            )
            if not ok:
                raise ValueError("当前旧密码校验失败")
            self.set_credentials(matched_user or username, new_password)
            return self._issue_session_locked(matched_user or username)
