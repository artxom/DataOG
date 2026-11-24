/**
 * Data.O.G 系统配置 · 离线数据库参考知识库管理面板 (KnowledgeSettingsView)
 *
 * 能力：
 * - 上传多种格式参考资料（.md/.txt/.sql/.html/.json/.csv/.docx/.xlsx），零新增重型依赖
 * - 浏览与管理内置主流方言速查卡 + 本机私有知识文档
 * - 导出/导入轻量 `.json.gz` 方言知识包与团队分享
 * - 离线 FTS5 + BM25 即时检索验证
 * - 可选 OpenAI 兼容 Embedding 语义混排配置（离线自动降级纯 FTS5）
 */

import { knowledgeApi } from "../core/platform_api.js";
import { badge, clearEl, el, formatBytes, pickLocalFile, statusDot, svgIcon } from "../core/dom.js";
import { store } from "../core/state.js";
import { Modal } from "../ui/modal.js";
import { TaskActivityBar } from "../ui/task_activity_bar.js";
import { Toast } from "../ui/toast.js";

const DIALECT_OPTIONS = [
  { code: "ALL", label: "全部方言" },
  { code: "DWS", label: "GaussDB(DWS)" },
  { code: "PGSQL", label: "PostgreSQL / openGauss" },
  { code: "MYSQL", label: "MySQL / OceanBase / Doris" },
  { code: "ORACLE", label: "Oracle" },
  { code: "DM8", label: "达梦 DM8" },
  { code: "KINGBASE", label: "人大金仓 KingbaseES" },
  { code: "INCEPTOR", label: "星环 Inceptor / Hive" },
  { code: "DB2", label: "IBM Db2" },
  { code: "GBASE", label: "南大通用 GBase" },
  { code: "SQLITE", label: "SQLite 3" },
  { code: "GENERIC", label: "通用调优与规范" },
];

export class KnowledgeSettingsView {
  constructor({ onChange = null } = {}) {
    this.onChange = onChange;
    this.summary = null;
    this.documents = [];
    this.embeddingConfig = {
      enabled: false,
      provider_id: "",
      base_url: "",
      has_api_key: false,
      api_key_masked: "",
      model: "",
      api_key: "",
      clear_api_key: false,
    };
    this.filterDialect = "ALL";
    this.includeBuiltin = true;
    this.searchKeyword = "";
    this.testQuery = "";
    this.testResult = null;
    this.embeddingExpanded = false;
    this.busy = false;

    this.activityBar = new TaskActivityBar({ graceMs: 120 });
    this.root = el("div", { style: { display: "flex", flexDirection: "column", gap: "14px" } });
  }

  destroy() {
    this.activityBar?.destroy();
  }

  async refreshFromServer() {
    try {
      const [sumRes, listRes] = await Promise.all([
        knowledgeApi.summary(),
        knowledgeApi.listDocuments({
          dialect: this.filterDialect,
          includeBuiltin: this.includeBuiltin,
          keyword: this.searchKeyword,
        }),
      ]);
      this.summary = sumRes.summary || listRes.summary || null;
      this.documents = listRes.documents || [];
      if (sumRes.embedding_config) {
        this.embeddingConfig = {
          ...sumRes.embedding_config,
          api_key: "",
          clear_api_key: false,
        };
      }
      this.render();
      this.onChange?.();
    } catch (err) {
      Toast.error(err.message || "加载知识库信息失败");
    }
  }

