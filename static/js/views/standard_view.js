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

    this.dictPane = new StandardDictView({
      onStateUpdate: () => this.refreshHeader?.(),
      onGoStep2: async () => {
        this.mode = "migrator";
        this.render();
        this.refreshHeader?.();
        await this.migratorPane.ensureLoaded();
      },
    });
    this.migratorPane = new StandardMigratorView({
      onStateUpdate: () => this.refreshHeader?.(),
    });
    this.comparatorPane = new CrossComparatorView({
      onStateUpdate: () => this.refreshHeader?.(),
      onPushToMigrator: async () => {
        this.mode = "migrator";
        this.migratorPane.subMode = "queue";
        this.render();
        this.refreshHeader?.();
        await this.migratorPane.reloadState();
      },
    });

    this.root = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });
  }

  destroy() {
    this.migratorPane?.destroy?.();
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
            else if (this.mode === "migrator") await this.migratorPane.ensureLoaded();
            else await this.comparatorPane.ensureLoaded();
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
    if (this.mode === "dictionary") {
      this.dictPane.render();
      clearEl(this.root, this.dictPane.root);
    } else if (this.mode === "migrator") {
      this.migratorPane.render();
      clearEl(this.root, this.migratorPane.root);
    } else {
      this.comparatorPane.render();
      clearEl(this.root, this.comparatorPane.root);
    }
  }
}
