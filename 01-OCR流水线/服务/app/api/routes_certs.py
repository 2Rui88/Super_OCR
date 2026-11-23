"""证件配置（YAML）可插拔管理 —— 查看 / 改草稿 / 试跑回测 / 确认上线 / 取消。

真源是 DB 的 cert_versions（active 集）；config/certs/*.yaml 仅首次种子。
worker 心跳发现整套指纹变化即热重建 registry，证件缓存键含指纹 →
上线新版后旧缓存自然失效，同内容图自动按新配置重算。
"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.certs.loader import validate_cert_text
from app.core import cert_versions

router = APIRouter(tags=["cert-configs"])


def _store(request: Request):
    return request.app.state.store


def _settings(request: Request):
    return request.app.state.settings


def _ensure_seeded(request: Request):
    cert_versions.ensure_seeded(_store(request), _settings(request).cert_dir)


# ---------------------------------------------------------------- 查看与导出

@router.get("/cert-configs")
def list_cert_configs(request: Request):
    _ensure_seeded(request)
    store = _store(request)
    texts = store.active_cert_texts()
    configs = []
    for type_id, text in sorted(texts.items()):
        cfg = validate_cert_text(text)
        draft = (store.list_cert_versions(type_id=type_id, status="draft") or [None])[0]
        configs.append({
            "type_id": type_id, "name": cfg.name,
            "yaml_text": text,
            "yaml_sha256": hashlib.sha256(text.encode()).hexdigest()[:12],
            "fields": [f.name for f in cfg.fields],
            "draft": ({"version_id": draft["id"], "note": draft["note"],
                       "yaml_text": draft["yaml_text"],
                       "created_at": draft["created_at"]} if draft else None),
        })
    return {"types": configs,
            "active_set_hash": store.get_cert_state_hash()}


@router.get("/cert-configs/{type_id}/versions")
def list_versions(type_id: str, request: Request):
    _ensure_seeded(request)
    rows = _store(request).list_cert_versions(type_id=type_id)
    for row in rows:
        row.pop("yaml_text", None)  # 历史列表不带全文，看全文走 active/草稿接口
    return {"versions": rows}


@router.get("/cert-configs/{type_id}/export", response_class=PlainTextResponse)
def export_active_yaml(type_id: str, request: Request):
    _ensure_seeded(request)
    texts = _store(request).active_cert_texts()
    if type_id not in texts:
        raise HTTPException(404, f"未知证件类型 {type_id}")
    return PlainTextResponse(
        texts[type_id], media_type="application/x-yaml",
        headers={"Content-Disposition":
                 f"attachment; filename={type_id}.yaml"})


# ---------------------------------------------------------------- 草稿

class DraftBody(BaseModel):
    yaml_text: str
    note: str = ""


@router.put("/cert-configs/{type_id}")
def save_draft(type_id: str, body: DraftBody, request: Request):
    _ensure_seeded(request)
    store = _store(request)
    if type_id not in store.active_cert_texts():
        raise HTTPException(404, f"未知证件类型 {type_id}（当前仅支持修改已有类别）")
    try:
        cfg = validate_cert_text(body.yaml_text)
    except Exception as exc:  # noqa: BLE001 —— 校验信息原样回给编辑界面
        raise HTTPException(422, f"YAML 校验失败：{exc}") from exc
    if cfg.type_id != type_id:
        raise HTTPException(422, f"type_id 必须保持为 {type_id}（当前是 {cfg.type_id}）")
    version_id = store.insert_cert_version(
        type_id=type_id, yaml_text=body.yaml_text,
        yaml_sha256=hashlib.sha256(body.yaml_text.encode()).hexdigest(),
        status="draft", note=body.note)
    return {"version_id": version_id, "name": cfg.name,
            "fields": [f.name for f in cfg.fields],
            "message": "草稿已保存，可创建试跑"}


def _latest_draft(store, type_id: str) -> dict:
    drafts = store.list_cert_versions(type_id=type_id, status="draft")
    if not drafts:
        raise HTTPException(404, f"{type_id} 没有待试跑的草稿，先 PUT 保存")
    return drafts[0]


@router.post("/cert-configs/{type_id}/confirm")
def confirm_draft(type_id: str, request: Request):
    """草稿级确认上线：与试跑解绑，把该类当前草稿切成生效版并刷新指纹。

    重跑（试跑）只是验证手段，可做可不做——确认动作始终作用于最新草稿。"""
    _ensure_seeded(request)
    store = _store(request)
    drafts = store.list_cert_versions(type_id=type_id, status="draft")
    if not drafts:
        raise HTTPException(409, f"{type_id} 没有草稿——先点「校验并保存草稿」")
    promoted = store.promote_cert_version(drafts[0]["id"])
    active_hash = cert_versions.refresh_active_hash(store)
    return {"message": f"{type_id} 草稿已上线，worker 将在数秒内热切换",
            "active_set_hash": active_hash, "promoted_version_id": promoted["id"]}


@router.post("/cert-configs/{type_id}/cancel")
def cancel_draft(type_id: str, request: Request):
    """草稿级取消：废弃当前草稿，生效配置不变。"""
    _ensure_seeded(request)
    store = _store(request)
    drafts = store.list_cert_versions(type_id=type_id, status="draft")
    if not drafts:
        raise HTTPException(409, f"{type_id} 没有草稿，无需取消")
    store.retire_cert_version(drafts[0]["id"])
    return {"message": f"{type_id} 草稿已废弃，生效配置保持不变"}


# ---------------------------------------------------------------- 试跑

@router.get("/cert-configs/trial-sources")
def list_trial_sources(request: Request):
    sources = []
    for label, path in _settings(request).trial_source_dirs:
        try:
            golden = cert_versions.load_golden(path)
            sources.append({"label": label, "path": path, "files": len(golden)})
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            continue  # 目录缺失/无标准答案的源直接不展示
    return {"sources": sources}


class TrialBody(BaseModel):
    source_label: str
    scope: str = "type"  # type=仅该证件类 | all=全量


@router.post("/cert-configs/{type_id}/trials")
def create_trial(type_id: str, body: TrialBody, request: Request):
    _ensure_seeded(request)
    store, settings = _store(request), _settings(request)
    draft = _latest_draft(store, type_id)
    source = next((p for label, p in settings.trial_source_dirs
                   if label == body.source_label), None)
    if source is None:
        raise HTTPException(404, f"未知试跑源 {body.source_label}")
    if body.scope not in ("type", "all"):
        raise HTTPException(422, "scope 只能是 type 或 all")
    try:
        trial_id = cert_versions.create_trial(
            store, upload_dir=settings.upload_dir, max_mb=settings.max_upload_mb,
            type_id=type_id, draft_version_id=draft["id"],
            draft_yaml=draft["yaml_text"], source_label=body.source_label,
            source_dir=source, scope=body.scope)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"trial_id": trial_id, "status": "running",
            "message": "两波任务已入队：草稿版 + 当时生效参照"}


@router.get("/cert-configs/trials")
def list_trials(request: Request):
    """重跑记录按文件展开：一次重跑 × N 张图 → N 条记录，行级查看对比。"""
    _ensure_seeded(request)
    store = _store(request)
    records = []
    for trial in store.list_cert_trials():
        report = json.loads(trial["report_json"]) if trial["report_json"] else None
        if report is None and trial["status"] == "running":
            # 轮询列表也能收尾：两波全部终态时立刻出报告，避免"运行中"假死
            report = cert_versions.finalize_if_done(store, trial)
            if report is not None:
                trial["status"] = "ready"
        if report:
            for idx, f in enumerate(report["files"], 1):
                records.append({
                    "record_no": f"{trial['id']}-{idx:02d}",
                    "trial_id": trial["id"], "file": f["file"],
                    "type_id": trial["type_id"], "status": trial["status"],
                    "verdict": f["verdict"],
                })
        else:
            progress = cert_versions.trial_progress(store, trial)
            total = progress["draft"]["total"] + progress["ref"]["total"]
            done = progress["draft"]["done"] + progress["ref"]["done"]
            records.append({
                "record_no": f"{trial['id']}-00", "trial_id": trial["id"],
                "file": "（重跑进行中）", "type_id": trial["type_id"],
                "status": trial["status"], "verdict": None,
                "progress": f"{done}/{total}",
            })
    return {"records": records}


@router.get("/cert-configs/trials/{trial_id}")
def get_trial(trial_id: int, request: Request):
    _ensure_seeded(request)
    store = _store(request)
    trial = store.get_cert_trial(trial_id)
    if trial is None:
        raise HTTPException(404, f"试跑 {trial_id} 不存在")
    result = {"trial_id": trial["id"], "type_id": trial["type_id"],
              "source": trial["source"], "status": trial["status"],
              "created_at": trial["created_at"],
              "draft_version_id": trial["draft_version_id"],
              "progress": cert_versions.trial_progress(store, trial)}
    if trial["status"] == "running":
        report = cert_versions.finalize_if_done(store, trial)
        if report is not None:
            result["status"] = "ready"
            result["report"] = report
    else:
        report = json.loads(trial["report_json"]) if trial["report_json"] else None
        if report is not None and report.get("schema") != 2:
            # 报告结构升级（如补字段级明细）：对已完成试跑透明重算并回写
            report = cert_versions.build_trial_report(store, trial)
            store.update_cert_trial(trial["id"], status=trial["status"],
                                    report_json=json.dumps(
                                        report, ensure_ascii=False))
        result["report"] = report
    return result


@router.post("/cert-configs/trials/{trial_id}/confirm")
def confirm_trial(trial_id: int, request: Request):
    _ensure_seeded(request)
    store = _store(request)
    trial = store.get_cert_trial(trial_id)
    if trial is None:
        raise HTTPException(404, f"试跑 {trial_id} 不存在")
    if trial["status"] == "confirmed":
        raise HTTPException(409, "该试跑已确认过")
    if trial["status"] != "ready":
        raise HTTPException(409, f"试跑尚未完成（{trial['status']}），不能确认")
    promoted = store.promote_cert_version(trial["draft_version_id"])
    if promoted is None:
        raise HTTPException(404, "草稿版本不存在，可能已被清理")
    active_hash = cert_versions.refresh_active_hash(store)
    store.update_cert_trial(trial_id, status="confirmed")
    return {"message": f"{trial['type_id']} 新版本已上线，worker 将在数秒内热切换",
            "active_set_hash": active_hash,
            "promoted_version_id": promoted["id"]}


@router.post("/cert-configs/trials/{trial_id}/cancel")
def cancel_trial(trial_id: int, request: Request):
    _ensure_seeded(request)
    store = _store(request)
    trial = store.get_cert_trial(trial_id)
    if trial is None:
        raise HTTPException(404, f"试跑 {trial_id} 不存在")
    if trial["status"] == "confirmed":
        raise HTTPException(409, "已上线的试跑不能取消（如需回退，改回旧 YAML 重新走流程）")
    stopped = store.fail_batch_open_tasks(
        [trial["draft_batch_id"], trial["ref_batch_id"]],
        error_code="TRIAL_CANCELLED", error_message="试跑已取消")
    store.update_cert_trial(trial_id, status="cancelled")
    store.retire_cert_version(trial["draft_version_id"])
    return {"message": f"已取消（终止未跑任务 {stopped} 个），草稿一并退役；生效配置未变"}
