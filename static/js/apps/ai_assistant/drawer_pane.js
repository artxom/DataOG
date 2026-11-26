/**
 * Data.O.G 智能数据助手右侧按需滑出检视抽屉 (drawer_pane.js)
 *
 * 包含两个标签页：
 * 1. `tables`: 焦点表与库表结构速览（快速勾选 `@焦点表`、查看字段类型/注释/PK/分布键/DDL）
 * 2. `knowledge`: 参考知识库检视与快速上传（支持从对话引用角标一键定位原文、方言检索、上传文档）
 */

import { knowledgeApi, platformApi } from "../../core/platform_api.js";
import { badge, clearEl, el, pickLocalFile, svgIcon } from "../../core/dom.js";
import { store } from "../../core/state.js";
import { Toast } from "../../ui/toast.js";

export class AssistantInspectorDrawer {
  constructor({
    onToggleFocusTable = null,
    onOpenSettingsKnowledge = null,
    onInsertTablePrompt = null,
  } = {}) {
    this.onToggleFocusTable = onToggleFocusTable;
    this.onOpenSettingsKnowledge = onOpenSettingsKnowledge;
    this.onInsertTablePrompt = onInsertTablePrompt;

    this.activeTab = "tables"; // tables | knowledge
    this.connectionId = "";
    this.dialect = "DWS";
    this.focusTables = [];
    this.tableKeyword = "";
    this.selectedTableDetail = null;
    this.loadingTable = false;

    this.kbQuery = "";
    this.kbHits = [];
    this.activeSectionData = null;
    this.activeChunkId = "";
    this.loadingKb = false;

    this.root = el("aside", { className: "ai-asst-drawer" });
  }

  setScope({ connectionId, dialect, focusTables } = {}) {
    if (connectionId !== undefined) {
      if (connectionId !== this.connectionId) {
        this.selectedTableDetail = null;
      }
      this.connectionId = connectionId;
    }
    if (dialect !== undefined) this.dialect = dialect || "DWS";
    if (Array.isArray(focusTables)) this.focusTables = [...focusTables];
    this.render();
  }

  async openCitation(chunkId) {
    if (!chunkId) return;
    this.activeTab = "knowledge";
    this.activeChunkId = chunkId;
    this.loadingKb = true;
    this.render();
    try {
      const res = await knowledgeApi.getChunkSection(chunkId, 1);
      this.activeSectionData = res;
    } catch (err) {
      Toast.error(err.message || "读取知识库原文失败");
    } finally {
      this.loadingKb = false;
      this.render();
    }
  }

  async inspectTable(schemaName, tableName) {
    if (!this.connectionId || !tableName) return;
    this.loadingTable = true;
    this.render();
    try {
      const res = await platformApi.getTableDetail(this.connectionId, schemaName || "", tableName, false);
      this.selectedTableDetail = res.table || res;
    } catch (err) {
      Toast.error(err.message || "加载表结构失败");
    } finally {
      this.loadingTable = false;
      this.render();
    }
  }

  async searchKb() {
    if (!this.kbQuery.trim()) return;
    this.loadingKb = true;
    this.render();
    try {
      const res = await knowledgeApi.search({
        query: this.kbQuery.trim(),
        dialect: this.dialect,
        top_k: 6,
        include_builtin: true,
        use_embeddings: true,
      });
      this.kbHits = res.hits || [];
      this.activeSectionData = null;
    } catch (err) {
      Toast.error(err.message || "知识库检索失败");
    } finally {
      this.loadingKb = false;
      this.render();
    }
  }

  async quickUploadDoc() {
    const file = await pickLocalFile(".md,.txt,.sql,.html,.htm,.json,.csv,.docx,.xlsx");
    if (!file) return;
    this.loadingKb = true;
    this.render();
    try {
      const res = await knowledgeApi.uploadDocument(file, {
        dialect: this.dialect || "GENERIC",
      });
      Toast.success(res.message || "参考资料已入库");
      if (this.kbQuery.trim()) {
        await this.searchKb();
      }
    } catch (err) {
      Toast.error(err.message || "上传失败");
    } finally {
      this.loadingKb = false;
      this.render();
    }
  }

