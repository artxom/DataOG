/**
 * 单库落标贯标视图 — 上传向导、导出报告、数据库载入与 SQL/重建预览弹窗混入类
 */

import { migratorApi } from "../../api/migrator_api.js";
import { isAbortError } from "../../core/http.js";
import { platformApi } from "../../core/platform_api.js";
import { badge, el } from "../../core/dom.js";
import { store } from "../../core/state.js";
import { openExportReportModal } from "../../ui/export_report_modal.js";
import { Modal } from "../../ui/modal.js";
import { SqlCodeBox } from "../../ui/sql_code_box.js";
import { Toast } from "../../ui/toast.js";
import { openUploadWizardModal } from "../../ui/upload_wizard_modal.js";

export class StandardMigratorModalsMixin {
  openBindingUploadWizard() {
    const connId = this.activeConnId || this.ws?.active_connection_id || "";
    if (!connId) {
      Toast.warn("请先选择一个数据源连接");
      return;
    }
    const fqActive = this.activeTable
      ? this.activeSchema
        ? `${this.activeSchema}.${this.activeTable}`
        : this.activeTable
      : "";

    openUploadWizardModal({
      title: "导入「表字段 ↔ 数据标准」对照关系",
      description:
        "先下载系统标准模板（支持直接导出当前已选表的全部物理字段预填模板），在「数据项编号」列填写对应标准编码后上传。",
      templateColumns: [
        { name: "模式名" },
        { name: "表名", required: true },
        { name: "物理字段名", required: true },
        { name: "数据项编号", required: true },
        { name: "标准英文简称 / 中文名称" },
      ],
      onDownloadBlankTemplate: () => migratorApi.downloadBindingTemplate({}),
      prefillButtonLabel: fqActive ? `导出「${fqActive}」预填字段模板 (.xlsx)` : "",
      onDownloadPrefilledTemplate: fqActive
        ? () =>
            migratorApi.downloadBindingTemplate({
              connectionId: connId,
              schemaName: this.activeSchema,
              tables: fqActive,
            })
        : null,
      modes: [
        {
          value: "merge",
          label: "增量合并更新（推荐）",
          desc: "按表字段新增或更新绑定，不影响同表未列出的其他字段",
        },
        {
          value: "replace_table",
          label: "按表覆盖重建",
          desc: "对 Excel 中涉及到的表先清空旧绑定再写入最新对照关系",
        },
      ],
      defaultMode: "merge",
      accept: ".xlsx,.xlsm",
      onUpload: async (file, mode, uploadCtx) => {
        const res = await migratorApi.uploadBindings(
          file,
          {
            connectionId: connId,
            defaultSchema: this.activeSchema || "",
            mode: mode || "merge",
            formatProfile: this.formatProfile,
          },
          uploadCtx
        );
        if (res.table_stats) this.tableStats = res.table_stats;
        if (res.active_table) {
          this.tableCheck = res.active_table;
          this.activeSchema = res.active_table.table?.schema_name || this.activeSchema;
          this.activeTable = res.active_table.table?.table_name || this.activeTable;
        } else if (res.tables?.length) {
          const picked =
            (this.activeTable &&
              res.tables.find(
                (t) =>
                  String(t.table?.table_name || "").toLowerCase() ===
                    String(this.activeTable).toLowerCase() &&
                  (!this.activeSchema ||
                    String(t.table?.schema_name || "").toLowerCase() ===
                      String(this.activeSchema).toLowerCase())
              )) ||
            res.tables.find((t) => t.table?.exists) ||
            res.tables[0];
          this.tableCheck = picked;
          this.activeSchema = picked.table?.schema_name || this.activeSchema;
          this.activeTable = picked.table?.table_name || this.activeTable;
        }
        Toast.success(res.message || "对照关系导入完成");
        if (res.warning) Toast.warn(res.warning);
        this.tree.renderTreeBody();
        this.render();
        this.onStateUpdate?.();
      },
    });
  }

