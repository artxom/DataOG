---
name: release-and-ship
description: >-
  Use whenever finishing code, frontend, packaging, or runtime changes in DataOG
  to execute the mandatory SemVer version bump, GitHub Flow merge to main,
  annotated vX.Y.Z tag creation and push that triggers GitHub Actions
  Windows DataOG.exe compilation and Release publishing.
---

# Data.O.G 发版与自动编译打包闭环规范 (Release & Ship)

在 Data.O.G 仓库中，`.github/workflows/ci.yml` 仅在推送 `v*` 标签或手动触发 `workflow_dispatch` 时运行，专注于纯 Windows 打包（`python build_exe.py --skip-tests`）与 2 秒极速启动冒烟自检（`DataOG.exe --smoke-test`），并自动发布至 GitHub Releases；全量自动化单元测试均在**发布合入 `main` 前于本地完成**。因此，**任何涉及代码、前端、打包配置或运行时行为的变更合入 `main` 后，必须执行本发版闭环**。

## 1. 确定下一个 SemVer 版本号

1. 查看当前最新正式标签：
   ```bash
   git tag -l --sort=-v:refname | head -n 5
   ```
2. 按变更性质确定 `X.Y.Z`：
   - **缺陷修复、性能优化、文档或交互收敛**：递增修订号 `PATCH + 1`（例如 `v2.7.0` → `v2.7.1`）。
   - **新增独立功能特性或模块能力**：递增次版本号 `MINOR + 1, PATCH = 0`（例如 `v2.7.1` → `v2.8.0`）。
   - **架构或持久化结构重大升级**：递增主版本号 `MAJOR + 1, MINOR = 0, PATCH = 0`。

## 2. 同步版本声明（必查清单）

将新版本号 `X.Y.Z` 精确同步到以下 6 个位置：

1. `app.py`：FastAPI 实例参数 `version="X.Y.Z"`、`APP_VERSION = "X.Y.Z"` 与启动横幅中的 `Data.O.G vX.Y.Z`。
2. `core/platform/context.py`：`self.app_version = "X.Y.Z"` 及 `build_platform_bootstrap()` 返回字典中的 `"version": "X.Y.Z"`。
3. `static/index.html`：`<meta name="dataog-ui-version" content="X.Y.Z" />`。
4. `CHANGELOG.md`：将 `## [Unreleased]` 下已完成的变更归并至新建章节 `## [X.Y.Z] - YYYY-MM-DD`（同类型 `### Added` / `### Changed` / `### Fixed` / `### Security` 合并去重），并保留空的 `## [Unreleased]` 标题供下一次迭代使用。
5. `README.md`：更新顶部一级标题 `Data.O.G vX.Y.Z`、目录速览中的 `app.py` 版本注释、「里程碑版本演进记录」表格（将新版本行标为 `当前正式版` 并移除上一版本的该标记），以及自动化测试总项数。
6. `AGENTS.md`：更新第 5 节当前主干版本为 `vX.Y.Z` 与测试总数（同步确保 `AGENTS.md` 文件字节数 `< 24,000` 字节，避免截断）。

> [!CAUTION]
> **严禁误改工作区文件格式协议版本号**：`core/platform/workspace_hub.py` 中的 `WORKSPACE_FORMAT_VERSION` 代表 `.dataog.db` 团队共享工作区底层结构兼容协议，**不得**随常规代码发版改动；仅当工作区表结构或导入合并协议发生不兼容演进时才按协议变更规则调整。

## 3. 本地双门禁全量校验（合并前必过）

版本声明与文档同步完成后，在本地运行全量自动化测试与极速启动冒烟自检，确认无回归：

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
python3 app.py --smoke-test
```

同步执行 `git status -sb` 与 `git diff --stat`，确认工作区干净、无未跟踪临时文件、无 `.db` 状态库或私钥文件。

## 4. 提交、合入 `main`、打标推送与收尾

1. 在工作分支提交并合并回 `main`：
   ```bash
   git add .
   git commit -m "chore(release): 发布 vX.Y.Z 版本"
   git checkout main
   git merge --no-ff <working-branch> -m "Merge <working-branch>: 发布 vX.Y.Z"
   ```
2. 若需，在 `main` 上确认双门禁通过后推送 `main`，并创建推送附注标签触发 GitHub Actions Windows 打包与 Release 发布：
   ```bash
   git push origin main
   git tag -a vX.Y.Z -m "Release vX.Y.Z: <简明版本特性与修复摘要>"
   git push origin vX.Y.Z
   ```
3. 清理已合并的临时工作分支，并验证远端分支与标签就位：
   ```bash
   git branch -d <working-branch>
   git ls-remote --tags origin vX.Y.Z
   ```
