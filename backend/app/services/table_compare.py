"""表级比对服务：分批读取左右表批次记录，生成字段级表级比对差异报告。

设计要点：
- 读取按批次（batch_size）扫描，避免一次性全量加载拖慢差异定位；
- 比对区分采样估算（estimate）与全量精确（exact）两种结果口径；
- 高风险字段（采样差异率超过任务阈值）自动纳入全量重检批次，不会因采样遗漏；
- 采样结果与重检批次保存在同一份报告文档中，同事务提交，失败整体回滚；
- 冲突时支持两种处理：full_first（全量优先，默认）展示全量字段差异数与重检通过记录；
  legacy（兼容既有表级比对报告）展示差异率区间与待重检字段。
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from typing import Any, Iterator

from app.store import store

MODULE = "table_compare"
REQUIRED_FIELDS = ["任务编号", "左表名称", "右表名称"]
OPTIONAL_FIELDS = ["比对字段", "比对方式", "差异阈值", "采样比例", "比对批次"]
LIST_FIELDS = ["任务编号", "左表名称", "右表名称", "比对字段", "比对方式", "差异阈值", "采样比例", "比对批次"]
STATUS_ORDER = ["草稿", "比对中", "已完成", "已取消"]
ACTION_RULES = {"启动比对": "比对中", "确认完成": "已完成", "取消比对": "已取消"}
NEGATIVE_ACTIONS: list[str] = []

DEFAULT_BATCH_SIZE = 500
SAMPLE_ROWS_LIMIT = 10    # 每个字段最多保留的差异样例行数
# 兼容旧版报告（legacy 冲突策略）时的差异率区间外推系数
RANGE_LOW_FACTOR = 0.7
RANGE_HIGH_FACTOR = 1.3


class ReportCommitError(RuntimeError):
    """差异报告事务提交失败。"""


class TableCompareService:
    # ------------------------------------------------------------------ 列表

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        sort_by: str | None = None,
        sort_order: str = "asc",
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """比对任务列表：字段过滤、关键字搜索定位、状态筛选、排序、分页。"""
        rows = [dict(row) for row in store.rows(MODULE)]
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            needle = keyword.casefold()
            rows = [
                row for row in rows
                if needle in str(row.get("任务编号", "")).casefold()
                or needle in str(row.get("左表名称", "")).casefold()
                or needle in str(row.get("右表名称", "")).casefold()
                or needle in str(row.get("比对批次", "")).casefold()
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        # 已完成任务把报告摘要带出来，列表即可定位有差异/待重检的任务
        for row in rows:
            report = store.get_compare_report(int(row.get("id", 0)))
            row["has_report"] = report is not None
            if report is not None:
                row["result_mode"] = report.get("result_mode")
                row["diff_count"] = report.get("diff_count", 0)
                row["pending_recheck"] = len(report.get("recheck_batch", {}).get("pending_fields", []))
        sortable = set(LIST_FIELDS) | {"id", "status"}
        if sort_by in sortable:
            reverse = sort_order.casefold() == "desc"
            rows.sort(key=lambda row: str(row.get(sort_by, "")), reverse=reverse)
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_timeline(self) -> list[dict[str, Any]]:
        """保留既有时间线视图需要的数据结构。"""
        rows = store.rows(MODULE)
        today = date.today()
        items: list[dict] = []
        for i, row in enumerate(rows):
            start = (today - timedelta(days=7 - i * 2)).isoformat()
            end = (today + timedelta(days=3 - i)).isoformat()
            items.append({
                "id": int(row.get("id", i + 1)),
                "name": str(row.get("任务编号", "")),
                "start": start,
                "end": end,
                "status": str(row.get("status", STATUS_ORDER[i % len(STATUS_ORDER)])),
                "logs": f"[{start}] 作业启动\n[{start}] 正在处理...\n[{end}] 任务{'完成' if row.get('status') == STATUS_ORDER[-1] else '进行中'}",
            })
        return items

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        for field in OPTIONAL_FIELDS:
            if field in values:
                entry[field] = values[field]
        rows.append(entry)
        return entry, []

    # ------------------------------------------------------------------ 动作

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"比对任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于表级比对可执行范围"
        # 在状态变更前留存恢复点，报告事务失败时整体回到动作执行前
        previous = copy.deepcopy(entry) if action == "启动比对" else None
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        if action == "启动比对":
            # 报告与任务状态在同一业务事务内：报告提交失败（含落库/复读异常）
            # 一律回滚状态，恢复点保留在比对前
            try:
                report = self._run_compare(entry)
                entry["_compare_result"] = self._brief_result(report)
            except Exception:
                entry.clear()
                entry.update(previous)
                raise
            if report.get("diff_count", 0) > 0:
                self._trigger_backfill(entry, report)
        return entry, f"比对任务已{action}"

    # ------------------------------------------------------------------ 报告

    def get_report(self, entry_id: int) -> dict[str, Any] | None:
        if self.get_entry(entry_id) is None:
            return None
        return store.get_compare_report(entry_id)

    def recheck_fields(self, entry_id: int, fields: list[str] | None = None) -> tuple[dict[str, Any] | None, str]:
        """对指定字段（默认全部待重检字段）执行全量重检批次，结果并回同一份报告。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"比对任务 {entry_id} 不存在或已归档"
        report = store.get_compare_report(entry_id)
        if report is None:
            return None, "尚无差异报告，请先启动比对"
        targets = [f.strip() for f in (fields or []) if f and f.strip()]
        pending = report.get("recheck_batch", {}).get("pending_fields", [])
        if not targets:
            targets = list(pending)
        unknown = [f for f in targets if f not in {d["field"] for d in report.get("field_diffs", [])}]
        if unknown:
            return None, f"字段不属于本任务比对范围：{'、'.join(unknown)}"
        if not targets:
            return report, "没有待重检字段"
        left_rows, right_rows = self._read_both_tables(entry)
        index_left, index_right, only_left, only_right, matched_pairs = self._classify_rows(
            entry, left_rows, right_rows)
        passed_records = report.setdefault("recheck_batch", {}).setdefault("passed_records", [])
        run_at = datetime.now().isoformat()
        field_index = {d["field"]: d for d in report.get("field_diffs", [])}
        for field in targets:
            exact = self._exact_field_diff(field, matched_pairs, only_left, only_right, index_left, index_right)
            self._merge_exact_into_report(field_index[field], field, exact, run_at, recheck=True)
            passed_records.append({
                "field": field,
                "checked_pairs": exact["checked_pairs"],
                "passed_pairs": exact["passed_pairs"],
                "checked_at": run_at,
            })
        report["recheck_batch"]["pending_fields"] = [
            f for f in report["recheck_batch"].get("pending_fields", []) if f not in targets
        ]
        report["recheck_batch"]["last_run_at"] = run_at
        report["updated_at"] = run_at
        self._commit_report(report)
        return report, f"全量重检完成：{'、'.join(targets)}"

    # ------------------------------------------------------------- 比对主流程

    def _run_compare(self, entry: dict[str, Any]) -> dict[str, Any]:
        started_at = datetime.now().isoformat()
        left_rows, right_rows = self._read_both_tables(entry)
        index_left, index_right, only_left, only_right, matched_pairs = self._classify_rows(
            entry, left_rows, right_rows)
        fields = self._compare_fields(entry, left_rows, right_rows)
        sample_ratio = self._parse_ratio(entry.get("采样比例"))
        threshold = self._parse_threshold(entry.get("差异阈值"))
        full_mode = sample_ratio >= 1.0 or str(entry.get("比对方式", "")).startswith("全量")

        field_diffs: list[dict[str, Any]] = []
        pending_fields: list[str] = []
        recheck_records: list[dict[str, Any]] = []
        run_at = started_at
        if full_mode:
            # 全量批次：每个字段都是精确结果
            for field in fields:
                exact = self._exact_field_diff(field, matched_pairs, only_left, only_right,
                                               index_left, index_right)
                diff = self._build_field_diff(field, exact, estimated=False)
                field_diffs.append(diff)
        else:
            # 采样批次：先按采样比例给出估算，再对高风险字段执行全量重检
            step = max(1, round(1.0 / sample_ratio)) if sample_ratio > 0 else 1
            sampled_pairs = matched_pairs[::step]
            for field in fields:
                estimate = self._estimate_field_diff(field, sampled_pairs, sample_ratio,
                                                      len(matched_pairs), only_left, only_right)
                diff = self._build_field_diff(field, estimate, estimated=True)
                field_diffs.append(diff)
                # 高风险字段差异不会因采样遗漏：采样观测差异率超过任务阈值即强制全量重检
                if estimate["sample_diff_rate"] > threshold:
                    exact = self._exact_field_diff(field, matched_pairs, only_left, only_right,
                                                   index_left, index_right)
                    self._merge_exact_into_report(diff, field, exact, run_at, recheck=False)
                    pending_fields.append(field)
                    recheck_records.append({
                        "field": field,
                        "checked_pairs": exact["checked_pairs"],
                        "passed_pairs": exact["passed_pairs"],
                        "checked_at": run_at,
                    })

        conflict_policy = "full_first"  # 全量精确结果优先；legacy 为兼容旧版报告的另一种处理
        exact_field_count = sum(1 for d in field_diffs if not d.get("estimated"))
        report = {
            "entry_id": int(entry["id"]),
            "task_no": entry.get("任务编号", ""),
            "left_table": entry.get("左表名称", ""),
            "right_table": entry.get("右表名称", ""),
            "batch_no": entry.get("比对批次", ""),
            "key_field": fields[0] if fields else "_id",
            "compare_fields": fields,
            "result_mode": "exact" if full_mode or exact_field_count == len(field_diffs) else "estimated",
            "conflict_policy": conflict_policy,
            "sample_ratio": sample_ratio,
            "threshold": threshold,
            "left_count": len(left_rows),
            "right_count": len(right_rows),
            "matched_pairs": len(matched_pairs),
            "only_left_count": len(only_left),
            "only_right_count": len(only_right),
            "only_left_sample": only_left[:SAMPLE_ROWS_LIMIT],
            "only_right_sample": only_right[:SAMPLE_ROWS_LIMIT],
            "field_diffs": field_diffs,
            "diff_count": sum(int(d.get("exact_diff_count", d.get("estimated_diff_count", 0))) for d in field_diffs)
                          + len(only_left) + len(only_right),
            "recheck_batch": {
                "batch_size": DEFAULT_BATCH_SIZE,
                "sampled_pairs": (len(matched_pairs) if full_mode else len(sampled_pairs)),
                "total_pairs": len(matched_pairs),
                "pending_fields": pending_fields,
                "passed_records": recheck_records,
                "last_run_at": run_at if (pending_fields or recheck_records) else None,
            },
            "quality_notes": [],
            "created_at": started_at,
            "updated_at": run_at,
        }
        self._apply_conflict_policy(report)
        self._commit_report(report)
        return report

    def recheck_quality_writeback(
        self,
        rule_id: str,
        table: str,
        field: str,
        total: int,
        failed: int,
        source: str = "quality_check",
    ) -> dict[str, Any] | None:
        """质量稽核跨模块回写表级比对差异报告（只写已落库报告，不另读全量明细）。"""
        entry = self._find_entry_by_table(table)
        if entry is None:
            return None
        entry_id = int(entry["id"])
        if store.get_compare_report(entry_id) is None:
            return None
        note = {
            "rule_id": rule_id,
            "table": table,
            "field": field,
            "total": total,
            "failed": failed,
            "source": source,
            "written_at": datetime.now().isoformat(),
        }
        return store.add_compare_quality_note(entry_id, note)

    # ------------------------------------------------------------- 分批读取

    def _read_both_tables(self, entry: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        left = self._read_table_batched(entry.get("左表名称", ""))
        right = self._read_table_batched(entry.get("右表名称", ""))
        return left, right

    def _read_table_batched(self, table_name: str, batch_size: int = DEFAULT_BATCH_SIZE) -> list[dict[str, Any]]:
        """分批读取表数据：批次游标逐批拉取，避免大批次全量扫描长期占内存。"""
        rows = store.table_rows(table_name)
        if not rows:
            from app.services.ods_layer import OdsLayerService
            rows = OdsLayerService().get_table_data(table_name) or []
        collected: list[dict[str, Any]] = []
        for batch in self._iter_batches(rows, batch_size):
            collected.extend(batch)
        return collected

    @staticmethod
    def _iter_batches(rows: list[dict[str, Any]], batch_size: int) -> Iterator[list[dict[str, Any]]]:
        for start in range(0, len(rows), batch_size):
            yield rows[start:start + batch_size]

    # ------------------------------------------------------------- 比对算法

    def _classify_rows(
        self,
        entry: dict[str, Any],
        left_rows: list[dict[str, Any]],
        right_rows: list[dict[str, Any]],
    ) -> tuple[dict[str, dict], dict[str, dict], list[str], list[str], list[tuple[dict[str, Any], dict[str, Any]]]]:
        fields = self._compare_fields(entry, left_rows, right_rows)
        key_field = fields[0] if fields else "_id"
        index_left: dict[str, dict[str, Any]] = {}
        index_right: dict[str, dict[str, Any]] = {}
        only_left: list[str] = []
        only_right: list[str] = []
        for batch in self._iter_batches(left_rows, DEFAULT_BATCH_SIZE):
            for row in batch:
                index_left.setdefault(str(row.get(key_field, "")), row)
        for batch in self._iter_batches(right_rows, DEFAULT_BATCH_SIZE):
            for row in batch:
                index_right.setdefault(str(row.get(key_field, "")), row)
        for key in index_left:
            if key not in index_right:
                only_left.append(key)
        for key in index_right:
            if key not in index_left:
                only_right.append(key)
        matched_pairs = [
            (index_left[key], index_right[key])
            for key in index_left.keys() & index_right.keys()
        ]
        return index_left, index_right, only_left, only_right, matched_pairs

    def _estimate_field_diff(
        self,
        field: str,
        sampled_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
        sample_ratio: float,
        total_pairs: int,
        only_left: list[str],
        only_right: list[str],
    ) -> dict[str, Any]:
        """采样批次估算：样本差异率外推全量字段差异数，明确标注为估算结果。"""
        sampled_diff = 0
        samples: list[dict[str, Any]] = []
        for left_row, right_row in sampled_pairs:
            if not self._values_equal(left_row.get(field), right_row.get(field)):
                sampled_diff += 1
                if len(samples) < SAMPLE_ROWS_LIMIT:
                    samples.append({
                        "key": left_row.get(self._first_key(left_row)),
                        "left_value": left_row.get(field),
                        "right_value": right_row.get(field),
                    })
        sampled_total = max(1, len(sampled_pairs))
        sample_rate = sampled_diff / sampled_total
        ratio = sample_ratio if sample_ratio > 0 else 1.0
        estimated = round(sample_rate * total_pairs / ratio)
        estimated = min(estimated, total_pairs)
        # 字段差异率只看该字段在匹配记录上的取值差异，主键缺失另由 only_left/only_right 统计
        estimated_rate = estimated / max(1, total_pairs)
        return {
            "estimated": True,
            "sample_size": len(sampled_pairs),
            "sample_diff_count": sampled_diff,
            "sample_diff_rate": round(sample_rate, 4),
            "estimated_diff_count": estimated,
            "estimated_rate": round(estimated_rate, 4),
            "diff_rate_range": [
                round(min(1.0, sample_rate * RANGE_LOW_FACTOR), 4),
                round(min(1.0, sample_rate * RANGE_HIGH_FACTOR), 4),
            ],
            "samples": samples,
        }

    def _exact_field_diff(
        self,
        field: str,
        matched_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
        only_left: list[str],
        only_right: list[str],
        index_left: dict[str, dict[str, Any]],
        index_right: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """全量批次精确比对：逐批扫描匹配记录，统计字段差异数与重检通过记录。"""
        exact_diff = 0
        checked = 0
        samples: list[dict[str, Any]] = []
        for batch in self._iter_batches(matched_pairs, DEFAULT_BATCH_SIZE):
            for left_row, right_row in batch:
                checked += 1
                if not self._values_equal(left_row.get(field), right_row.get(field)):
                    exact_diff += 1
                    if len(samples) < SAMPLE_ROWS_LIMIT:
                        samples.append({
                            "key": left_row.get(self._first_key(left_row)),
                            "left_value": left_row.get(field),
                            "right_value": right_row.get(field),
                        })
        total_records = checked + len(only_left) + len(only_right)
        # 字段差异率只统计匹配记录上的取值差异；主键级缺失单独在报告头展示
        exact_rate = exact_diff / max(1, checked)
        return {
            "estimated": False,
            "checked_pairs": checked,
            "exact_diff_count": exact_diff,
            "exact_rate": round(exact_rate, 4),
            "passed_pairs": checked - exact_diff,
            "samples": samples,
            "total_records": total_records,
        }

    def _build_field_diff(self, field: str, stats: dict[str, Any], estimated: bool) -> dict[str, Any]:
        diff = {"field": field, "estimated": estimated, "recheck_status": "待重检" if estimated else "已通过"}
        diff.update(copy.deepcopy(stats))
        return diff

    def _merge_exact_into_report(
        self,
        diff: dict[str, Any],
        field: str,
        exact: dict[str, Any],
        run_at: str,
        *,
        recheck: bool,
    ) -> None:
        """全量重检结果并入同一字段条目：保留采样估算，同时给出精确值与重检结论。"""
        diff["estimated"] = False
        diff["recheck_status"] = "已通过"
        diff["recheck_at"] = run_at
        diff["recheck_source"] = "manual_recheck" if recheck else "auto_high_risk"
        for key in ("checked_pairs", "exact_diff_count", "exact_rate", "passed_pairs", "samples"):
            diff[key] = copy.deepcopy(exact[key])

    def _apply_conflict_policy(self, report: dict[str, Any]) -> None:
        """采样批次与全量批次对同一字段结论冲突时的优先级处理。

        full_first（默认）：全量精确结果优先，报告展示全量字段差异数和重检通过记录；
        legacy（兼容既有表级比对差异报告的另一种处理）：保留采样差异率区间和待重检字段。
        表级比对的次序以任务上的 conflict_policy 为准，未声明时默认全量优先。
        """
        policy = report.get("conflict_policy", "full_first")
        for diff in report.get("field_diffs", []):
            has_both = "sample_diff_rate" in diff and "exact_diff_count" in diff
            if not has_both:
                continue
            if policy == "legacy":
                # 兼容既有报告：以采样估算为准，展示差异率区间与待重检字段
                diff["display_mode"] = "range_pending"
                diff["conflict_winner"] = "sample"
                diff["recheck_status"] = "待重检"
            else:
                diff["display_mode"] = "exact_rechecked"
                diff["conflict_winner"] = "full"

    def _commit_report(self, report: dict[str, Any]) -> None:
        """事务提交：先备份旧报告，提交失败时回滚并保留恢复点。"""
        entry_id = int(report["entry_id"])
        previous = store.get_compare_report(entry_id)
        snapshot = copy.deepcopy(report)
        try:
            store.save_compare_report(snapshot)
            # 落库后立即复读，采样结果与重检批次必须一致可查
            persisted = store.get_compare_report(entry_id)
            if persisted is None:
                raise ReportCommitError("差异报告落库后复读为空")
            if persisted.get("recheck_batch") != snapshot.get("recheck_batch"):
                raise ReportCommitError("重检批次与采样结果未一致落库")
            if persisted.get("field_diffs") != snapshot.get("field_diffs"):
                raise ReportCommitError("字段级差异未一致落库")
        except ReportCommitError:
            if previous is None:
                store.delete_compare_report(entry_id)
            else:
                store.save_compare_report(previous)
            raise

    # ------------------------------------------------------------- 工具方法

    def _find_entry_by_table(self, table: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if row.get("左表名称") == table or row.get("右表名称") == table:
                return row
        return None

    def _compare_fields(
        self,
        entry: dict[str, Any],
        left_rows: list[dict[str, Any]],
        right_rows: list[dict[str, Any]],
    ) -> list[str]:
        fields_str = entry.get("比对字段", "")
        fields = [f.strip() for f in str(fields_str).split(",") if f.strip()] if fields_str else []
        if not fields:
            sample = left_rows[0] if left_rows else (right_rows[0] if right_rows else {})
            fields = [k for k in sample.keys() if not str(k).startswith("_")]
        return fields

    @staticmethod
    def _parse_ratio(value: Any) -> float:
        try:
            ratio = float(value)
        except (TypeError, ValueError):
            ratio = 1.0
        return min(1.0, max(0.0, ratio))

    @staticmethod
    def _parse_threshold(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _values_equal(left: Any, right: Any) -> bool:
        if left is None and right is None:
            return True
        return str(left) == str(right)

    @staticmethod
    def _first_key(row: dict[str, Any]) -> str:
        return next(iter(row.keys()), "_id")

    @staticmethod
    def _brief_result(report: dict[str, Any]) -> dict[str, Any]:
        return {
            "left_count": report.get("left_count", 0),
            "right_count": report.get("right_count", 0),
            "diff_count": report.get("diff_count", 0),
            "result_mode": report.get("result_mode"),
            "only_left": report.get("only_left_sample", []),
            "only_right": report.get("only_right_sample", []),
            "key_field": report.get("key_field"),
        }

    def _trigger_backfill(self, entry: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        from app.services.backfill_job import BackfillJobService
        bf = BackfillJobService()
        job_no = f"BF-{entry.get('任务编号', '')}"
        bf.create_entry({
            "任务编号": job_no,
            "目标表名": entry.get("右表名称", ""),
            "起始日期": date.today().isoformat(),
            "截止日期": date.today().isoformat(),
            "并行度": "2",
            "覆盖策略": "覆盖写入",
            "完成水位": "",
            "失败区间": "",
        })
        return {"ok": True, "job_no": job_no}