  openChecklistUploadWizard() {
    openUploadWizardModal({
      title: "导入单库贯标改造清单",
      description:
        "支持导入左旧右新结构化字段贯标清单；未包含模式名列时将自动继承当前绑定的数据源默认 Schema。",
      templateColumns: [
        { name: "模式名" },
        { name: "表名", required: true },
        { name: "修改前字段名", required: true },
        { name: "修改后字段名", required: true },
        { name: "修改后类型", required: true },
        { name: "修改后注释" },
      ],
      onDownloadBlankTemplate: () => migratorApi.downloadTemplate(),
      accept: ".xlsx,.xlsm",
      onUpload: async (file, _mode, uploadCtx) => {
        const res = await migratorApi.uploadChecklist(
          file,
          {
            defaultSchema: this.ws?.active_connection?.default_schema || this.activeSchema || "",
            defaultTable: this.activeTable || "",
          },
          uploadCtx
        );
        this._applyStateResponse(res);
      },
    });
  }

  openExportDialog() {
    const cid =
      (this.subMode === "queue" ? this.ws?.active_connection_id : this.activeConnId) ||
      this.activeConnId ||
      this.ws?.active_connection_id ||
      "";
    if (!cid) {
      if (this.subMode === "queue" && (this.ws?.items || []).length > 0) {
        this._runExportReportTask(
          "导出贯标队列 Excel (.xlsx)",
          ({ taskId, signal }) => migratorApi.exportReport({ taskId, signal }),
          { syncCatalog: false }
        );
        return;
      }
      Toast.warn("请先在左侧选择或绑定数据源连接");
      return;
    }
    const conn = this._findConnection(cid);
    const dbLabel = conn ? `${conn.name} (${conn.db_type || "DB"})` : cid;
    const fmtProfile =
      cid === this.activeConnId ? this.formatProfile : conn?.standard_format || "AUTO";
    const dbType = String(conn?.db_type || "").toUpperCase();
    const defSchema = String(
      conn?.effective_default_schema ||
        conn?.default_schema ||
        (dbType === "SQLITE" ? "main" : "public")
    );
    const aliasDialects = new Set([
      "SQLITE",
      "INCEPTOR",
      "MYSQL",
      "MARIADB",
      "OCEANBASE",
      "DORIS",
      "STARROCKS",
      "TIDB",
    ]);
    const normSch = (rawS) => {
      const s = String(rawS || "").trim() || defSchema;
      if (s.toLowerCase() === "public" && defSchema.toLowerCase() !== "public" && aliasDialects.has(dbType)) {
        return defSchema;
      }
      if (dbType === "SQLITE") return "main";
      return s;
    };

    let liveStats = cid === this.activeConnId ? this.tableStats || {} : {};
    let liveCatalog = store.state.catalogTrees[cid] || null;

    const buildSchemaItems = () => {
      const map = new Map();
      const ensureEntry = (schName) => {
        const norm = normSch(schName);
        const low = norm.toLowerCase();
        if (!map.has(low)) {
          map.set(low, { name: norm, totalSet: new Set(), boundSet: new Set() });
        }
        return map.get(low);
      };

      for (const s of liveCatalog?.schemas || []) {
        const sName = typeof s === "string" ? s : s?.schema_name;
        if (sName) ensureEntry(sName);
      }
      for (const t of liveCatalog?.tables || []) {
        const tName = String(t?.table_name || "").trim().toLowerCase();
        if (!tName) continue;
        const entry = ensureEntry(t?.schema_name || defSchema);
        entry.totalSet.add(tName);
      }
      for (const [k, st] of Object.entries(liveStats || {})) {
        if (!st || Number(st.bound_count || 0) <= 0) continue;
        const dotIdx = k.indexOf(".");
        const rawS = dotIdx >= 0 ? k.slice(0, dotIdx) : defSchema;
        const rawT = (dotIdx >= 0 ? k.slice(dotIdx + 1) : k).trim().toLowerCase();
        if (!rawT) continue;
        const entry = ensureEntry(rawS);
        entry.boundSet.add(rawT);
        entry.totalSet.add(rawT);
      }

      return Array.from(map.values())
        .map((it) => ({
          name: it.name,
          total_count: it.totalSet.size,
          bound_count: it.boundSet.size,
        }))
        .sort((a, b) => {
          const aHas = a.bound_count > 0 ? 1 : 0;
          const bHas = b.bound_count > 0 ? 1 : 0;
          if (aHas !== bHas) return bHas - aHas;
          if (a.bound_count !== b.bound_count) return b.bound_count - a.bound_count;
          return a.name.localeCompare(b.name);
        });
    };

    let currentItems = buildSchemaItems();

    const ctrl = openExportReportModal({
      title: "导出库级落标与贯标报告",
      scopeBadge: `${conn ? conn.name : cid}`,
      getScopeSummary: (fState) => {
        const sel = Array.isArray(fState?.schemas) ? fState.schemas : [];
        const onlyBound = fState?.onlyBoundTables !== false;
        const selSet = new Set(sel.map((s) => String(s).toLowerCase()));
        const matched = sel.length
          ? currentItems.filter((it) => selSet.has(it.name.toLowerCase()))
          : currentItems;
        const totalTables = matched.reduce((acc, it) => acc + (it.total_count || 0), 0);
        const boundTables = matched.reduce((acc, it) => acc + (it.bound_count || 0), 0);
        const skippedTables = Math.max(0, totalTables - boundTables);
        const hasCatalogOrStats = Boolean(
          (liveCatalog?.tables || []).length || Object.keys(liveStats || {}).length
        );
        const scopeTag = sel.length ? `已选 ${sel.length} 个 Schema` : "全部 Schema";
        const modeTag = onlyBound ? "仅已绑标表" : "包含未绑标表";

        if (onlyBound && boundTables === 0 && hasCatalogOrStats) {
          return {
            badge: `${scopeTag} · ${modeTag}`,
            badgeTone: "warn",
            isWarn: true,
            hint: `数据源 [${dbLabel}] 在所选范围内暂无已绑定落标需求的表（可切换 Schema 或取消勾选「仅包含已绑标表」导出全库表）。`,
          };
        }
        return {
          badge: `${scopeTag} · ${modeTag}`,
          badgeTone: onlyBound ? "info" : "neutral",
          isWarn: false,
          hint: onlyBound
            ? hasCatalogOrStats
              ? `数据源 [${dbLabel}] 预计核验 ${boundTables} 张表一级在落标范围内的库表` +
                (skippedTables > 0 ? `，自动跳过 ${skippedTables} 张零绑标表` : "") +
                "（不触发无关表扫描）。"
              : `数据源 [${dbLabel}] 将仅对至少绑定 1 个落标字段的表进行核验并输出报告（不触发全库无关表扫描）。`
            : `数据源 [${dbLabel}] 将导出所选范围内全部 ${totalTables || "所有"} 张表的落标核验与贯标队列汇总。`,
        };
      },
      filterGroups: [
        {
          id: "schemas",
          label: "选择导出的 Schema（可多选，不选代表全部）",
          allLabel: "全部 Schema",
          items: currentItems,
          onRefreshItems: async () => {
            try {
              const cat = await platformApi.refreshMetadata(cid);
              if (cat) {
                liveCatalog = cat;
                store.updateCatalogTree(cid, cat);
              }
              currentItems = buildSchemaItems();
              return currentItems;
            } catch (err) {
              Toast.error(err?.message || "刷新目录失败");
              return null;
            }
          },
        },
      ],
      toggles: [
        {
          id: "onlyBoundTables",
          label: "仅包含已绑定落标需求的表（表级落标范围）",
          desc: "默认开启：若整张表没有任何字段绑定数据标准，将直接跳过不放入报告且不触发在线结构探测。",
          defaultChecked: true,
        },
      ],
      options: [
        {
          id: "html",
          title: "离线交互网页报告 (.html)",
          badge: "推荐",
          badgeTone: "success",
          icon: "explorer",
          shortExt: ".html",
          ctaLabel: "导出离线网页报告 (.html)",
          desc: "单个离线 HTML 文件，双击浏览器打开即可按库/表/状态交互筛选与查阅贯标 SQL，并内置「导出单 Sheet Excel (.xlsx)」按钮。",
          onExport: (fState) => {
            const schemasParam = (fState?.schemas || []).join(",");
            const onlyBound = fState?.onlyBoundTables !== false;
            return this._runExportReportTask(
              "导出离线网页报告 (.html)",
              ({ taskId, signal }) =>
                migratorApi.exportBindingReportHtml({
                  connectionId: cid,
                  formatProfile: fmtProfile,
                  tables: "",
                  schemas: schemasParam,
                  onlyBoundTables: onlyBound,
                  taskId,
                  signal,
                }),
              { syncCatalog: true, connId: cid }
            );
          },
        },
        {
          id: "xlsx",
          title: "直接导出库级 Excel (.xlsx)",
          badge: "单 Sheet 汇总",
          badgeTone: "info",
          icon: "table",
          shortExt: ".xlsx",
          ctaLabel: "导出库级 Excel (.xlsx)",
          desc: "在一个 Sheet「全库落标与贯标报告」中汇集所选范围内库表与字段的物理结构、数标匹配、落标状态与贯标队列进展。",
          onExport: (fState) => {
            const schemasParam = (fState?.schemas || []).join(",");
            const onlyBound = fState?.onlyBoundTables !== false;
            return this._runExportReportTask(
              "导出库级落标 Excel (.xlsx)",
              ({ taskId, signal }) =>
                migratorApi.exportBindingReport({
                  connectionId: cid,
                  formatProfile: fmtProfile,
                  tables: "",
                  schemas: schemasParam,
                  onlyBoundTables: onlyBound,
                  taskId,
                  signal,
                }),
              { syncCatalog: true, connId: cid }
            );
          },
        },
      ],
      secondaryActions: [
        {
          label: "全库预填对照模板 (.xlsx)",
          icon: "download",
          hint: "预填所选 Schema 下所有表字段与已绑标准，补充数据项编号后可直接回传导入",
          onClick: (fState) =>
            this._runExportReportTask(
              "下载预填对照模板 (.xlsx)",
              ({ taskId, signal }) =>
                migratorApi.downloadBindingTemplate({
                  connectionId: cid,
                  schemas: (fState?.schemas || []).join(","),
                  scope: "all",
                  taskId,
                  signal,
                }),
              { syncCatalog: true, connId: cid }
            ),
        },
        {
          label: "仅导出贯标队列 (.xlsx)",
          icon: "download",
          hint: "仅导出当前贯标改造执行台中的任务清单",
          onClick: () =>
            this._runExportReportTask(
              "导出贯标队列 Excel (.xlsx)",
              ({ taskId, signal }) => migratorApi.exportReport({ taskId, signal }),
              { syncCatalog: false }
            ),
        },
      ],
    });

    const needCat = !liveCatalog?.tables?.length;
    const needStats = cid !== this.activeConnId || !Object.keys(liveStats || {}).length;
    if (ctrl && (needCat || needStats)) {
      Promise.all([
        needCat ? platformApi.getMetadataTree(cid, false).catch(() => null) : null,
        needStats ? migratorApi.getBindingsOverview(cid, fmtProfile).catch(() => null) : null,
      ]).then(([catRes, ovRes]) => {
        if (this._destroyed || ctrl.isClosed()) return;
        let changed = false;
        if (catRes) {
          liveCatalog = catRes;
          store.updateCatalogTree(cid, catRes);
          changed = true;
        }
        if (ovRes?.table_stats) {
          liveStats = ovRes.table_stats;
          if (cid === this.activeConnId) this.tableStats = ovRes.table_stats;
          changed = true;
        }
        if (changed) {
          currentItems = buildSchemaItems();
          ctrl.updateGroupItems("schemas", currentItems);
        }
      });
    }
  }

