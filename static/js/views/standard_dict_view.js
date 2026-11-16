/**
 * Data.O.G 数据标准第 1 步：全局标准字典管理 (StandardDictView)
 * 以「数据项编号」为主键维护全行最新数据标准，支持 46 列模板先导导入、增量合并、在线增删改与多库物理类型查阅
 */

import { standardsApi } from "../core/api.js";
import { badge, clearEl, el, svgIcon } from "../core/dom.js";
import { store } from "../core/state.js";
import { DataGrid } from "../ui/data_grid.js";
import { Modal } from "../ui/modal.js";
import { Toast } from "../ui/toast.js";
import { openUploadWizardModal } from "../ui/upload_wizard_modal.js";

const PREVIEW_TYPE_COLS = [
  { key: "type_dws", label: "仓库DWS格式" },
  { key: "type_dw_mart", label: "数据仓库 集市" },
  { key: "type_argodb", label: "ArgoDB格式" },
  { key: "type_mysql", label: "MySQL物理类型" },
  { key: "type_oracle", label: "Oracle物理类型" },
  { key: "type_ob_oracle", label: "OB-Oracle格式" },
  { key: "type_ob_mysql", label: "OB-MySQL格式" },
  { key: "type_dameng", label: "达梦格式" },
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

    this.root = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });
    this.subbarMount = el("div", {});
    this.bodyMount = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });
    this.pagerMount = el("div", {});

    this.searchInput = el("input", {
      className: "mac-input mac-input-sm",
      type: "search",
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      placeholder: "搜索编号 / 中文名 / 英文简称 / 域...",
      style: { width: "240px" },
      onInput: (e) => {
        this.keyword = e.target.value;
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => {
          this.page = 1;
          this.loadList();
        }, 220);
      },
    });
  }

  async ensureLoaded(force = false) {
    if (this.loadedOnce && !force) return;
    await this.loadList();
  }

  async loadList() {
    try {
      const res = await standardsApi.list({
        keyword: this.keyword.trim(),
        regCategory: this.regCategory,
        dataCategory: this.dataCategory,
        page: this.page,
        pageSize: this.pageSize,
      });
      this.loadedOnce = true;
      this.items = res.items || [];
      this.total = res.total || 0;
      this.page = res.page || 1;
      this.totalPages = res.total_pages || 1;
      this.summary = res.summary || this.summary;
      store.updateStandardDictSummary(this.summary);
      this.render();
      this.onStateUpdate?.();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  openImportWizard() {
    openUploadWizardModal({
      title: "导入数据标准字典",
      description:
        "完整兼容 46 列标准数据字典结构（含各数据库物理格式列）。默认按「数据项编号」增量合并更新，自动保留已绑定的库表字段关系。",
      templateColumns: [
        { name: "数据项编号", required: true },
        { name: "中文名称", required: true },
        { name: "英文简称", required: true },
        { name: "仓库DWS数据格式", required: true },
        { name: "数据仓库 集市" },
        { name: "Mysql / Oracle / ArgoDB" },
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
        if (res.summary) store.updateStandardDictSummary(res.summary);
        Toast.success(res.message || `已导入 ${res.imported_count || 0} 条数据标准`);
        this.page = 1;
        await this.loadList();
      },
    });
  }

  openEditItemModal(existing = null) {
    const isEdit = Boolean(existing?.std_code);
    const data = {
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
      type_dws: existing?.type_dws || "",
      type_dw_mart: existing?.type_dw_mart || "",
      type_argodb: existing?.type_argodb || "",
      type_mysql: existing?.type_mysql || "",
      type_oracle: existing?.type_oracle || "",
      type_dameng: existing?.type_dameng || "",
      type_ob_oracle: existing?.type_ob_oracle || "",
      type_ob_mysql: existing?.type_ob_mysql || "",
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
      mkField("仓库DWS数据格式", "type_dws", "如 NVARCHAR2(50) / DECIMAL(18,2)"),
      mkField("数据仓库 集市", "type_dw_mart", "如 VARCHAR2(50)"),
      mkField("ArgoDB / Inceptor", "type_argodb", "如 VARCHAR(32)"),
      mkField("MySQL 物理类型", "type_mysql", "如 VARCHAR(32)"),
      mkField("Oracle 物理类型", "type_oracle", "如 VARCHAR2(32)"),
      mkField("达梦 DM8 格式", "type_dameng", "如 VARCHAR(32)"),
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
          await this.loadList();
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

  render() {
    this.searchInput.value = this.keyword;

    const regSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "监管分类筛选",
        onChange: (e) => {
          this.regCategory = e.target.value;
          this.page = 1;
          this.loadList();
        },
      },
      [
        el("option", { value: "", text: "全部监管分类" }),
        ...(this.summary.reg_categories || []).map((c) => {
          const o = el("option", { value: c.value, text: `${c.value} (${c.count})` });
          if (c.value === this.regCategory) o.selected = true;
          return o;
        }),
      ]
    );

    const catSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "数据类别筛选",
        onChange: (e) => {
          this.dataCategory = e.target.value;
          this.page = 1;
          this.loadList();
        },
      },
      [
        el("option", { value: "", text: "全部数据类别" }),
        ...(this.summary.data_categories || []).map((c) => {
          const o = el("option", { value: c.value, text: `${c.value} (${c.count})` });
          if (c.value === this.dataCategory) o.selected = true;
          return o;
        }),
      ]
    );

    const typeColSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "预览物理格式列",
        onChange: (e) => {
          this.previewTypeCol = e.target.value;
          this.render();
        },
      },
      PREVIEW_TYPE_COLS.map((p) => {
        const o = el("option", { value: p.key, text: `展示: ${p.label}` });
        if (p.key === this.previewTypeCol) o.selected = true;
        return o;
      })
    );

    const subbar = el(
      "div",
      { className: "mac-subbar" },
      el(
        "div",
        { className: "mac-subbar-group" },
        badge(`标准字典 ${this.summary.total_count ?? 0} 条`, "info"),
        this.summary.filename && this.summary.filename !== "未导入"
          ? badge(`最近导入: ${this.summary.filename}`, "neutral")
          : null,
        regSelect,
        catSelect,
        typeColSelect
      ),
      el(
        "div",
        { className: "mac-subbar-group" },
        el("search", {}, this.searchInput),
        this.summary.total_count > 0
          ? el("button", {
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
                if (!ok) return;
                await standardsApi.clear();
                Toast.success("标准字典已清空");
                this.page = 1;
                await this.loadList();
              },
            })
          : null
      )
    );
    clearEl(this.subbarMount, subbar);

    if (this.summary.total_count === 0 && !this.keyword && !this.regCategory && !this.dataCategory) {
      const hero = el(
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
      clearEl(this.bodyMount, hero);
      clearEl(this.pagerMount);
      clearEl(this.root, this.subbarMount, this.bodyMount, this.pagerMount);
      return;
    }

    const activeColDef = PREVIEW_TYPE_COLS.find((p) => p.key === this.previewTypeCol) || PREVIEW_TYPE_COLS[0];

    const grid = new DataGrid({
      ariaLabel: "数据标准字典",
      rowKey: "std_code",
      rows: this.items,
      emptyText: "当前筛选条件下无匹配的数据标准条目",
      onRowClick: (row) => this.openEditItemModal(row),
      columns: [
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
              text: r[this.previewTypeCol] || r.type_dws || r.type_dw_mart || "-",
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
                  if (!ok) return;
                  await standardsApi.deleteItem(r.std_code);
                  Toast.success("已删除");
                  await this.loadList();
                },
              })
            ),
        },
      ],
    });
    clearEl(this.bodyMount, grid.root);

    const pager = el(
      "div",
      { className: "mac-subbar", style: { borderTop: "1px solid var(--os-border)", borderBottom: "none" } },
      el(
        "div",
        { style: { fontSize: "11.5px", color: "var(--os-muted)" } },
        `匹配 ${this.total} 条 · 第 ${this.page} / ${this.totalPages} 页`
      ),
      el(
        "div",
        { className: "mac-subbar-group" },
        el(
          "select",
          {
            className: "mac-input input-xs",
            "aria-label": "每页条数",
            onChange: (e) => {
              this.pageSize = Number(e.target.value) || 50;
              this.page = 1;
              this.loadList();
            },
          },
          [20, 50, 100].map((n) => {
            const o = el("option", { value: String(n), text: `每页 ${n} 条` });
            if (this.pageSize === n) o.selected = true;
            return o;
          })
        ),
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "上一页",
          disabled: this.page <= 1,
          onClick: () => {
            this.page = Math.max(1, this.page - 1);
            this.loadList();
          },
        }),
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "下一页",
          disabled: this.page >= this.totalPages,
          onClick: () => {
            this.page = Math.min(this.totalPages, this.page + 1);
            this.loadList();
          },
        })
      )
    );
    clearEl(this.pagerMount, pager);
    clearEl(this.root, this.subbarMount, this.bodyMount, this.pagerMount);
  }
}
