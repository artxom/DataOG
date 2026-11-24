"""Data.O.G v3.2.0 全链路性能剖析、全栈基准压测与脱敏诊断导出测试."""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import shutil
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from app import create_app
from core.platform.context import PlatformContext
from tests.test_modules_and_api import TestClient


class TestDiagnosticsAndBenchmark(unittest.TestCase):
    def test_request_tracing_headers_spans_lock_timing_and_auth_gate(self) -> None:
        """验证请求 Trace、响应头计时、子阶段 Span、SQLite 锁计时及 Gate 1/2 鉴权隔离."""
        with tempfile.TemporaryDirectory() as tmp:
            priv_root = os.path.join(tmp, "diag_priv_vault")
            app = create_app(private_root=priv_root)
            ctx: PlatformContext = app.state.platform_ctx

            # 1. 未激活状态下，所有诊断路由必须被 Gate 1/2 拦截返回 401
            unauth_client = TestClient(app, auto_activate=False)
            for method, ep in (
                ("GET", "/api/platform/diagnostics/ping"),
                ("GET", "/api/platform/diagnostics/overview"),
                ("GET", "/api/platform/diagnostics/traces/abc123"),
                ("POST", "/api/platform/diagnostics/benchmark"),
                ("POST", "/api/platform/diagnostics/export"),
                ("POST", "/api/platform/diagnostics/clear"),
                ("PUT", "/api/platform/diagnostics/config"),
            ):
                r = unauth_client.request(method, ep, json_body={})
                self.assertEqual(r.status_code, 401, f"{method} {ep} 应拦截未激活请求")
                self.assertEqual(r.json().get("code"), "LICENSE_REQUIRED")

            # 2. 激活后发起正常请求，校验响应头计时与 Span 捕获
            client = TestClient(app)
            boot_resp = client.get("/api/platform/bootstrap")
            self.assertEqual(boot_resp.status_code, 200)
            boot_tid = boot_resp.headers.get("x-dataog-trace-id") or ""
            self.assertTrue(boot_tid)
            self.assertIsNotNone(boot_resp.headers.get("x-dataog-server-ms"))
            self.assertIsNotNone(boot_resp.headers.get("x-dataog-auth-ms"))
            self.assertIsNotNone(boot_resp.headers.get("x-dataog-handler-ms"))
            self.assertIn("total;dur=", boot_resp.headers.get("server-timing") or "")

            # ping 返回 200 且附带 trace 头
            ping_resp = client.get("/api/platform/diagnostics/ping")
            self.assertEqual(ping_resp.status_code, 200)
            self.assertTrue(ping_resp.json()["ok"])
            self.assertTrue(ping_resp.headers.get("x-dataog-trace-id"))

            # 读取 bootstrap 完整 Span 瀑布
            tr_resp = client.get(f"/api/platform/diagnostics/traces/{boot_tid}")
            self.assertEqual(tr_resp.status_code, 200)
            trace_data = tr_resp.json()["trace"]
            cats = set((trace_data.get("category_totals") or {}).keys())
            self.assertIn("license", cats)
            self.assertIn("bootstrap", cats)
            span_names = {
                f"{s.get('category')}.{s.get('name')}"
                for s in (trace_data.get("spans") or [])
            }
            self.assertIn("bootstrap.connections", span_names)
            self.assertIn("bootstrap.catalog_trees", span_names)
            self.assertIn("bootstrap.license", span_names)

            # 不存在的 trace_id 返回 404
            self.assertEqual(
                client.get("/api/platform/diagnostics/traces/nonexistent999").status_code,
                404,
            )

            # 3. 验证 _TimedRLock 锁竞争计时（后台持锁 0.15s，确保在 Windows 定时器粒度下产生确定性等待）
            held = threading.Event()

            def _hold_store_lock() -> None:
                with ctx.store._lock:
                    held.set()
                    time.sleep(0.15)

            thr = threading.Thread(target=_hold_store_lock)
            thr.start()
            self.assertTrue(held.wait(timeout=2.0))
            with ctx.store._lock:
                pass
            _ = client.get("/api/platform/connections")
            thr.join(timeout=2.0)

            ov = client.get("/api/platform/diagnostics/overview").json()
            self.assertTrue(ov["ok"])
            self.assertGreater(ov["sqlite_stats"]["lock_acquires"], 0)
            self.assertGreaterEqual(ov["sqlite_stats"]["lock_wait_max_ms"], 20.0)
            self.assertGreater(ov["sqlite_stats"]["private_opens"], 0)
            self.assertGreater(ov["sqlite_stats"]["workspace_opens"], 0)

            # 4. 配置慢请求阈值与清空采样
            cfg_res = client.put(
                "/api/platform/diagnostics/config",
                json={"slow_threshold_ms": 120.0},
            )
            self.assertEqual(cfg_res.status_code, 200)
            self.assertEqual(cfg_res.json()["slow_threshold_ms"], 120.0)

            clr_res = client.post("/api/platform/diagnostics/clear")
            self.assertEqual(clr_res.status_code, 200)
            # 原先的 bootstrap trace 已被清空；仅保留 /clear 本次收尾请求（或 0 条）
            self.assertEqual(
                client.get(f"/api/platform/diagnostics/traces/{boot_tid}").status_code,
                404,
            )
            ov_after = client.get("/api/platform/diagnostics/overview").json()
            self.assertLessEqual(ov_after["summary"]["recent_count"], 2)
            self.assertNotIn(
                boot_tid,
                [t.get("trace_id") for t in ov_after["recent_traces"]],
            )

    def test_full_benchmark_auto_findings_and_strict_redacted_export_bundle(self) -> None:
        """验证全栈 Benchmark（含临时文件清理）、自动瓶颈研判与导出包 100% 零路径/零密钥脱敏."""
        db_secret = "Sup3rSecretDbPass!2026"
        llm_secret = "sk-supersecretkey99999xyz"
        bearer_secret = "Bearer TopSecretTokenValue12345"

        with tempfile.TemporaryDirectory() as tmp:
            priv_root = os.path.join(tmp, "secret_diag_vault_root")
            app = create_app(private_root=priv_root)
            ctx: PlatformContext = app.state.platform_ctx
            client = TestClient(app)

            # 写入 SQLite 连接与含密凭据 + AI API Key + 含敏感信息服务日志行
            sqlite_file = os.path.join(tmp, "sample_local.db")
            with contextlib.closing(sqlite3.connect(sqlite_file)) as sconn:
                sconn.execute("CREATE TABLE t_demo (id INTEGER PRIMARY KEY, val TEXT)")
                sconn.commit()

            ctx.connection_hub.save_profile(
                {
                    "name": "本地测试SQLite",
                    "db_type": "SQLITE",
                    "sqlite_path": sqlite_file,
                }
            )
            ctx.connection_hub.save_profile(
                {
                    "name": "内网DWS库",
                    "db_type": "DWS",
                    "host": "10.99.88.77",
                    "port": 8000,
                    "dbname": "prod_dw",
                    "user": "etl_user",
                    "password": db_secret,
                }
            )
            ctx.llm_hub.save_provider(
                {
                    "provider_id": "deepseek",
                    "api_key": llm_secret,
                    "enabled": True,
                },
                provider_id="deepseek",
            )
            ctx.store.add_log(
                "ERROR",
                "连接测试",
                f"模拟出错日志 password={db_secret} path={priv_root} {bearer_secret}",
            )

            log_dir = os.path.join(priv_root, "rt_log")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "rt_svc.log"), "w", encoding="utf-8") as fh:
                fh.write(f"[12:00:00] [INFO   ] 启动路径 {priv_root} key={llm_secret}\n")

            # 先发一次 bootstrap 记录 trace_id
            boot_resp = client.get("/api/platform/bootstrap")
            boot_tid = boot_resp.headers.get("x-dataog-trace-id") or ""
            self.assertTrue(boot_tid)

            # 运行全栈基准测试（覆盖 SQLite 连接探测；外网 LLM 暂不启用以免拖慢单测）
            bench_resp = client.post(
                "/api/platform/diagnostics/benchmark",
                json={"include_connections": False, "include_llm": False},
            )
            self.assertEqual(bench_resp.status_code, 200)
            bench_json = bench_resp.json()
            self.assertTrue(bench_json["ok"])
            bench = bench_json["benchmark"]
            for required_key in (
                "event_loop_lag",
                "threadpool_dispatch",
                "cpu_ed25519",
                "license_probe",
                "dns_and_hostname",
                "disk_io",
                "sqlite_io",
                "bootstrap_profile",
            ):
                self.assertIn(required_key, bench)
            self.assertGreater(bench["disk_io"]["fsync_4k_iterations"], 0)
            self.assertGreater(bench["sqlite_io"]["private_open_close_avg_ms"], 0.0)
            self.assertGreaterEqual(bench["bootstrap_profile"]["json_bytes"], 100)

            # 确认临时基准文件全部清理完毕
            leftover_tmp = [
                fn
                for fn in os.listdir(ctx.workspace_hub.tmp_dir)
                if fn.startswith(".diag_")
            ]
            self.assertEqual(leftover_tmp, [])

            # 模拟环境代理未豁免 127.0.0.1，且前端存在巨大环回差值
            client_snap = {
                "captured_at": "2026-11-24T12:00:00Z",
                "environment": {"user_agent": "MockBrowser/1.0"},
                "last_loopback_benchmark": {
                    "seq_rtt_avg_ms": 480.0,
                    "seq_rtt_p95_ms": 650.0,
                    "seq_network_gap_p95_ms": 640.0,
                },
                "requests": [
                    {
                        "trace_id": boot_tid,
                        "ts_iso": "2026-11-24T12:00:00Z",
                        "method": "GET",
                        "url_path": "/api/platform/bootstrap",
                        "status": 200,
                        "client_total_ms": 1350.0,
                        "fetch_ms": 1340.0,
                        "body_parse_ms": 10.0,
                    }
                ],
            }

            with mock.patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://proxy_user:proxy_pass@10.10.1.5:8080",
                    "NO_PROXY": "",
                    "no_proxy": "",
                },
                clear=False,
            ):
                exp_resp = client.post(
                    "/api/platform/diagnostics/export",
                    json={
                        "client_snapshot": client_snap,
                        "include_service_log": True,
                        "include_execution_logs": True,
                    },
                )

            self.assertEqual(exp_resp.status_code, 200)
            self.assertEqual(exp_resp.headers.get("x-content-type-options"), "nosniff")
            self.assertIn(".json", exp_resp.headers.get("content-disposition") or "")

            raw_export_text = exp_resp.text
            bundle = json.loads(raw_export_text)
            self.assertEqual(bundle["schema_version"], "1.0")
            self.assertIn("environment", bundle)
            self.assertIn("benchmark", bundle)
            self.assertTrue(bundle["recent_service_log_lines"])
            self.assertTrue(bundle["recent_execution_logs"])

            # 验证 traces_merged 正确关联前后端 trace_id 并计算 network_gap_ms
            matched = [
                m for m in bundle["traces_merged"] if m.get("trace_id") == boot_tid
            ]
            self.assertEqual(len(matched), 1)
            self.assertTrue(matched[0]["matched_server"])
            self.assertGreater(matched[0]["network_gap_ms"], 500.0)

            # 验证自动瓶颈研判识别了客户端环回异常与代理未豁免
            finding_codes = {f["code"] for f in bundle["auto_findings"]}
            self.assertIn("CLIENT_LOOPBACK_OVERHEAD", finding_codes)
            self.assertIn("PROXY_NO_BYPASS_LOOPBACK", finding_codes)

            # 验证 proxy URL 已剥离账密
            env_proxies = bundle["environment"]["network_proxy"]["env_proxies"]
            self.assertEqual(env_proxies.get("http_proxy"), "http://10.10.1.5:8080")
            self.assertNotIn("proxy_pass", raw_export_text)
            self.assertNotIn("proxy_user", raw_export_text)

            # 严苛安全红线：绝不泄露任何私有路径、内部混淆文件名、明文密码、API Key 或激活码
            act_code = str(ctx.store.get_meta("license_activation_code", "") or "")
            forbidden_markers = [
                priv_root,
                os.path.basename(ctx.store.private_db_path),
                os.path.basename(ctx.store.workspace_db_path),
                "ws_store",
                "rt_log",
                "rt_tmp",
                ".chk",
                db_secret,
                llm_secret,
                "TopSecretTokenValue12345",
            ]
            if act_code:
                forbidden_markers.append(act_code)
            for marker in forbidden_markers:
                self.assertNotIn(
                    marker,
                    raw_export_text,
                    f"导出诊断包绝不允许泄露敏感信息 ({marker})",
                )
                self.assertNotIn(
                    marker,
                    bench_resp.text,
                    f"Benchmark 响应绝不允许泄露敏感信息 ({marker})",
                )

    def test_frontend_perf_monitor_and_diagnostics_view_via_node(self) -> None:
        """Node.js 真实执行：验证 perf_monitor、http.js 采集差值、DiagnosticsView 压测与导出契约."""
        if getattr(sys, "frozen", False) or not shutil.which("node"):
            return

        from app import resolve_static_dir

        project_root = os.path.dirname(resolve_static_dir())
        node_script = r"""
import path from "node:path";
import { pathToFileURL } from "node:url";

class MockNode {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.style = {};
    this.dataset = {};
    this.className = "";
    this.value = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.open = false;
    this.returnValue = "";
    const self = this;
    this.classList = {
      add(...cs) { self.className = [...new Set([...(self.className||"").split(/\s+/).filter(Boolean), ...cs])].join(" "); },
      remove(...cs) { const s = new Set(cs); self.className = (self.className||"").split(/\s+/).filter(x => x && !s.has(x)).join(" "); },
      toggle(c, f) { const has = (self.className||"").split(/\s+/).includes(c); const on = f === undefined ? !has : Boolean(f); if (on) this.add(c); else this.remove(c); },
      contains(c) { return (self.className||"").split(/\s+/).includes(c); },
    };
  }
  appendChild(c) { if (c) this.children.push(c); return c; }
  replaceChildren(...ns) { this.children = ns.filter(Boolean); }
  setAttribute(k, v) { this.attributes.set(k, String(v)); }
  getAttribute(k) { return this.attributes.get(k) ?? null; }
  addEventListener(e, fn) { (this.listeners.get(e) || this.listeners.set(e, []).get(e)).push(fn); }
  removeEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  showModal() { this.open = true; }
  close(v) { this.open = false; this.returnValue = v || ""; }
  click() { for (const fn of this.listeners.get("click") || []) fn({ stopPropagation() {}, preventDefault() {}, target: this }); }
  remove() {}
}

const byId = new Map();
for (const id of ["global-modal", "global-modal-title", "global-modal-body", "global-modal-footer", "global-modal-close", "toast-root"]) {
  const n = new MockNode(id === "global-modal" ? "dialog" : "div");
  n.id = id;
  byId.set(id, n);
}

let blobDownloaded = false;
globalThis.Node = MockNode;
globalThis.HTMLDialogElement = class HTMLDialogElement {};
if (!globalThis.URL) {
  globalThis.URL = class MockURL {};
}
globalThis.URL.createObjectURL = () => "blob:mock-diag";
globalThis.URL.revokeObjectURL = () => {};
globalThis.Blob = class MockBlob {
  constructor(parts) { this.size = String(parts?.[0] || "").length; }
};
globalThis.window = { addEventListener() {}, removeEventListener() {}, dispatchEvent() {}, innerWidth: 1440, innerHeight: 900 };
globalThis.document = {
  head: new MockNode("head"),
  body: Object.assign(new MockNode("body"), { appendChild(c) { if (c?.tagName === "A") blobDownloaded = true; return c; } }),
  createElement: (tag) => new MockNode(tag),
  createElementNS: (_ns, tag) => new MockNode(tag),
  createTextNode: (t) => Object.assign(new MockNode("#text"), { textContent: String(t) }),
  getElementById: (id) => byId.get(id) || null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  removeEventListener() {},
};

const fetchCalls = [];
globalThis.fetch = async (url, opts = {}) => {
  const u = String(url);
  fetchCalls.push({ url: u, method: opts.method || "GET" });
  if (u.includes("/diagnostics/ping")) {
    return {
      ok: true,
      status: 200,
      headers: new Map([
        ["content-type", "application/json"],
        ["x-dataog-trace-id", "ping_tid_1"],
        ["x-dataog-server-ms", "1.20"],
        ["x-dataog-auth-ms", "0.30"],
        ["x-dataog-handler-ms", "0.80"],
      ]),
      json: async () => ({ ok: true, ts_ms: 1700000000000 }),
    };
  }
  if (u.includes("/diagnostics/benchmark")) {
    return {
      ok: true,
      status: 200,
      headers: new Map([["content-type", "application/json"]]),
      json: async () => ({ ok: true, benchmark: { event_loop_lag: { avg_lag_ms: 0.4, max_lag_ms: 1.1 } } }),
    };
  }
  if (u.includes("/diagnostics/overview")) {
    return {
      ok: true,
      status: 200,
      headers: new Map([["content-type", "application/json"]]),
      json: async () => ({
        ok: true,
        summary: { total_requests: 5, server_p95_ms: 42.0, auth_gate1_avg_ms: 0.5, slow_threshold_ms: 300, recent_count: 1, slow_count: 0 },
        auto_findings: [{ code: "OK_BASELINE", severity: "info", title: "正常", evidence: "ok", suggestion: "none" }],
        environment: { os: { system: "Darwin" }, storage: { storage_drive_type: "POSIX_LOCAL" }, network_proxy: { has_proxy_configured: false, no_proxy_covers_loopback: true }, components: { native_license_shield_loaded: true } },
        sqlite_stats: { lock_wait_max_ms: 0.2 },
        last_benchmark: {
          event_loop_lag: { avg_lag_ms: 0.4, max_lag_ms: 1.1 },
          threadpool_dispatch: { avg_ms: 0.5, max_ms: 1.2 },
          license_probe: { device_probe_cached_ms: 0.1, device_probe_fresh_ms: 2.2 },
          dns_and_hostname: { gethostbyname_ms: 0.5, getaddrinfo_localhost_ms: 0.6 },
          cpu_ed25519: { avg_ms: 3.1 },
          disk_io: { fsync_4k_avg_ms: 1.4, fsync_4k_max_ms: 2.1, write_1mb_mb_s: 320, read_1mb_mb_s: 890 },
          sqlite_io: { private_open_close_avg_ms: 0.8, workspace_open_close_avg_ms: 0.9, temp_wal_write_commit_avg_ms: 1.3 },
          bootstrap_profile: { total_ms: 14.2, json_bytes: 2048 },
        },
        recent_traces: [{ trace_id: "tr_demo_1", started_at: "2026-11-24T12:00:00.000", method: "GET", path: "/api/platform/bootstrap", status_code: 200, total_ms: 14.2, auth_gate1_ms: 0.4, handler_ms: 13.5, spans: [{ category: "bootstrap", name: "connections", start_offset_ms: 1.0, duration_ms: 4.5, is_main_thread: false, thread_name: "AnyIO" }] }],
        slow_traces: [],
      }),
    };
  }
  if (u.includes("/diagnostics/export")) {
    return {
      ok: true,
      status: 200,
      headers: new Map([
        ["content-type", "application/json"],
        ["content-disposition", 'attachment; filename="DataOG_Diagnostics_demo.json"'],
      ]),
      blob: async () => new globalThis.Blob(['{"ok":true}']),
    };
  }
  return {
    ok: true,
    status: 200,
    headers: new Map([
      ["content-type", "application/json"],
      ["x-dataog-trace-id", "tr_demo_1"],
      ["x-dataog-server-ms", "14.20"],
      ["x-dataog-auth-ms", "0.40"],
      ["x-dataog-handler-ms", "13.50"],
    ]),
    json: async () => ({ ok: true }),
  };
};

const root = process.argv[1];
const imp = (rel) => import(pathToFileURL(path.join(root, rel)).href);

const { requestJson } = await imp("static/js/core/http.js");
const { perfMonitor } = await imp("static/js/core/perf_monitor.js");
const { SettingsView } = await imp("static/js/views/settings_view.js");

await requestJson("/api/platform/connections");
const snap0 = perfMonitor.collectClientSnapshot();
if (snap0.requests.length !== 1 || snap0.requests[0].trace_id !== "tr_demo_1" || snap0.requests[0].server_total_ms !== 14.2) {
  throw new Error("Expected perfMonitor to record trace headers from requestJson");
}

const sv = new SettingsView();
const host = new MockNode("div");
sv.mount(host, { section: "diagnostics" });
await new Promise((r) => setTimeout(r, 20));
if (!fetchCalls.some((c) => c.url.includes("/diagnostics/overview"))) {
  throw new Error("Expected SettingsView.mount({section:'diagnostics'}) to fetch overview");
}

await sv.diagnosticsPane.runFullBenchmark();
if (!perfMonitor.lastLoopbackBenchmark || perfMonitor.lastLoopbackBenchmark.seq_count !== 10 || perfMonitor.lastLoopbackBenchmark.conc_count !== 6) {
  throw new Error("Expected runFullBenchmark to execute 10 sequential + 6 concurrent loopback pings");
}
if (!fetchCalls.some((c) => c.url.includes("/diagnostics/benchmark"))) {
  throw new Error("Expected POST /diagnostics/benchmark call");
}

await sv.diagnosticsPane.exportBundle();
if (!blobDownloaded || !fetchCalls.some((c) => c.url.includes("/diagnostics/export"))) {
  throw new Error("Expected exportBundle to download diagnostics JSON");
}

await sv.diagnosticsPane.openTraceWaterfallModal(sv.diagnosticsPane.overview.recent_traces[0]);
if (!byId.get("global-modal").open) {
  throw new Error("Expected openTraceWaterfallModal to open native <dialog>");
}

console.log("DIAGNOSTICS_FRONTEND_E2E_OK");
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", node_script, project_root],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node 前端诊断验证失败:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("DIAGNOSTICS_FRONTEND_E2E_OK", proc.stdout)

    def test_benchmark_optional_connections_and_llm_probes(self) -> None:
        """验证可选数据库连接 (SQLite) 与 AI 端点 (/models) 逐段探测及零路径/零密钥脱敏."""

        class _MockModelsHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.endswith("/models"):
                    body = json.dumps(
                        {"object": "list", "data": [{"id": "intranet-qwen-32b"}]}
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                return

        class _MockServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        server = _MockServer(("127.0.0.1", 0), _MockModelsHandler)
        srv_port = int(server.server_address[1])
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                priv_root = os.path.join(tmp, "probe_priv_root")
                sqlite_file = os.path.join(tmp, "probe_target_secret_dir.db")
                with contextlib.closing(sqlite3.connect(sqlite_file)) as sconn:
                    sconn.execute("CREATE TABLE t_probe (id INTEGER PRIMARY KEY)")
                    sconn.commit()

                app = create_app(private_root=priv_root)
                ctx: PlatformContext = app.state.platform_ctx
                client = TestClient(app)

                ctx.connection_hub.save_profile(
                    {
                        "name": "本地探针SQLite",
                        "db_type": "SQLITE",
                        "sqlite_path": sqlite_file,
                    }
                )
                ctx.llm_hub.save_provider(
                    {
                        "provider_id": "custom",
                        "base_url": f"http://127.0.0.1:{srv_port}/v1",
                        "api_key": "sk-mockprobe-secret-12345",
                        "default_model": "intranet-qwen-32b",
                        "enabled": True,
                    },
                    provider_id="custom",
                )

                resp = client.post(
                    "/api/platform/diagnostics/benchmark",
                    json={"include_connections": True, "include_llm": True},
                )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                bench = data["benchmark"]

                conns = bench.get("connections_probe") or []
                self.assertEqual(len(conns), 1)
                self.assertTrue(conns[0]["ok"])
                self.assertEqual(conns[0]["host"], "<local_sqlite>")
                self.assertIsNotNone(conns[0]["handshake_ms"])

                llms = bench.get("llm_probe") or []
                self.assertEqual(len(llms), 1)
                self.assertTrue(llms[0]["ok"])
                self.assertEqual(llms[0]["provider_id"], "custom")
                self.assertEqual(llms[0]["model_count"], 1)
                self.assertIsNotNone(llms[0]["models_http_ms"])

                self.assertNotIn("sk-mockprobe-secret-12345", resp.text)
                self.assertNotIn(priv_root, resp.text)
                self.assertNotIn(sqlite_file, resp.text)
                self.assertNotIn(os.path.basename(sqlite_file), resp.text)
                ctx.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_persistent_sqlite_reuse_and_gate1_fast_path_non_blocking(self) -> None:
        """验证持久复用 SQLite 连接、热切换 checkpoint、Gate 1 纯内存快速通道及锁定即时失效."""
        with tempfile.TemporaryDirectory() as tmp:
            priv_root = os.path.join(tmp, "persist_vault")
            app = create_app(private_root=priv_root)
            ctx: PlatformContext = app.state.platform_ctx
            client = TestClient(app)
            try:
                act_code = str(ctx.store.get_meta("license_activation_code", "") or "")
                self.assertTrue(act_code)

                # 首请求预热 Gate 1 内存缓存
                boot = client.get("/api/platform/bootstrap")
                self.assertEqual(boot.status_code, 200)

                # 后续连续请求走纯内存 Gate 1 快速通道且持久复用私有/工作区 SQLite 连接
                conn_resp = client.get("/api/platform/connections")
                self.assertEqual(conn_resp.status_code, 200)
                ping_resp = client.get("/api/platform/diagnostics/ping")
                self.assertEqual(ping_resp.status_code, 200)

                conn_tid = conn_resp.headers.get("x-dataog-trace-id") or ""
                self.assertTrue(conn_tid)
                tr_json = client.get(f"/api/platform/diagnostics/traces/{conn_tid}").json()
                span_names = {
                    f"{s.get('category')}.{s.get('name')}"
                    for s in ((tr_json.get("trace") or {}).get("spans") or [])
                }
                self.assertIn("license.gate1_fast_hit", span_names)
                self.assertNotIn("license.device_probe", span_names)

                ov = client.get("/api/platform/diagnostics/overview").json()
                sq_stats = ov["sqlite_stats"]
                self.assertGreater(sq_stats["private_reuses"], 0)
                self.assertGreater(sq_stats["workspace_reuses"], 0)
                self.assertLessEqual(sq_stats["private_opens"], 2)
                self.assertLessEqual(sq_stats["workspace_opens"], 2)

                # 热切换新工作区并确认 WAL checkpoint + 持久连接重建正常
                ctx.workspace_hub.create_workspace(name="PerfWS2", switch_immediately=True)
                ws_list_resp = client.get("/api/platform/workspaces")
                self.assertEqual(ws_list_resp.status_code, 200)
                ctx.store.checkpoint_workspace()

                # 锁定工作台必须立即失效 Gate 1 热缓存并返回 401，重新激活恢复 200
                self.assertEqual(client.post("/api/platform/auth/lock").status_code, 200)
                self.assertEqual(client.get("/api/platform/connections").status_code, 401)
                self.assertEqual(
                    client.post(
                        "/api/platform/auth/activate",
                        json={"activation_code": act_code},
                    ).status_code,
                    200,
                )
                self.assertEqual(client.get("/api/platform/connections").status_code, 200)
            finally:
                ctx.close()

    def test_binding_stats_large_scale_speed_batch_prune_and_readonly_view(self) -> None:
        """验证大规模元数据时 get_connection_binding_stats 线性性能、批量僵尸清理及只读态零冗余写盘."""
        from modules.standard_migrator.service import StandardMigratorService

        with tempfile.TemporaryDirectory() as tmp:
            priv_root = os.path.join(tmp, "scale_vault")
            live_db = os.path.join(tmp, "live_src.db")
            with contextlib.closing(sqlite3.connect(live_db)) as sconn:
                sconn.execute("CREATE TABLE t_live (col_a TEXT, col_b INTEGER)")
                sconn.commit()

            app = create_app(private_root=priv_root)
            ctx: PlatformContext = app.state.platform_ctx
            try:
                prof = ctx.connection_hub.save_profile(
                    {
                        "name": "ScaleSQLite",
                        "db_type": "SQLITE",
                        "sqlite_path": live_db,
                    }
                )
                cid = prof.connection_id

                # 写入 250 张表 × 30 列 = 7,500 条 meta_columns
                meta_rows = [
                    (
                        cid,
                        "dwd",
                        f"tbl_{t:04d}",
                        c + 1,
                        f"col_{c:02d}",
                        "VARCHAR(64)",
                        "VARCHAR(64)",
                        0,
                        None,
                        "",
                        0,
                        "[]",
                        "",
                        "2026-11-24 14:00:00",
                    )
                    for t in range(250)
                    for c in range(30)
                ]
                with ctx.store.module_db() as conn:
                    conn.executemany(
                        """
                        INSERT INTO meta_columns (
                            connection_id, schema_name, table_name, ordinal_position,
                            column_name, data_type, normalized_type, is_not_null,
                            column_default, column_comment, is_primary_key,
                            index_names_json, lineage_source, refreshed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        meta_rows,
                    )

                # 写入 150 表 × 30 列 = 4,500 条绑定 + 5 条僵尸绑定 + 1 张未缓存表绑定
                bindings = [
                    {
                        "schema_name": "dwd",
                        "table_name": f"tbl_{t:04d}",
                        "column_name": f"col_{c:02d}",
                        "std_code": f"STD{c:04d}",
                        "compliance_status": "COMPLIANT",
                    }
                    for t in range(150)
                    for c in range(30)
                ]
                for z in range(3):
                    bindings.append(
                        {
                            "schema_name": "dwd",
                            "table_name": "tbl_0000",
                            "column_name": f"zombie_col_{z}",
                            "std_code": "STD0001",
                            "compliance_status": "NON_COMPLIANT",
                        }
                    )
                for z in range(2):
                    bindings.append(
                        {
                            "schema_name": "dwd",
                            "table_name": "tbl_0001",
                            "column_name": f"zombie_col_{z}",
                            "std_code": "STD0001",
                            "compliance_status": "NON_COMPLIANT",
                        }
                    )
                for u in range(2):
                    bindings.append(
                        {
                            "schema_name": "dwd",
                            "table_name": "tbl_uncached",
                            "column_name": f"u_col_{u}",
                            "std_code": "STD0001",
                            "compliance_status": "COMMENT_ONLY",
                        }
                    )
                ctx.standard_catalog.bulk_save_field_bindings(cid, bindings, mode="merge")

                t0 = time.perf_counter()
                stats = ctx.standard_catalog.get_connection_binding_stats(cid)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                self.assertLess(elapsed_ms, 1000.0, f"大规模绑标统计耗时过长: {elapsed_ms:.1f}ms")
                self.assertEqual(stats["dwd.tbl_0000"]["bound_count"], 30)
                self.assertEqual(stats["dwd.tbl_0000"]["compliant_count"], 30)
                self.assertEqual(stats["dwd.tbl_0000"]["total_cols"], 30)
                self.assertEqual(stats["dwd.tbl_0001"]["bound_count"], 30)
                self.assertEqual(stats["dwd.tbl_uncached"]["bound_count"], 2)
                self.assertEqual(stats["dwd.tbl_uncached"]["comment_only_count"], 2)
                self.assertEqual(stats["dwd.tbl_uncached"]["total_cols"], 0)

                # 单连接批量清理 2 张表的 5 条僵尸绑定
                live_cols = [f"col_{c:02d}" for c in range(30)]
                removed = ctx.standard_catalog.prune_stale_tables_bindings_batch(
                    cid,
                    [("dwd", "tbl_0000", live_cols), ("dwd", "tbl_0001", live_cols)],
                )
                self.assertEqual(removed, 5)
                self.assertEqual(
                    ctx.standard_catalog.prune_stale_tables_bindings_batch(
                        cid, [("dwd", "tbl_0000", live_cols)]
                    ),
                    0,
                )

                # 验证只读打开表核验：状态未变时跳过冗余 UPDATE 写事务
                ctx.standard_catalog.upsert_item(
                    {
                        "std_code": "STD_LIVE_A",
                        "cn_name": "测试列A",
                        "en_abbr": "col_a",
                        "type_dws": "TEXT",
                        "type_mysql": "TEXT",
                    }
                )
                ctx.standard_catalog.upsert_field_bindings(
                    cid,
                    [
                        {
                            "schema_name": "main",
                            "table_name": "t_live",
                            "column_name": "col_a",
                            "std_code": "STD_LIVE_A",
                        }
                    ],
                )
                mig = StandardMigratorService(ctx)
                first_res = mig.get_table_compliance(cid, "main", "t_live", force_refresh=True)
                self.assertEqual(first_res["columns"][0]["compliance_status"], "COMMENT_ONLY")

                with mock.patch.object(
                    ctx.standard_catalog,
                    "update_binding_compliance_batch",
                    wraps=ctx.standard_catalog.update_binding_compliance_batch,
                ) as mock_upd:
                    second_res = mig.get_table_compliance(cid, "main", "t_live", force_refresh=False)
                self.assertEqual(second_res["columns"][0]["compliance_status"], "COMMENT_ONLY")
                mock_upd.assert_not_called()
            finally:
                ctx.close()
