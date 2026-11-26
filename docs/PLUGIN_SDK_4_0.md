# Data.O.G 4.0 操作系统化插件平台与 Antigravity Init SDK 开发指南

> **Data.O.G v4.0.0** 正式将平台微内核升级为「操作系统底座 + 独立可拼插应用生态」：任何新业务应用都可以在完全独立的 Antigravity 工作区中开发、离线预览、自动化校验并打包为加密签名 `.dogapp` / `.zip` 插件包，无缝安装至任意 `DataOG.exe` 客户端。

---

## 1. 一键导出独立开发 Init 工作区（零漂移）

在 Data.O.G 主仓库根目录执行：

```bash
python3 tools/export_plugin_sdk.py \
  --out dist/DataOG-Plugin-Init-v4.0.0 \
  --zip dist/DataOG-Plugin-Init-v4.0.0.zip \
  --plugin-id my_new_app \
  --name "我的新应用"
```

导出的工作区内部自带：
- `.dataog_sdk/stubs/core/platform/*.pyi` & `frontend_api.d.ts` & `CONTRACT.md`：直接从主仓真实代码自动内省生成的精确接口契约（永不漂移）。
- `.dataog_sdk/dev_host.py` + `.dataog_sdk/ui_shell/`：零主仓依赖的离线双轨开发宿主，内置双库 SQLite WAL、`conn_demo_sqlite` 真只读示例库、Mock 大模型/标准与真实 macOS 桌面外壳。
- `.dataog_sdk/validate_plugin.py`：离线 AST + CSS/JS 安全扫描 + ASGI 冒烟强合约校验器。
- `.dataog_sdk/pack_plugin.py`：采用 `DGAP` 加密混淆信封（BLAKE2b-512-CTR + HMAC-SHA256）+ Ed25519 双重签名构建银行网关安全的 `.dogapp` / `.zip`（零内嵌私钥）。
- `AGENTS.md` + `.agents/skills/dataog-plugin-dev/SKILL.md`：专为 Antigravity 大模型上下文工程优化的开箱即用规则与技能。

---

## 2. 双轨开发与联调工作流

1. **用 Antigravity 打开导出的 Init 工作区目录**，大模型只需专注当前插件的 `backend/` 与 `frontend/`，上下文干净无噪声。
2. **离线自测与预览**：
   ```bash
   python3 .dataog_sdk/validate_plugin.py
   python3 -m unittest discover -s tests -p "test_*.py" -v
   python3 .dataog_sdk/dev_host.py --port 18090
   ```
3. **热挂载真实 Data.O.G 联调（连接真实数仓 / LLM）**：
   ```bash
   python3 /path/to/DataOG/app.py --dev-plugin /path/to/my_plugin_workspace
   ```
   > 安全边界：`--dev-plugin` 仅在源码开发态生效，PyInstaller 冻结态 `DataOG.exe` 自动忽略未签名开发目录。

---

## 3. 签名发布与安装生命周期

1. **签名打包**：
   ```bash
   python3 .dataog_sdk/pack_plugin.py --sign-key /path/to/master_ed25519.key --output-dir dist
   ```
2. **安装渠道（二选一）**：
   - **界面安装**：打开 `Data.O.G → 系统配置 → 应用插件` 上传 `.zip` / `.dogapp`，验签预览后确认安装，重启生效；
   - **目录吸纳**：将 `.dogapp` 或外层 `.zip` 放入客户端同级 `plugins/` 目录，启动时自动验签吸纳。
3. **故障隔离与安全卸载**：
   - 任意外置插件启动报错自动隔离 (`quarantined`) 并记录日志，绝不拖垮底座与内置 5 大核心应用；
   - 卸载默认保留插件在工作区与私有库中创建的业务表，可在确认框主动勾选清理（系统内建表受硬白名单保护，绝不可删）。
