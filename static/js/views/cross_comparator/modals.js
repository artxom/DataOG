/**
 * 跨环境比对视图 — 导出报告、铺底上传与数标书签选择弹窗混入类
 */

import { crossApi } from "../../api/cross_api.js";
import { badge, el } from "../../core/dom.js";
import { openExportReportModal } from "../../ui/export_report_modal.js";
import { Modal } from "../../ui/modal.js";
import { openStandardPickerModal } from "../../ui/standard_picker_modal.js";
import { Toast } from "../../ui/toast.js";
import { openUploadWizardModal } from "../../ui/upload_wizard_modal.js";

export class CrossComparatorModalsMixin {
  openExportDialog() {
    const ov = this.overview || {};
    const srcConn = ov.source_connection || {};
    const tgtConn = ov.target_connection || {};
    if (!srcConn.configured && !tgtConn.configured && !(ov.tables || []).length) {
      Toast.warn("请先配置跨环境来源库与目标库，或先保存/导入映射表对");
      return;
    }
    const srcName = srcConn.configured ? srcConn.name || "来源库" : "未配来源库";
    const tgtName = tgtConn.configured ? tgtConn.name || "目标库" : "未配目标库";

    openExportReportModal({
      title: "导出跨环境库级比对报告",
      scopeBadge: `${srcName} ↔ ${tgtName}`,
      scopeHint:
        "汇集全部已保存映射表对的五虚拟列比对情况与双方落标标准，同时附上未配对同名表的推荐清单（不做自动字段预比对）；若库目录为空将先自动触发全库目录采集。",
      options: [
        {
          id: "html",
          title: "离线交互网页报告 (.html)",
          badge: "推荐",
          badgeTone: "success",
          icon: "explorer",
          shortExt: ".html",
          ctaLabel: "导出离线网页报告 (.html)",
          desc: "独立离线 HTML 文件，双击即可离线还原「五虚拟列」图形化对标看板与「推荐同名表」清单，并支持随时一键导出单 Sheet Excel。",
          onExport: () =>
            this._runExportReportTask("导出跨环境离线网页报告 (.html)", ({ taskId, signal }) =>
              crossApi.exportReportHtml({
                formatProfile: this.formatProfile,
                sourceFormatProfile: this.sourceFormatProfile,
                targetFormatProfile: this.targetFormatProfile,
                taskId,
                signal,
              })
            ),
        },
        {
          id: "xlsx",
          title: "直接导出库级 Excel (.xlsx)",
          badge: "单 Sheet 全库表",
          badgeTone: "info",
          icon: "table",
          shortExt: ".xlsx",
          ctaLabel: "导出库级 Excel (.xlsx)",
          desc: "单个 Sheet「跨环境库级比对报告」同时包含所有已配对表五栏字段明细及未配对同名表推荐清单，附状态配色与自动筛选。",
          onExport: () =>
            this._runExportReportTask("导出跨环境库级 Excel (.xlsx)", ({ taskId, signal }) =>
              crossApi.exportReportExcel({
                formatProfile: this.formatProfile,
                sourceFormatProfile: this.sourceFormatProfile,
                targetFormatProfile: this.targetFormatProfile,
                taskId,
                signal,
              })
            ),
        },
      ],
      secondaryActions: [
        {
          label: "导出三方绑定铺底表 (.xlsx)",
          icon: "download",
          hint: "保留旧版可回传重导入系统的数据标准铺底 Excel 格式",
          onClick: () => {
            if (!this.activeTableKey && !(ov.tables || []).length) {
              Toast.warn("暂无可导出的已保存映射表对");
              return;
            }
            this._runExportReportTask(
              "导出三方绑定铺底表 (.xlsx)",
              () => crossApi.exportExcel(this.activeTableKey),
              { syncCatalogs: false }
            );
          },
        },
        {
          label: "导出跨环境配置包 (.json)",
          icon: "download",
          hint: "导出已保存跨环境映射表对与字段绑定的 JSON 备份包",
          onClick: () =>
            this._runExportReportTask(
              "导出跨环境配置包 (.json)",
              () => crossApi.exportJson(),
              { syncCatalogs: false }
            ),
        },
      ],
    });
  }

  _openPickerForCandidate(side, columnName) {
    const list = side === "source" ? this.sourceCandidates : this.targetCandidates;
    const colObj = list.find(
      (c) => String(c.column_name || "").toLowerCase() === String(columnName || "").toLowerCase()
    );
    if (!colObj) return;
    const ov = this.overview || {};
    const dbType =
      side === "source"
        ? ov.source_connection?.db_type || "DWS"
        : ov.target_connection?.db_type || "DWS";
    const fmt = side === "source" ? this.sourceFormatProfile : this.targetFormatProfile;

    openStandardPickerModal({
      title: `绑定${side === "source" ? "来源" : "目标"}字段标准 · ${colObj.column_name}`,
      initialQuery: colObj.column_comment || colObj.column_name,
      currentStdCode: "",
      dbType,
      formatProfile: fmt,
      onSelect: (picked) => {
        if (!picked) return;
        const taken = this._takeCandidate(side, colObj.column_name);
        if (!taken) return;
        this._commitNewStandardBinding(side, taken, picked);
      },
    });
  }

