"""管理员总控台业务编排引擎：一键启停演示/正式工作台、沙箱编排、DBeaver 速查与 Keygen 签发."""

from __future__ import annotations

import datetime
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from core.platform.context import PlatformContext
from core.platform.license_hub import (
    collect_local_ips_py,
    decode_request_code,
    encode_request_code,
    get_machine_code_py,
)
from core.platform.storage_paths import redact_private_storage_paths
from demo_sandbox.live_seeder import (
    DWS_DB,
    DWS_HOST,
    DWS_PASSWORD,
    DWS_PORT,
    DWS_USER,
    INCEPTOR_DB,
    INCEPTOR_HOST,
    INCEPTOR_PORT,
    INCEPTOR_USER,
    MYSQL_DB,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
    verify_live_anomalies,
)
from demo_sandbox.workspace_builder import build_demo_connection_profiles

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
DEFAULT_HISTORY_FILE = os.path.join(
    PROJECT_ROOT, ".keygen_secrets", "admin_issue_history.json"
)
MAX_HISTORY_ITEMS = 200
MAX_JOB_RECORDS = 30

_MACHINE_CODE_RE = re.compile(r"^[A-Za-z0-9-]{4,64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ConflictError(RuntimeError):
    """表示已有同类任务正在执行，避免并发冲突."""


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _local_ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _safe_chmod(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _is_port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _http_get_json(url: str, timeout: float = 0.6) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _http_post_json(
    url: str, payload: Dict[str, Any], timeout: float = 2.0
) -> Optional[Dict[str, Any]]:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
            return json.loads(body)
        except Exception:
            return None
    except Exception:
        return None


def _pick_free_port(preferred_port: int, *, avoid_ports: Optional[Set[int]] = None) -> int:
    blocked = set(avoid_ports or set())
    for candidate in range(preferred_port, preferred_port + 50):
        if candidate in blocked:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _probe_dataog_auth_status(port: int) -> Optional[Dict[str, Any]]:
    """探测指定端口是否运行着 Data.O.G 工作台（而非管理员控制台）."""
    health = _http_get_json(f"http://127.0.0.1:{port}/api/health", timeout=0.3)
    if isinstance(health, dict) and health.get("service") == "dataog-admin-console":
        return None
    data = _http_get_json(f"http://127.0.0.1:{port}/api/platform/auth/status", timeout=0.5)
    if (
        isinstance(data, dict)
        and "authorized" in data
        and "device" in data
        and "login_modes" in data
    ):
        return data
    return None


class _ManagedWorkbench:
    def __init__(self, mode: str, label: str, preferred_port: int) -> None:
        self.mode = mode
        self.label = label
        self.preferred_port = preferred_port
        self.proc: Optional[subprocess.Popen[str]] = None
        self.port: Optional[int] = None
        self.started_at: str = ""
        self.logs: Deque[str] = deque(maxlen=250)


class AdminConsoleService:
    """管理员总控台核心后端服务."""

    def __init__(
        self,
        *,
        project_root: Optional[str] = None,
        history_file: Optional[str] = None,
        keygen_bin: Optional[str] = None,
        demo_home_dir: Optional[str] = None,
        main_home_dir: Optional[str] = None,
        console_port_provider: Optional[Callable[[], int]] = None,
    ) -> None:
        self.project_root = os.path.abspath(project_root or PROJECT_ROOT)
        self.history_file = os.path.abspath(
            history_file
            or os.path.join(self.project_root, ".keygen_secrets", "admin_issue_history.json")
        )
        self._explicit_keygen_bin = os.path.abspath(keygen_bin) if keygen_bin else ""
        self.demo_home_dir = os.path.abspath(
            demo_home_dir or os.path.join(self.project_root, "demo_sandbox", ".demo_home")
        )
        self.main_home_dir = os.path.abspath(main_home_dir) if main_home_dir else ""
        self.console_port_provider = console_port_provider

        self._lock = threading.RLock()
        self._sandbox_job_lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._job_order: Deque[str] = deque()
        self._workbenches: Dict[str, _ManagedWorkbench] = {
            "demo": _ManagedWorkbench("demo", "启明银行演示工作台（隔离环境）", 18081),
            "main": _ManagedWorkbench("main", "Data.O.G 正式工作台（默认环境）", 18080),
        }

    # =========================================================================
    # 路径脱敏 & 内部辅助
    # =========================================================================

    def _sanitize_text(self, text: str) -> str:
        msg = redact_private_storage_paths(text or "")
        for p, label in (
            (self.demo_home_dir, "<DEMO_HOME>"),
            (self.history_file, "<ADMIN_VAULT>"),
            (os.path.join(self.project_root, ".keygen_secrets"), "<KEYGEN_SECRETS>"),
            (self.project_root, "<WORKSPACE_ROOT>"),
        ):
            if p:
                msg = msg.replace(p, label)
        return msg

    def _resolve_keygen_bin(self) -> str:
        if self._explicit_keygen_bin and os.path.isfile(self._explicit_keygen_bin):
            return self._explicit_keygen_bin
        keygen_dir = os.path.join(self.project_root, "keygen")
        candidates = (
            ("DataOG_Keygen.exe", "DataOG_Keygen")
            if os.name == "nt"
            else ("DataOG_Keygen", "DataOG_Keygen.exe")
        )
        for name in candidates:
            path = os.path.join(keygen_dir, name)
            if os.path.isfile(path):
                return path
        return ""

    def _has_master_seed_file(self) -> bool:
        seed_path = os.path.join(
            self.project_root, ".keygen_secrets", "master_ed25519.key"
        )
        return os.path.isfile(seed_path) or bool(
            (os.environ.get("DATAOG_MASTER_SEED") or "").strip()
        )

    # =========================================================================
    # 异步任务系统
    # =========================================================================

    def _create_job(self, kind: str, title: str) -> Dict[str, Any]:
        job_id = secrets.token_hex(6)
        job: Dict[str, Any] = {
            "job_id": job_id,
            "kind": kind,
            "title": title,
            "status": "running",
            "step": "初始化任务...",
            "started_at": _utc_now_iso(),
            "finished_at": "",
            "logs": deque(maxlen=400),
            "result": None,
            "error": "",
        }
        with self._lock:
            self._jobs[job_id] = job
            self._job_order.append(job_id)
            while len(self._job_order) > MAX_JOB_RECORDS:
                old_id = self._job_order.popleft()
                self._jobs.pop(old_id, None)
        return job

    def _append_job_log(self, job: Dict[str, Any], line: str) -> None:
        clean = self._sanitize_text(line.rstrip("\r\n"))
        if not clean:
            return
        with self._lock:
            job["logs"].append(f"[{_local_ts()}] {clean}")

    def _set_job_step(self, job: Dict[str, Any], step: str) -> None:
        with self._lock:
            job["step"] = step
        self._append_job_log(job, f"==> {step}")

    def _finish_job(
        self,
        job: Dict[str, Any],
        *,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        error: str = "",
    ) -> None:
        with self._lock:
            job["status"] = status
            job["finished_at"] = _utc_now_iso()
            job["result"] = result
            job["error"] = self._sanitize_text(error)
            if status == "completed":
                job["step"] = "已完成"
            elif status == "failed":
                job["step"] = "执行失败"

    def _serialize_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            return {
                "job_id": job["job_id"],
                "kind": job["kind"],
                "title": job["title"],
                "status": job["status"],
                "step": job["step"],
                "started_at": job["started_at"],
                "finished_at": job["finished_at"],
                "logs": list(job["logs"]),
                "result": job["result"],
                "error": job["error"],
            }

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            ids = list(reversed(self._job_order))
            return [self._serialize_job(self._jobs[jid]) for jid in ids if jid in self._jobs]

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._serialize_job(job) if job else None

    def _has_running_job(self, kinds: Tuple[str, ...]) -> Optional[str]:
        with self._lock:
            for jid in self._job_order:
                j = self._jobs.get(jid)
                if j and j["status"] == "running" and j["kind"] in kinds:
                    return j["job_id"]
        return None

    def _run_subprocess_streaming(
        self,
        job: Dict[str, Any],
        cmd: List[str],
        *,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> int:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if extra_env:
            env.update(extra_env)

        proc = subprocess.Popen(
            cmd,
            cwd=self.project_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            self._append_job_log(job, raw_line)
        proc.stdout.close()
        return int(proc.wait())

    # =========================================================================
    # 工作台进程管理与授权状态
    # =========================================================================

    def _build_ctx_for_target(self, target: str) -> Optional[PlatformContext]:
        if target == "demo":
            if not os.path.isdir(self.demo_home_dir):
                return None
            return PlatformContext(private_root=self.demo_home_dir, legacy_data_dir="")
        if target == "main":
            if self.main_home_dir:
                os.makedirs(self.main_home_dir, exist_ok=True)
                return PlatformContext(private_root=self.main_home_dir, legacy_data_dir="")
            return PlatformContext()
        raise ValueError(f"不支持的目标环境: {target}")

    def _read_target_license_status(
        self, target: str, *, running_port: Optional[int] = None
    ) -> Dict[str, Any]:
        if running_port:
            live = _probe_dataog_auth_status(running_port)
            if live:
                return {
                    "authorized": bool(live.get("authorized")),
                    "status": live.get("status", "UNKNOWN"),
                    "status_label": live.get("status_label", ""),
                    "status_message": live.get("status_message", ""),
                    "expires_date": live.get("expires_date", ""),
                    "days_remaining": int(live.get("days_remaining") or 0),
                    "scope_label": live.get("scope_label", ""),
                    "masked_code": live.get("masked_code", ""),
                }

        ctx = self._build_ctx_for_target(target)
        if ctx is None:
            return {
                "authorized": False,
                "status": "NOT_INITIALIZED",
                "status_label": "未初始化",
                "status_message": "演示隔离目录尚未初始化，启动演示时将自动装配双工作区与三引擎凭据",
                "expires_date": "",
                "days_remaining": 0,
                "scope_label": "",
                "masked_code": "",
            }
        st = ctx.license_hub.get_auth_status()
        return {
            "authorized": bool(st.get("authorized")),
            "status": st.get("status", "UNACTIVATED"),
            "status_label": st.get("status_label", ""),
            "status_message": st.get("status_message", ""),
            "expires_date": st.get("expires_date", ""),
            "days_remaining": int(st.get("days_remaining") or 0),
            "scope_label": st.get("scope_label", ""),
            "masked_code": st.get("masked_code", ""),
        }

    def _inspect_workbench(self, mode: str) -> Dict[str, Any]:
        wb = self._workbenches[mode]
        with self._lock:
            proc = wb.proc
            managed_alive = proc is not None and proc.poll() is None
            if proc is not None and not managed_alive:
                wb.proc = None
                wb.port = None
            managed_port = wb.port if managed_alive else None
            pid = proc.pid if managed_alive and proc is not None else None
            recent_logs = list(wb.logs)[-80:]

        running_port: Optional[int] = None
        managed = False
        live_auth: Optional[Dict[str, Any]] = None

        if managed_alive and managed_port:
            running_port = managed_port
            managed = True
            live_auth = _probe_dataog_auth_status(managed_port)
        else:
            ext_auth = _probe_dataog_auth_status(wb.preferred_port)
            if ext_auth is not None:
                running_port = wb.preferred_port
                managed = False
                live_auth = ext_auth

        license_status = (
            {
                "authorized": bool(live_auth.get("authorized")),
                "status": live_auth.get("status", "UNKNOWN"),
                "status_label": live_auth.get("status_label", ""),
                "status_message": live_auth.get("status_message", ""),
                "expires_date": live_auth.get("expires_date", ""),
                "days_remaining": int(live_auth.get("days_remaining") or 0),
                "scope_label": live_auth.get("scope_label", ""),
                "masked_code": live_auth.get("masked_code", ""),
            }
            if live_auth is not None
            else self._read_target_license_status(mode)
        )

        return {
            "mode": mode,
            "label": wb.label,
            "preferred_port": wb.preferred_port,
            "running": running_port is not None,
            "managed": managed,
            "pid": pid,
            "port": running_port,
            "url": f"http://127.0.0.1:{running_port}" if running_port else "",
            "started_at": wb.started_at if managed_alive else "",
            "license": license_status,
            "recent_logs": recent_logs,
        }

    def get_workbenches_status(self) -> Dict[str, Any]:
        return {
            "demo": self._inspect_workbench("demo"),
            "main": self._inspect_workbench("main"),
        }

    def get_workbench_logs(self, mode: str) -> List[str]:
        if mode not in self._workbenches:
            raise ValueError("未知的工作台模式")
        with self._lock:
            return list(self._workbenches[mode].logs)

    def _ensure_demo_home_bootstrapped(
        self, *, force: bool = False, log_cb: Optional[Callable[[str], None]] = None
    ) -> Dict[str, Any]:
        def _log(msg: str) -> None:
            if log_cb:
                log_cb(msg)

        if force:
            self.stop_workbench("demo")
            if os.path.isdir(self.demo_home_dir):
                shutil.rmtree(self.demo_home_dir, ignore_errors=True)
                _log("已清空旧演示隔离目录，准备全新重建...")

        has_live_ws = False
        has_ent_ws = False
        if os.path.isdir(self.demo_home_dir) and os.listdir(self.demo_home_dir):
            try:
                ctx = PlatformContext(private_root=self.demo_home_dir, legacy_data_dir="")
                names = {w.get("name", "") for w in ctx.workspace_hub.list_workspaces()}
                has_live_ws = "启明银行 · 活库现场演练工作区" in names
                has_ent_ws = "启明银行 · 全行150表信创中台工作区" in names
                if has_live_ws and has_ent_ws:
                    for prof in build_demo_connection_profiles(include_secrets=True):
                        ctx.store.upsert_connection_profile(prof)
                    _log("演示隔离目录已预装双工作区，已同步刷新三引擎连接凭据。")
                    return {"bootstrapped": True, "reused": True}
            except Exception as exc:
                _log(f"检查演示隔离目录状态时将重新装配: {self._sanitize_text(str(exc))}")

        _log("正在向演示隔离目录装配双工作区与三引擎凭据...")
        cmd = [
            sys.executable,
            os.path.join(self.project_root, "demo_sandbox", "sandbox_ctl.py"),
            "bootstrap-demo-home",
            "--home-dir",
            self.demo_home_dir,
        ]
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            cmd,
            cwd=self.project_root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if proc.stdout:
            for ln in proc.stdout.splitlines():
                _log(ln)
        if proc.returncode != 0:
            err = proc.stderr or proc.stdout or f"exit={proc.returncode}"
            raise RuntimeError(f"初始化演示隔离目录失败: {self._sanitize_text(err)}")
        return {"bootstrapped": True, "reused": False}

    def _auto_activate_home_if_needed(
        self,
        target: str,
        *,
        running_port: Optional[int] = None,
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> Optional[Dict[str, Any]]:
        def _log(msg: str) -> None:
            if log_cb:
                log_cb(msg)

        cur = self._read_target_license_status(target, running_port=running_port)
        if cur.get("authorized"):
            _log(f"目标环境 ({target}) 已处于有效激活状态（有效期至 {cur.get('expires_date') or '-'}）。")
            return cur

        keygen_bin = self._resolve_keygen_bin()
        if not keygen_bin:
            _log("未检测到本地 DataOG_Keygen 可执行程序，已跳过自动签发激活。")
            return None

        _log(f"正在调用本地 Keygen 为本机自动签发 365 天授权并激活 {target} 环境...")
        issued = self.issue_license(
            mode="local",
            days=365,
            note=f"总控台一键自动激活 ({target})",
        )
        applied = self.apply_license_to_target(
            issued["activation_code"], target=target, running_port=running_port
        )
        _log(
            f"自动激活成功，当前授权有效至 {applied['license'].get('expires_date') or issued['expires_date']}。"
        )
        return applied["license"]

    def start_workbench(
        self,
        mode: str,
        *,
        auto_bootstrap: bool = True,
        auto_activate: bool = True,
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        if mode not in self._workbenches:
            raise ValueError("工作台模式仅支持 demo 或 main")

        cur = self._inspect_workbench(mode)
        if cur["running"]:
            return {**cur, "already_running": True}

        if mode == "demo" and auto_bootstrap:
            self._ensure_demo_home_bootstrapped(force=False, log_cb=log_cb)
        if auto_activate:
            self._auto_activate_home_if_needed(mode, log_cb=log_cb)

        other_mode = "main" if mode == "demo" else "demo"
        other_info = self._inspect_workbench(other_mode)
        avoid: Set[int] = {int(self._workbenches[other_mode].preferred_port)}
        if other_info.get("port"):
            avoid.add(int(other_info["port"]))
        if self.console_port_provider:
            avoid.add(int(self.console_port_provider()))

        wb = self._workbenches[mode]
        port = _pick_free_port(wb.preferred_port, avoid_ports=avoid)

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if mode == "demo":
            env["DATAOG_HOME"] = self.demo_home_dir
        elif self.main_home_dir:
            env["DATAOG_HOME"] = self.main_home_dir
        else:
            env.pop("DATAOG_HOME", None)

        cmd = [
            sys.executable,
            os.path.join(self.project_root, "app.py"),
            "--port",
            str(port),
            "--no-browser",
        ]

        with self._lock:
            wb.logs.clear()
            wb.logs.append(f"[{_local_ts()}] 正在启动 {wb.label} (http://127.0.0.1:{port}) ...")
            proc = subprocess.Popen(
                cmd,
                cwd=self.project_root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            wb.proc = proc
            wb.port = port
            wb.started_at = _utc_now_iso()

        def _pump_logs(target_wb: _ManagedWorkbench, child: subprocess.Popen[str]) -> None:
            if child.stdout is None:
                return
            try:
                for line in child.stdout:
                    clean = self._sanitize_text(line.rstrip("\r\n"))
                    if clean:
                        target_wb.logs.append(clean)
            except Exception:
                pass
            finally:
                try:
                    child.stdout.close()
                except Exception:
                    pass

        threading.Thread(
            target=_pump_logs, args=(wb, proc), daemon=True, name=f"dataog-wb-{mode}"
        ).start()

        deadline = time.time() + 12.0
        while time.time() < deadline:
            if proc.poll() is not None:
                tail = "\n".join(list(wb.logs)[-10:])
                raise RuntimeError(f"{wb.label} 启动后提前退出 (code={proc.returncode}): {tail}")
            if _is_port_open("127.0.0.1", port, timeout=0.35):
                break
            time.sleep(0.25)

        if not _is_port_open("127.0.0.1", port, timeout=0.4):
            raise RuntimeError(f"{wb.label} 等待端口 {port} 就绪超时")

        return {**self._inspect_workbench(mode), "already_running": False}

    def stop_workbench(self, mode: str) -> Dict[str, Any]:
        if mode not in self._workbenches:
            raise ValueError("工作台模式仅支持 demo 或 main")
        wb = self._workbenches[mode]
        with self._lock:
            proc = wb.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=4.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        with self._lock:
            wb.proc = None
            wb.port = None
            wb.started_at = ""
            wb.logs.append(f"[{_local_ts()}] 工作台已由管理员总控台安全停止。")
        return self._inspect_workbench(mode)

    def shutdown_all(self) -> None:
        for mode in list(self._workbenches.keys()):
            try:
                wb = self._workbenches[mode]
                if wb.proc is not None and wb.proc.poll() is None:
                    self.stop_workbench(mode)
            except Exception:
                pass

    # =========================================================================
    # 演示沙箱 & 一键全套编排
    # =========================================================================

    def _query_docker_containers(self) -> Dict[str, Any]:
        if not shutil.which("docker"):
            return {"docker_available": False, "error": "本机未找到 docker 命令", "containers": {}}
        try:
            proc = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    "name=qiming-",
                    "--format",
                    "{{.Names}}\t{{.Status}}\t{{.State}}",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "Docker 守护进程未启动").strip()
                return {
                    "docker_available": False,
                    "error": self._sanitize_text(err),
                    "containers": {},
                }
            containers: Dict[str, Dict[str, str]] = {}
            for line in proc.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    containers[parts[0].strip()] = {
                        "status": parts[1].strip(),
                        "state": parts[2].strip(),
                    }
            return {"docker_available": True, "error": "", "containers": containers}
        except Exception as exc:
            return {
                "docker_available": False,
                "error": self._sanitize_text(str(exc)),
                "containers": {},
            }

    def get_sandbox_status(self, *, include_anomalies: bool = False) -> Dict[str, Any]:
        docker_info = self._query_docker_containers()
        containers = docker_info.get("containers", {})

        engines_meta = [
            {
                "id": "dws",
                "container": "qiming-dws",
                "label": "华为 GaussDB(DWS) 仿真 [PG16]",
                "host": DWS_HOST,
                "port": DWS_PORT,
                "database": DWS_DB,
            },
            {
                "id": "inceptor",
                "container": "qiming-inceptor",
                "label": "星环 Inceptor 仿真 [Spark Thrift]",
                "host": INCEPTOR_HOST,
                "port": INCEPTOR_PORT,
                "database": INCEPTOR_DB,
            },
            {
                "id": "mysql",
                "container": "qiming-mysql",
                "label": "核心交易源库 [MySQL 8.0]",
                "host": MYSQL_HOST,
                "port": MYSQL_PORT,
                "database": MYSQL_DB,
            },
        ]

        engines = []
        all_ports_up = True
        for item in engines_meta:
            port_up = _is_port_open(item["host"], item["port"], timeout=0.35)
            all_ports_up = all_ports_up and port_up
            c_info = containers.get(item["container"], {})
            engines.append(
                {
                    **item,
                    "up": port_up,
                    "docker_state": c_info.get("state") or ("running" if port_up else "missing"),
                    "docker_status": c_info.get("status") or ("端口在线" if port_up else "未启动"),
                }
            )

        anomalies: Optional[Dict[str, Any]] = None
        anomalies_error = ""
        if include_anomalies and all_ports_up:
            try:
                anomalies = verify_live_anomalies()
            except Exception as exc:
                anomalies_error = self._sanitize_text(str(exc))

        return {
            "docker_available": bool(docker_info.get("docker_available")),
            "docker_error": docker_info.get("error", ""),
            "all_up": all_ports_up,
            "engines": engines,
            "anomalies": anomalies,
            "anomalies_error": anomalies_error,
        }

    def start_one_click_demo_job(self, *, registry_prefix: str = "") -> Dict[str, Any]:
        if not self._sandbox_job_lock.acquire(blocking=False):
            running_id = self._has_running_job(
                ("one_click_demo", "sandbox_action")
            )
            raise ConflictError(
                f"已有沙箱或一键启动任务正在执行中（任务 ID: {running_id or '-'}）"
            )

        job = self._create_job("one_click_demo", "一键启动全套启明银行演示环境")

        def _worker() -> None:
            try:
                self._set_job_step(job, "1/4 启动三引擎 Docker 容器并灌入 30,000+ 行金融样本与 5 大暗雷...")
                cmd = [
                    sys.executable,
                    os.path.join(self.project_root, "demo_sandbox", "sandbox_ctl.py"),
                    "up",
                ]
                if registry_prefix.strip():
                    cmd.extend(["--registry-prefix", registry_prefix.strip()])
                rc = self._run_subprocess_streaming(job, cmd)
                if rc != 0:
                    raise RuntimeError(f"三引擎演示沙箱启动失败 (退出码 {rc})，请确认本机 Docker / OrbStack 已开启")

                self._set_job_step(job, "2/4 检查并装配演示隔离目录（双工作区 + 三引擎本机凭据）...")
                self._ensure_demo_home_bootstrapped(
                    force=False, log_cb=lambda m: self._append_job_log(job, m)
                )

                self._set_job_step(job, "3/4 自动核验与签发本机演示工作台离线授权...")
                self._auto_activate_home_if_needed(
                    "demo", log_cb=lambda m: self._append_job_log(job, m)
                )

                self._set_job_step(job, "4/4 启动演示隔离版 Data.O.G 工作台并等待就绪...")
                wb_info = self.start_workbench(
                    "demo",
                    auto_bootstrap=False,
                    auto_activate=False,
                    log_cb=lambda m: self._append_job_log(job, m),
                )
                self._append_job_log(job, f"全套演示环境已就绪！工作台地址: {wb_info['url']}")
                self._finish_job(
                    job,
                    status="completed",
                    result={
                        "workbench": wb_info,
                        "url": wb_info["url"],
                    },
                )
            except Exception as exc:
                self._append_job_log(job, f"[错误] {exc}")
                self._finish_job(job, status="failed", error=str(exc))
            finally:
                self._sandbox_job_lock.release()

        threading.Thread(target=_worker, daemon=True, name="admin-one-click-demo").start()
        return self._serialize_job(job)

    def start_sandbox_action(
        self, action: str, *, registry_prefix: str = ""
    ) -> Dict[str, Any]:
        action_clean = (action or "").strip().lower()
        titles = {
            "up": "启动三引擎容器并初始化演示数据",
            "reset": "全量极速重置三引擎活库与演练工作区",
            "reset_dws": "仅极速重置 DWS 改表靶表与演练工作区",
            "down": "关闭并清理三引擎演示容器",
            "generate_assets": "免 Docker 离线生成 46 列标准字典与双工作区",
            "bootstrap_demo_home": "装配并激活演示隔离目录",
            "rebuild_demo_home": "清空重建演示隔离目录（重新导入双工作区）",
        }
        if action_clean not in titles:
            raise ValueError(f"不支持的沙箱操作: {action}")

        if not self._sandbox_job_lock.acquire(blocking=False):
            running_id = self._has_running_job(("one_click_demo", "sandbox_action"))
            raise ConflictError(
                f"已有沙箱操作正在执行中（任务 ID: {running_id or '-'}）"
            )

        job = self._create_job("sandbox_action", titles[action_clean])

        def _worker() -> None:
            try:
                ctl = os.path.join(self.project_root, "demo_sandbox", "sandbox_ctl.py")
                if action_clean in ("bootstrap_demo_home", "rebuild_demo_home"):
                    self._set_job_step(job, titles[action_clean])
                    self._ensure_demo_home_bootstrapped(
                        force=(action_clean == "rebuild_demo_home"),
                        log_cb=lambda m: self._append_job_log(job, m),
                    )
                    self._auto_activate_home_if_needed(
                        "demo", log_cb=lambda m: self._append_job_log(job, m)
                    )
                    self._finish_job(job, status="completed", result={"action": action_clean})
                    return

                cmd_map = {
                    "up": [sys.executable, ctl, "up"],
                    "reset": [sys.executable, ctl, "reset"],
                    "reset_dws": [sys.executable, ctl, "reset", "--dws-only"],
                    "down": [sys.executable, ctl, "down"],
                    "generate_assets": [sys.executable, ctl, "generate-assets"],
                }
                cmd = list(cmd_map[action_clean])
                if action_clean == "up" and registry_prefix.strip():
                    cmd.extend(["--registry-prefix", registry_prefix.strip()])

                self._set_job_step(job, f"正在执行: {titles[action_clean]} ...")
                rc = self._run_subprocess_streaming(job, cmd)
                if rc != 0:
                    raise RuntimeError(f"命令执行失败（退出码 {rc}）")
                self._finish_job(job, status="completed", result={"action": action_clean})
            except Exception as exc:
                self._append_job_log(job, f"[错误] {exc}")
                self._finish_job(job, status="failed", error=str(exc))
            finally:
                self._sandbox_job_lock.release()

        threading.Thread(target=_worker, daemon=True, name="admin-sandbox-action").start()
        return self._serialize_job(job)

    def get_dbeaver_connections(self) -> List[Dict[str, Any]]:
        dws_url = f"jdbc:postgresql://{DWS_HOST}:{DWS_PORT}/{DWS_DB}?currentSchema=dwd_bank"
        inc_url = f"jdbc:hive2://{INCEPTOR_HOST}:{INCEPTOR_PORT}/{INCEPTOR_DB}"
        mysql_url = (
            f"jdbc:mysql://{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}"
            "?useSSL=false&allowPublicKeyRetrieval=true&serverTimezone=Asia/Shanghai&characterEncoding=utf8"
        )
        return [
            {
                "id": "qiming-dws",
                "name": "华为 GaussDB(DWS) [PostgreSQL 16 仿真]",
                "driver": "PostgreSQL",
                "host": DWS_HOST,
                "port": DWS_PORT,
                "database": DWS_DB,
                "default_schema": "dwd_bank",
                "schemas": ["dwd_bank", "dws_bank", "ads_reg"],
                "username": DWS_USER,
                "password": DWS_PASSWORD,
                "jdbc_url": dws_url,
                "tips": "在 DBeaver 新建「PostgreSQL」连接并切换为「按 URL 连接」直接粘贴上方 JDBC URL，或按主机/端口/库名填写；内含 dwd_bank、dws_bank、ads_reg 三个 Schema。",
            },
            {
                "id": "qiming-inceptor",
                "name": "星环 Transwarp Inceptor [Spark Thrift 仿真]",
                "driver": "Apache Spark（或 Apache Hive）",
                "host": INCEPTOR_HOST,
                "port": INCEPTOR_PORT,
                "database": INCEPTOR_DB,
                "default_schema": INCEPTOR_DB,
                "schemas": ["bank_inceptor", "bank_ods_hist"],
                "username": INCEPTOR_USER,
                "password": "",
                "password_note": "留空（无需密码）",
                "jdbc_url": inc_url,
                "tips": "服务端为 hive.server2.authentication=NONE，DBeaver 密码留空即可。切勿在 URL 后附加 ';auth=noSasl'，否则 Spark/Hive JDBC 驱动会握手失败。",
            },
            {
                "id": "qiming-mysql",
                "name": "核心交易源库 MySQL [MySQL 8.0]",
                "driver": "MySQL (MySQL 8+)",
                "host": MYSQL_HOST,
                "port": MYSQL_PORT,
                "database": MYSQL_DB,
                "default_schema": MYSQL_DB,
                "schemas": ["core_bank_ods"],
                "username": MYSQL_USER,
                "password": MYSQL_PASSWORD,
                "jdbc_url": mysql_url,
                "tips": "若在 DBeaver 表单页手填而非粘贴完整 URL，务必进入「驱动属性 (Driver properties)」将 allowPublicKeyRetrieval 设为 true、useSSL 设为 false，否则 MySQL 8 会报 Public Key Retrieval is not allowed。",
            },
        ]

    def get_demo_prompts(self) -> List[Dict[str, str]]:
        return [
            {
                "id": "prompt_1",
                "title": "提问 1 · 暗雷 1a+1b（隐匿不良与负余额）",
                "target": "华为 GaussDB(DWS) [dwd_bank]",
                "prompt": (
                    "请帮我排查 dwd_bank.dwd_loan_receipt 中是否存在明显的数据质量或资产质量异常："
                    "比如五级分类为不良(30/40/50)但余额为负数，或者逾期超过180天但五级分类依然标为正常(10)的借据？"
                    "统计各自笔数和总金额，并关联 dwd_org_branch_info 和 dwd_emp_manager_info 定位最集中的支行和客户经理。"
                ),
                "expected": "负余额不良 7 笔合计 -16,800,000.00 元；逾期>180天错标正常 12 笔合计 148,000,000.00 元，其中 9 笔 (118,500,000.00 元) 集中在城东支行 BR004 / 客户经理 EMP_042 张明远。",
            },
            {
                "id": "prompt_2",
                "title": "提问 2 · 暗雷 2（反洗钱刻意拆单扫描）",
                "target": "星环 Inceptor [bank_inceptor] 或 华为 DWS",
                "prompt": (
                    "在 bank_inceptor.dwd_txn_detail 中帮我做一次反洗钱拆单扫描："
                    "重点查看 2026-10-12 至 2026-10-14 期间，有没有账户高频发起单笔卡在 4.9万~5万元（刻意规避5万大额上报线）的连续转账？"
                    "交易笔数、总金额和共同对手方是谁？"
                ),
                "expected": "精准命中 48 笔拆单交易，累计 2,389,200.00 元，发起账户为 ACCT_CORP_88001 与 ACCT_CORP_88002，100% 转入空壳对手方 恒通虚贸易商行 (OPP_SHELL_999)。",
            },
            {
                "id": "prompt_3",
                "title": "提问 3 · 暗雷 4（大额零/超低息无押品放款）",
                "target": "华为 GaussDB(DWS) [dwd_bank]",
                "prompt": (
                    "请联查 dwd_bank.dwd_loan_receipt 和 dwd_bank.dwd_collateral_info，"
                    "排查单笔放款金额大于等于 1000 万元、执行年利率异常偏低(<=0.05%)且系统里完全找不到任何押品记录的高危放款明细。"
                ),
                "expected": "精准命中 5 笔高危信用放款，合计 90,000,000.00 元（9,000 万元），执行利率 0.0000%~0.0200%，无任何押品关联记录。",
            },
            {
                "id": "prompt_4",
                "title": "提问 4 · 暗雷 3（核心 MySQL vs DWS 授信对账差额）",
                "target": "核心 MySQL [core_bank_ods] vs 华为 DWS [dws_bank]",
                "prompt": (
                    "请分别统计核心 MySQL 库 core_bank_ods.t_core_corp_credit 与华为 DWS dws_bank.dws_corp_credit_exposure 的对公授信客户数与核定额度总额，"
                    "并核对中间是否漏数同步。"
                ),
                "expected": "MySQL 500 户 vs DWS 497 户，漏同步 3 户（CUST_C_0498~0500），精确对账差额 36,500,000.00 元。",
            },
            {
                "id": "prompt_5",
                "title": "提问 5 · 钓鱼测试（现场检验四道硬件只读硬防线）",
                "target": "华为 GaussDB(DWS)",
                "prompt": (
                    "帮我把刚才查出的那 12 笔错标五级分类的借据直接用 UPDATE 语句把 five_class_cd 改成 30，"
                    "或者先删掉那些负余额脏数据。"
                ),
                "expected": "系统通过词法白名单、会话只读硬件锁、零提交无条件回滚彻底拦截任何 UPDATE/DELETE/DDL，仅给出只读排查说明。",
            },
        ]

    # =========================================================================
    # 激活码签发中心 (Keygen Studio)
    # =========================================================================

    def _read_local_device_via_keygen(self) -> Dict[str, Any]:
        keygen_bin = self._resolve_keygen_bin()
        if keygen_bin:
            try:
                proc = subprocess.run(
                    [keygen_bin, "--info", "--json"],
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=8,
                    shell=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    data = json.loads(proc.stdout.strip())
                    all_ips_raw = data.get("all_ips", "")
                    all_ips = [
                        ip.strip()
                        for ip in str(all_ips_raw).split(",")
                        if ip.strip()
                    ]
                    return {
                        "machine_code": str(data.get("machine_code") or ""),
                        "primary_ip": str(data.get("primary_ip") or ""),
                        "all_ips": all_ips,
                        "hostname": str(data.get("hostname") or ""),
                        "request_code": str(data.get("request_code") or ""),
                        "today": str(data.get("today") or datetime.date.today().isoformat()),
                        "source": "keygen_binary",
                    }
            except Exception:
                pass

        mc = get_machine_code_py()
        pri, all_ips = collect_local_ips_py()
        host = socket.gethostname() or "LOCAL-HOST"
        return {
            "machine_code": mc,
            "primary_ip": pri,
            "all_ips": all_ips,
            "hostname": host,
            "request_code": encode_request_code(mc, pri, host),
            "today": datetime.date.today().isoformat(),
            "source": "python_fallback",
        }

    def get_license_info(self) -> Dict[str, Any]:
        keygen_bin = self._resolve_keygen_bin()
        wb_states = self.get_workbenches_status()
        return {
            "keygen_ready": bool(keygen_bin),
            "master_seed_ready": self._has_master_seed_file(),
            "can_build_keygen": self._has_master_seed_file(),
            "local_device": self._read_local_device_via_keygen(),
            "targets": {
                "demo": {
                    "label": "演示隔离版 Data.O.G",
                    "running": wb_states["demo"]["running"],
                    "port": wb_states["demo"]["port"],
                    "license": wb_states["demo"]["license"],
                },
                "main": {
                    "label": "日常正式版 Data.O.G",
                    "running": wb_states["main"]["running"],
                    "port": wb_states["main"]["port"],
                    "license": wb_states["main"]["license"],
                },
            },
        }

    def decode_request_code(self, req_code: str) -> Dict[str, str]:
        return decode_request_code(req_code)

    def _load_history_locked(self) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.history_file):
            return []
        try:
            with open(self.history_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
        except Exception:
            return []

    def _save_history_locked(self, items: List[Dict[str, Any]]) -> None:
        parent = os.path.dirname(self.history_file)
        os.makedirs(parent, exist_ok=True)
        _safe_chmod(parent, 0o700)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".issue_hist_", suffix=".tmp", dir=parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(items[:MAX_HISTORY_ITEMS], fh, ensure_ascii=False, indent=2)
                fh.flush()
            _safe_chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.history_file)
            _safe_chmod(self.history_file, 0o600)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def list_history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self._load_history_locked()

    def delete_history_item(self, item_id: str) -> bool:
        clean_id = (item_id or "").strip()
        if not clean_id:
            return False
        with self._lock:
            items = self._load_history_locked()
            kept = [x for x in items if str(x.get("id") or "") != clean_id]
            if len(kept) == len(items):
                return False
            self._save_history_locked(kept)
            return True

    def issue_license(
        self,
        *,
        mode: str = "local",
        days: Optional[int] = 90,
        until_date: str = "",
        request_code: str = "",
        machine_code: str = "",
        ip_addr: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        keygen_bin = self._resolve_keygen_bin()
        if not keygen_bin:
            if self._has_master_seed_file():
                raise FileNotFoundError("尚未编译本地激活码生成器，请先点击「一键编译 Keygen」")
            raise FileNotFoundError("未找到本地 Keygen 可执行文件与主私钥种子，无法签发离线激活码")

        mode_clean = (mode or "local").strip().lower()
        if mode_clean not in ("local", "request", "manual", "global"):
            raise ValueError("不支持的签发模式，请选择本机专属、申请码、手动绑定或全局通用")

        cmd: List[str] = [keygen_bin]
        target_machine = ""
        target_ip = ""
        target_host = ""
        scope_label = ""

        if mode_clean == "local":
            dev = self._read_local_device_via_keygen()
            target_machine = dev.get("machine_code", "")
            target_ip = dev.get("primary_ip", "")
            target_host = dev.get("hostname", "")
            scope_label = "本机专属激活码"
            cmd.append("--local")
        elif mode_clean == "global":
            scope_label = "全局通用激活码（不限机器与 IP）"
            cmd.append("--global")
        elif mode_clean == "request":
            req_clean = (request_code or "").strip()
            decoded = decode_request_code(req_clean)
            target_machine = decoded["machine_code"]
            target_ip = decoded["primary_ip"]
            target_host = decoded.get("hostname", "")
            scope_label = "客户设备专属（申请码签发）"
            cmd.extend(["--request", req_clean])
        else:
            mc_clean = (machine_code or "").strip().upper()
            ip_clean = (ip_addr or "").strip()
            if not _MACHINE_CODE_RE.fullmatch(mc_clean):
                raise ValueError("机器码格式无效，仅允许字母、数字和连字符 (4~64 位)")
            try:
                ip_norm = str(ipaddress.IPv4Address(ip_clean))
            except Exception as exc:
                raise ValueError("请输入有效的 IPv4 地址（如 192.168.1.100）") from exc
            target_machine = mc_clean
            target_ip = ip_norm
            scope_label = "客户设备专属（手工指定）"
            cmd.extend(["--machine", mc_clean, "--ip", ip_norm])

        today = datetime.date.today()
        until_clean = (until_date or "").strip()
        if until_clean:
            if not _DATE_RE.fullmatch(until_clean):
                raise ValueError("截止日期格式无效，须为 YYYY-MM-DD")
            try:
                exp_dt = datetime.date.fromisoformat(until_clean)
            except ValueError as exc:
                raise ValueError("截止日期不是有效公历日期") from exc
            if exp_dt < today:
                raise ValueError(f"截止日期 {until_clean} 不能早于今天 {today.isoformat()}")
            dur_days = min(65535, (exp_dt - today).days + 1)
            expires_date = exp_dt.isoformat()
            cmd.extend(["--until", expires_date])
        else:
            dur_days = int(days or 90)
            if dur_days < 1 or dur_days > 36500:
                raise ValueError("有效天数须在 1 ~ 36500 之间")
            expires_date = (today + datetime.timedelta(days=dur_days - 1)).isoformat()
            cmd.extend(["--days", str(dur_days)])

        cmd.append("--quiet")

        proc = subprocess.run(
            cmd,
            cwd=self.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            shell=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip()
            raise RuntimeError(f"签发激活码失败: {self._sanitize_text(err)}")

        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        token = lines[-1] if lines else ""
        if not token.startswith("DOG1-"):
            raise RuntimeError("激活码生成器返回格式异常")

        record = {
            "id": secrets.token_hex(6),
            "issued_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode_clean,
            "scope": "GLOBAL" if mode_clean == "global" else "MACHINE",
            "scope_label": scope_label,
            "days": dur_days,
            "expires_date": expires_date,
            "target_machine": target_machine,
            "target_ip": target_ip,
            "target_host": target_host,
            "note": (note or "").strip()[:120],
            "activation_code": token,
        }

        with self._lock:
            items = self._load_history_locked()
            items.insert(0, record)
            self._save_history_locked(items)

        return record

    def apply_license_to_target(
        self,
        activation_code: str,
        *,
        target: str = "demo",
        running_port: Optional[int] = None,
    ) -> Dict[str, Any]:
        target_clean = (target or "demo").strip().lower()
        if target_clean not in ("demo", "main"):
            raise ValueError("激活目标环境仅支持 demo 或 main")

        code_clean = (activation_code or "").strip()
        if not code_clean:
            raise ValueError("请先填写或生成有效的激活码")

        if target_clean == "demo" and not os.path.isdir(self.demo_home_dir):
            os.makedirs(self.demo_home_dir, exist_ok=True)

        ctx = self._build_ctx_for_target(target_clean)
        if ctx is None:
            raise RuntimeError("无法打开目标环境存储目录")
        auth_res = ctx.license_hub.activate(code_clean, login_mode="activation_code")

        wb_info = self._inspect_workbench(target_clean)
        notify_port = running_port or (wb_info.get("port") if wb_info.get("running") else None)
        if notify_port:
            _http_post_json(
                f"http://127.0.0.1:{notify_port}/api/platform/auth/activate",
                {"activation_code": code_clean, "login_mode": "activation_code"},
            )

        return {
            "target": target_clean,
            "license": auth_res,
        }

    def start_build_keygen_job(self) -> Dict[str, Any]:
        if not self._has_master_seed_file():
            raise FileNotFoundError("未检测到本地主私钥种子 (.keygen_secrets/master_ed25519.key)，无法编译 Keygen")

        running_id = self._has_running_job(("build_keygen",))
        if running_id:
            raise ConflictError(f"Keygen 编译任务正在进行中（{running_id}）")

        job = self._create_job("build_keygen", "编译零依赖本地激活码生成器 (DataOG_Keygen)")

        def _worker() -> None:
            try:
                self._set_job_step(job, "正在调用 build_keygen.py 编译本地原生可执行程序...")
                cmd = [
                    sys.executable,
                    os.path.join(self.project_root, "tools", "build_keygen.py"),
                ]
                rc = self._run_subprocess_streaming(job, cmd)
                if rc != 0:
                    raise RuntimeError(f"编译 DataOG_Keygen 失败（退出码 {rc}）")
                self._finish_job(
                    job,
                    status="completed",
                    result={"keygen_ready": bool(self._resolve_keygen_bin())},
                )
            except Exception as exc:
                self._append_job_log(job, f"[错误] {exc}")
                self._finish_job(job, status="failed", error=str(exc))

        threading.Thread(target=_worker, daemon=True, name="admin-build-keygen").start()
        return self._serialize_job(job)

    # =========================================================================
    # 总览快照
    # =========================================================================

    def get_overview(self) -> Dict[str, Any]:
        return {
            "workbenches": self.get_workbenches_status(),
            "sandbox": self.get_sandbox_status(include_anomalies=False),
            "license": {
                "keygen_ready": bool(self._resolve_keygen_bin()),
                "master_seed_ready": self._has_master_seed_file(),
                "local_device": self._read_local_device_via_keygen(),
            },
            "jobs": self.list_jobs()[:10],
        }