  openUploadDocumentModal() {
    let selectedFile = null;
    let dialect = this.filterDialect !== "ALL" ? this.filterDialect : "DWS";
    let docTitle = "";
    let tagsText = "";

    const fileLabel = el("span", {
      className: "muted",
      style: { fontSize: "12px" },
      text: "未选择文件（支持 .md / .txt / .sql / .html / .json / .csv / .docx / .xlsx，最大 15 MB）",
    });

    const pickBtn = el(
      "button",
      {
        type: "button",
        className: "mac-btn",
        onClick: async () => {
          const f = await pickLocalFile(".md,.txt,.sql,.html,.htm,.json,.csv,.docx,.xlsx");
          if (!f) return;
          selectedFile = f;
          fileLabel.textContent = `${f.name} (${formatBytes(f.size)})`;
          if (!docTitle) {
            docTitle = f.name.replace(/\.[^.]+$/, "");
            titleInput.value = docTitle;
          }
        },
      },
      svgIcon("upload", 13),
      "选择参考资料文件"
    );

    const dialectSel = el(
      "select",
      {
        className: "mac-input",
        onChange: (e) => {
          dialect = e.target.value;
        },
      },
      DIALECT_OPTIONS.filter((d) => d.code !== "ALL").map((d) => {
        const opt = el("option", { value: d.code, text: `${d.code} · ${d.label}` });
        if (d.code === dialect) opt.selected = true;
        return opt;
      })
    );

    const titleInput = el("input", {
      className: "mac-input",
      placeholder: "可选：自定义资料标题（默认取文件名）",
      onInput: (e) => {
        docTitle = e.target.value;
      },
    });

    const tagsInput = el("input", {
      className: "mac-input",
      placeholder: "可选标签，逗号分隔（如：执行计划,错误码,建表规范）",
      onInput: (e) => {
        tagsText = e.target.value;
      },
    });

    const uploadBtn = el("button", {
      type: "button",
      className: "mac-btn mac-btn-primary",
      text: "导入并离线索引",
      onClick: async () => {
        if (!selectedFile) {
          Toast.info("请先选择要导入的参考资料文件");
          return;
        }
        uploadBtn.disabled = true;
        uploadBtn.textContent = "解析索引中...";
        try {
          const res = await knowledgeApi.uploadDocument(selectedFile, {
            dialect,
            title: docTitle.trim(),
            tags: tagsText.trim(),
          });
          Modal.close("done");
          Toast.success(res.message || "已导入至本地知识库");
          await this.refreshFromServer();
        } catch (err) {
          Toast.error(err.message || "导入失败");
          uploadBtn.disabled = false;
          uploadBtn.textContent = "导入并离线索引";
        }
      },
    });

    Modal.open({
      title: "上传数据库参考资料至本地知识库",
      width: "min(520px, 92vw)",
      body: el(
        "div",
        { style: { display: "flex", flexDirection: "column", gap: "12px" } },
        el(
          "div",
          { className: "form-field" },
          el("label", { className: "form-label", text: "1. 选择资料文件" }),
          el("div", { style: { display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" } }, pickBtn, fileLabel)
        ),
        el(
          "div",
          { className: "form-field" },
          el("label", { className: "form-label", text: "2. 适用数据库方言（严格隔离检索范围）" }),
          dialectSel
        ),
        el(
          "div",
          { className: "form-field" },
          el("label", { className: "form-label", text: "3. 资料名称" }),
          titleInput
        ),
        el(
          "div",
          { className: "form-field" },
          el("label", { className: "form-label", text: "4. 检索标签" }),
          tagsInput
        )
      ),
      footer: [
        el("button", { type: "button", className: "mac-btn", text: "取消", onClick: () => Modal.close("cancel") }),
        uploadBtn,
      ],
    });
  }

  openQuickNoteModal() {
    let dialect = this.filterDialect !== "ALL" ? this.filterDialect : "DWS";
    let title = "";
    let body = "";
    let tags = "";

    const saveBtn = el("button", {
      type: "button",
      className: "mac-btn mac-btn-primary",
      text: "保存并建立索引",
      onClick: async () => {
        if (!title.trim() || !body.trim()) {
          Toast.info("请填写标题与参考正文内容");
          return;
        }
        saveBtn.disabled = true;
        try {
          const res = await knowledgeApi.createTextDocument({
            title: title.trim(),
            content: body.trim(),
            dialect,
            tags: tags.split(",").map((x) => x.trim()).filter(Boolean),
          });
          Modal.close("done");
          Toast.success(res.message || "已收录至知识库");
          await this.refreshFromServer();
        } catch (err) {
          Toast.error(err.message);
          saveBtn.disabled = false;
        }
      },
    });

    Modal.open({
      title: "快速录入 SQL 规范 / 排障经验笔记",
      width: "min(620px, 94vw)",
      body: el(
        "div",
        { style: { display: "flex", flexDirection: "column", gap: "10px" } },
        el(
          "div",
          { style: { display: "grid", gridTemplateColumns: "1fr 220px", gap: "10px" } },
          el("input", {
            className: "mac-input",
            placeholder: "标题（如：DWS 倾斜排查与分布键规范）",
            onInput: (e) => {
              title = e.target.value;
            },
          }),
          el(
            "select",
            {
              className: "mac-input",
              onChange: (e) => {
                dialect = e.target.value;
              },
            },
            DIALECT_OPTIONS.filter((d) => d.code !== "ALL").map((d) => {
              const opt = el("option", { value: d.code, text: `${d.code} · ${d.label}` });
              if (d.code === dialect) opt.selected = true;
              return opt;
            })
          )
        ),
        el("input", {
          className: "mac-input",
          placeholder: "标签（逗号分隔，可选）",
          onInput: (e) => {
            tags = e.target.value;
          },
        }),
        el("textarea", {
          className: "mac-input mono",
          rows: 10,
          style: { minHeight: "200px", resize: "vertical" },
          placeholder: "粘贴 Markdown / SQL 示例 / 常见报错码解决经验...",
          onInput: (e) => {
            body = e.target.value;
          },
        })
      ),
      footer: [
        el("button", { type: "button", className: "mac-btn", text: "取消", onClick: () => Modal.close("cancel") }),
        saveBtn,
      ],
    });
  }

  async openDocPreviewModal(docId) {
    try {
      const res = await knowledgeApi.getDocument(docId, true);
      const doc = res.document;
      if (!doc) return;
      const chunks = doc.chunks || [];
      Modal.open({
        title: `《${doc.title}》 (${doc.dialect} · ${chunks.length} 个片段)`,
        width: "min(760px, 94vw)",
        body: el(
          "div",
          { style: { display: "flex", flexDirection: "column", gap: "10px", maxHeight: "62vh", overflowY: "auto" } },
          chunks.map((c) =>
            el(
              "div",
              { className: "mac-group-box", style: { padding: "10px 12px" } },
              el(
                "div",
                { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "6px" } },
                el("strong", { style: { fontSize: "12.5px" }, text: `#${c.chunk_index + 1} ${c.heading_path || "正文"}` }),
                badge(`${c.char_count} 字符`, "neutral")
              ),
              el("pre", {
                className: "mono",
                style: {
                  margin: 0,
                  whiteSpace: "pre-wrap",
                  fontSize: "12px",
                  lineHeight: "1.5",
                  color: "var(--os-text-secondary)",
                },
                text: c.content,
              })
            )
          )
        ),
        footer: [el("button", { type: "button", className: "mac-btn mac-btn-primary", text: "关闭", onClick: () => Modal.close("ok") })],
      });
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async _runSearchTest() {
    if (!this.testQuery.trim()) {
      Toast.info("请输入测试检索词（如 ORA-00942、分布键、分页）");
      return;
    }
    try {
      const res = await knowledgeApi.search({
        query: this.testQuery.trim(),
        dialect: this.filterDialect === "ALL" ? "" : this.filterDialect,
        top_k: 5,
        include_builtin: this.includeBuiltin,
        use_embeddings: true,
      });
      this.testResult = res;
      this.render();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async _handleImportPack() {
    const file = await pickLocalFile(".gz,.json.gz");
    if (!file) return;
    try {
      const res = await knowledgeApi.importPack(file, { overwriteExisting: false });
      Toast.success(res.message || "知识包导入成功");
      await this.refreshFromServer();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async _handleExportPack() {
    try {
      await knowledgeApi.exportPack({
        dialect: this.filterDialect === "ALL" ? "" : this.filterDialect,
      });
      Toast.success("已导出轻量离线知识包 (.json.gz)");
    } catch (err) {
      Toast.error(err.message);
    }
  }

  render() {
    const sum = this.summary || {};
    const statsBar = el(
      "div",
      { className: "mac-group-box mac-section" },
      el(
        "div",
        { className: "mac-section-header" },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } },
          el("h3", { className: "mac-section-title", text: "离线参考知识库 (Layer 2 本机全局)" }),
          sum.fts5_available ? statusDot("FTS5 离线引擎就绪", "success") : statusDot("BM25 离线引擎就绪", "info"),
          sum.embedding_enabled ? badge("混合语义排序已开启", "info") : badge("纯离线零依赖模式", "neutral")
        ),
        el(
          "div",
          { style: { display: "flex", gap: "6px", flexWrap: "wrap" } },
          el(
            "button",
            { type: "button", className: "mac-btn mac-btn-sm mac-btn-primary", onClick: () => this.openUploadDocumentModal() },
            svgIcon("upload", 12),
            "上传资料"
          ),
          el(
            "button",
            { type: "button", className: "mac-btn mac-btn-sm", onClick: () => this.openQuickNoteModal() },
            svgIcon("plus", 12),
            "录入笔记"
          ),
          el(
            "button",
            { type: "button", className: "mac-btn mac-btn-sm", onClick: () => this._handleImportPack() },
            "导入知识包"
          ),
          el(
            "button",
            { type: "button", className: "mac-btn mac-btn-sm", onClick: () => this._handleExportPack() },
            svgIcon("download", 12),
            "导出分享"
          )
        )
      ),
      el(
        "div",
        { style: { display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: "10px", marginTop: "8px" } },
        [
          { label: "用户上传资料", val: `${sum.user_docs ?? 0} 篇` },
          { label: "内置方言速查卡", val: `${sum.builtin_docs ?? 11} 篇` },
          { label: "离线索引切片", val: `${sum.total_chunks ?? 0} 段` },
          { label: "索引正文体积", val: formatBytes(sum.total_bytes || 0) },
        ].map((card) =>
          el(
            "div",
            {
              style: {
                padding: "8px 10px",
                borderRadius: "8px",
                background: "var(--os-subtle-bg)",
                border: "1px solid var(--os-border)",
              },
            },
            el("div", { className: "muted", style: { fontSize: "11px" }, text: card.label }),
            el("div", { style: { fontSize: "15px", fontWeight: "700", marginTop: "2px" }, text: card.val })
          )
        )
      )
    );

    const filterRow = el(
      "div",
      { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } },
      el(
        "select",
        {
          className: "mac-input mac-input-sm",
          style: { width: "210px" },
          onChange: (e) => {
            this.filterDialect = e.target.value;
            this.refreshFromServer();
          },
        },
        DIALECT_OPTIONS.map((d) => {
          const opt = el("option", { value: d.code, text: d.code === "ALL" ? d.label : `${d.code} · ${d.label}` });
          if (d.code === this.filterDialect) opt.selected = true;
          return opt;
        })
      ),
      el("input", {
        className: "mac-input mac-input-sm",
        style: { flex: "1", minWidth: "180px" },
        placeholder: "筛选资料标题或文件名...",
        value: this.searchKeyword,
        onInput: (e) => {
          this.searchKeyword = e.target.value;
        },
        onKeydown: (e) => {
          if (e.key === "Enter") this.refreshFromServer();
        },
      }),
      el(
        "label",
        { style: { display: "inline-flex", alignItems: "center", gap: "5px", fontSize: "12px", cursor: "pointer" } },
        el("input", {
          type: "checkbox",
          checked: this.includeBuiltin,
          onChange: (e) => {
            this.includeBuiltin = e.target.checked;
            this.refreshFromServer();
          },
        }),
        el("span", { text: "显示内置速查卡" })
      ),
      el("button", { type: "button", className: "mac-btn mac-btn-sm", text: "刷新", onClick: () => this.refreshFromServer() })
    );

    const docListEl = this.documents.length
      ? el(
          "div",
          { style: { display: "flex", flexDirection: "column", gap: "8px", marginTop: "10px" } },
          this.documents.map((d) =>
            el(
              "div",
              {
                style: {
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: "12px",
                  padding: "10px 12px",
                  borderRadius: "9px",
                  border: "1px solid var(--os-border)",
                  background: "var(--os-win-bg)",
                  opacity: d.enabled ? "1" : "0.62",
                },
              },
              el(
                "div",
                { style: { minWidth: "0", flex: "1" } },
                el(
                  "div",
                  { style: { display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" } },
                  el("strong", { style: { fontSize: "13px" }, text: d.title }),
                  badge(d.dialect, "info"),
                  d.is_builtin ? badge("内置速查卡", "neutral") : badge("用户资料", "success"),
                  badge(`${d.chunk_count} 段 · ${formatBytes(d.file_size)}`, "neutral")
                ),
                el("div", {
                  className: "muted",
                  style: { fontSize: "11.5px", marginTop: "4px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
                  text: d.summary || d.file_name,
                })
              ),
              el(
                "div",
                { style: { display: "flex", alignItems: "center", gap: "6px", flexShrink: "0" } },
                el("button", {
                  type: "button",
                  className: "btn btn-xs",
                  text: "查看切片",
                  onClick: () => this.openDocPreviewModal(d.doc_id),
                }),
                el("button", {
                  type: "button",
                  className: "btn btn-xs",
                  text: d.enabled ? "停用" : "启用",
                  onClick: async () => {
                    try {
                      await knowledgeApi.updateDocument(d.doc_id, { enabled: !d.enabled });
                      await this.refreshFromServer();
                    } catch (err) {
                      Toast.error(err.message);
                    }
                  },
                }),
                !d.is_builtin
                  ? el("button", {
                      type: "button",
                      className: "btn btn-xs btn-danger",
                      text: "删除",
                      onClick: async () => {
                        const ok = await Modal.confirm({
                          title: "删除知识文档",
                          message: `确定永久删除参考资料《${d.title}》吗？`,
                          confirmText: "删除",
                          danger: true,
                        });
                        if (!ok) return;
                        try {
                          await knowledgeApi.deleteDocument(d.doc_id);
                          Toast.success("已删除");
                          await this.refreshFromServer();
                        } catch (err) {
                          Toast.error(err.message);
                        }
                      },
                    })
                  : null
              )
            )
          )
        )
      : el("div", { className: "muted", style: { padding: "18px 0", textAlign: "center", fontSize: "12.5px" }, text: "当前筛选下暂无知识文档，点击上方「上传资料」添加。" });

    const listBox = el("div", { className: "mac-group-box mac-section" }, filterRow, docListEl);

    // 离线检索验证盒
    const searchTestBox = el(
      "div",
      { className: "mac-group-box mac-section" },
      el(
        "div",
        { className: "mac-section-header" },
        el("h3", { className: "mac-section-title", text: "离线检索效果实时测试" }),
        this.testResult ? badge(`模式: ${this.testResult.retrieval_mode}`, "info") : null
      ),
      el(
        "div",
        { style: { display: "flex", gap: "8px", marginTop: "6px" } },
        el("input", {
          className: "mac-input",
          style: { flex: "1" },
          placeholder: "输入错误码或技术词（例如 ORA-00942、DISTRIBUTE BY、分页、索引失效）...",
          value: this.testQuery,
          onInput: (e) => {
            this.testQuery = e.target.value;
          },
          onKeydown: (e) => {
            if (e.key === "Enter") this._runSearchTest();
          },
        }),
        el("button", { type: "button", className: "mac-btn mac-btn-primary", text: "测试检索", onClick: () => this._runSearchTest() })
      ),
      this.testResult
        ? el(
            "div",
            { style: { display: "flex", flexDirection: "column", gap: "6px", marginTop: "10px" } },
            (this.testResult.hits || []).length
              ? this.testResult.hits.map((h) =>
                  el(
                    "div",
                    {
                      style: {
                        padding: "8px 10px",
                        borderRadius: "8px",
                        background: "var(--os-subtle-bg)",
                        border: "1px solid var(--os-border)",
                      },
                    },
                    el(
                      "div",
                      { style: { display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" } },
                      badge(h.dialect, "info"),
                      el("strong", { style: { fontSize: "12px" }, text: `${h.doc_title} · ${h.heading_path}` }),
                      badge(`相关度 ${h.score}`, "neutral")
                    ),
                    el("div", { className: "muted", style: { fontSize: "12px", marginTop: "4px" }, text: h.snippet })
                  )
                )
              : el("div", { className: "muted", style: { fontSize: "12px", padding: "6px 0" }, text: "未检索到匹配片段。" })
          )
        : null
    );

    // 可选 Embedding 语义混排配置（默认折叠）
    const cfg = this.embeddingConfig;
    const providers = store.state.llmProviders || [];
    const embeddingBox = el(
      "div",
      { className: "mac-group-box mac-section" },
      el(
        "div",
        { className: "mac-section-header", style: { cursor: "pointer" }, onClick: () => { this.embeddingExpanded = !this.embeddingExpanded; this.render(); } },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "8px" } },
          el("h3", { className: "mac-section-title", text: "可选：Embedding 语义混排扩展（离线环境默认关闭）" }),
          cfg.enabled ? badge("已启用", "success") : badge("纯 FTS5 离线", "neutral")
        ),
        el("span", { className: "muted", style: { fontSize: "12px" }, text: this.embeddingExpanded ? "收起" : "展开配置" })
      ),
      this.embeddingExpanded
        ? el(
            "div",
            { style: { display: "flex", flexDirection: "column", gap: "10px", marginTop: "10px" } },
            el("p", {
              className: "muted",
              style: { margin: 0, fontSize: "12px" },
              text: "接入内网 Ollama / OpenAI 兼容 `/embeddings` 接口可提升同义表述召回；断网或接口不可用时会自动无感退回纯 FTS5。",
            }),
            this.activityBar.el,
            el(
              "div",
              { className: "form-grid" },
              el(
                "label",
                { style: { display: "inline-flex", alignItems: "center", gap: "6px", fontSize: "12.5px" } },
                el("input", {
                  type: "checkbox",
                  checked: Boolean(cfg.enabled),
                  onChange: (e) => {
                    cfg.enabled = e.target.checked;
                  },
                }),
                el("span", { text: "开启 Embedding + FTS5 混合排序" })
              ),
              el(
                "div",
                { className: "form-field" },
                el("label", { className: "form-label", text: "复用已配置 AI 服务商（留空则使用下方自定义地址）" }),
                el(
                  "select",
                  {
                    className: "mac-input",
                    onChange: (e) => {
                      cfg.provider_id = e.target.value;
                    },
                  },
                  [el("option", { value: "", text: "自定义 Embedding 地址" })].concat(
                    providers.map((p) => {
                      const opt = el("option", { value: p.provider_id, text: `${p.name} (${p.base_url})` });
                      if (p.provider_id === cfg.provider_id) opt.selected = true;
                      return opt;
                    })
                  )
                )
              ),
              el(
                "div",
                { className: "form-field" },
                el("label", { className: "form-label", text: "Embedding Base URL（可选，如 http://127.0.0.1:11434/v1）" }),
                el("input", {
                  className: "mac-input mono",
                  value: cfg.base_url || "",
                  placeholder: "http://127.0.0.1:11434/v1",
                  onInput: (e) => {
                    cfg.base_url = e.target.value;
                  },
                })
              ),
              el(
                "div",
                { className: "form-field" },
                el("label", { className: "form-label", text: "Embedding 模型名称" }),
                el("input", {
                  className: "mac-input mono",
                  value: cfg.model || "",
                  placeholder: "bge-m3 / nomic-embed-text / text-embedding-3-small",
                  onInput: (e) => {
                    cfg.model = e.target.value;
                  },
                })
              ),
              el(
                "div",
                { className: "form-field span-2" },
                el("label", { className: "form-label", text: "API Key（本地 Ollama 可留空）" }),
                el("input", {
                  className: "mac-input mono",
                  type: "password",
                  autocomplete: "new-password",
                  value: cfg.api_key || "",
                  placeholder: cfg.has_api_key ? `已保存 (${cfg.api_key_masked})，留空保持不变` : "sk-...",
                  onInput: (e) => {
                    cfg.api_key = e.target.value;
                  },
                })
              )
            ),
            el(
              "div",
              { style: { display: "flex", justifyContent: "flex-end", gap: "8px" } },
              el("button", {
                type: "button",
                className: "mac-btn",
                text: "保存并重建向量索引",
                onClick: async () => {
                  try {
                    await knowledgeApi.saveEmbeddingConfig(cfg);
                    const res = await this.activityBar.track(
                      "重建知识库向量索引",
                      ({ taskId, signal }) => knowledgeApi.reindexEmbeddings("", { taskId, signal }),
                      { prefix: "kb_emb", cancellable: false }
                    );
                    Toast.success(res.message || "向量索引重建完成");
                    await this.refreshFromServer();
                  } catch (err) {
                    Toast.error(err.message);
                  }
                },
              }),
              el("button", {
                type: "button",
                className: "mac-btn mac-btn-primary",
                text: "保存配置",
                onClick: async () => {
                  try {
                    const res = await knowledgeApi.saveEmbeddingConfig(cfg);
                    this.embeddingConfig = { ...res.embedding_config, api_key: "", clear_api_key: false };
                    Toast.success(res.message || "已保存");
                    await this.refreshFromServer();
                  } catch (err) {
                    Toast.error(err.message);
                  }
                },
              })
            )
          )
        : null
    );

    clearEl(this.root, statsBar, listBox, searchTestBox, embeddingBox);
  }
}
