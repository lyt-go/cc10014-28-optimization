"""测试：表级比对差异报告（分批读取、字段级差异、采样/全量、重检、事务回滚、跨模块回写）。

覆盖需求：
- 分批读取左右表批次记录，报告保留字段级差异、采样比例与重检结果；
- 采样结果与重检批次一致落库，质量稽核只读已落库报告，不扫全量明细；
- 报告事务提交失败时回滚，列表定位与筛选恢复点保留；
- 比对任务列表过滤、搜索定位、检索查询、排序、翻页和分页；
- 质量稽核跨模块回写表级比对差异报告；
- 采样批次与全量批次冲突时 full_first / legacy 两种处理；
- 同一批次区分估算与精确结果，高风险字段差异不因采样遗漏。
"""
from __future__ import annotations

import pytest

from app.seed import SEED_ROWS
from app.store import store
from app.services.quality_check import QualityCheckService
from app.services.table_compare import ReportCommitError, TableCompareService


def reset_store():
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store._table_data.clear()
    store._versions.clear()
    store._lineage_edges.clear()
    store._quality_results.clear()
    store._snapshot_data.clear()
    store._archive_records.clear()
    store._backfill_partitions.clear()
    store._compare_reports.clear()


def make_rows(prefix: str, n: int, *, amount_delta_on_even: int = 0,
              status_bad: set | None = None, missing: set | None = None):
    rows = []
    for i in range(1, n + 1):
        if missing and i in missing:
            continue
        rows.append({
            "order_id": f"{prefix}{i:03d}",
            "order_amount": str(i * 10 + (amount_delta_on_even if i % 2 == 0 else 0)),
            "order_status": "非法状态" if status_bad and i in status_bad else "已完成",
            "dt": "2026-11-01",
        })
    return rows