  _openPickerForBoundFieldNewRow(side, fromRowIdx) {
    const fromRow = this.centerRows[fromRowIdx];
    const colObj = fromRow?.[`${side}_col`];
    if (!colObj) return;
    const ov = this.overview || {};
    const dbType =
      side === "source"
        ? ov.source_connection?.db_type || "DWS"
        : ov.target_connection?.db_type || "DWS";
    const fmt = side === "source" ? this.sourceFormatProfile : this.targetFormatProfile;
    const curCode = fromRow[`${side}_std_code`] || "";

    openStandardPickerModal({
      title: `另绑${side === "source" ? "来源" : "目标"}字段新标准 · ${colObj.column_name}`,
      initialQuery: colObj.column_comment || colObj.column_name || curCode,
      currentStdCode: curCode,
      dbType,
      formatProfile: fmt,
      onSelect: (picked) => {
        if (!picked) return;
        const liveIdx = this.centerRows.indexOf(fromRow);
        if (liveIdx === -1 || !fromRow[`${side}_col`]) return;
        this._commitNewStandardBinding(side, fromRow[`${side}_col`], picked, {
          removeFromRowIdx: liveIdx,
        });
      },
    });
  }

  _openBookmarkPicker(rowIdx, side) {
    const row = this.centerRows[rowIdx];
    if (!row) return;
    const ov = this.overview || {};
    const isBoth = side === "both";
    const colObj = isBoth ? row.target_col || row.source_col : row[`${side}_col`];
    if (!colObj) return;

    const currentCode = isBoth
      ? row.target_std_code || row.source_std_code
      : row[`${side}_std_code`] || "";
    const dbType =
      side === "source"
        ? ov.source_connection?.db_type || "DWS"
        : ov.target_connection?.db_type || "DWS";
    const fmt = side === "source" ? this.sourceFormatProfile : this.targetFormatProfile;

    openStandardPickerModal({
      title: isBoth
        ? `调整数据标准 · ${row.source_col?.column_name} ↔ ${row.target_col?.column_name}`
        : `调整${side === "source" ? "来源" : "目标"}字段标准 · ${colObj.column_name}`,
      initialQuery: currentCode || colObj.column_comment || colObj.column_name,
      currentStdCode: currentCode,
      dbType,
      formatProfile: fmt,
      onSelect: (picked) => {
        const liveIdx = this.centerRows.indexOf(row);
        if (liveIdx === -1) return;
        if (!picked) {
          if (isBoth) {
            if (row.source_col) this._returnToCandidates("source", row.source_col);
            if (row.target_col) this._returnToCandidates("target", row.target_col);
            this.centerRows.splice(liveIdx, 1);
          } else {
            this._unbindBoundField(side, liveIdx);
            return;
          }
          this._markDirty();
          return;
        }
        const code = picked.std_code;
        this._cacheStdItem(picked);
        if (!isBoth) {
          const oppSide = side === "source" ? "target" : "source";
          if (!row[`${oppSide}_col`] && row[`${side}_col`]) {
            const existingRow = this.centerRows.find(
              (r, idx) => idx !== liveIdx && !r[`${side}_col`] && r[`${oppSide}_std_code`] === code
            );
            if (existingRow) {
              const existingIdx = this.centerRows.indexOf(existingRow);
              this._bindFieldToRow(side, row[`${side}_col`], existingIdx, {
                removeFromRowIdx: liveIdx,
                stdItem: picked,
              });
              return;
            }
          }
        }
        if (isBoth || side === "source") {
          row.source_std_code = code;
          row.source_std = this._toStdBrief(picked, "source");
        }
        if (isBoth || side === "target") {
          row.target_std_code = code;
          row.target_std = this._toStdBrief(picked, "target");
          const ev = this._evaluateProvisionalTargetCompliance(row.target_col, row.target_std);
          row.target_compliance = ev.target_compliance;
          row.target_needs_migration = ev.target_needs_migration;
        }
        this._recomputeRowState(row);
        this._markDirty();
        if (row.source_std?._pendingType || row.target_std?._pendingType) {
          this._resolvePendingStdTypesAsync([code]);
        }
      },
    });
  }

  openPairingUploadWizard() {
    openUploadWizardModal({
      title: "批量铺底跨环境映射关系",
      description:
        "先下载系统映射模板（支持按当前已选来源/目标表直接导出预填模板），批量铺底后可直接在五虚拟列工作台微调。",
      templateColumns: [
        { name: "来源模式名" },
        { name: "来源表名", required: true },
        { name: "来源字段名", required: true },
        { name: "来源数据项编号" },
        { name: "目标模式名" },
        { name: "目标表名", required: true },
        { name: "目标字段名", required: true },
        { name: "目标数据项编号" },
      ],
      onDownloadBlankTemplate: () => crossApi.downloadPairingTemplate({}),
      prefillButtonLabel:
        this.draftTableBind.source_table || this.draftTableBind.target_table
          ? "导出当前两表预填映射模板 (.xlsx)"
          : "",
      onDownloadPrefilledTemplate:
        this.draftTableBind.source_table || this.draftTableBind.target_table
          ? () =>
              crossApi.downloadPairingTemplate({
                tableKey: this.activeTableKey,
                sourceSchema: this.draftTableBind.source_schema,
                sourceTable: this.draftTableBind.source_table,
                targetSchema: this.draftTableBind.target_schema,
                targetTable: this.draftTableBind.target_table,
              })
          : null,
      accept: ".xlsx,.xlsm",
      onUpload: async (file, _mode, uploadCtx) => {
        const res = await crossApi.uploadPairings(file, this.targetFormatProfile || "AUTO", {
          ...uploadCtx,
          sourceFormatProfile: this.sourceFormatProfile,
          targetFormatProfile: this.targetFormatProfile,
        });
        this.overview = res;
        this._syncSideFormatsFromOverview(res);
        this.activeTableKey = res.active_table_key || "";
        this._hydrateDraftFromComparison(res.active_comparison);
        Toast.success(res.message || "跨环境映射铺底成功");
        this.render();
        this.onStateUpdate?.();
      },
    });
  }
}