  async openLoadFromDbModal() {
    const schemaInput = el("input", {
      className: "mac-input",
      placeholder: "默认 Schema",
      value: this.ws?.active_connection?.default_schema || this.activeSchema || "",
    });
    const tableInput = el("input", {
      className: "mac-input",
      placeholder: "留空读取多表",
      value: this.activeTable || "",
    });
    const maxInput = el("input", {
      className: "mac-input",
      type: "number",
      value: "30",
      min: "1",
      max: "200",
    });

    const body = el(
      "div",
      { className: "form-grid" },
      el("div", { className: "form-field span-2" }, el("label", { className: "form-label", text: "Schema" }), schemaInput),
      el("div", { className: "form-field" }, el("label", { className: "form-label", text: "指定表名" }), tableInput),
      el("div", { className: "form-field" }, el("label", { className: "form-label", text: "最大表数" }), maxInput)
    );

    const okBtn = el("button", {
      type: "button",
      className: "mac-btn mac-btn-primary",
      text: "载入",
      onClick: async () => {
        try {
          okBtn.disabled = true;
          const res = await this._runTracked("从数据库载入表结构", ({ taskId, signal }) =>
            migratorApi.loadFromDb(
              {
                connection_id: this.ws?.active_connection_id || this.activeConnId || null,
                schema_name: schemaInput.value.trim(),
                table_name: tableInput.value.trim(),
                max_tables: Number(maxInput.value) || 30,
              },
              { taskId, signal }
            )
          );
          if (this._destroyed) return;
          Modal.close("ok");
          this._applyStateResponse(res);
        } catch (err) {
          if (isAbortError(err)) return;
          Toast.error(err.message);
        } finally {
          okBtn.disabled = false;
        }
      },
    });

    Modal.open({
      title: "从数据库载入表结构",
      body,
      footer: [
        el("button", { type: "button", className: "mac-btn", text: "取消", onClick: () => Modal.close() }),
        okBtn,
      ],
    });
  }

