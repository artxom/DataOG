"""Data.O.G v2.9.0 模块化微内核架构、3.0 插件零耦合扩展能力与代码瘦身回归测试."""

from __future__ import annotations

import contextlib
import datetime
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.testclient import TestClient

from app import create_app, resolve_static_dir
from core import dws_engine
from core.platform import dialects
from core.platform.asset_store import AssetStore
from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import (
    MEDIA_TYPE_HTML,
    file_download_response,
    json_error,
)
from core.platform.license_hub import (
    SCOPE_GLOBAL,
    ed25519_derive_pubkey,
    issue_activation_code_for_seed,
)
from core.platform.module_registry import (
    ModuleSpec,
    discover_module_specs,
    mount_module_spec,
)
from core.platform.offline_report import wrap_offline_html


_VALID_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!\xbc3"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _activate_test_ctx(ctx: PlatformContext) -> None:
    seed = b"dataog-modular-v29-test-seed-001"
    ctx.license_hub._bind_test_pubkey(ed25519_derive_pubkey(seed))
    code = issue_activation_code_for_seed(
        seed,
        scope=SCOPE_GLOBAL,
        duration_days=90,
        expiry_date=datetime.date.today() + datetime.timedelta(days=89),
    )
    ctx.license_hub.activate(code)


class TestModularArchitectureV29(unittest.TestCase):
    def test_module_discovery_order_and_zero_edit_3x_plugin_lifecycle(self) -> None:
        """验证后端模块自动发现排序，以及零修改核心代码动态挂载 3.0 插件（路由 + 工作区表 + 私有表 + 自定义图标槽位）."""
        specs = discover_module_specs()
        ids = [s.module_id for s in specs]
        self.assertEqual(
            ids,
            [
                "standard_migrator",
                "cross_comparator",
                "ddl_translator",
                "data_explorer",
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(private_root=tmp)
            ctx: PlatformContext = app.state.platform_ctx
            _activate_test_ctx(ctx)

            # 构造模拟 3.0 独立插件模块（完全不修改 app.py / storage / workspace_hub / asset_store）
            dq_router = APIRouter(prefix="/api/modules/dq_studio", tags=["dq_studio"])

            @dq_router.get("/rules")
            def list_dq_rules(pctx: PlatformContext = Depends(get_platform_context)):
                with pctx.store.module_db(scope="workspace") as conn:
                    rows = conn.execute(
                        "SELECT rule_id, rule_name FROM dq_rules ORDER BY rule_id"
                    ).fetchall()
                return {
                    "ok": True,
                    "rules": [dict(r) for r in rows],
                    "default_conn": pctx.store.get_meta("dq_default_conn_id", ""),
                }

            def _init_dq(pctx: PlatformContext) -> None:
                pctx.store.register_table_schema(
                    "dq_rules",
                    """
                    CREATE TABLE IF NOT EXISTS dq_rules (
                        rule_id TEXT PRIMARY KEY,
                        rule_name TEXT NOT NULL DEFAULT ''
                    );
                    """,
                    [("rule_name", "TEXT NOT NULL DEFAULT ''")],
                    scope="workspace",
                )
                pctx.store.register_table_schema(
                    "dq_local_secrets",
                    """
                    CREATE TABLE IF NOT EXISTS dq_local_secrets (
                        token_id TEXT PRIMARY KEY,
                        secret_val TEXT NOT NULL DEFAULT ''
                    );
                    """,
                    [("secret_val", "TEXT NOT NULL DEFAULT ''")],
                    scope="private",
                )

            dq_spec = ModuleSpec(
                module_id="dq_studio",
                title="数据质量工作台",
                subtitle="3.0 模拟独立插件",
                api_prefix="/api/modules/dq_studio",
                router=dq_router,
                desktop_app_id="dq_studio",
                workspace_tables=("dq_rules",),
                private_tables=("dq_local_secrets",),
                workspace_meta_keys=("dq_default_conn_id",),
                init_hook=_init_dq,
                order=50,
            )
            mount_module_spec(app, ctx, dq_spec)

            # 写入 3.0 插件的工作区表、工作区元键与私有表
            with ctx.store.module_db(scope="workspace") as conn:
                conn.execute(
                    "INSERT INTO dq_rules (rule_id, rule_name) VALUES (?, ?)",
                    ("R-001", "证件号非空率校验"),
                )
            with ctx.store.module_db(scope="private") as conn:
                conn.execute(
                    "INSERT INTO dq_local_secrets (token_id, secret_val) VALUES (?, ?)",
                    ("T-1", "LOCAL_ONLY_SECRET_DO_NOT_EXPORT"),
                )
            ctx.store.save_meta("dq_default_conn_id", "conn-prod-dws")

            client = TestClient(app)
            # 1. 路由挂载 + Gate 1/2 鉴权正常工作
            res = client.get("/api/modules/dq_studio/rules")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["default_conn"], "conn-prod-dws")
            self.assertEqual(body["rules"][0]["rule_id"], "R-001")

            # 2. bootstrap 清单包含新注册插件
            boot = client.get("/api/platform/bootstrap").json()
            mod_ids = [m["module_id"] for m in boot["modules"]]
            self.assertIn("dq_studio", mod_ids)

            # 3. AssetStore 自动放行新 3.0 桌面应用图标槽位，同时拦截未注册/非法槽位
            up_ok = client.post(
                "/api/platform/appearance/upload",
                files={"file": ("dq.png", _VALID_PNG_BYTES, "image/png")},
                data={"slot": "app:dq_studio"},
            )
            self.assertEqual(up_ok.status_code, 200)
            self.assertTrue(up_ok.json()["appearance"]["app_icons"].get("dq_studio"))

            up_bad = client.post(
                "/api/platform/appearance/upload",
                files={"file": ("x.png", _VALID_PNG_BYTES, "image/png")},
                data={"slot": "app:unregistered_evil"},
            )
            self.assertEqual(up_bad.status_code, 400)

            with self.assertRaises(ValueError):
                ctx.asset_store.register_app_id("../escape")

            # 4. 导出 .dataog.db 工作区：3.0 插件工作区表与工作区元键完整随包导出，私有表严格零泄露
            exported_bytes, _fname = ctx.workspace_hub.export_workspace_bytes()
            export_file = os.path.join(tmp, "exported_3x.dataog.db")
            with open(export_file, "wb") as fh:
                fh.write(exported_bytes)
            with contextlib.closing(sqlite3.connect(export_file)) as econn:
                econn.row_factory = sqlite3.Row
                tables = {
                    r["name"]
                    for r in econn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("dq_rules", tables)
                self.assertNotIn("dq_local_secrets", tables)
                exported_rules = [
                    dict(r)
                    for r in econn.execute("SELECT * FROM dq_rules").fetchall()
                ]
                self.assertEqual(len(exported_rules), 1)
                self.assertEqual(exported_rules[0]["rule_name"], "证件号非空率校验")
                wm = {
                    r["key"]: r["value"]
                    for r in econn.execute("SELECT key, value FROM workspace_meta").fetchall()
                }
                self.assertIn("dq_default_conn_id", wm)
                self.assertIn("conn-prod-dws", wm["dq_default_conn_id"])

            # 5. 导入该 .dataog.db 为新工作区后，3.0 插件表数据完整恢复
            imp = ctx.workspace_hub.import_workspace_as_new(
                filename="exported_3x.dataog.db",
                raw_bytes=exported_bytes,
                name="团队共享3X工作区",
                switch_immediately=True,
            )
            self.assertTrue(imp["workspace"]["is_active"])
            res_after = client.get("/api/modules/dq_studio/rules").json()
            self.assertEqual(len(res_after["rules"]), 1)
            self.assertEqual(res_after["default_conn"], "conn-prod-dws")

            # 6. 克隆工作区与新建空白工作区均自动初始化/保留 3.0 插件共享表
            cloned = ctx.workspace_hub.create_workspace(
                name="克隆3X工作区",
                clone_from_workspace_id=imp["workspace"]["workspace_id"],
                switch_immediately=True,
            )
            self.assertTrue(cloned["is_active"])
            res_cloned = client.get("/api/modules/dq_studio/rules").json()
            self.assertEqual(len(res_cloned["rules"]), 1)

            blank = ctx.workspace_hub.create_workspace(
                name="空白3X工作区",
                switch_immediately=True,
            )
            self.assertTrue(blank["is_active"])
            res_blank = client.get("/api/modules/dq_studio/rules").json()
            self.assertEqual(res_blank["rules"], [])
            self.assertEqual(res_blank["default_conn"], "")

            # 7. 非法 module_id 必须被 ModuleSpec 阻断
            with self.assertRaises(ValueError):
                ModuleSpec(module_id="../evil", title="Bad")
            with self.assertRaises(ValueError):
                ModuleSpec(module_id="123bad", title="Bad")

    def test_dws_engine_deduplication_http_utils_and_offline_report_sdk(self) -> None:
        """验证 dws_engine 与 core.platform.dialects 零重复拷贝，以及 http_utils / offline_report 底座原语."""
        self.assertIs(dws_engine.clean_identifier_input, dialects.clean_identifier_input)
        self.assertIs(dws_engine.validate_identifier, dialects.validate_identifier)
        self.assertIs(dws_engine.clean_sql_type_input, dialects.clean_sql_type_input)
        self.assertIs(dws_engine.validate_sql_type, dialects.validate_sql_type)
        self.assertIs(dws_engine.quote_ident_str, dialects.quote_ident_str)
        self.assertIs(dws_engine.format_existing_db_ident, dialects.format_existing_db_ident)
        self.assertIs(dws_engine.quote_literal_str, dialects.quote_literal_str)
        self.assertIs(
            dws_engine.format_reloptions_with_clause,
            dialects.format_reloptions_with_clause,
        )
        self.assertIs(dws_engine.parse_dws_tabledef, dialects.parse_dws_tabledef)
        self.assertIs(
            dws_engine.update_partition_clause_columns,
            dialects.update_partition_clause_columns,
        )

        # http_utils 安全响应头与 CRLF 防注入
        resp = file_download_response(
            b"<html>ok</html>",
            "测试\r\n注入报告.html",
            media_type=MEDIA_TYPE_HTML,
            ascii_fallback="report.html",
        )
        self.assertEqual(resp.headers.get("x-content-type-options"), "nosniff")
        cd = resp.headers.get("content-disposition") or ""
        self.assertNotIn("\r", cd)
        self.assertNotIn("\n", cd)
        self.assertIn("filename=report.html", cd)
        self.assertIn("filename*=UTF-8''", cd)

        err_resp = json_error("参数非法", 422)
        self.assertEqual(err_resp.status_code, 422)

        # offline_report 通用离线外壳供 3.0 模块复用
        html_bytes = wrap_offline_html(
            "3.0 离线质检报告 <script>",
            {"score": 98.5, "xss": "</script><img onerror=1>"},
            "console.log('dq');",
        )
        html_text = html_bytes.decode("utf-8")
        self.assertIn("&lt;script&gt;", html_text)
        self.assertNotIn("</script><img", html_text)
        self.assertIn("\\u003c/script\\u003e", html_text)

    def test_frontend_app_registry_decoupling_and_lazy_3x_app_via_node(self) -> None:
        """静态架构防线 + Node.js 真实执行：验证 main.js / ui/* 零业务视图耦合，以及 3.0 桌面应用即插即用."""
        static_dir = resolve_static_dir()

        # 1. main.js 不得静态导入任何 ./views/*，也不得硬编码 switch(appId)
        with open(os.path.join(static_dir, "js", "main.js"), encoding="utf-8") as fh:
            main_js = fh.read()
        self.assertNotRegex(main_js, r'from\s+["\']\.\/views\/')
        self.assertNotIn("createAppInstance", main_js)
        self.assertIn("loadAppInstance", main_js)

        # 2. static/js/ui/* 公共组件严禁依赖业务模块 api/* 或 views/*
        ui_dir = os.path.join(static_dir, "js", "ui")
        for fname in os.listdir(ui_dir):
            if not fname.endswith(".js"):
                continue
            with open(os.path.join(ui_dir, fname), encoding="utf-8") as fh:
                content = fh.read()
            self.assertNotRegex(
                content,
                r'from\s+["\']\.\.\/(api|views|apps)\/',
                f"公共 UI 组件 {fname} 不得反向依赖业务模块 API 或视图",
            )

        # 3. Node.js 真实执行 app_registry + state + dom，验证动态注册 3.0 桌面应用与懒加载、样式表注入、路由别名
        if getattr(sys, "frozen", False) or not shutil.which("node"):
            return

        project_root = os.path.dirname(static_dir)
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
    this.hidden = false;
    const self = this;
    this.classList = {
      add(...cs) { self.className = [...new Set([...(self.className||"").split(/\s+/).filter(Boolean), ...cs])].join(" "); },
      remove(...cs) { const s = new Set(cs); self.className = (self.className||"").split(/\s+/).filter(x => x && !s.has(x)).join(" "); },
      toggle(c, f) { const has = (self.className||"").split(/\s+/).includes(c); const on = f === undefined ? !has : Boolean(f); if (on) this.add(c); else this.remove(c); },
      contains(c) { return (self.className||"").split(/\s+/).includes(c); },
    };
  }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren(...ns) { this.children = ns.filter(Boolean); }
  setAttribute(k, v) { this.attributes.set(k, String(v)); }
  getAttribute(k) { return this.attributes.get(k) ?? null; }
  addEventListener(e, fn) { (this.listeners.get(e) || this.listeners.set(e, []).get(e)).push(fn); }
  removeEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  remove() {}
}

