"""Data.O.G 4.0 Antigravity Init SDK 零漂移一键导出器 (tools/export_plugin_sdk.py).

设计目标：
1. 直接内省当前 Data.O.G 真实代码 (`core/platform/*.py`、`static/js/core/*.js`、`static/js/ui/*.js`、`static/css/tokens.css`)
   自动生成 `.pyi` 类型桩、`frontend_api.d.ts` 与 `CONTRACT.md`，彻底消除文档与代码漂移；
2. 拷贝真实精简版 macOS 桌面 UI 壳 (`.dataog_sdk/ui_shell/`) 与离线双轨开发宿主 (`dev_host.py`)、
   强合约校验器 (`validate_plugin.py`)、加密签名打包器 (`pack_plugin.py`) 及 Antigravity `AGENTS.md` + Skill；
3. 支持一键导出干净目录 (`--out`) 及可直接分发给新工作区的压缩包 (`--zip`)。
"""

from __future__ import annotations

import argparse
import ast
import datetime
import io
import json
import os
import re
import shutil
import sys
import zipfile
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import sdk_templates


def _detect_platform_version() -> str:
    kernel_path = os.path.join(_REPO_ROOT, "core", "app_kernel.py")
    if os.path.isfile(kernel_path):
        with open(kernel_path, "r", encoding="utf-8") as fh:
            m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', fh.read(), re.M)
        if m:
            return m.group(1).strip()
    return "4.0.0"


def _format_arg_default(node: Optional[ast.expr]) -> str:
    if node is None:
        return ""
    try:
        return f" = {ast.unparse(node)}"
    except Exception:
        return " = ..."


def _format_ann(node: Optional[ast.expr]) -> str:
    if node is None:
        return "Any"
    try:
        return ast.unparse(node)
    except Exception:
        return "Any"


