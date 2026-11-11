/**
 * Data.O.G 桌面模块 3：数据标准 (StandardView)
 * 窗口顶栏通过 macOS 分段控制器整合「贯标」与「比对」双模式
 */

import { clearEl, el } from "../core/dom.js";
import { store } from "../core/state.js";
import { CrossComparatorView } from "./cross_comparator_view.js";
import { StandardMigratorView } from "./standard_migrator_view.js";

export class StandardView {
  constructor() {
    this.mode = "migrator"; // migrator | comparator
    this.refreshHeader = null;

    this.migratorPane = new StandardMigratorView({
      onStateUpdate: () => this.refreshHeader?.(),
    });
    this.comparatorPane = new CrossComparatorView({
      onStateUpdate: () => this.refreshHeader?.(),
      onPushToMigrator: async () => {
        this.mode = "migrator";
        this.render();
        this.refreshHeader?.();
        await this.migratorPane.reloadState();
      },
    });

    this.root = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });
  }

  destroy() {}

  getHeaderParts() {
    store.setAppSummary("standard", this.mode === "migrator" ? "贯标" : "比对");

    const center = el(
      "div",
      { className: "seg-group" },
      [
        { id: "migrator", label: "贯标" },
        { id: "comparator", label: "比对" },
      ].map((tab) =>
        el("button", {
          type: "button",
          className: `seg-btn ${this.mode === tab.id ? "is-active" : ""}`,
          text: tab.label,
          onClick: async () => {
            this.mode = tab.id;
            this.render();
            this.refreshHeader?.();
            if (this.mode === "migrator") await this.migratorPane.ensureLoaded();
            else await this.comparatorPane.ensureLoaded();
          },
        })
      )
    );

    const right =
      this.mode === "migrator"
        ? this.migratorPane.renderHeaderActions()
        : this.comparatorPane.renderHeaderActions();

    return { center, right };
  }

  async mount(container, context = null, refreshHeader = null) {
    this.refreshHeader = refreshHeader;
    if (context?.mode === "comparator") {
      this.mode = "comparator";
    } else if (context?.mode === "migrator") {
      this.mode = "migrator";
    }
    if (context?.tableKey) {
      this.comparatorPane.activeTableKey = context.tableKey;
    }

    clearEl(container, this.root);
    this.render();
    this.refreshHeader?.();

    if (this.mode === "migrator") {
      await this.migratorPane.ensureLoaded();
    } else {
      await this.comparatorPane.ensureLoaded();
    }
  }

  render() {
    if (this.mode === "migrator") {
      this.migratorPane.render();
      clearEl(this.root, this.migratorPane.root);
    } else {
      this.comparatorPane.render();
      clearEl(this.root, this.comparatorPane.root);
    }
  }
}
