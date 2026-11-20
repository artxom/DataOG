"""启明商业银行信创数仓演示沙箱 (demo_sandbox) 零 Docker 自动化测试."""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import openpyxl

from core.platform.context import PlatformContext
from core.platform.storage import PRIVATE_ONLY_TABLES
from demo_sandbox.banking_catalog import (
    build_standard_items,
    export_binding_import_template_xlsx,
    export_standard_dictionary_xlsx,
)
from demo_sandbox.live_seeder import generate_banking_dataset
from demo_sandbox.workspace_builder import CONN_DWS_ID, build_all_workspaces
from modules.cross_comparator.service import CrossComparatorService
from modules.standard_migrator.service import StandardMigratorService


class TestDemoSandboxAssets(unittest.TestCase):
    """无需 Docker 即可在 CI 中验证金融标准字典、造数暗雷、双零密钥工作区与百表离线报告闭环."""

    def test_banking_dataset_deterministic_and_anomalies_exact(self) -> None:
        ds = generate_banking_dataset()
        self.assertEqual(len(ds["branches"]), 12)
        self.assertEqual(len(ds["managers"]), 60)
        self.assertEqual(len(ds["customers"]), 2000)
        self.assertEqual(len(ds["contracts"]), 1500)
        self.assertEqual(len(ds["receipts"]), 3000)
        self.assertEqual(len(ds["txns"]), 12000)
        self.assertEqual(len(ds["swap_demos"]), 200)

        # 验证 48 笔反洗钱拆单严格落入 [49500, 50000) 且总额精确 2,389,200.00
        aml_txns = [t for t in ds["txns"] if str(t[0]).startswith("TXN_AML_")]
        self.assertEqual(len(aml_txns), 48)
        self.assertTrue(all(49500.0 <= float(t[6]) < 50000.0 for t in aml_txns))
        self.assertAlmostEqual(sum(float(t[6]) for t in aml_txns), 2389200.00, places=2)

        # 验证核心 MySQL vs DWS 3,650 万元精确差额
        my_sum = sum(float(r[3]) for r in ds["mysql_corp_credits"])
        dw_sum = sum(float(r[3]) for r in ds["dw_corp_credits"])
        self.assertEqual(len(ds["mysql_corp_credits"]), 500)
        self.assertEqual(len(ds["dw_corp_credits"]), 497)
        self.assertAlmostEqual(my_sum - dw_sum, 36500000.00, places=2)

    def test_standard_dictionary_and_workspaces_end_to_end_zero_secrets(self) -> None:
        items = build_standard_items()
        self.assertEqual(len(items), 164)

        with tempfile.TemporaryDirectory(prefix="dataog_sandbox_ci_") as tmpdir:
            out_dir = os.path.join(tmpdir, "output")
            priv_root = os.path.join(tmpdir, "priv_home")
            res = build_all_workspaces(out_dir, live_probe=False)

            dict_xlsx = Path(out_dir) / "qiming_bank_data_standards_v2026.xlsx"
            bind_xlsx = Path(out_dir) / "qiming_dws_binding_import_demo.xlsx"
            live_ws = Path(res["live_workspace"])
            ent_ws = Path(res["enterprise_workspace"])
            self.assertTrue(dict_xlsx.is_file())
            self.assertTrue(bind_xlsx.is_file())
            self.assertTrue(live_ws.is_file())
            self.assertTrue(ent_ws.is_file())

            # 1. 验证两份 .dataog.db 严禁包含任何 Layer 2 私有表或凭据
            for ws_path in (live_ws, ent_ws):
                conn = sqlite3.connect(str(ws_path))
                try:
                    tbls = {
                        r[0]
                        for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    self.assertFalse(tbls & set(PRIVATE_ONLY_TABLES), f"{ws_path} leaked private tables: {tbls}")
                    topo_cols = {
                        r[1].lower()
                        for r in conn.execute("PRAGMA table_info(connection_topology)").fetchall()
                    }
                    self.assertNotIn("user", topo_cols)
                    self.assertNotIn("password", topo_cols)
                finally:
                    conn.close()

            # 2. 在隔离 PlatformContext 中实测导入 46 列标准字典 Excel 与双工作区
            ctx = PlatformContext(private_root=priv_root, legacy_data_dir="")
            imp_dict = ctx.standard_catalog.import_dictionary_excel(
                dict_xlsx.name, dict_xlsx.read_bytes(), mode="replace"
            )
            self.assertEqual(int(imp_dict["imported_count"]), 164)

            imp_live = ctx.workspace_hub.import_workspace_as_new(
                filename=live_ws.name,
                raw_bytes=live_ws.read_bytes(),
                name="活库演练",
                switch_immediately=True,
            )
            self.assertEqual(imp_live["workspace"]["name"], "活库演练")
            self.assertGreaterEqual(len(ctx.store.list_items()), 11)

            # 3. 切换到 150 表企业工作区，验证单库与跨环境离线 .html / .xlsx 报告全谱系检出
            ctx.workspace_hub.import_workspace_as_new(
                filename=ent_ws.name,
                raw_bytes=ent_ws.read_bytes(),
                name="150表压测",
                switch_immediately=True,
            )

            mig = StandardMigratorService(ctx)
            single_html = mig.export_binding_report_html_bytes(connection_id=CONN_DWS_ID)
            single_xlsx = mig.export_binding_report_bytes(connection_id=CONN_DWS_ID)
            self.assertIn(b"<!DOCTYPE html>", single_html[:512])
            wb_single = openpyxl.load_workbook(io.BytesIO(single_xlsx), read_only=True)
            self.assertEqual(len(wb_single.sheetnames), 1)
            wb_single.close()

            cc = CrossComparatorService(ctx)
            rpt_data = cc.build_database_cross_report_data()
            agg = rpt_data["aggregate"]
            self.assertEqual(agg["saved_pairs_count"], 120)
            self.assertEqual(agg["recommended_pairs_count"], 20)
            self.assertEqual(len(rpt_data["source_only_tables"]), 16)
            self.assertEqual(len(rpt_data["target_only_tables"]), 22)
            self.assertGreater(agg["unified_cols"], 0)
            self.assertGreater(agg["split_diff_cols"], 0)
            self.assertGreater(agg["split_partial_cols"], 0)
            self.assertGreater(agg["capacity_insufficient_cols"], 0)
            self.assertGreater(agg["type_conversion_cols"], 0)

            cross_html = cc.export_cross_report_html_bytes()
            cross_xlsx = cc.export_cross_report_excel_bytes()
            self.assertIn(b"<!DOCTYPE html>", cross_html[:512])
            wb_cross = openpyxl.load_workbook(io.BytesIO(cross_xlsx), read_only=True)
            self.assertEqual(len(wb_cross.sheetnames), 1)
            wb_cross.close()


if __name__ == "__main__":
    unittest.main()
