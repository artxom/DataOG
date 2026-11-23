/**
 * 跨环境比对视图 — 导出报告、铺底上传与数标书签选择弹窗混入类
 */

import { crossApi } from "../../api/cross_api.js";
import { platformApi } from "../../core/platform_api.js";
import { store } from "../../core/state.js";
import { openExportReportModal } from "../../ui/export_report_modal.js";
import { openStandardPickerModal } from "../../ui/standard_picker_modal.js";
import { Toast } from "../../ui/toast.js";
import { openUploadWizardModal } from "../../ui/upload_wizard_modal.js";

export class CrossComparatorModalsMixin {
  openExportDialog() {
    const ov = this.overview || {};
    const srcConn = ov.source_connection || {};
    const tgtConn = ov.target_connection || {};
    const savedTables = Array.isArray(ov.tables) ? ov.tables : [];
    if (!srcConn.configured && !tgtConn.configured && !savedTables.length) {
      Toast.warn("请先配置跨环境来源库与目标库，或先保存/导入映射表对");
      return;
    }
    const srcName = srcConn.configured ? srcConn.name || "来源库" : "未配来源库";
    const tgtName = tgtConn.configured ? tgtConn.name || "目标库" : "未配目标库";
    const srcCid = srcConn.configured ? String(srcConn.connection_id || "") : "";
    const tgtCid = tgtConn.configured ? String(tgtConn.connection_id || "") : "";

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

    const makeNormSch = (connInfo) => {
      const dbType = String(connInfo.db_type || "").toUpperCase();
      const defSch = String(
        connInfo.effective_default_schema ||
          connInfo.default_schema ||
          (dbType === "SQLITE" ? "main" : "public")
      );
      const norm = (rawS) => {
        const s = String(rawS || "").trim() || defSch;
        if (s.toLowerCase() === "public" && defSch.toLowerCase() !== "public" && aliasDialects.has(dbType)) {
          return defSch;
        }
        if (dbType === "SQLITE") return "main";
        return s;
      };
      return { defSch, norm };
    };
    const { defSch: srcDefSch, norm: normSrcSch } = makeNormSch(srcConn);
    const { defSch: tgtDefSch, norm: normTgtSch } = makeNormSch(tgtConn);

    const buildSideSchemaItems = (role) => {
      const connInfo = role === "source" ? srcConn : tgtConn;
      const cid = role === "source" ? srcCid : tgtCid;
      const cachedTree = cid ? store.state.catalogTrees[cid] : null;
      const defSch = role === "source" ? srcDefSch : tgtDefSch;
      const normSch = role === "source" ? normSrcSch : normTgtSch;

      const map = new Map();
      const ensureEntry = (rawS) => {
        const norm = normSch(rawS);
        const low = norm.toLowerCase();
        if (!map.has(low)) {
          map.set(low, { name: norm, totalSet: new Set(), boundSet: new Set() });
        }
        return map.get(low);
      };

      const rawSchemas = connInfo.schemas?.length
        ? connInfo.schemas
        : (cachedTree?.schemas || []).map((s) => (typeof s === "string" ? s : s?.schema_name));
      for (const sName of rawSchemas || []) {
        if (sName) ensureEntry(sName);
      }

      const rawTables = connInfo.tables?.length ? connInfo.tables : cachedTree?.tables || [];
      for (const t of rawTables) {
        const tName = String(t?.table_name || "").trim().toLowerCase();
        if (!tName) continue;
        ensureEntry(t?.schema_name || defSch).totalSet.add(tName);
      }

      const bStats = connInfo.binding_stats || {};
      for (const [k, st] of Object.entries(bStats)) {
        if (!st || Number(st.bound_count || 0) <= 0) continue;
        const dotIdx = k.indexOf(".");
        const rawS = dotIdx >= 0 ? k.slice(0, dotIdx) : defSch;
        const rawT = (dotIdx >= 0 ? k.slice(dotIdx + 1) : k).trim().toLowerCase();
        if (!rawT) continue;
        const entry = ensureEntry(rawS);
        entry.boundSet.add(rawT);
        entry.totalSet.add(rawT);
      }

      for (const tb of savedTables) {
        const sVal = role === "source" ? tb.source_schema : tb.target_schema;
        const tVal = role === "source" ? tb.source_table : tb.target_table;
        const tLow = String(tVal || tb.std_table_name || "").trim().toLowerCase();
        if (!tLow) continue;
        const entry = ensureEntry(sVal || defSch);
        entry.totalSet.add(tLow);
        if (Number(tb.bound_std_count || 0) > 0) {
          entry.boundSet.add(tLow);
        }
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

    let srcItems = buildSideSchemaItems("source");
    let tgtItems = buildSideSchemaItems("target");

    const filterGroups = [];
    if (srcConn.configured || srcItems.length) {
      filterGroups.push({
        id: "source_schemas",
        label: `来源库 Schema · ${srcName}（不选代表全部）`,
        allLabel: "全部来源 Schema",
        items: srcItems,
        onRefreshItems: srcCid
          ? async () => {
              try {
                const cat = await platformApi.refreshMetadata(srcCid);
                if (cat) {
                  store.updateCatalogTree(srcCid, cat);
                  srcConn.tables = cat.tables || [];
                  srcConn.schemas = (cat.schemas || []).map((s) =>
                    typeof s === "string" ? s : s.schema_name
                  );
                }
                srcItems = buildSideSchemaItems("source");
                return srcItems;
              } catch (err) {
                Toast.error(err?.message || "刷新来源库目录失败");
                return null;
              }
            }
          : null,
      });
    }
    if (tgtConn.configured || tgtItems.length) {
      filterGroups.push({
        id: "target_schemas",
        label: `目标库 Schema · ${tgtName}（不选代表全部）`,
        allLabel: "全部目标 Schema",
        items: tgtItems,
        onRefreshItems: tgtCid
          ? async () => {
              try {
                const cat = await platformApi.refreshMetadata(tgtCid);
                if (cat) {
                  store.updateCatalogTree(tgtCid, cat);
                  tgtConn.tables = cat.tables || [];
                  tgtConn.schemas = (cat.schemas || []).map((s) =>
                    typeof s === "string" ? s : s.schema_name
                  );
                }
                tgtItems = buildSideSchemaItems("target");
                return tgtItems;
              } catch (err) {
                Toast.error(err?.message || "刷新目标库目录失败");
                return null;
              }
            }
          : null,
      });
    }

    const ctrl = openExportReportModal({
      title: "导出跨环境库级比对报告",
      scopeBadge: `${srcName} ↔ ${tgtName}`,
      getScopeSummary: (fState) => {
        const selSrc = Array.isArray(fState?.source_schemas) ? fState.source_schemas : [];
        const selTgt = Array.isArray(fState?.target_schemas) ? fState.target_schemas : [];
        const onlyBound = fState?.onlyBoundTables !== false;
        const srcSet = new Set(selSrc.map((s) => String(s).toLowerCase()));
        const tgtSet = new Set(selTgt.map((s) => String(s).toLowerCase()));

        const srcMatched = selSrc.length
          ? srcItems.filter((it) => srcSet.has(it.name.toLowerCase()))
          : srcItems;
        const tgtMatched = selTgt.length
          ? tgtItems.filter((it) => tgtSet.has(it.name.toLowerCase()))
          : tgtItems;
        const srcBoundCnt = srcMatched.reduce((acc, it) => acc + (it.bound_count || 0), 0);
        const tgtBoundCnt = tgtMatched.reduce((acc, it) => acc + (it.bound_count || 0), 0);

        const srcStats = srcConn.binding_stats || {};
        const tgtStats = tgtConn.binding_stats || {};

        const matchingPairs = savedTables.filter((tb) => {
          const ssRaw = String(tb.source_schema || srcDefSch).toLowerCase();
          const tsRaw = String(tb.target_schema || tgtDefSch).toLowerCase();
          const ssNorm = normSrcSch(tb.source_schema || srcDefSch).toLowerCase();
          const tsNorm = normTgtSch(tb.target_schema || tgtDefSch).toLowerCase();
          if (srcSet.size && !srcSet.has(ssNorm) && !srcSet.has(ssRaw)) return false;
          if (tgtSet.size && !tgtSet.has(tsNorm) && !tgtSet.has(tsRaw)) return false;
          if (!onlyBound) return true;
          if (Number(tb.bound_std_count || 0) > 0) return true;
          const st = String(tb.source_table || tb.std_table_name || "").toLowerCase();
          const tt = String(tb.target_table || tb.std_table_name || "").toLowerCase();
          return (
            Number((srcStats[`${ssNorm}.${st}`] || srcStats[`${ssRaw}.${st}`] || {}).bound_count || 0) > 0 ||
            Number((tgtStats[`${tsNorm}.${tt}`] || tgtStats[`${tsRaw}.${tt}`] || {}).bound_count || 0) > 0
          );
        });

        const schemaLabelParts = [];
        if (selSrc.length) schemaLabelParts.push(`源 ${selSrc.length} Schema`);
        if (selTgt.length) schemaLabelParts.push(`目标 ${selTgt.length} Schema`);
        const scopeLabel = `${schemaLabelParts.length ? schemaLabelParts.join(" / ") : "全部 Schema"} · ${
          onlyBound ? "仅已绑标表" : "包含未绑标表"
        }`;

        const hasKnownData = Boolean(
          savedTables.length || srcItems.length || tgtItems.length
        );
        if (onlyBound && hasKnownData && !matchingPairs.length && srcBoundCnt === 0 && tgtBoundCnt === 0) {
          return {
            badge: scopeLabel,
            badgeTone: "warn",
            isWarn: true,
            hint: "所选 Schema 范围内暂无已绑定数据标准的映射表对或单库绑标表（可调整 Schema 或取消勾选「仅包含已绑标表」）。",
          };
        }
        return {
          badge: scopeLabel,
          badgeTone: onlyBound ? "info" : "neutral",
          isWarn: false,
          hint: onlyBound
            ? `命中 ${matchingPairs.length} 对已绑标映射表对（来源 ${srcBoundCnt} / 目标 ${tgtBoundCnt} 张含绑标表），自动跳过零绑标表（未配对表不做字段预比对）。`
            : `汇集所选范围内全部 ${matchingPairs.length} 对映射表对及未配对表清单（未配对表不做自动字段预比对）。`,
        };
      },
      filterGroups,
      toggles: [
        {
          id: "onlyBoundTables",
          label: "仅包含已绑定落标需求的表（表级落标范围）",
          desc: "默认开启：仅对至少有 1 个字段绑定数据标准的映射表对、推荐同名表与单侧独有表进行比对并输出报告。",
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
          desc: "独立离线 HTML 文件，双击即可离线还原「五虚拟列」图形化对标看板与「推荐同名表」清单，并支持随时一键导出单 Sheet Excel。",
          onExport: (fState) =>
            this._runExportReportTask(
              "导出跨环境离线网页报告 (.html)",
              ({ taskId, signal }) =>
                crossApi.exportReportHtml({
                  formatProfile: this.formatProfile,
                  sourceFormatProfile: this.sourceFormatProfile,
                  targetFormatProfile: this.targetFormatProfile,
                  sourceSchemas: (fState?.source_schemas || []).join(","),
                  targetSchemas: (fState?.target_schemas || []).join(","),
                  onlyBoundTables: fState?.onlyBoundTables !== false,
                  taskId,
                  signal,
                }),
              { syncCatalogs: true }
            ),
        },
        {
          id: "xlsx",
          title: "直接导出库级 Excel (.xlsx)",
          badge: "单 Sheet 汇总",
          badgeTone: "info",
          icon: "table",
          shortExt: ".xlsx",
          ctaLabel: "导出库级 Excel (.xlsx)",
          desc: "单个 Sheet「跨环境库级比对报告」同时包含所选范围内已配对表五栏字段明细及含绑标未配对表清单，附状态配色与自动筛选。",
          onExport: (fState) =>
            this._runExportReportTask(
              "导出跨环境库级 Excel (.xlsx)",
              ({ taskId, signal }) =>
                crossApi.exportReportExcel({
                  formatProfile: this.formatProfile,
                  sourceFormatProfile: this.sourceFormatProfile,
                  targetFormatProfile: this.targetFormatProfile,
                  sourceSchemas: (fState?.source_schemas || []).join(","),
                  targetSchemas: (fState?.target_schemas || []).join(","),
                  onlyBoundTables: fState?.onlyBoundTables !== false,
                  taskId,
                  signal,
                }),
              { syncCatalogs: true }
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

    if (ctrl) {
      if (srcCid && !(srcConn.tables || []).length) {
        platformApi
          .getMetadataTree(srcCid, false)
          .then((cat) => {
            if (this._destroyed || ctrl.isClosed() || !cat) return;
            store.updateCatalogTree(srcCid, cat);
            srcConn.tables = cat.tables || [];
            srcConn.schemas = (cat.schemas || []).map((s) =>
              typeof s === "string" ? s : s.schema_name
            );
            srcItems = buildSideSchemaItems("source");
            ctrl.updateGroupItems("source_schemas", srcItems);
          })
          .catch(() => {});
      }
      if (tgtCid && !(tgtConn.tables || []).length) {
        platformApi
          .getMetadataTree(tgtCid, false)
          .then((cat) => {
            if (this._destroyed || ctrl.isClosed() || !cat) return;
            store.updateCatalogTree(tgtCid, cat);
            tgtConn.tables = cat.tables || [];
            tgtConn.schemas = (cat.schemas || []).map((s) =>
              typeof s === "string" ? s : s.schema_name
            );
            tgtItems = buildSideSchemaItems("target");
            ctrl.updateGroupItems("target_schemas", tgtItems);
          })
          .catch(() => {});
      }
    }
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