const headNode = new MockNode("head");
globalThis.Node = MockNode;
globalThis.window = { addEventListener() {}, removeEventListener() {}, dispatchEvent() {}, innerWidth: 1440, innerHeight: 900 };
globalThis.document = {
  head: headNode,
  body: new MockNode("body"),
  createElement: (tag) => new MockNode(tag),
  createElementNS: (_ns, tag) => new MockNode(tag),
  createTextNode: (t) => Object.assign(new MockNode("#text"), { textContent: String(t) }),
  getElementById: () => null,
  querySelector: (sel) => {
    const m = sel.match(/href="([^"]+)"/);
    if (!m) return null;
    return headNode.children.find((c) => c.getAttribute("href") === m[1]) || null;
  },
  addEventListener() {},
  removeEventListener() {},
};

const root = process.argv[1];
const imp = (rel) => import(pathToFileURL(path.join(root, rel)).href);

const { DESKTOP_APPS, store } = await imp("static/js/core/state.js");
const {
  getDesktopApp,
  loadAppInstance,
  registerDesktopApp,
} = await imp("static/js/core/app_registry.js");
const { renderAppSquircle } = await imp("static/js/core/dom.js");

if (DESKTOP_APPS.length !== 4) {
  throw new Error(`Expected 4 built-in desktop apps, got ${DESKTOP_APPS.length}`);
}