class TestCompareReport:
    def setup_method(self):
        reset_store()

    # ------------------------------------------------------------ 字段级差异

    def test_full_batch_records_field_level_diff(self):
        """全量批次应按字段记录差异数，并区分仅左/仅右的主键集合。"""
        store.set_table_rows("ods_orders", make_rows("O", 4))
        store.set_table_rows("dwd_order_detail",
                             make_rows("O", 4, amount_delta_on_even=50, missing={4}))
        svc = TableCompareService()
        svc.run_action(1, "启动比对")
        report = store.get_compare_report(1)
        assert report["result_mode"] == "exact"
        amount = next(d for d in report["field_diffs"] if d["field"] == "order_amount")
        assert amount["estimated"] is False
        assert amount["exact_diff_count"] == 1  # O004 缺失后，匹配行中仅 O002 金额不一致
        assert amount["samples"], "字段级差异应保留样例行"
        assert report["only_left_count"] == 1
        assert report["only_right_count"] == 0
        assert report["diff_count"] > 0

    def test_batched_read_does_not_require_full_table_load(self):
        """大批次数据按批读取后结果与记录数一致，分批不丢行。"""
        store.set_table_rows("ods_orders", make_rows("O", 1200))
        store.set_table_rows("dwd_order_detail", make_rows("O", 1200))
        svc = TableCompareService()
        svc.run_action(1, "启动比对")
        report = store.get_compare_report(1)
        assert report["left_count"] == 1200
        assert report["right_count"] == 1200
        assert report["matched_pairs"] == 1200
        assert report["diff_count"] == 0

    def test_report_keeps_sample_ratio_and_recheck_result(self):
        """报告同时保留采样比例、字段级差异与重检批次结果。"""
        store.set_table_rows("ods_orders", make_rows("O", 20))
        store.set_table_rows("dwd_order_detail",
                             make_rows("O", 20, amount_delta_on_even=50))
        entry = store.find("table_compare", 1)
        entry["采样比例"] = "0.25"
        entry["差异阈值"] = "0.05"
        TableCompareService().run_action(1, "启动比对")
        report = store.get_compare_report(1)
        assert report["sample_ratio"] == 0.25
        assert report["recheck_batch"]["batch_size"] > 0
        amount = next(d for d in report["field_diffs"] if d["field"] == "order_amount")
        assert amount["exact_diff_count"] == 10
        record = report["recheck_batch"]["passed_records"][0]
        assert record["field"] == "order_amount"
        assert record["checked_pairs"] == 20
        assert record["passed_pairs"] == 10

    # ------------------------------------------------------------ 估算 vs 精确

    def test_same_batch_distinguishes_estimated_and_exact(self):
        """同一批次报告必须区分估算结果与精确结果。"""
        store.set_table_rows("ods_orders", make_rows("O", 20))
        store.set_table_rows("dwd_order_detail",
                             make_rows("O", 20, amount_delta_on_even=50))
        entry = store.find("table_compare", 1)
        entry["采样比例"] = "0.5"
        entry["差异阈值"] = "0.9"  # 高阈值：样本差异率不达标，保留估算不自动重检
        TableCompareService().run_action(1, "启动比对")
        report = store.get_compare_report(1)
        assert report["result_mode"] == "estimated"
        amount = next(d for d in report["field_diffs"] if d["field"] == "order_amount")
        assert amount["estimated"] is True
        assert amount["sample_diff_count"] >= 1
        assert "estimated_diff_count" in amount
        assert "diff_rate_range" in amount
        assert amount["recheck_status"] == "待重检"

    def test_high_risk_field_force_full_recheck(self):
        """采样差异率超阈值的高风险字段必须自动全量重检，不会因采样遗漏。"""
        store.set_table_rows("ods_orders", make_rows("O", 20))
        store.set_table_rows("dwd_order_detail",
                             make_rows("O", 20, amount_delta_on_even=50))
        entry = store.find("table_compare", 1)
        entry["采样比例"] = "0.25"
        entry["差异阈值"] = "0.05"
        TableCompareService().run_action(1, "启动比对")
        report = store.get_compare_report(1)
        amount = next(d for d in report["field_diffs"] if d["field"] == "order_amount")
        assert amount["estimated"] is False
        assert amount["exact_diff_count"] == 10
        assert amount["recheck_status"] == "已通过"
        assert amount["recheck_source"] == "auto_high_risk"
        assert "order_amount" in report["recheck_batch"]["pending_fields"]

    def test_manual_recheck_clears_pending_fields(self):
        """手动全量重检后待重检字段清空，重检通过记录并回同一份报告。"""
        store.set_table_rows("ods_orders", make_rows("O", 20))
        store.set_table_rows("dwd_order_detail",
                             make_rows("O", 20, amount_delta_on_even=50))
        entry = store.find("table_compare", 1)
        entry["采样比例"] = "0.5"
        entry["差异阈值"] = "0.9"  # 高阈值：不自动重检，保留估算态由手动重检覆盖
        svc = TableCompareService()
        svc.run_action(1, "启动比对")
        report, message = svc.recheck_fields(1, ["order_amount"])
        assert "全量重检完成" in message
        amount = next(d for d in report["field_diffs"] if d["field"] == "order_amount")
        assert amount["exact_diff_count"] == 10
        assert amount["recheck_source"] == "manual_recheck"
        assert report["recheck_batch"]["pending_fields"] == []

    # ------------------------------------------------------------ 一致落库

    def test_sample_and_recheck_persisted_together(self):
        """采样结果与重检批次必须同文档一致落库。"""
        store.set_table_rows("ods_orders", make_rows("O", 20))
        store.set_table_rows("dwd_order_detail",
                             make_rows("O", 20, amount_delta_on_even=50))
        entry = store.find("table_compare", 1)
        entry["采样比例"] = "0.25"
        entry["差异阈值"] = "0.05"
        TableCompareService().run_action(1, "启动比对")
        report = store.get_compare_report(1)
        # 报告中的字段差异与重检批次来自同一文档，不存在第二套全量明细
        assert report["recheck_batch"]["passed_records"][0]["field"] == "order_amount"
        amount = next(d for d in report["field_diffs"] if d["field"] == "order_amount")
        assert amount["exact_diff_count"] == report["recheck_batch"]["passed_records"][0]["checked_pairs"] - \
            report["recheck_batch"]["passed_records"][0]["passed_pairs"]

    def test_commit_failure_rolls_back_report_and_status(self):
        """报告事务提交失败时回滚：无残留报告，任务状态恢复到比对前，筛选仍可定位。"""
        store.set_table_rows("ods_orders", make_rows("O", 3))
        store.set_table_rows("dwd_order_detail", make_rows("O", 3))
        svc = TableCompareService()
        original_save = store.save_compare_report

        def broken_save(report):
            raise RuntimeError("模拟事务提交失败")

        store.save_compare_report = broken_save
        try:
            with pytest.raises(RuntimeError):
                svc.run_action(2, "启动比对")
        finally:
            store.save_compare_report = original_save
        assert store.get_compare_report(2) is None
        entry = store.find("table_compare", 2)
        assert entry["status"] == "草稿"  # 恢复点：比对前状态
        assert entry["pending"] is True
        # 列表定位与筛选仍可找到该任务
        items, total = svc.list_entries(keyword="CMP-PAY")
        assert total == 1 and items[0]["id"] == 2

    def test_reread_mismatch_triggers_rollback(self):
        """落库复读不一致（采样与重检批次分裂）时整体回滚。"""
        store.set_table_rows("ods_orders", make_rows("O", 3))
        store.set_table_rows("dwd_order_detail", make_rows("O", 3))
        svc = TableCompareService()
        original_get = store.get_compare_report

        def broken_get(entry_id):
            report = original_get(entry_id)
            if report is not None:
                report = dict(report)
                report["recheck_batch"] = {}  # 模拟重检批次丢失
            return report

        store.get_compare_report = broken_get
        try:
            with pytest.raises(ReportCommitError):
                svc.run_action(1, "启动比对")
        finally:
            store.get_compare_report = original_get
        assert store.get_compare_report(1) is None

    # ------------------------------------------------------------ 列表能力

    def test_list_filter_search_sort_pagination(self):
        """列表过滤、搜索定位、排序、翻页与分页。"""
        svc = TableCompareService()
        # 字段过滤
        items, total = svc.list_entries(filters={"左表名称": "ods_orders"})
        assert total == 1 and items[0]["任务编号"] == "CMP-ORD-001"
        # 关键字搜索（任务编号/表名/批次）
        items, total = svc.list_entries(keyword="RAW-20261102")
        assert total == 1 and items[0]["id"] == 2
        # 状态筛选
        items, total = svc.list_entries(status="草稿")
        assert total == 2
        # 排序
        items, _ = svc.list_entries(sort_by="任务编号", sort_order="desc")
        assert [r["任务编号"] for r in items] == ["CMP-USR-001", "CMP-PAY-001", "CMP-ORD-001"]
        # 分页与翻页
        page1, total = svc.list_entries(page=1, size=2)
        page2, _ = svc.list_entries(page=2, size=2)
        assert total == 3 and len(page1) == 2 and len(page2) == 1
        assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})

    # ------------------------------------------------------------ 冲突策略

    def test_conflict_full_first_shows_exact_and_passed(self):
        """full_first（默认）：报告显示全量字段差异数和重检通过记录。"""
        store.set_table_rows("ods_orders", make_rows("O", 20))
        store.set_table_rows("dwd_order_detail",
                             make_rows("O", 20, amount_delta_on_even=50))
        entry = store.find("table_compare", 1)
        entry["采样比例"] = "0.25"
        entry["差异阈值"] = "0.05"
        TableCompareService().run_action(1, "启动比对")
        report = store.get_compare_report(1)
        amount = next(d for d in report["field_diffs"] if d["field"] == "order_amount")
        assert amount["conflict_winner"] == "full"
        assert amount["display_mode"] == "exact_rechecked"
        assert amount["exact_diff_count"] == 10
        assert report["recheck_batch"]["passed_records"]

    def test_conflict_legacy_shows_range_and_pending(self):
        """legacy（兼容既有报告）：报告显示差异率区间和待重检字段。"""
        store.set_table_rows("ods_orders", make_rows("O", 20))
        store.set_table_rows("dwd_order_detail",
                             make_rows("O", 20, amount_delta_on_even=50))
        entry = store.find("table_compare", 1)
        entry["采样比例"] = "0.25"
        entry["差异阈值"] = "0.05"
        svc = TableCompareService()
        svc.run_action(1, "启动比对")
        report = store.get_compare_report(1)
        report["conflict_policy"] = "legacy"
        svc._apply_conflict_policy(report)
        amount = next(d for d in report["field_diffs"] if d["field"] == "order_amount")
        assert amount["conflict_winner"] == "sample"
        assert amount["display_mode"] == "range_pending"
        assert amount["diff_rate_range"][0] <= amount["sample_diff_rate"] <= amount["diff_rate_range"][1]
        assert amount["recheck_status"] == "待重检"

    # ------------------------------------------------------------ 跨模块回写

    def test_quality_check_writeback_uses_persisted_report(self):
        """质量稽核执行后跨模块回写已落库报告，且不依赖全量明细重扫。"""
        left_rows = make_rows("O", 4, status_bad={2})
        left_rows[1]["order_amount"] = "-50"  # 触发 QC-ORD-001 的金额阈值失败
        store.set_table_rows("ods_orders", left_rows)
        store.set_table_rows("dwd_order_detail", make_rows("O", 4, missing={4}))
        svc = TableCompareService()
        svc.run_action(1, "启动比对")
        qc = QualityCheckService()
        outcome = qc.execute_check(1)
        assert outcome["ok"] is True
        report = store.get_compare_report(1)
        assert len(report["quality_notes"]) == 1
        note = report["quality_notes"][0]
        assert note["rule_id"] == "QC-ORD-001"
        assert note["source"] == "quality_check"
        assert note["failed"] >= 1
        # 手动回写复用同一份已落库结论
        again = qc.writeback_compare_report("QC-ORD-001")
        assert again["ok"] is True
        assert len(store.get_compare_report(1)["quality_notes"]) == 2

    def test_quality_writeback_skips_without_report(self):
        """没有已落库报告时回写安全跳过，不生成相反结论。"""
        store.set_table_rows("ods_payments", [{"pay_id": "P1", "pay_amount": "10", "dt": "d"}])
        qc = QualityCheckService()
        qc.execute_check(2)
        outcome = qc.writeback_compare_report("QC-PAY-001")
        assert outcome["ok"] is False
        assert "回写跳过" in outcome["message"]

    # ------------------------------------------------------------ 补数联动

    def test_diff_still_triggers_backfill(self):
        """存在差异时仍自动触发补数任务，无差异不触发。"""
        store.set_table_rows("ods_orders", make_rows("O", 3))
        store.set_table_rows("dwd_order_detail", make_rows("O", 3, missing={3}))
        TableCompareService().run_action(1, "启动比对")
        jobs = [r for r in store.rows("backfill_job") if r["任务编号"].startswith("BF-CMP")]
        assert len(jobs) == 1
        # 无差异任务不触发
        store.set_table_rows("ods_payments", [{"pay_id": "P1", "pay_amount": "10", "dt": "d"}])
        store.set_table_rows("dwd_payment_detail", [{"pay_id": "P1", "pay_amount": "10", "dt": "d"}])
        before = len(store.rows("backfill_job"))
        TableCompareService().run_action(2, "启动比对")
        assert len(store.rows("backfill_job")) == before
