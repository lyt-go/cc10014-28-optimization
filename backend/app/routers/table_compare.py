"""表级比对接口：比对任务列表（过滤/搜索/排序/分页）、差异报告、重检与导出。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.table_compare import (
    DEFAULT_BATCH_SIZE,
    LIST_FIELDS,
    STATUS_ORDER,
    TableCompareService,
)

router = APIRouter(prefix="/api/table_compare", tags=["表级比对"])
service = TableCompareService()
STATUSES = STATUS_ORDER


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页/排序等保留参数。"""
    reserved = {"keyword", "status", "page", "size", "sort_by", "sort_order"}
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and key not in reserved and value.strip()
    }


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按任务编号、左右表名或批次搜索定位"),
    status: str | None = Query(default=None, description="草稿、比对中、已完成、已取消"),
    sort_by: str | None = Query(default=None, description="排序字段"),
    sort_order: str = Query(default="asc", description="asc 或 desc"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """比对任务分页列表：字段过滤、关键字检索、状态筛选、排序与翻页。"""
    items, total = service.list_entries(
        filters=query_filters(request),
        keyword=keyword,
        status=status,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/timeline")
def timeline_entries() -> dict[str, Any]:
    """甘特时间线视图数据，保留既有工作台展示。"""
    return {"items": service.get_timeline()}


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """按当前筛选条件导出 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status,
        page=1, size=1_000_000)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in ["id", *LIST_FIELDS, "status"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=table_compare.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"比对任务 {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="比对任务已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    try:
        entry_or_none, message = service.run_action(entry_id, payload.action)
    except Exception as exc:  # 报告事务提交失败：整体回滚，返回可读原因与恢复点
        entry = service.get_entry(entry_id)
        return ActionResult(
            ok=False,
            message=f"差异报告事务提交失败已回滚：{exc}，任务状态恢复到比对前",
            entry=entry,
        )
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)


@router.get("/{entry_id}/result")
def get_compare_result(entry_id: int) -> dict[str, Any]:
    """返回比对简要结果，包含差异数量和补数任务（兼容既有调用）。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"比对任务 {entry_id} 不存在")
    return entry.get("_compare_result", {})


@router.get("/{entry_id}/report")
def get_compare_report(entry_id: int) -> dict[str, Any]:
    """表级比对差异报告详情：字段级差异、采样比例、重检批次与稽核回写。"""
    report = service.get_report(entry_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"比对任务 {entry_id} 暂无差异报告，请先启动比对")
    return report


@router.post("/{entry_id}/recheck")
def recheck_fields(entry_id: int, payload: dict[str, Any] | None = None) -> ActionResult:
    """对待重检字段执行全量重检批次，结果并回同一份差异报告。"""
    fields = (payload or {}).get("fields") or []
    report, message = service.recheck_fields(entry_id, fields)
    if report is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=report)


@router.get("/batch/size")
def batch_size() -> dict[str, int]:
    """暴露分批读取的批次大小，供前端展示扫描口径。"""
    return {"batch_size": DEFAULT_BATCH_SIZE}
