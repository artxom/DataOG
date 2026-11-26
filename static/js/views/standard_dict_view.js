/**
 * Data.O.G 数据标准第 1 步：全局标准字典管理 (StandardDictView)
 * 以「数据项编号」为主键维护全行最新数据标准，支持 46 列模板先导导入、增量合并、
 * AN–AS 列（oceanbase-oracle ~ 仓库DWS数据格式）在线增删改与多库物理类型查阅
 */

import { isAbortError } from "../core/http.js";
import { standardsApi } from "../core/platform_api.js";
import { badge, clearEl, el, svgIcon } from "../core/dom.js";
import { store } from "../core/state.js";
import { DataGrid } from "../ui/data_grid.js";
import { Modal } from "../ui/modal.js";
import { Toast } from "../ui/toast.js";
import { openUploadWizardModal } from "../ui/upload_wizard_modal.js";

// 严格对齐标准模板 AN (40) ~ AS (45) 列
const TEMPLATE_AN_AS_TYPE_FIELDS = [
  {
    key: "type_ob_oracle",
    col: "AN",
    label: "ocenbase-oracle数据格式 (AN)",
    placeholder: "如 VARCHAR2(50) / NUMBER(18,2)",
  },
  {
    key: "type_ob_mysql",
    col: "AO",
    label: "ocenbase-mysql数据格式 (AO)",
    placeholder: "如 VARCHAR(50) / DECIMAL(18,2)",
  },
  {
    key: "type_argodb",
    col: "AP",
    label: "argodb数据格式 (AP)",
    placeholder: "如 VARCHAR(50) / DECIMAL(18,2)",
  },
  {
    key: "type_oceanbase",
    col: "AQ",
    label: "ocenbase数据格式 (AQ)",
    placeholder: "如 VARCHAR(50) / DECIMAL(18,2)",
  },
  {
    key: "type_dw_mart",
    col: "AR",
    label: "数据仓库 集市 (AR)",
    placeholder: "如 VARCHAR2(50)",
  },
  {
    key: "type_dws",
    col: "AS",
    label: "仓库DWS数据格式 (AS)",
    placeholder: "如 NVARCHAR2(50) / DECIMAL(18,2)",
  },
];

const PREVIEW_TYPE_COLS = [
  { key: "type_dws", label: "仓库DWS数据格式 (AS)" },
  { key: "type_dw_mart", label: "数据仓库 集市 (AR)" },
  { key: "type_oceanbase", label: "ocenbase数据格式 (AQ)" },
  { key: "type_argodb", label: "argodb数据格式 (AP)" },
  { key: "type_ob_mysql", label: "ocenbase-mysql数据格式 (AO)" },
  { key: "type_ob_oracle", label: "ocenbase-oracle数据格式 (AN)" },
  { key: "type_mysql", label: "Mysql物理类型 (V)" },
  { key: "type_oracle", label: "Oracle物理类型 (W)" },
  { key: "type_dameng", label: "DaMeng数据格式 (Y)" },
  { key: "type_goldendb", label: "GoldenDb(MySQL模式) (Z)" },
];

