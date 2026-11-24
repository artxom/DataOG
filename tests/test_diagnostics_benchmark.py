"""Data.O.G v3.2.0 全链路性能剖析、全栈基准压测与脱敏诊断导出测试."""

from __future__ import annotations

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

            # 3. 验证 _TimedRLock 锁竞争计时
            held = threading.Event()

            def _hold_store_lock() -> None:
                with ctx.store._lock:
                    held.set()
                    time.sleep(0.03)

            thr = threading.Thread(target=_hold_store_lock)
            thr.start()
            self.assertTrue(held.wait(timeout=2.0))
            _ = client.get("/api/platform/connections")
            thr.join(timeout=2.0)

            ov = client.get("/api/platform/diagnostics/overview").json()
            self.assertTrue(ov["ok"])
            self.assertGreater(ov["sqlite_stats"]["lock_acquires"], 0)
            self.assertGreaterEqual(ov["sqlite_stats"]["lock_wait_max_ms"], 10.0)
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
            with sqlite3.connect(sqlite_file) as sconn:
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
                {"HTTP_PROXY": "http://proxy_user:proxy_pass@10.10.1.5:8080", "NO_PROXY": ""},
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
globalThis.URL = {
  createObjectURL: () => "blob:mock-diag",
  revokeObjectURL: () => {},
};
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
                with sqlite3.connect(sqlite_file) as sconn:
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
        finally:
            server.shutdown()
            server.server_close()
