/**
 * DataOG v2.0.0 — 类 DataGrip / DBeaver 一体化工作台主入口 (ES Module, 零 Node 构建链)
 */

import { platformApi } from "./core/api.js";
import { badge, clearEl, el, svgIcon } from "./core/dom.js";
import { store } from "./core/state.js";
import { DataGrid } from "./ui/data_grid.js";
import { DatabaseTree } from "./ui/database_tree.js";
import { Modal } from "./ui/modal.js";
import { initWorkbenchSplitters } from "./ui/split_pane.js";
import { Toast } from "./ui/toast.js";
import { ConnectionsView } from "./views/connections_view.js";
import { CrossComparatorView } from "./views/cross_comparator_view.js";
import { DdlTranslatorView } from "./views/ddl_translator_view.js";
import { SqlConsoleView } from "./views/sql_console_view.js";
import { StandardMigratorView } from "./views/standard_migrator_view.js";

const MODULE_REGISTRY = [
  { id: "connections", label: "数据源", title: "数据源与双轨驱动中心", icon: "plug" },
  { id: "sql_console", label: "SQL台", title: "SQL 查询与表数据浏览台", icon: "terminal" },
  { id: "standard_migrator", label: "贯标台", title: "单库批量落标贯标执行台", icon: "shield" },
  { id: "cross_comparator", label: "三栏比对", title: "16:9 跨环境三栏数标比对中心", icon: "columns" },
  { id: "ddl_translator", label: "DDL穿透", title: "跨方言建表 DDL 翻译与四层视图穿透器", icon: "translate" },
];

const THEME_MODES = ["system", "light", "dark"];
const THEME_LABELS = {
  system: "主题: 自动",
  light: "主题: 浅色",
  dark: "主题: 深色",
};

function applyThemeMode(mode) {
  const valid = THEME_MODES.includes(mode) ? mode : "system";
  localStorage.setItem("dataog_theme", valid);
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (meta) {
    meta.setAttribute("content", valid === "system" ? "light dark" : valid);
  }
  if (valid === "system") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", valid);
  }
  const btn = document.getElementById("btn-toggle-theme");
  if (btn) {
    btn.textContent = THEME_LABELS[valid];
  }
}

function initThemeController() {
  const saved = localStorage.getItem("dataog_theme") || "system";
  applyThemeMode(saved);
  document.getElementById("btn-toggle-theme")?.addEventListener("click", () => {
    const cur = localStorage.getItem("dataog_theme") || "system";
    const next = THEME_MODES[(THEME_MODES.indexOf(cur) + 1) % THEME_MODES.length];
    applyThemeMode(next);
  });
}

function initActivityBar(splitters) {
  const topNav = document.getElementById("activity-nav-top");
  const bottomNav = document.getElementById("activity-nav-bottom");
  if (!topNav || !bottomNav) return;

  const renderButtons = () => {
    const active = store.state.activeModule;
    clearEl(
      topNav,
      MODULE_REGISTRY.map((m) =>
        el(
          "button",
          {
            type: "button",
            className: `activity-btn ${active === m.id ? "is-active" : ""}`,
            title: m.title,
            "aria-label": m.title,
            onClick: () => store.navigateTo(m.id),
          },
          svgIcon(m.icon, 19),
          el("span", { className: "activity-btn-label", text: m.label })
        )
      )
    );
  };

  clearEl(
    bottomNav,
    el(
      "button",
      {
        type: "button",
        className: "activity-btn",
        title: "折叠 / 展开左侧全局数据库目录树 (Cmd/Ctrl + B)",
        onClick: () => splitters.toggleLeftNav(),
      },
      svgIcon("sidebar", 18),
      el("span", { className: "activity-btn-label", text: "目录树" })
    ),
    el(
      "button",
      {
        type: "button",
        className: "activity-btn",
        title: "折叠 / 展开底部全局任务与 SQL 日志面板 (Cmd/Ctrl + J)",
        onClick: () => splitters.toggleBottomDrawer(),
      },
      svgIcon("logs", 18),
      el("span", { className: "activity-btn-label", text: "日志栏" })
    )
  );

  renderButtons();
  store.subscribe((_s, reason) => {
    if (reason === "navigate" || reason === "bootstrap") {
      renderButtons();
    }
  });
}