// 验证 4 个内建桌面应用均可通过懒加载动态 import 正常实例化并销毁
for (const builtinApp of [...DESKTOP_APPS]) {
  const bInst = await loadAppInstance(builtinApp.id);
  if (!bInst || typeof bInst.mount !== "function" || typeof bInst.getHeaderParts !== "function") {
    throw new Error(`Built-in lazy loader returned invalid view for ${builtinApp.id}`);
  }
  bInst.destroy?.();
}

let loaderCalls = 0;
const customPath = "M12 3l8 4v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7l8-4z";
const manifest = registerDesktopApp({
  id: "dq_studio",
  title: "数据质量",
  defaultSummary: "规则 · 检核",
  iconName: "dq_studio",
  iconPath: customPath,
  iconGradient: "linear-gradient(145deg, #10b981, #0ea5e9)",
  stylesheets: ["/js/apps/dq_studio/style.css"],
  legacyRoutes: { dq_legacy: { tab: "rules" } },
  order: 50,
  loader: async () => {
    loaderCalls += 1;
    return {
      appId: "dq_studio",
      mount() {},
      destroy() {},
    };
  },
});

if (DESKTOP_APPS.length !== 5 || !getDesktopApp("dq_studio")) {
  throw new Error("DESKTOP_APPS did not dynamically include dq_studio");
}