  _renderTablesTab() {
    if (!this.connectionId) {
      return el(
        "div",
        { className: "ai-asst-drawer-empty" },
        el("p", { text: "当前处于「离线纯方言模式」，未连接具体数据库。" }),
        el("p", { className: "muted", text: "可在顶栏选择已配置连接以浏览库表与勾选焦点表，或切至「参考知识库」标签查阅方言资料。" })
      );
    }

    const tree = store.state.catalogTrees?.[this.connectionId] || {};
    const tables = tree.tables || [];
    const kw = this.tableKeyword.trim().toLowerCase();
    const filtered = kw
      ? tables.filter((t) => `${t.schema_name}.${t.table_name} ${t.table_comment || ""}`.toLowerCase().includes(kw))
      : tables;

    const focusSet = new Set(this.focusTables.map((x) => x.toLowerCase()));

    const searchRow = el(
      "div",
      { style: { display: "flex", gap: "6px" } },
      el("input", {
        className: "mac-input mac-input-sm",
        style: { flex: "1" },
        placeholder: `搜索库表 (${tables.length} 个对象)...`,
        value: this.tableKeyword,
        onInput: (e) => {
          this.tableKeyword = e.target.value;
          this.render();
        },
      }),
      el(
        "button",
        {
          type: "button",
          className: "mac-btn mac-btn-sm",
          title: "刷新库表目录",
          onClick: async () => {
            try {
              const res = await platformApi.refreshMetadataTree(this.connectionId);
              if (res && (res.tree || res.schemas || res.tables)) {
                store.updateCatalogTree(this.connectionId, res.tree || res);
              }
              Toast.success(res.message || "库表目录已刷新");
              this.render();
            } catch (err) {
              Toast.error(err.message);
            }
          },
        },
        svgIcon("refresh", 12)
      )
    );

    const listEl = el(
      "div",
      { className: "ai-asst-drawer-list" },
      filtered.slice(0, 80).map((t) => {
        const fq = t.schema_name ? `${t.schema_name}.${t.table_name}` : t.table_name;
        const checked = focusSet.has(fq.toLowerCase()) || focusSet.has(t.table_name.toLowerCase());
        return el(
          "div",
          { className: `ai-asst-drawer-item ${checked ? "is-focused" : ""}` },
          el("input", {
            type: "checkbox",
            checked,
            title: "设为 @焦点表 注入上下文",
            onChange: () => this.onToggleFocusTable?.(fq),
          }),
          el(
            "button",
            {
              type: "button",
              className: "ai-asst-drawer-item-btn",
              onClick: () => this.inspectTable(t.schema_name, t.table_name),
            },
            el("span", { className: "mono", style: { fontSize: "12px" }, text: fq }),
            t.table_comment
              ? el("span", { className: "muted", style: { fontSize: "11px" }, text: t.table_comment })
              : null
          )
        );
      })
    );

    let detailEl = null;
    if (this.loadingTable) {
      detailEl = el("div", { className: "muted", style: { fontSize: "12px", padding: "8px" }, text: "正在探查字段结构..." });
    } else if (this.selectedTableDetail) {
      const m = this.selectedTableDetail;
      const cols = m.columns || [];
      detailEl = el(
        "div",
        { className: "ai-asst-table-detail" },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "6px" } },
          el("strong", { className: "mono", style: { fontSize: "12px" }, text: `${m.schema_name}.${m.table_name}` }),
          el(
            "button",
            {
              type: "button",
              className: "btn btn-xs",
              text: "引用提问",
              onClick: () => this.onInsertTablePrompt?.(`${m.schema_name}.${m.table_name}`),
            }
          )
        ),
        m.distribute_clause
          ? el("div", { className: "muted mono", style: { fontSize: "11px" }, text: m.distribute_clause })
          : null,
        el(
          "div",
          { className: "ai-asst-table-cols" },
          cols.slice(0, 60).map((c) =>
            el(
              "div",
              { className: "ai-asst-col-row" },
              el("span", { className: "mono", style: { fontWeight: c.is_primary_key ? "700" : "500" }, text: c.column_name }),
              el("span", { className: "muted mono", text: c.data_type }),
              c.column_comment ? el("span", { className: "muted", text: c.column_comment }) : null
            )
          )
        )
      );
    }

    return el(
      "div",
      { style: { display: "flex", flexDirection: "column", gap: "10px", height: "100%", minHeight: "0" } },
      searchRow,
      listEl,
      detailEl
    );
  }

  _renderKnowledgeTab() {
    const controls = el(
      "div",
      { style: { display: "flex", flexDirection: "column", gap: "8px" } },
      el(
        "div",
        { style: { display: "flex", gap: "6px" } },
        el("input", {
          className: "mac-input mac-input-sm",
          style: { flex: "1" },
          placeholder: `按 ${this.dialect} 检索参考资料...`,
          value: this.kbQuery,
          onInput: (e) => {
            this.kbQuery = e.target.value;
          },
          onKeydown: (e) => {
            if (e.key === "Enter") this.searchKb();
          },
        }),
        el("button", { type: "button", className: "mac-btn mac-btn-sm mac-btn-primary", text: "检索", onClick: () => this.searchKb() })
      ),
      el(
        "div",
        { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "6px" } },
        el(
          "button",
          { type: "button", className: "mac-btn mac-btn-sm", onClick: () => this.quickUploadDoc() },
          svgIcon("upload", 12),
          `上传 ${this.dialect} 资料`
        ),
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "管理全部文档",
          onClick: () => this.onOpenSettingsKnowledge?.(),
        })
      )
    );

    let bodyEl;
    if (this.loadingKb) {
      bodyEl = el("div", { className: "muted", style: { fontSize: "12px", padding: "12px" }, text: "检索知识库中..." });
    } else if (this.activeSectionData) {
      const doc = this.activeSectionData.document || {};
      const secs = this.activeSectionData.sections || [];
      bodyEl = el(
        "div",
        { className: "ai-asst-kb-viewer" },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "6px", marginBottom: "8px" } },
          el("strong", { style: { fontSize: "12.5px" }, text: `《${doc.title || "参考资料"}》` }),
          badge(doc.dialect || this.dialect, "info")
        ),
        secs.map((s) =>
          el(
            "div",
            {
              className: `ai-asst-kb-chunk ${s.chunk_id === this.activeChunkId ? "is-highlighted" : ""}`,
            },
            el("div", { style: { fontSize: "11.5px", fontWeight: "600", marginBottom: "4px" }, text: s.heading_path || "正文" }),
            el("pre", { className: "mono", style: { margin: 0, whiteSpace: "pre-wrap", fontSize: "11.5px", lineHeight: "1.5" }, text: s.content })
          )
        )
      );
    } else if (this.kbHits.length) {
      bodyEl = el(
        "div",
        { className: "ai-asst-kb-viewer" },
        this.kbHits.map((h) =>
          el(
            "button",
            {
              type: "button",
              className: "ai-asst-kb-hit-card",
              onClick: () => this.openCitation(h.chunk_id),
            },
            el(
              "div",
              { style: { display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" } },
              badge(h.dialect, "info"),
              el("strong", { style: { fontSize: "12px" }, text: h.doc_title }),
              badge(h.is_builtin ? "速查卡" : "私有资料", h.is_builtin ? "neutral" : "success")
            ),
            el("div", { className: "muted", style: { fontSize: "11px", marginTop: "2px" }, text: h.heading_path }),
            el("div", { style: { fontSize: "11.5px", marginTop: "4px", lineHeight: "1.45" }, text: h.snippet })
          )
        )
      );
    } else {
      bodyEl = el(
        "div",
        { className: "ai-asst-drawer-empty" },
        el("p", { text: "点击对话回复中的「知识引用角标」，或输入关键词搜索当前方言参考资料。" })
      );
    }

    return el(
      "div",
      { style: { display: "flex", flexDirection: "column", gap: "10px", height: "100%", minHeight: "0" } },
      controls,
      bodyEl
    );
  }

  render() {
    const tabs = el(
      "div",
      { className: "ai-asst-drawer-tabs" },
      [
        { id: "tables", label: "库表速览" },
        { id: "knowledge", label: "参考知识库" },
      ].map((t) =>
        el("button", {
          type: "button",
          className: `seg-btn ${this.activeTab === t.id ? "is-active" : ""}`,
          text: t.label,
          onClick: () => {
            this.activeTab = t.id;
            this.render();
          },
        })
      )
    );

    const body = this.activeTab === "tables" ? this._renderTablesTab() : this._renderKnowledgeTab();
    clearEl(this.root, tabs, el("div", { className: "ai-asst-drawer-body" }, body));
  }
}