function initBottomLogDrawer(splitters) {
  const listEl = document.getElementById("drawer-log-list");
  const countBadge = document.getElementById("log-count-badge");
  const levelSelect = document.getElementById("log-level-filter");

  const renderLogs = () => {
    if (!listEl || !countBadge) return;
    const allLogs = store.state.logs || [];
    const level = levelSelect?.value || "ALL";
    const filtered = level === "ALL" ? allLogs : allLogs.filter((l) => (l.level || "").toUpperCase() === level);
    countBadge.textContent = String(allLogs.length);

    if (!filtered.length) {
      clearEl(
        listEl,
        el("div", { style: { padding: "8px 0", color: "var(--text-muted)" }, text: "暂无审计与 SQL 任务日志" })
      );
      return;
    }

    clearEl(
      listEl,
      filtered.map((item) => {
        const lvl = (item.level || "INFO").toUpperCase();
        const tone = lvl === "ERROR" ? "danger" : lvl === "WARN" ? "warn" : "info";
        return el(
          "div",
          { className: "log-entry" },
          el("span", { className: "log-time", text: item.created_at || "" }),
          badge(lvl, tone),
          el("span", { className: "log-cat", text: `[${item.category || "系统"}]` }),
          el(
            "div",
            { className: "log-msg" },
            el("div", { text: item.message || "" }),
            item.sql_text ? el("pre", { className: "log-sql-snippet", text: item.sql_text }) : null
          )
        );
      })
    );
  };

  levelSelect?.addEventListener("change", renderLogs);
  document.getElementById("btn-toggle-drawer")?.addEventListener("click", () => splitters.toggleBottomDrawer());
  document.getElementById("btn-refresh-logs")?.addEventListener("click", async () => {
    try {
      const res = await platformApi.getLogs(180);
      store.updateLogs(res.logs || []);
    } catch (err) {
      Toast.error(err.message);
    }
  });
  document.getElementById("btn-clear-logs")?.addEventListener("click", async () => {
    try {
      await platformApi.clearLogs();
      store.updateLogs([]);
      Toast.info("全局审计与 SQL 日志已清空");
    } catch (err) {
      Toast.error(err.message);
    }
  });

  store.subscribe((_s, reason) => {
    if (reason === "logs" || reason === "bootstrap") {
      renderLogs();
    }
  });
  renderLogs();
}

