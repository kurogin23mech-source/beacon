"""Beacon API - FastAPI backend for project management."""

from __future__ import annotations

import os
import sys

# Add lib/ to path so we can import core
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import core
import firestore_client as db

app = FastAPI(title="Beacon API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(project_id: str) -> dict:
    data = db.get_project(project_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return data


def _save(project_id: str, data: dict) -> None:
    core.validate_project(data)
    db.save_project(project_id, data)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class MilestoneCreate(BaseModel):
    title: str
    target_date: str = ""

class MilestoneUpdate(BaseModel):
    title: str = ""
    progress: str = ""
    target_date: str = ""
    status: str = ""

class EntryCreate(BaseModel):
    description: str
    type: str = "task"
    date: str = ""
    detail: str = ""

class EntryUpdate(BaseModel):
    description: str = ""
    status: str = ""
    detail: str = ""
    date: str = ""

class LogCommit(BaseModel):
    hash: str
    message: str
    date: str
    summary: str = ""
    ms_id: str = ""
    progress: str = ""

class SummaryUpdate(BaseModel):
    text: str


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    return _load(project_id)


@app.put("/api/projects/{project_id}")
def put_project(project_id: str, body: dict):
    core.validate_project(body)
    db.save_project(project_id, body)
    return {"status": "ok", "project_id": project_id}


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/milestones")
def create_milestone(project_id: str, body: MilestoneCreate):
    data = _load(project_id)
    ms_id = core.milestone_add(data, body.title, body.target_date)
    _save(project_id, data)
    return {"ms_id": ms_id, "title": body.title}


@app.get("/api/projects/{project_id}/milestones/{ms_id}")
def get_milestone(project_id: str, ms_id: str):
    data = _load(project_id)
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            entries = ms.get("entries", [])
            total, done = core.count_task_status(entries)
            return {
                **ms,
                "total_tasks": total,
                "done_tasks": done,
                "entries": core.entries_to_json(entries),
            }
    raise HTTPException(status_code=404, detail=f"Milestone '{ms_id}' not found")


@app.patch("/api/projects/{project_id}/milestones/{ms_id}")
def update_milestone(project_id: str, ms_id: str, body: MilestoneUpdate):
    data = _load(project_id)
    try:
        ms = core.milestone_update(
            data, ms_id,
            title=body.title, progress=body.progress,
            target_date=body.target_date, status=body.status,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return {"id": ms["id"], "title": ms["title"], "status": ms["status"],
            "progress": ms.get("progress", 0)}


@app.post("/api/projects/{project_id}/milestones/{ms_id}/start")
def start_milestone(project_id: str, ms_id: str):
    data = _load(project_id)
    try:
        ms = core.milestone_start(data, ms_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return {"id": ms["id"], "title": ms["title"], "status": "in_progress"}


@app.post("/api/projects/{project_id}/milestones/{ms_id}/done")
def done_milestone(project_id: str, ms_id: str):
    data = _load(project_id)
    try:
        ms = core.milestone_done(data, ms_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return {"id": ms["id"], "title": ms["title"], "status": "done"}


@app.delete("/api/projects/{project_id}/milestones/{ms_id}")
def delete_milestone(project_id: str, ms_id: str):
    data = _load(project_id)
    try:
        ms = core.milestone_delete(data, ms_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return {"id": ms["id"], "status": "cancelled"}


# ---------------------------------------------------------------------------
# Entries (tasks / commits / notes)
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/milestones/{ms_id}/entries")
def create_entry(project_id: str, ms_id: str, body: EntryCreate):
    data = _load(project_id)
    try:
        eid = core.task_add(
            data, ms_id, body.description,
            entry_type=body.type, date=body.date, detail=body.detail,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return {"entry_id": eid, "description": body.description}


@app.patch("/api/projects/{project_id}/entries/{entry_id}")
def update_entry(project_id: str, entry_id: str, body: EntryUpdate):
    data = _load(project_id)
    try:
        ms, entry = core.task_update(
            data, entry_id,
            description=body.description, status=body.status,
            detail=body.detail, date=body.date,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return core.entries_to_json([entry])[0]


@app.post("/api/projects/{project_id}/entries/{entry_id}/done")
def done_entry(project_id: str, entry_id: str):
    data = _load(project_id)
    import datetime
    today = datetime.date.today().isoformat()
    try:
        ms, entry = core.task_done(data, entry_id, date=today)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return {"entry_id": entry_id, "status": "done"}


@app.delete("/api/projects/{project_id}/entries/{entry_id}")
def delete_entry(project_id: str, entry_id: str):
    data = _load(project_id)
    try:
        entry = core.task_delete(data, entry_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return {"entry_id": entry_id, "status": "cancelled"}


# ---------------------------------------------------------------------------
# Log (commit recording)
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/log")
def log_commit(project_id: str, body: LogCommit):
    data = _load(project_id)
    try:
        result = core.log_commit(
            data, ms_id=body.ms_id, commit_hash=body.hash,
            message=body.message, date=body.date,
            summary=body.summary, progress=body.progress,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return result


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

@app.patch("/api/projects/{project_id}/summary")
def update_summary(project_id: str, body: SummaryUpdate):
    data = _load(project_id)
    data["summary"] = body.text
    _save(project_id, data)
    return {"summary": body.text}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}