def _format_fn_sig(fn_node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_method: bool = False) -> str:
    args = fn_node.args
    parts: List[str] = []
    pos_args = list(args.posonlyargs) + list(args.args)
    defaults: List[Optional[ast.expr]] = [None] * (len(pos_args) - len(args.defaults)) + list(args.defaults)
    for idx, (arg, dflt) in enumerate(zip(pos_args, defaults)):
        if is_method and idx == 0 and arg.arg in ("self", "cls"):
            parts.append(arg.arg)
            continue
        ann = f": {_format_ann(arg.annotation)}" if arg.annotation is not None else ""
        parts.append(f"{arg.arg}{ann}{_format_arg_default(dflt)}")
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        parts.append("*")
    for arg, dflt in zip(args.kwonlyargs, args.kw_defaults):
        ann = f": {_format_ann(arg.annotation)}" if arg.annotation is not None else ""
        parts.append(f"{arg.arg}{ann}{_format_arg_default(dflt)}")
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    ret = _format_ann(fn_node.returns) if fn_node.returns is not None else "Any"
    prefix = "async def" if isinstance(fn_node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {fn_node.name}({', '.join(parts)}) -> {ret}: ..."


def _generate_pyi_from_source(src_path: str) -> str:
    with open(src_path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=src_path)

    rel = os.path.relpath(src_path, _REPO_ROOT).replace(os.sep, "/")
    lines: List[str] = [
        f'"""Auto-generated stub from `{rel}` — DO NOT EDIT MANUALLY."""',
        "from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple",
        "",
    ]
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            lines.append(f"class {node.name}:")
            doc = ast.get_docstring(node)
            if doc:
                first_line = doc.strip().splitlines()[0][:120].replace('"', "'")
                lines.append(f'    """{first_line}"""')
            methods = [
                n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and (n.name == "__init__" or not n.name.startswith("_"))
            ]
            if not methods:
                lines.append("    ...")
            else:
                for m in methods:
                    lines.append(f"    {_format_fn_sig(m, is_method=True)}")
            lines.append("")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            lines.append(_format_fn_sig(node, is_method=False))
    return "\n".join(lines).rstrip() + "\n"


def _introspect_js_exports(js_path: str) -> List[str]:
    with open(js_path, "r", encoding="utf-8") as fh:
        src = fh.read()
    exports: List[str] = []
    for m in re.finditer(r"export\s+(?:async\s+)?(?:function|class|const|let)\s+([A-Za-z0-9_]+)", src):
        name = m.group(1)
        if name not in exports:
            exports.append(name)
    return exports


def _generate_frontend_dts() -> str:
    lines: List[str] = [
        "/** Auto-generated Data.O.G 4.0 Frontend Platform Exports (from static/js/core & static/js/ui) */",
        "",
    ]
    for sub in ("core", "ui"):
        dir_path = os.path.join(_REPO_ROOT, "static", "js", sub)
        for fname in sorted(os.listdir(dir_path)):
            if not fname.endswith(".js"):
                continue
            syms = _introspect_js_exports(os.path.join(dir_path, fname))
            if not syms:
                continue
            lines.append(f'declare module "/js/{sub}/{fname}" {{')
            for sym in syms:
                lines.append(f"  export const {sym}: any;")
            lines.append("}")
            lines.append("")
    return "\n".join(lines)


def _extract_css_tokens() -> List[str]:
    tokens_path = os.path.join(_REPO_ROOT, "static", "css", "tokens.css")
    if not os.path.isfile(tokens_path):
        return []
    with open(tokens_path, "r", encoding="utf-8") as fh:
        src = fh.read()
    found: List[str] = []
    for m in re.finditer(r"(--(?:os|status|font|bg|border|text|accent)-[a-z0-9-]+)\s*:", src):
        tok = m.group(1)
        if tok not in found:
            found.append(tok)
    return found[:40]


def _build_contract_md(platform_version: str, js_exports_summary: Dict[str, List[str]], css_tokens: List[str]) -> str:
    token_csv = ", ".join(f"`{t}`" for t in css_tokens[:20])
    core_lines = "\n".join(
        f"- `/js/core/{fname}`: {', '.join(f'`{s}`' for s in syms)}"
        for fname, syms in sorted(js_exports_summary.items())
        if fname.startswith("core/") is False and "/" not in fname
    )
    js_lines = "\n".join(
        f"- `/js/{rel}`: {', '.join(f'`{s}`' for s in syms)}"
        for rel, syms in sorted(js_exports_summary.items())
    )
    _ = core_lines
    return f"""# Data.O.G 4.0 Plugin Platform Contract (ABI `v4.0` / Base `v{platform_version}`)

> 本契约文件由 `tools/export_plugin_sdk.py` 从主仓源码自动内省生成，与平台真实 API 严格保持 100% 一致。

## 1. 插件目录与 `plugin.json` 契约

```text
<plugin_workspace>/
├── plugin.json              # 插件清单 (schema_version=1, api_version="4.0")
├── backend/
│   ├── __init__.py          # 导出 MODULE_SPEC = ModuleSpec(...)
│   ├── router.py            # FastAPI APIRouter (全端点注入 get_platform_context)
│   ├── service.py           # 业务逻辑与 SQLite 表演进
│   └── vendor/              # 可选纯 Python 第三方包 (严禁 .pyd/.so/.dll/.exe)
├── frontend/
│   ├── register.js          # 调用 registerDesktopApp(...)
│   ├── api.js               # 仅从 /js/core/http.js 导入请求原语
│   ├── view.js              # 实现 mount(container, ctx, refreshHeader) / destroy() / getHeaderParts()
│   └── style.css            # 插件独立样式（严禁依赖或修改平台 workbench.css）
├── tests/
│   └── test_plugin.py       # 离线单元测试
└── .dataog_sdk/             # SDK 运行期（dev_host.py / validate_plugin.py / pack_plugin.py / stubs）
```

## 2. 后端底座能力 (`core.platform.*` & `ctx: PlatformContext`)

详细签名请查阅 `.dataog_sdk/stubs/core/platform/*.pyi`：
- **数据库与只读沙箱**：`ctx.connection_hub.readonly_session(conn_id)`、`ctx.query_engine.execute_readonly_sql(conn_id, sql, limit=100)`
- **库表元数据**：`ctx.metadata_hub.get_catalog_tree(conn_id)`、`ctx.metadata_hub.get_table_metadata(conn_id, schema, table)`
- **三层隔离存储**：`ctx.store.register_table_schema(table_name, create_sql, expected_cols, scope="workspace"|"private")`、`ctx.store.module_db(scope=...)`、`ctx.store.get_meta` / `save_meta`
- **标准字典与 AI**：`ctx.standard_catalog`、`ctx.llm_hub.chat_completion(...)`、`ctx.knowledge_hub.search(...)`
- **HTTP 与任务追踪 (`core.platform.http_utils`)**：`json_error`、`extract_task_id`、`file_download_response`、`run_tracked_task`、`run_tracked_upload`
- **插件注册 (`core.platform.module_registry`)**：`ModuleSpec(module_id, title, subtitle, api_prefix, router, desktop_app_id, workspace_tables, private_tables, workspace_meta_keys, init_hook, order)`

## 3. 前端零构建底座能力 (`/js/core/*` & `/js/ui/*`)

{js_lines}

## 4. macOS HIG 语义化 CSS Tokens

可直接复用以下变量以保证深浅色自适应一致：{token_csv}
"""


def _copy_ui_shell(ui_shell_dir: str, platform_version: str) -> None:
    os.makedirs(ui_shell_dir, exist_ok=True)
    static_src = os.path.join(_REPO_ROOT, "static")

    # 拷贝 index.html（同步 version meta）与 ico
    index_src = os.path.join(static_src, "index.html")
    with open(index_src, "r", encoding="utf-8") as fh:
        html = fh.read()
    html = re.sub(
        r'(<meta\s+name="dataog-ui-version"\s+content=")[^"]+(")',
        rf"\g<1>{platform_version}\g<2>",
        html,
    )
    with open(os.path.join(ui_shell_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)

    ico_src = os.path.join(static_src, "dataog.ico")
    if os.path.isfile(ico_src):
        shutil.copy2(ico_src, os.path.join(ui_shell_dir, "dataog.ico"))

    for rel_dir in ("css", os.path.join("js", "core"), os.path.join("js", "ui")):
        src_d = os.path.join(static_src, rel_dir)
        dst_d = os.path.join(ui_shell_dir, rel_dir)
        if os.path.isdir(dst_d):
            shutil.rmtree(dst_d)
        shutil.copytree(src_d, dst_d)

    shutil.copy2(os.path.join(static_src, "js", "main.js"), os.path.join(ui_shell_dir, "js", "main.js"))

    apps_dir = os.path.join(ui_shell_dir, "js", "apps")
    os.makedirs(apps_dir, exist_ok=True)
    with open(os.path.join(apps_dir, "index.js"), "w", encoding="utf-8") as fh:
        fh.write(sdk_templates.UI_SHELL_SETTINGS_APP_INDEX_JS)


def _render_tpl(text: str, vars_map: Dict[str, str]) -> str:
    out = text
    for k, v in vars_map.items():
        out = out.replace(k, v)
    return out


def export_plugin_sdk(
    *,
    out_dir: str,
    zip_path: Optional[str] = None,
    plugin_id: str = "hello_workbench",
    plugin_name: str = "示例扩展应用",
    plugin_summary: str = "基于 Data.O.G 4.0 SDK 构建的独立可拼插应用模块",
    author: str = "Data.O.G Developer",
) -> Dict[str, Any]:
    if not re.match(r"^[a-z][a-z0-9_]{1,47}$", plugin_id):
        raise ValueError(f"非法的 plugin_id: {plugin_id!r}")

    abs_out = os.path.abspath(out_dir)
    if os.path.exists(abs_out):
        shutil.rmtree(abs_out)
    os.makedirs(abs_out, exist_ok=True)

    platform_version = _detect_platform_version()
    sdk_dir = os.path.join(abs_out, ".dataog_sdk")
    stubs_dir = os.path.join(sdk_dir, "stubs", "core", "platform")
    os.makedirs(stubs_dir, exist_ok=True)

    # 1. 自动生成 .pyi 类型桩（消除文档漂移）
    stub_targets = [
        ("core/platform/context.py", "context.pyi"),
        ("core/platform/module_registry.py", "module_registry.pyi"),
        ("core/platform/http_utils.py", "http_utils.pyi"),
        ("core/platform/storage/base.py", "storage.pyi"),
        ("core/platform/connection_hub.py", "connection_hub.pyi"),
        ("core/platform/metadata_hub.py", "metadata_hub.pyi"),
        ("core/platform/query_engine.py", "query_engine.pyi"),
        ("core/platform/llm_hub.py", "llm_hub.pyi"),
        ("core/platform/knowledge_hub.py", "knowledge_hub.pyi"),
        ("core/platform/standard_catalog.py", "standard_catalog.pyi"),
        ("core/platform/excel_io.py", "excel_io.pyi"),
        ("core/platform/sql_risk_guard.py", "sql_risk_guard.pyi"),
    ]
    for src_rel, stub_name in stub_targets:
        src_full = os.path.join(_REPO_ROOT, src_rel)
        if os.path.isfile(src_full):
            stub_content = _generate_pyi_from_source(src_full)
            with open(os.path.join(stubs_dir, stub_name), "w", encoding="utf-8") as fh:
                fh.write(stub_content)

    # 2. 前端导出声明 & CONTRACT.md
    js_exports: Dict[str, List[str]] = {}
    for sub in ("core", "ui"):
        d = os.path.join(_REPO_ROOT, "static", "js", sub)
        for fname in sorted(os.listdir(d)):
            if fname.endswith(".js"):
                js_exports[f"{sub}/{fname}"] = _introspect_js_exports(os.path.join(d, fname))

    with open(os.path.join(sdk_dir, "frontend_api.d.ts"), "w", encoding="utf-8") as fh:
        fh.write(_generate_frontend_dts())

    css_tokens = _extract_css_tokens()
    with open(os.path.join(sdk_dir, "CONTRACT.md"), "w", encoding="utf-8") as fh:
        fh.write(_build_contract_md(platform_version, js_exports, css_tokens))

    with open(os.path.join(sdk_dir, "sdk_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "sdk_version": platform_version,
                "platform_abi": "4.0",
                "exported_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "stubs": [s[1] for s in stub_targets],
                "frontend_modules": sorted(js_exports.keys()),
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )

    # 3. 写入真实 UI Shell & 离线 DevHost / Validator / Packager
    _copy_ui_shell(os.path.join(sdk_dir, "ui_shell"), platform_version)
    with open(os.path.join(sdk_dir, "dev_host.py"), "w", encoding="utf-8") as fh:
        fh.write(sdk_templates.DEV_HOST_PY_TEMPLATE.replace("__PLATFORM_VERSION__", platform_version))
    with open(os.path.join(sdk_dir, "validate_plugin.py"), "w", encoding="utf-8") as fh:
        fh.write(sdk_templates.VALIDATE_PLUGIN_PY_TEMPLATE)
    with open(os.path.join(sdk_dir, "pack_plugin.py"), "w", encoding="utf-8") as fh:
        fh.write(sdk_templates.PACK_PLUGIN_PY_TEMPLATE)

    # 4. 写入插件脚手架、测试与 Antigravity 上下文规范
    app_id = plugin_id[:32]
    ws_table = f"plg_{plugin_id}_notes"[:60]
    meta_key = f"{plugin_id}_last_note_id"
    vars_map = {
        "__PLATFORM_VERSION__": platform_version,
        "__PLUGIN_ID__": plugin_id,
        "__APP_ID__": app_id,
        "__PLUGIN_NAME__": plugin_name,
        "__PLUGIN_SUMMARY__": plugin_summary,
        "__AUTHOR__": author,
        "__WS_TABLE__": ws_table,
        "__META_KEY__": meta_key,
    }

    plugin_json_obj = {
        "schema_version": 1,
        "api_version": "4.0",
        "plugin_id": plugin_id,
        "name": plugin_name,
        "version": "1.0.0",
        "summary": plugin_summary,
        "author": author,
        "min_platform_version": "4.0.0",
        "max_platform_version": "",
        "desktop_app": {
            "app_id": app_id,
            "title": plugin_name,
            "default_summary": plugin_summary,
            "entry_js": "register.js",
            "stylesheets": ["style.css"],
            "order": 70,
        },
        "backend": {
            "entry": "backend/__init__.py",
            "api_prefix": f"/api/modules/{plugin_id}",
        },
        "storage": {
            "workspace_tables": [ws_table],
            "private_tables": [],
            "workspace_meta_keys": [meta_key],
        },
    }
    with open(os.path.join(abs_out, "plugin.json"), "w", encoding="utf-8") as fh:
        json.dump(plugin_json_obj, fh, ensure_ascii=False, indent=2)

    file_specs = [
        ("AGENTS.md", sdk_templates.STARTER_AGENTS_MD_TEMPLATE),
        ("README.md", sdk_templates.STARTER_README_MD_TEMPLATE),
        (".gitignore", sdk_templates.STARTER_GITIGNORE),
        (".agents/skills/dataog-plugin-dev/SKILL.md", sdk_templates.STARTER_SKILL_MD_TEMPLATE),
        ("backend/__init__.py", sdk_templates.STARTER_BACKEND_INIT),
        ("backend/schemas.py", sdk_templates.STARTER_BACKEND_SCHEMAS),
        ("backend/service.py", sdk_templates.STARTER_BACKEND_SERVICE),
        ("backend/router.py", sdk_templates.STARTER_BACKEND_ROUTER),
        ("frontend/register.js", sdk_templates.STARTER_FRONTEND_REGISTER),
        ("frontend/api.js", sdk_templates.STARTER_FRONTEND_API),
        ("frontend/view.js", sdk_templates.STARTER_FRONTEND_VIEW),
        ("frontend/style.css", sdk_templates.STARTER_FRONTEND_CSS),
        ("tests/test_plugin.py", sdk_templates.STARTER_TEST_PY),
    ]
    for rel_path, tpl in file_specs:
        full_p = os.path.join(abs_out, rel_path.replace("/", os.sep))
        os.makedirs(os.path.dirname(full_p), exist_ok=True)
        with open(full_p, "w", encoding="utf-8") as fh:
            fh.write(_render_tpl(tpl, vars_map))
    os.makedirs(os.path.join(abs_out, "backend", "vendor"), exist_ok=True)
    with open(os.path.join(abs_out, "backend", "vendor", ".gitkeep"), "w", encoding="utf-8") as fh:
        fh.write("")

    written_zip = ""
    if zip_path:
        abs_zip = os.path.abspath(zip_path)
        os.makedirs(os.path.dirname(abs_zip), exist_ok=True)
        with zipfile.ZipFile(abs_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for cur_root, dirs, files in os.walk(abs_out):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                for fname in sorted(files):
                    full = os.path.join(cur_root, fname)
                    arc = os.path.relpath(full, abs_out).replace(os.sep, "/")
                    zf.write(full, arc)
        written_zip = abs_zip

    return {
        "ok": True,
        "out_dir": abs_out,
        "zip_path": written_zip,
        "platform_version": platform_version,
        "plugin_id": plugin_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 Data.O.G 4.0 Antigravity 独立插件开发 Init SDK 工作区")
    parser.add_argument("--out", default="dist/DataOG-Plugin-Init", help="导出工作区目录路径")
    parser.add_argument("--zip", default="", help="可选同步导出 .zip 归档路径")
    parser.add_argument("--plugin-id", default="hello_workbench", help="初始脚手架 plugin_id")
    parser.add_argument("--name", default="示例扩展应用", help="初始脚手架插件中文名称")
    parser.add_argument("--author", default="Data.O.G Developer", help="插件作者")
    args = parser.parse_args()

    res = export_plugin_sdk(
        out_dir=args.out,
        zip_path=args.zip or None,
        plugin_id=args.plugin_id,
        plugin_name=args.name,
        author=args.author,
    )
    print(f"[SDK-EXPORT] OK | out={res['out_dir']} | platform=v{res['platform_version']} | plugin_id={res['plugin_id']}")
    if res["zip_path"]:
        print(f"[SDK-EXPORT] ZIP={res['zip_path']}")


if __name__ == "__main__":
    main()