function initGlobalMetadataSearch() {
  const input = document.getElementById("global-meta-search-input");
  const btn = document.getElementById("btn-global-meta-search");

  const runSearch = async () => {
    const kw = (input?.value || "").trim();
    if (!kw) {
      Toast.warn("请输入要检索的表名、视图名、字段名或注释关键词");
      return;
    }
    try {
      const res = await platformApi.searchMetadata(kw, "", 150);
      const hits = res.hits || [];
      const connMap = new Map((store.state.connections || []).map((c) => [c.connection_id, c.name]));

      const grid = new DataGrid({
        ariaLabel: "全局元数据索引检索结果",
        rows: hits,
        rowKey: (_r, i) => String(i),
        emptyText: `未在本地 MetadataHub 索引中检索到包含「${kw}」的表或字段（请先在左侧目录树刷新目标库缓存）`,
        maxHeight: "55vh",
        onRowDblClick: (r) => {
          Modal.close("jump");
          store.navigateTo("sql_console", {
            action: "preview_table",
            connectionId: r.connection_id,
            schemaName: r.schema_name,
            tableName: r.table_name,
          });
        },
        columns: [
          {
            key: "hit_type",
            label: "命中级别",
            render: (r) => badge(r.hit_type === "COLUMN" ? "字段命中" : "表/视图命中", r.hit_type === "COLUMN" ? "info" : "success"),
          },
          {
            key: "connection_id",
            label: "所属数据源",
            render: (r) => connMap.get(r.connection_id) || r.connection_id,
          },
          {
            key: "table_name",
            label: "模式.表/视图",
            render: (r) => `${r.schema_name}.${r.table_name}`,
          },
          {
            key: "column_name",
            label: "字段名 / 类型",
            render: (r) => (r.column_name ? `${r.column_name} (${r.data_type || ""})` : "-"),
          },
          {
            key: "comment",
            label: "中文注释",
            render: (r) => r.column_comment || r.table_comment || "-",
          },
          {
            key: "jump",
            label: "快捷跳转",
            sortable: false,
            render: (r) =>
              el(
                "div",
                { style: { display: "flex", gap: "4px" } },
                el("button", {
                  type: "button",
                  className: "btn btn-xs btn-primary",
                  text: "浏览数据",
                  onClick: () => {
                    Modal.close("jump");
                    store.navigateTo("sql_console", {
                      action: "preview_table",
                      connectionId: r.connection_id,
                      schemaName: r.schema_name,
                      tableName: r.table_name,
                    });
                  },
                }),
                el("button", {
                  type: "button",
                  className: "btn btn-xs btn-secondary",
                  text: "翻译DDL",
                  onClick: () => {
                    Modal.close("jump");
                    store.navigateTo("ddl_translator", {
                      connectionId: r.connection_id,
                      schemaName: r.schema_name,
                      tableName: r.table_name,
                    });
                  },
                })
              ),
          },
        ],
      });

      Modal.open({
        title: `全局元数据检索结果 — 关键词「${kw}」（共命中 ${hits.length} 项）`,
        width: "min(900px, 94vw)",
        body: grid.root,
        footer: [
          el("button", { type: "button", className: "btn btn-secondary", text: "关闭", onClick: () => Modal.close() }),
        ],
      });
    } catch (err) {
      Toast.error(err.message);
    }
  };

  btn?.addEventListener("click", runSearch);
  input?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      runSearch();
    }
  });
}

async function bootstrapWorkbench() {
  initThemeController();
  const splitters = initWorkbenchSplitters();
  initActivityBar(splitters);
  initBottomLogDrawer(splitters);
  initGlobalMetadataSearch();

  // 全局键盘快捷键：Cmd/Ctrl + B 折叠左树，Cmd/Ctrl + J 折叠底栏日志
  window.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "b") {
      e.preventDefault();
      splitters.toggleLeftNav();
    } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "j") {
      e.preventDefault();
      splitters.toggleBottomDrawer();
    }
  });

  const treeMount = document.getElementById("database-tree-mount");
  if (treeMount) {
    new DatabaseTree(treeMount);
  }

  const workspaceEl = document.getElementById("module-workspace");
  const viewInstances = {
    connections: new ConnectionsView(),
    sql_console: new SqlConsoleView(),
    standard_migrator: new StandardMigratorView(),
    cross_comparator: new CrossComparatorView(),
    ddl_translator: new DdlTranslatorView(),
  };

  const renderActiveModule = () => {
    if (!workspaceEl) return;
    const modId = store.state.activeModule || "connections";
    const view = viewInstances[modId] || viewInstances.connections;
    const ctx = store.consumeModuleContext();
    view.mount(workspaceEl, ctx);
  };

  const syncJrePill = () => {
    const pill = document.getElementById("jre-runtime-pill");
    if (!pill) return;
    const jre = store.state.javaRuntime || { available: false };
    pill.textContent = jre.available ? "双轨引擎: 原生 + JDBC 就绪" : "双轨引擎: 原生免Java就绪";
    pill.className = `status-pill ${jre.available ? "status-pill-ok" : "status-pill-muted"}`;
    pill.title = jre.message || "";
  };

  store.subscribe((_s, reason) => {
    if (reason === "bootstrap") {
      syncJrePill();
    }
    if (reason === "navigate") {
      renderActiveModule();
    }
  });

  const refreshAll = async () => {
    try {
      const data = await platformApi.bootstrap();
      store.applyBootstrap(data);
      renderActiveModule();
    } catch (err) {
      Toast.error(`初始化工作台失败: ${err.message}`);
    }
  };

  document.getElementById("btn-refresh-platform")?.addEventListener("click", async () => {
    await refreshAll();
    Toast.success("已同步最新全局连接、驱动与日志状态");
  });

  await refreshAll();
}

bootstrapWorkbench();
