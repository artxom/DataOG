/**
 * Data.O.G 桌面应用插件清单总装配入口 (static/js/apps/index.js)
 *
 * 所有内置 2.X 应用与未来 3.0 扩展应用均在此通过 `registerDesktopApp` 声明式注册，
 * 并采用原生 ES Module 动态 `import()` 按需懒加载，保证首屏轻量启动与模块零耦合。
 *
 * 3.0 新增独立功能模块接入示例：
 *   import "./my_new_app/register.js";
 */

import { registerDesktopApp } from "../core/app_registry.js";

let _builtinsRegistered = false;

function registerBuiltinDesktopApps() {
  if (_builtinsRegistered) return;
  _builtinsRegistered = true;

  registerDesktopApp({
    id: "settings",
    title: "系统配置",
    defaultSummary: "外观 · 工作区 · 连接 · 知识库 · AI",
    iconName: "settings",
    order: 10,
    resetOnWorkspaceSwitch: false,
    legacyRoutes: {
      connections: { section: "connections" },
    },
    loader: async () => {
      const mod = await import("../views/settings_view.js");
      return new mod.SettingsView();
    },
  });

  registerDesktopApp({
    id: "explorer",
    title: "数据探查",
    defaultSummary: "SQL 查询",
    iconName: "explorer",
    order: 20,
    resetOnWorkspaceSwitch: true,
    legacyRoutes: {
      sql_console: {},
    },
    loader: async () => {
      const mod = await import("../views/sql_console_view.js");
      return new mod.SqlConsoleView();
    },
  });

  registerDesktopApp({
    id: "standard",
    title: "数据标准",
    defaultSummary: "字典 · 落标 · 比对",
    iconName: "standard",
    order: 30,
    resetOnWorkspaceSwitch: true,
    legacyRoutes: {
      standard_migrator: { mode: "migrator" },
      cross_comparator: { mode: "comparator" },
    },
    loader: async () => {
      const mod = await import("../views/standard_view.js");
      return new mod.StandardView();
    },
  });

  registerDesktopApp({
    id: "polyglot",
    title: "多邻国",
    defaultSummary: "命名 · 方言",
    iconName: "polyglot",
    order: 40,
    resetOnWorkspaceSwitch: true,
    legacyRoutes: {
      ddl_translator: { mode: "dialect" },
    },
    loader: async () => {
      const mod = await import("../views/ddl_translator_view.js");
      return new mod.PolyglotView();
    },
  });
}

registerBuiltinDesktopApps();

// 3.0 独立功能模块插件（按需懒加载）
import "./ai_assistant/register.js";