store.openApp("dq_legacy", { connId: "c1" });
if (store.state.activeApp !== "dq_studio") {
  throw new Error(`Expected activeApp dq_studio, got ${store.state.activeApp}`);
}
const ctx = store.consumeModuleContext();
if (ctx?.tab !== "rules" || ctx?.connId !== "c1") {
  throw new Error("Legacy route context merge failed");
}

const inst1 = await loadAppInstance("dq_studio");
const inst2 = await loadAppInstance("dq_studio");
if (inst1.appId !== "dq_studio" || loaderCalls !== 2) {
  throw new Error("Lazy loader execution failed");
}
if (headNode.children.length !== 1 || headNode.children[0].getAttribute("href") !== "/js/apps/dq_studio/style.css") {
  throw new Error("Stylesheet injection should be idempotent and appended once");
}

const squircle = renderAppSquircle("dq_studio", {}, "lg", manifest);
if (squircle.style.background !== "linear-gradient(145deg, #10b981, #0ea5e9)") {
  throw new Error("Custom iconGradient not applied");
}
const svgEl = squircle.children[0];
const pathEl = svgEl?.children?.[0];
if (pathEl?.getAttribute("d") !== customPath) {
  throw new Error("Custom registered SVG icon path not rendered");
}
console.log("MODULAR_FRONTEND_REGISTRY_OK");
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", node_script, project_root],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node 前端模块化注册验证失败:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("MODULAR_FRONTEND_REGISTRY_OK", proc.stdout)

    def test_http_utils_tracked_task_and_upload_lifecycle_with_extra_modules(self) -> None:
        """端到端验证 http_utils.run_tracked_task / run_tracked_upload 与 create_app(extra_modules=...) 注入."""
        from core.platform.http_utils import run_tracked_task, run_tracked_upload

        demo_router = APIRouter(prefix="/api/modules/sdk_probe", tags=["sdk_probe"])

        @demo_router.post("/run")
        async def api_probe_run(
            request: Request,
            should_fail: bool = Query(default=False),
            task_id: str = Query(default=""),
            ctx: PlatformContext = Depends(get_platform_context),
        ):
            def _work(*, progress_cb=None, cancel_check=None):
                if progress_cb:
                    progress_cb(40, "执行中", "正在处理样本", 1, 2)
                if should_fail:
                    raise RuntimeError("模拟业务异常")
                return {
                    "ok": True,
                    "cancelled": bool(cancel_check and cancel_check()),
                    "message": "探针任务完成",
                }

            return await run_tracked_task(
                request=request,
                task_id=task_id,
                ctx=ctx,
                title="SDK 探针任务",
                start_detail="开始执行",
                fn=_work,
                inject_cancel_check=True,
                count_fn=lambda _r: (2, 2),
            )

        @demo_router.post("/upload")
        async def api_probe_upload(
            file: UploadFile = File(...),
            progress_id: str = Form(default=""),
            ctx: PlatformContext = Depends(get_platform_context),
        ):
            raw = await file.read()

            def _parse(data: bytes, *, progress_cb=None):
                if progress_cb:
                    progress_cb(60, "解析中", "读取内容", 1, 1)
                if b"FAIL" in data:
                    raise ValueError("内容校验失败")
                return {"ok": True, "size": len(data), "message": f"成功导入 {len(data)} 字节"}

            return await run_tracked_upload(
                ctx=ctx,
                progress_id=progress_id,
                title="SDK 探针上传",
                fn=_parse,
                args=(raw,),
                count_fn=lambda r: (r["size"], r["size"]),
            )

        spec = ModuleSpec(
            module_id="sdk_probe",
            title="SDK 探针",
            router=demo_router,
            order=99,
        )

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(private_root=tmp, extra_modules=[spec])
            ctx: PlatformContext = app.state.platform_ctx
            _activate_test_ctx(ctx)
            client = TestClient(app)

            # 1. 成功任务 + task_progress 完成状态
            r_ok = client.post("/api/modules/sdk_probe/run?task_id=tsk_probe_ok_1")
            self.assertEqual(r_ok.status_code, 200)
            self.assertTrue(r_ok.json()["ok"])
            t_snap = client.get("/api/platform/tasks/tsk_probe_ok_1").json()
            self.assertEqual(t_snap["progress"]["status"], "completed")
            self.assertEqual(t_snap["progress"]["processed"], 2)

            # 2. 失败任务 -> 400 + task_progress failed 状态
            r_err = client.post("/api/modules/sdk_probe/run?should_fail=true&task_id=tsk_probe_err_1")
            self.assertEqual(r_err.status_code, 400)
            self.assertFalse(r_err.json()["ok"])
            t_err = client.get("/api/platform/tasks/tsk_probe_err_1").json()
            self.assertEqual(t_err["progress"]["status"], "failed")

            # 3. 上传成功与失败进度追踪
            u_ok = client.post(
                "/api/modules/sdk_probe/upload",
                files={"file": ("ok.txt", b"hello-3.0", "text/plain")},
                data={"progress_id": "prg_probe_ok_1"},
            )
            self.assertEqual(u_ok.status_code, 200)
            self.assertEqual(u_ok.json()["size"], 9)
            p_ok = client.get("/api/platform/import-progress/prg_probe_ok_1").json()
            self.assertEqual(p_ok["progress"]["status"], "completed")

            u_bad = client.post(
                "/api/modules/sdk_probe/upload",
                files={"file": ("bad.txt", b"FAIL_NOW", "text/plain")},
                data={"progress_id": "prg_probe_bad_1"},
            )
            self.assertEqual(u_bad.status_code, 400)
            p_bad = client.get("/api/platform/import-progress/prg_probe_bad_1").json()
            self.assertEqual(p_bad["progress"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