  async openItemSqlPreviewModal(itemRow) {
    if (!itemRow?.id) return;
    try {
      const preview = await migratorApi.previewItemSql(itemRow.id);
      const tblFull = `${preview.schema_name}.${preview.table_name}`;
      const colLabel =
        preview.old_col_name && preview.new_col_name && preview.old_col_name.toLowerCase() !== preview.new_col_name.toLowerCase()
          ? `${preview.old_col_name} → ${preview.new_col_name}`
          : preview.old_col_name || preview.new_col_name || `#${preview.item_id}`;

      const sqlBox = new SqlCodeBox({
        title: `${tblFull}.${colLabel}`,
        value: preview.sql_preview || "-- 暂无可执行 SQL",
        readOnly: true,
        minHeight: "220px",
        filename: `migrate_${preview.schema_name}_${preview.table_name}_${preview.old_col_name || preview.item_id}.sql`,
      });

      const ch = preview.changes || {};
      const strategyBadge = preview.needs_rebuild
        ? badge("需整表保序重建", "warn")
        : preview.is_comment_only
        ? badge("仅更新字段注释", "info")
        : badge("原地无损 ALTER", "neutral");

      const diffPills = [];
      if (ch.name_changed) {
        diffPills.push(badge(`字段名: ${ch.cur_name} → ${ch.new_name}`, "info"));
      }
      if (ch.type_changed) {
        diffPills.push(badge(`物理类型: ${ch.cur_type} → ${ch.new_type}`, "info"));
      }
      if (ch.comment_changed) {
        diffPills.push(badge(`注释: ${ch.cur_comment || "空"} → ${ch.new_comment}`, "info"));
      }

      const body = el(
        "div",
        { style: { display: "flex", flexDirection: "column", gap: "10px" } },
        el(
          "div",
          { style: { display: "flex", gap: "6px", flexWrap: "wrap", alignItems: "center" } },
          preview.connection_name ? badge(preview.connection_name, "neutral") : null,
          badge(preview.dialect || "DWS", "info"),
          strategyBadge,
          ...diffPills
        ),
        sqlBox.root
      );

      const footer = [
        el("button", { type: "button", className: "mac-btn", text: "关闭", onClick: () => Modal.close() }),
      ];

      if (preview.needs_rebuild) {
        footer.push(
          el("button", {
            type: "button",
            className: "mac-btn mac-btn-primary",
            text: "预览整表保序重建 SQL",
            onClick: () => {
              Modal.close();
              this.openRebuildPreviewModal(
                preview.schema_name,
                preview.table_name,
                preview.connection_id || ""
              );
            },
          })
        );
      } else if (preview.can_execute_online && itemRow.status !== "COMPLETED") {
        footer.push(
          el("button", {
            type: "button",
            className: "mac-btn mac-btn-primary",
            text: "立即执行该字段 SQL",
            onClick: async () => {
              Modal.close();
              await this.executeSingleItem(itemRow);
            },
          })
        );
      }

      Modal.open({
        title: `贯标 SQL 预览 · ${tblFull}.${colLabel}`,
        width: "min(760px, 92vw)",
        body,
        footer,
      });
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async openRebuildPreviewModal(schemaName, tableName, connectionId = "") {
    const connId = connectionId || "";
    try {
      const preview = await migratorApi.previewRebuild({
        schema_name: schemaName,
        table_name: tableName,
        connection_id: connId,
        keep_backup: this.keepBackup,
      });
      const sqlBox = new SqlCodeBox({
        title: `${schemaName}.${tableName}`,
        value: preview.sql_preview || preview.rebuild_sql || preview.sql || "-- 暂无重建 SQL",
        readOnly: true,
        minHeight: "280px",
        filename: `rebuild_${schemaName}_${tableName}.sql`,
      });

      const backupCb = el("input", {
        type: "checkbox",
        checked: this.keepBackup,
        onChange: (e) => (this.keepBackup = e.target.checked),
      });

      const body = el(
        "div",
        { style: { display: "flex", flexDirection: "column", gap: "10px" } },
        el(
          "div",
          { style: { display: "flex", gap: "6px", flexWrap: "wrap" } },
          badge(`${preview.column_count ?? "-"} 列`, "info"),
          badge(preview.distribute_clause || "继承原表分布", "warn"),
          preview.partition_clause ? badge("继承分区边界", "success") : null
        ),
        sqlBox.root,
        el(
          "label",
          { style: { display: "flex", alignItems: "center", gap: "7px", fontSize: "12px" } },
          backupCb,
          el("span", { text: "保留原表时间戳备份 (_bak_...)" })
        )
      );

      const execBtn = el("button", {
        type: "button",
        className: "mac-btn mac-btn-primary",
        text: "执行保序重建",
        onClick: async () => {
          try {
            execBtn.disabled = true;
            const res = await this._runTracked(
              `整表保序重建 · ${schemaName}.${tableName}`,
              ({ taskId, signal }) =>
                migratorApi.executeRebuild(
                  {
                    schema_name: schemaName,
                    table_name: tableName,
                    connection_id: preview.connection_id || connId,
                    keep_backup: this.keepBackup,
                  },
                  { taskId, signal }
                )
            );
            if (this._destroyed) return;
            Modal.close("done");
            this._applyStateResponse(res);
          } catch (err) {
            if (isAbortError(err)) return;
            Toast.error(err.message);
          } finally {
            execBtn.disabled = false;
          }
        },
      });

      Modal.open({
        title: `整表保序重建预览 · ${schemaName}.${tableName}`,
        width: "min(820px, 94vw)",
        body,
        footer: [
          el("button", { type: "button", className: "mac-btn", text: "取消", onClick: () => Modal.close() }),
          execBtn,
        ],
      });
    } catch (err) {
      Toast.error(err.message);
    }
  }
}
