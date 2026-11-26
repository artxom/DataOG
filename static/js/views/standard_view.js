/**
 * Data.O.G 桌面模块 3：数据标准 (StandardView)
 * 窗口顶栏通过 macOS 分段控制器整合三步递进闭环：
 * 1. 标准字典 -> 2. 落标绑定与贯标 -> 3. 跨环境比对
 */

import { clearEl, el } from "../core/dom.js";
import { store } from "../core/state.js";
import { CrossComparatorView } from "./cross_comparator_view.js";
import { StandardDictView } from "./standard_dict_view.js";
import { StandardMigratorView } from "./standard_migrator_view.js";

const MODE_LABELS = {
  dictionary: "1. 标准字典",
  migrator: "2. 落标绑定与贯标",
  comparator: "3. 跨环境比对",
};

export class StandardView {
  constructor() {
    this.mode = "dictionary"; // dictionary | migrator | comparator
    this.refreshHeader = null;
    this._dictPane = null;
    this._migratorPane = null;
    this._comparatorPane = null;

    this.root = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });
  }

  get dictPane() {
    if (!this._dictPane) {
      this._dictPane = new StandardDictView({
        onStateUpdate: () => this.refreshHeader?.(),
        onGoStep2: async () => {
          this.mode = "migrator";
          this.render();
          this.refreshHeader?.();
          await this.migratorPane.ensureLoaded();
        },
      });
    }
    return this._dictPane;
  }

  get migratorPane() {
    if (!this._migratorPane) {
      this._migratorPane = new StandardMigratorView({
        onStateUpdate: () => this.refreshHeader?.(),
      });
    }
    return this._migratorPane;
  }

  get comparatorPane() {
    if (!this._comparatorPane) {
      this._comparatorPane = new CrossComparatorView({
        onStateUpdate: () => this.refreshHeader?.(),
        onPushToMigrator: async () => {
          this.mode = "migrator";
          this.migratorPane.subMode = "queue";
          this.render();
          this.refreshHeader?.();
          await this.migratorPane.reloadState();
        },
      });
    }
    return this._comparatorPane;
  }

  destroy() {
    this._dictPane?.destroy?.();
    this._migratorPane?.destroy?.();
    this._comparatorPane?.destroy?.();
    this._dictPane = null;
    this._migratorPane = null;
    this._comparatorPane = null;
  }

  getHeaderParts() {
    store.setAppSummary("standard", MODE_LABELS[this.mode] || "数据标准");

    const center = el(
      "div",
      { className: "seg-group" },
      [
        { id: "dictionary", label: "1. 标准字典" },
        { id: "migrator", label: "2. 落标绑定与贯标" },
        { id: "comparator", label: "3. 跨环境比对" },
      ].map((tab) =>
        el("button", {
          type: "button",
          className: `seg-btn ${this.mode === tab.id ? "is-active" : ""}`,
          text: tab.label,
          onClick: async () => {
            this.mode = tab.id;
            this.render();
            this.refreshHeader?.();
            if (this.mode === "dictionary") await this.dictPane.ensureLoaded();
            else if (this.mode === "migrator") await this.migratorPane.refreshOnActivate();
            else await this.comparatorPane.refreshOnActivate();
          },
        })
      )
    );

    const right =
      this.mode === "dictionary"
        ? this.dictPane.renderHeaderActions()
        : this.mode === "migrator"
        ? this.migratorPane.renderHeaderActions()
        : this.comparatorPane.renderHeaderActions();

    return { center, right };
  }

  async mount(container, context = null, refreshHeader = null) {
    this.refreshHeader = refreshHeader;
    if (context?.mode && ["dictionary", "migrator", "comparator"].includes(context.mode)) {
      this.mode = context.mode;
    } else if (!context?.mode && (store.state.standardDictSummary?.total_count || 0) === 0) {
      this.mode = "dictionary";
    }
    if (context?.tableKey) {
      this.comparatorPane.activeTableKey = context.tableKey;
    }

    clearEl(container, this.root);
    this.render();
    this.refreshHeader?.();

    if (this.mode === "dictionary") {
      await this.dictPane.ensureLoaded();
    } else if (this.mode === "migrator") {
      await this.migratorPane.ensureLoaded();
    } else {
      await this.comparatorPane.ensureLoaded();
    }
  }

  render() {
    let targetPane = null;
    if (this.mode === "dictionary") {
      this.dictPane.render();
      targetPane = this.dictPane;
    } else if (this.mode === "migrator") {
      this.migratorPane.render();
      targetPane = this.migratorPane;
    } else {
      this.comparatorPane.render();
      targetPane = this.comparatorPane;
    }
    if (targetPane && (this.root.children.length !== 1 || this.root.children[0] !== targetPane.root)) {
      clearEl(this.root, targetPane.root);
    }
  }
}