export class StandardDictView {
  constructor({ onStateUpdate = null, onGoStep2 = null } = {}) {
    this.onStateUpdate = onStateUpdate;
    this.onGoStep2 = onGoStep2;
    this.loadedOnce = false;
    this.items = [];
    this.total = 0;
    this.page = 1;
    this.pageSize = 50;
    this.totalPages = 1;
    this.keyword = "";
    this.regCategory = "";
    this.dataCategory = "";
    this.previewTypeCol = "type_dws";
    this.summary = { total_count: 0, filename: "未导入", reg_categories: [], data_categories: [] };

    this._searchTimer = null;
    this._destroyed = false;
    this._isComposing = false;
    this._loadSeq = 0;
    this._loadAbort = null;
    this._grid = null;
    this._heroNode = null;
    this._lastSummarySig = "";
    this._lastHasItems = null;

    this.root = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });
    this.subbarMount = el("div", { className: "mac-subbar" });
    this.bodyMount = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });
    this.pagerMount = el("div", {
      className: "mac-subbar",
      style: { borderTop: "1px solid var(--os-border)", borderBottom: "none" },
    });

    this.totalBadge = badge("标准字典 0 条", "info");
    this.fileBadge = badge("", "neutral");
    this.fileBadge.hidden = true;
    this.fileBadge.style.display = "none";

    this.regSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "监管分类筛选",
        onChange: (e) => {
          this.regCategory = e.target.value;
          this.page = 1;
          this.loadList({ includeSummary: false });
        },
      },
      el("option", { value: "", text: "全部监管分类" })
    );

    this.catSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "数据类别筛选",
        onChange: (e) => {
          this.dataCategory = e.target.value;
          this.page = 1;
          this.loadList({ includeSummary: false });
        },
      },
      el("option", { value: "", text: "全部数据类别" })
    );

    this.typeColSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "预览物理格式列",
        onChange: (e) => {
          this.previewTypeCol = e.target.value;
          this._renderGridOnly();
        },
      },
      PREVIEW_TYPE_COLS.map((p) => {
        const o = el("option", { value: p.key, text: `展示: ${p.label}` });
        if (p.key === this.previewTypeCol) o.selected = true;
        return o;
      })
    );

    this.searchInput = el("input", {
      className: "mac-input mac-input-sm",
      type: "search",
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      placeholder: "搜索编号 / 中文名 / 英文简称 / 域...",
      style: { width: "240px" },
      onCompositionstart: () => {
        this._isComposing = true;
      },
      onCompositionend: (e) => {
        this._isComposing = false;
        this.keyword = e.target.value;
        this._scheduleSearch();
      },
      onInput: (e) => {
        this.keyword = e.target.value;
        if (!this._isComposing) {
          this._scheduleSearch();
        }
      },
    });

    this.clearBtn = el("button", {
      type: "button",
      className: "btn btn-xs btn-danger",
      text: "清空字典",
      onClick: async () => {
        const ok = await Modal.confirm({
          title: "清空标准字典",
          message: "确认清空平台当前维护的全部数据标准字典吗？（已绑定的库表字段记录会保留以便后续重导关联）",
          confirmText: "清空",
          danger: true,
        });
        if (!ok || this._destroyed) return;
        await standardsApi.clear();
        Toast.success("标准字典已清空");
        this.page = 1;
        await this.loadList({ includeSummary: true });
      },
    });
    this.clearBtn.hidden = true;
    this.clearBtn.style.display = "none";

    clearEl(
      this.subbarMount,
      el(
        "div",
        { className: "mac-subbar-group" },
        this.totalBadge,
        this.fileBadge,
        this.regSelect,
        this.catSelect,
        this.typeColSelect
      ),
      el(
        "div",
        { className: "mac-subbar-group" },
        el("search", {}, this.searchInput),
        this.clearBtn
      )
    );

    this.pagerInfoEl = el("div", {
      style: { fontSize: "11.5px", color: "var(--os-muted)" },
      text: "匹配 0 条 · 第 1 / 1 页",
    });
    this.pageSizeSelect = el(
      "select",
      {
        className: "mac-input input-xs",
        "aria-label": "每页条数",
        onChange: (e) => {
          this.pageSize = Number(e.target.value) || 50;
          this.page = 1;
          this.loadList({ includeSummary: false });
        },
      },
      [20, 50, 100].map((n) => {
        const o = el("option", { value: String(n), text: `每页 ${n} 条` });
        if (this.pageSize === n) o.selected = true;
        return o;
      })
    );
    this.prevPageBtn = el("button", {
      type: "button",
      className: "btn btn-xs",
      text: "上一页",
      disabled: true,
      onClick: () => {
        this.page = Math.max(1, this.page - 1);
        this.loadList({ includeSummary: false });
      },
    });
    this.nextPageBtn = el("button", {
      type: "button",
      className: "btn btn-xs",
      text: "下一页",
      disabled: true,
      onClick: () => {
        this.page = Math.min(this.totalPages, this.page + 1);
        this.loadList({ includeSummary: false });
      },
    });

    clearEl(
      this.pagerMount,
      this.pagerInfoEl,
      el("div", { className: "mac-subbar-group" }, this.pageSizeSelect, this.prevPageBtn, this.nextPageBtn)
    );
    clearEl(this.root, this.subbarMount, this.bodyMount, this.pagerMount);
  }

  destroy() {
    this._destroyed = true;
    clearTimeout(this._searchTimer);
    this._searchTimer = null;
    this._loadAbort?.abort?.();
    this._loadAbort = null;
    this._grid?.destroy?.();
    this._grid = null;
    this._heroNode = null;
    this.items = [];
    clearEl(this.bodyMount);
  }

  _scheduleSearch() {
    clearTimeout(this._searchTimer);
    this._searchTimer = setTimeout(() => {
      if (this._destroyed) return;
      this.page = 1;
      this.loadList({ includeSummary: false });
    }, 240);
  }

  async ensureLoaded(force = false) {
    if (this._destroyed) return;
    if (this.loadedOnce && !force) return;
    await this.loadList({ includeSummary: true });
  }

  async loadList({ includeSummary = !this.loadedOnce } = {}) {
    if (this._destroyed) return;
    this._loadAbort?.abort?.();
    const ctrl = typeof AbortController === "function" ? new AbortController() : null;
    this._loadAbort = ctrl;
    const seq = ++this._loadSeq;

    try {
      const res = await standardsApi.list({
        keyword: this.keyword.trim(),
        regCategory: this.regCategory,
        dataCategory: this.dataCategory,
        page: this.page,
        pageSize: this.pageSize,
        includeSummary,
        signal: ctrl?.signal,
      });
      if (this._destroyed || seq !== this._loadSeq) return;
      if (this._loadAbort === ctrl) this._loadAbort = null;

      this.loadedOnce = true;
      this.items = res.items || [];
      this.total = res.total || 0;
      this.page = res.page || 1;
      this.totalPages = res.total_pages || 1;

      if (res.summary) {
        this.summary = res.summary;
        store.updateStandardDictSummary(this.summary);
      }

      this.render();
      const hasItems = (this.summary.total_count || 0) > 0;
      if (this._lastHasItems !== hasItems) {
        this._lastHasItems = hasItems;
        this.onStateUpdate?.();
      }
    } catch (err) {
      if (isAbortError(err)) return;
      if (this._destroyed || seq !== this._loadSeq) return;
      if (this._loadAbort === ctrl) this._loadAbort = null;
      Toast.error(err.message);
    }
  }

  openImportWizard() {
    openUploadWizardModal({
      title: "导入数据标准字典",
      description:
        "完整兼容 46 列标准数据字典结构（含模板 AN–AS 列各数据库物理格式）。默认按「数据项编号」增量合并更新，自动保留已绑定的库表字段关系。",
      templateColumns: [
        { name: "数据项编号", required: true },
        { name: "中文名称", required: true },
        { name: "英文简称", required: true },
        { name: "仓库DWS数据格式 (AS)", required: true },
        { name: "数据仓库 集市 (AR)" },
        { name: "ocenbase-oracle ~ argodb (AN-AQ)" },
        { name: "引用域 / 业务定义" },
      ],
      onDownloadBlankTemplate: () => standardsApi.downloadTemplate(),
      modes: [
        {
          value: "upsert",
          label: "增量合并更新（推荐）",
          desc: "按数据项编号新增或覆盖标准，保留全部库表字段绑定",
        },
        {
          value: "replace",
          label: "全量覆盖标准字典",
          desc: "清空现有字典后重新入库（仍保留既有字段编号关联）",
        },
      ],
      defaultMode: "upsert",
      accept: ".xlsx,.xlsm",
      onUpload: async (file, mode, uploadCtx) => {
        const res = await standardsApi.uploadDictionary(file, mode || "upsert", uploadCtx);
        if (res.summary) {
          this.summary = res.summary;
          store.updateStandardDictSummary(res.summary);
        }
        Toast.success(res.message || `已导入 ${res.imported_count || 0} 条数据标准`);
        this.page = 1;
        await this.loadList({ includeSummary: true });
      },
    });
  }

  openEditItemModal(existing = null) {
    const isEdit = Boolean(existing?.std_code);
    const baseExisting = existing && typeof existing === "object" ? { ...existing } : {};
    delete baseExisting.updated_at;

    const data = {
      ...baseExisting,
      std_code: existing?.std_code || "",
      cn_name: existing?.cn_name || "",
      en_abbr: existing?.en_abbr || "",
      en_name: existing?.en_name || "",
      reg_category: existing?.reg_category || "业务数据",
      data_category: existing?.data_category || "",
      domain_name: existing?.domain_name || "",
      domain_code: existing?.domain_code || "",
      max_length: existing?.max_length || "",
      min_scale: existing?.min_scale || "",
      type_ob_oracle: existing?.type_ob_oracle || "",
      type_ob_mysql: existing?.type_ob_mysql || "",
      type_argodb: existing?.type_argodb || "",
      type_oceanbase: existing?.type_oceanbase || "",
      type_dw_mart: existing?.type_dw_mart || "",
      type_dws: existing?.type_dws || "",
      source_system: existing?.source_system || "",
      biz_definition: existing?.biz_definition || "",
      biz_rule: existing?.biz_rule || "",
      remark: existing?.remark || "",
    };

    const mkField = (label, key, placeholder = "", span2 = false) => {
      const ctrl =
        key === "biz_definition" || key === "biz_rule"
          ? el("textarea", {
              className: "mac-input",
              style: { height: "54px", padding: "6px 9px", resize: "vertical" },
              placeholder,
              value: data[key],
              onInput: (e) => (data[key] = e.target.value),
            })
          : el("input", {
              className: "mac-input",
              placeholder,
              value: data[key],
              onInput: (e) => (data[key] = e.target.value),
            });
      return el(
        "div",
        { className: `form-field ${span2 ? "span-2" : ""}`.trim() },
        el("label", { className: "form-label", text: label }),
        ctrl
      );
    };

    const body = el(
      "div",
      { className: "form-grid" },
      mkField("数据项编号 *", "std_code", "如 CUST000101"),
      mkField("监管分类", "reg_category", "业务数据 / 技术数据"),
      mkField("中文名称 *", "cn_name", "如 客户统一编号"),
      mkField("英文简称 *", "en_abbr", "如 CUST_ID"),
      el(
        "div",
        { className: "form-section-title" },
        el("span", { text: "物理数据格式（标准模板 AN–AS 列）" }),
        badge("AN → AS", "neutral")
      ),
      ...TEMPLATE_AN_AS_TYPE_FIELDS.map((f) => mkField(f.label, f.key, f.placeholder)),
      el(
        "div",
        { className: "form-section-title" },
        el("span", { text: "属性与业务说明" })
      ),
      mkField("引用域中文名", "domain_name", "如 金额类(18,2)"),
      mkField("数据类别", "data_category", "如 金额类 / 编码类"),
      mkField("最大录入长度", "max_length", "如 32"),
      mkField("最小录入精度", "min_scale", "如 2"),
      mkField("制定依据系统", "source_system", "如 存款中心"),
      mkField("英文全称", "en_name", "Customer Identifier"),
      mkField("业务定义", "biz_definition", "业务口径与用途描述", true),
      mkField("业务规则", "biz_rule", "值域或校验规则", true),
      mkField("备注", "remark", "", true)
    );

    const saveBtn = el("button", {
      type: "button",
      className: "mac-btn mac-btn-primary",
      text: isEdit ? "保存修改" : "新增标准",
      onClick: async () => {
        try {
          saveBtn.disabled = true;
          const payload = {
            ...data,
            std_code: data.std_code.trim(),
            cn_name: data.cn_name.trim(),
            en_abbr: data.en_abbr.trim(),
          };
          const res = isEdit
            ? await standardsApi.updateItem(existing.std_code, payload)
            : await standardsApi.createItem(payload);
          Toast.success(res.message || "已保存");
          Modal.close("saved");
          await this.loadList({ includeSummary: true });
        } catch (err) {
          Toast.error(err.message);
        } finally {
          saveBtn.disabled = false;
        }
      },
    });

    Modal.open({
      title: isEdit ? `编辑标准 · ${existing.std_code}` : "新增数据标准条目",
      width: "min(680px, 94vw)",
      body,
      footer: [
        el("button", { type: "button", className: "mac-btn", text: "取消", onClick: () => Modal.close() }),
        saveBtn,
      ],
    });
  }

  renderHeaderActions() {
    return el(
      "div",
      { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
      el(
        "button",
        {
          type: "button",
          className: "mac-btn mac-btn-sm",
          onClick: () => this.openEditItemModal(null),
        },
        svgIcon("plus", 13),
        el("span", { text: "新增标准" })
      ),
      el(
        "button",
        {
          type: "button",
          className: "mac-btn mac-btn-sm mac-btn-primary",
          onClick: () => this.openImportWizard(),
        },
        svgIcon("upload", 13),
        el("span", { text: "导入标准字典" })
      ),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: "导出字典",
        onClick: () =>
          standardsApi
            .exportDictionary({
              keyword: this.keyword.trim(),
              regCategory: this.regCategory,
              dataCategory: this.dataCategory,
            })
            .catch((e) => Toast.error(e.message)),
      }),
      this.summary.total_count > 0 && typeof this.onGoStep2 === "function"
        ? el("button", {
            type: "button",
            className: "mac-btn mac-btn-sm",
            text: "下一步：落标绑定 →",
            onClick: () => this.onGoStep2(),
          })
        : null
    );
  }

  _syncSubbarFromSummary() {
    const totalCount = Number(this.summary?.total_count ?? 0);
    this.totalBadge.textContent = `标准字典 ${totalCount} 条`;

    const hasFile = Boolean(this.summary?.filename && this.summary.filename !== "未导入");
    this.fileBadge.textContent = hasFile ? `最近导入: ${this.summary.filename}` : "";
    this.fileBadge.hidden = !hasFile;
    this.fileBadge.style.display = hasFile ? "" : "none";

    const showClear = totalCount > 0;
    this.clearBtn.hidden = !showClear;
    this.clearBtn.style.display = showClear ? "" : "none";

    const regList = this.summary?.reg_categories || [];
    const catList = this.summary?.data_categories || [];
    const sig = JSON.stringify({
      r: regList,
      c: catList,
      sr: this.regCategory,
      sc: this.dataCategory,
    });
    if (sig !== this._lastSummarySig) {
      this._lastSummarySig = sig;
      clearEl(
        this.regSelect,
        el("option", { value: "", text: "全部监管分类" }),
        regList.map((c) => {
          const o = el("option", { value: c.value, text: `${c.value} (${c.count})` });
          if (c.value === this.regCategory) o.selected = true;
          return o;
        })
      );
      this.regSelect.value = this.regCategory;

      clearEl(
        this.catSelect,
        el("option", { value: "", text: "全部数据类别" }),
        catList.map((c) => {
          const o = el("option", { value: c.value, text: `${c.value} (${c.count})` });
          if (c.value === this.dataCategory) o.selected = true;
          return o;
        })
      );
      this.catSelect.value = this.dataCategory;
    }

    if (
      globalThis.document?.activeElement !== this.searchInput &&
      this.searchInput.value !== this.keyword
    ) {
      this.searchInput.value = this.keyword;
    }
    if (this.typeColSelect.value !== this.previewTypeCol) {
      this.typeColSelect.value = this.previewTypeCol;
    }
  }

  _buildColumns() {
    const activeColDef =
      PREVIEW_TYPE_COLS.find((p) => p.key === this.previewTypeCol) || PREVIEW_TYPE_COLS[0];
    return [
      {
        key: "std_code",
        label: "数据项编号",
        render: (r) => badge(r.std_code, r.is_standard_dict === "1" ? "info" : "neutral"),
      },
      {
        key: "cn_name",
        label: "中文名称",
        render: (r) =>
          el(
            "div",
            {},
            el("div", { style: { fontWeight: "600" }, text: r.cn_name }),
            r.biz_definition
              ? el("div", {
                  style: {
                    fontSize: "11px",
                    color: "var(--os-muted)",
                    maxWidth: "280px",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  },
                  title: r.biz_definition,
                  text: r.biz_definition,
                })
              : null
          ),
      },
      {
        key: "en_abbr",
        label: "英文简称",
        render: (r) => el("span", { className: "mono", style: { fontWeight: "600" }, text: r.en_abbr }),
      },
      {
        key: this.previewTypeCol,
        label: activeColDef.label,
        render: (r) =>
          el("span", {
            className: "mono",
            text: r[this.previewTypeCol] || "-",
          }),
      },
      {
        key: "domain_name",
        label: "引用域 / 类别",
        render: (r) =>
          el(
            "div",
            { style: { fontSize: "11.5px" } },
            el("div", { text: r.domain_name || "-" }),
            r.data_category
              ? el("div", { style: { fontSize: "11px", color: "var(--os-muted)" }, text: r.data_category })
              : null
          ),
      },
      {
        key: "source_system",
        label: "依据系统",
        render: (r) => r.source_system || r.reg_category || "-",
      },
      {
        key: "actions",
        label: "操作",
        sortable: false,
        render: (r) =>
          el(
            "div",
            { style: { display: "flex", gap: "4px" } },
            el("button", {
              type: "button",
              className: "btn btn-xs",
              text: "编辑",
              onClick: (e) => {
                e.stopPropagation();
                this.openEditItemModal(r);
              },
            }),
            el("button", {
              type: "button",
              className: "btn btn-xs btn-danger",
              text: "删除",
              onClick: async (e) => {
                e.stopPropagation();
                const ok = await Modal.confirm({
                  title: "删除标准条目",
                  message: `确认从标准字典中删除 [${r.std_code} · ${r.cn_name}] 吗？`,
                  confirmText: "删除",
                  danger: true,
                });
                if (!ok || this._destroyed) return;
                await standardsApi.deleteItem(r.std_code);
                Toast.success("已删除");
                await this.loadList({ includeSummary: true });
              },
            })
          ),
      },
    ];
  }

  _renderGridOnly() {
    const columns = this._buildColumns();
    if (!this._grid) {
      this._grid = new DataGrid({
        ariaLabel: "数据标准字典",
        rowKey: "std_code",
        rows: this.items,
        columns,
        emptyText: "当前筛选条件下无匹配的数据标准条目",
        onRowClick: (row) => this.openEditItemModal(row),
      });
    } else {
      const prevTop = this._grid.root.scrollTop || 0;
      const prevLeft = this._grid.root.scrollLeft || 0;
      this._grid.setData(this.items, columns);
      this._grid.root.scrollTop = prevTop;
      this._grid.root.scrollLeft = prevLeft;
    }
    if (this.bodyMount.children?.[0] !== this._grid.root || this.bodyMount.children.length !== 1) {
      clearEl(this.bodyMount, this._grid.root);
    }
  }

  _ensureHeroNode() {
    if (!this._heroNode) {
      this._heroNode = el(
        "div",
        { className: "explorer-empty-state" },
        el(
          "div",
          { className: "explorer-empty-card", style: { maxWidth: "540px", padding: "26px 28px" } },
          el("div", { style: { marginBottom: "10px", color: "var(--os-accent)" } }, svgIcon("book", 34)),
          el("h3", { style: { margin: "0 0 8px 0", fontSize: "15px" }, text: "从维护统一数据标准字典开始" }),
          el("p", {
            style: { margin: "0 0 18px 0", fontSize: "12.5px", color: "var(--os-text-secondary)", lineHeight: "1.6" },
            text: "请先下载 46 列标准字典系统模板并导入最新企业数据字典；完成后即可进入「2. 落标绑定与贯标」与「3. 跨环境比对」。",
          }),
          el(
            "div",
            { style: { display: "flex", justifyContent: "center", gap: "10px", flexWrap: "wrap" } },
            el(
              "button",
              {
                type: "button",
                className: "mac-btn",
                onClick: () => standardsApi.downloadTemplate().catch((e) => Toast.error(e.message)),
              },
              svgIcon("download", 14),
              el("span", { text: "下载系统标准模板 (.xlsx)" })
            ),
            el(
              "button",
              {
                type: "button",
                className: "mac-btn mac-btn-primary",
                onClick: () => this.openImportWizard(),
              },
              svgIcon("upload", 14),
              el("span", { text: "导入标准字典" })
            ),
            el(
              "button",
              {
                type: "button",
                className: "mac-btn",
                onClick: () => this.openEditItemModal(null),
              },
              svgIcon("plus", 14),
              el("span", { text: "手工新增" })
            )
          )
        )
      );
    }
    return this._heroNode;
  }

  render() {
    this._syncSubbarFromSummary();

    if (this.summary.total_count === 0 && !this.keyword && !this.regCategory && !this.dataCategory) {
      this._grid?.destroy?.();
      this._grid = null;
      const hero = this._ensureHeroNode();
      if (this.bodyMount.children?.[0] !== hero || this.bodyMount.children.length !== 1) {
        clearEl(this.bodyMount, hero);
      }
      this.pagerMount.hidden = true;
      this.pagerMount.style.display = "none";
      return;
    }

    this._renderGridOnly();
    this.pagerMount.hidden = false;
    this.pagerMount.style.display = "";
    this.pagerInfoEl.textContent = `匹配 ${this.total} 条 · 第 ${this.page} / ${this.totalPages} 页`;
    this.pageSizeSelect.value = String(this.pageSize);
    this.prevPageBtn.disabled = this.page <= 1;
    this.nextPageBtn.disabled = this.page >= this.totalPages;
  }
}
