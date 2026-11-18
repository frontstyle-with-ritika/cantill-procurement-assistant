"""HTTP routes for batch processing, buyer actions, and policy management."""

import csv
import io
import json
import sqlite3
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.decision_engine import process_batch, preferred_export
from app.policy_engine import (
    compare_policies,
    load_policy,
    save_and_activate_policy,
    validate_policy,
)


router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = PROJECT_ROOT / "data"
DB_PATH = DATA_ROOT / "corvo.sqlite3"
CSV_COLUMNS = [
    "pr_no", "pr_line", "resolved_item_code", "group_id", "po_id",
    "decision", "route", "supplier_id", "unit_price", "group_value_inr",
    "suggested_supplier_ids", "policy_version", "reason",
]


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS batch_runs (
            batch_id TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            line_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS decisions (
            row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            row_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending',
            actor TEXT,
            comment TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            comment TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    return db


def json_response_row(row: sqlite3.Row) -> dict[str, Any]:
    result = json.loads(row["row_json"])
    result.update({
        "row_id": row["row_id"],
        "status": row["status"],
        "actor": row["actor"],
        "comment": row["comment"],
        "updated_at": row["updated_at"],
    })
    return result


def save_chat_request(parsed: dict[str, Any]) -> dict[str, str]:
    """Save a requester-confirmed chat PR as a pending buyer decision."""
    decision = parsed.get("decision_result")
    if not decision:
        raise ValueError("The chat request has no completed policy decision to save.")
    batch_id = f"chat-{uuid.uuid4()}"
    created_at = datetime.now().isoformat(timespec="seconds")
    pr_no = decision["pr_no"]
    db = connect_db()
    try:
        db.execute(
            "INSERT INTO batch_runs VALUES (?, ?, ?, ?)",
            (batch_id, "Confirmed chat request", created_at, 1),
        )
        db.execute(
            "INSERT INTO decisions (batch_id, row_json, status) VALUES (?, ?, 'Pending')",
            (batch_id, json.dumps(decision, ensure_ascii=False)),
        )
        db.execute(
            "INSERT INTO audit_log (entity_type, entity_id, action, actor, comment, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("chat_pr", pr_no, "created", parsed.get("requester", "Chat requester"), "Requester confirmed the chat PR", created_at),
        )
        db.commit()
    finally:
        db.close()
    return {"pr_no": pr_no, "batch_id": batch_id}


def csv_download(content: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/process-batch")
async def process_pr_batch(request: Request):
    """Accept raw CSV text and return row decisions plus CSV export text."""
    body = await request.body()
    try:
        csv_text = body.decode("utf-8-sig")
        result = process_batch(csv_text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not process this CSV: {exc}") from exc
    if not result["rows"]:
        raise HTTPException(status_code=400, detail="The CSV has no purchase-request rows.")

    batch_id = str(uuid.uuid4())
    created_at = datetime.now().isoformat(timespec="seconds")
    file_name = request.headers.get("x-file-name", "uploaded_pr_batch.csv")
    db = connect_db()
    try:
        db.execute(
            "INSERT INTO batch_runs VALUES (?, ?, ?, ?)",
            (batch_id, file_name, created_at, len(result["rows"])),
        )
        saved_rows = []
        for row in result["rows"]:
            cursor = db.execute(
                "INSERT INTO decisions (batch_id, row_json) VALUES (?, ?)",
                (batch_id, json.dumps(row, ensure_ascii=False)),
            )
            saved = dict(row)
            saved["row_id"] = cursor.lastrowid
            saved["status"] = "Pending"
            saved_rows.append(saved)
        db.commit()
    finally:
        db.close()

    return {
        "batch_id": batch_id,
        "file_name": file_name,
        "line_count": len(saved_rows),
        "decisions": saved_rows,
        "csv": result["csv"],
    }


@router.get("/api/batches")
def list_batches():
    db = connect_db()
    try:
        rows = db.execute("SELECT * FROM batch_runs ORDER BY created_at DESC").fetchall()
        return {"batches": [dict(row) for row in rows]}
    finally:
        db.close()


@router.get("/api/buyer/queue")
def buyer_queue(batch_id: str | None = None):
    db = connect_db()
    try:
        if batch_id:
            rows = db.execute(
                "SELECT * FROM decisions WHERE batch_id = ? ORDER BY row_id", (batch_id,)
            ).fetchall()
        else:
            latest = db.execute(
                "SELECT batch_id FROM batch_runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            rows = db.execute(
                "SELECT * FROM decisions WHERE batch_id = ? ORDER BY row_id",
                (latest["batch_id"],),
            ).fetchall() if latest else []
        return {"decisions": [json_response_row(row) for row in rows]}
    finally:
        db.close()


@router.post("/api/buyer/decisions/{row_id}/action")
async def buyer_action(row_id: int, request: Request):
    payload = await request.json()
    action = payload.get("action")
    actor = (payload.get("actor") or "Procurement buyer").strip()
    comment = (payload.get("comment") or "").strip()
    if action not in {"approve", "override"}:
        raise HTTPException(status_code=400, detail="Action must be approve or override.")
    if action == "override" and not comment:
        raise HTTPException(status_code=400, detail="An override needs a written reason.")

    db = connect_db()
    try:
        record = db.execute("SELECT * FROM decisions WHERE row_id = ?", (row_id,)).fetchone()
        if record is None:
            raise HTTPException(status_code=404, detail="Decision not found.")
        row_data = json.loads(record["row_json"])
        status = "Approved" if action == "approve" else "Overridden"
        audit_action = action
        audit_comment = comment
        if action == "override":
            new_decision = payload.get("decision")
            new_route = payload.get("route")
            if not new_decision or not new_route:
                raise HTTPException(status_code=400, detail="An override needs a replacement decision and route.")
            row_data["original_decision"] = row_data["decision"]
            row_data["original_route"] = row_data["route"]
            row_data["decision"] = new_decision
            row_data["route"] = new_route
        else:
            if row_data.get("route") == "NOT_PROCESSED":
                raise HTTPException(status_code=400, detail="This request cannot be approved until its missing information is resolved.")

            decision = row_data.get("decision")
            route = row_data.get("route", "")
            existing_reference = None
            group_id = row_data.get("group_id")
            if group_id:
                peers = db.execute(
                    "SELECT row_id, row_json FROM decisions WHERE batch_id = ?",
                    (record["batch_id"],),
                ).fetchall()
                for peer in peers:
                    if peer["row_id"] == row_id:
                        continue
                    peer_data = json.loads(peer["row_json"])
                    if (peer_data.get("group_id") == group_id
                            and peer_data.get("decision") == decision
                            and peer_data.get("route") == route
                            and peer_data.get("action_reference")):
                        existing_reference = peer_data["action_reference"]
                        break
            if decision == "PO":
                reference = row_data.get("po_id") or existing_reference or f"PO-{row_id:05d}"
                action_type = "purchase order"
                audit_action = "decision_approved" if existing_reference else "po_created"
            elif decision == "SOURCING_REQUEST":
                prefix = route if route in {"RFQ", "RFP"} else "SOURCING"
                action_type = "sourcing request"
                reference = existing_reference or f"{prefix}-{uuid.uuid4().hex[:8].upper()}"
                audit_action = "decision_approved" if existing_reference else "sourcing_request_created"
            else:
                raise HTTPException(status_code=400, detail="This decision cannot be approved.")

            row_data["action_reference"] = reference
            row_data["action_type"] = action_type
            row_data["action_created_at"] = datetime.now().isoformat(timespec="seconds")
            verb = "Added this line to" if existing_reference else "Created"
            audit_comment = f"{verb} {action_type} {reference} after buyer approval."
        now = datetime.now().isoformat(timespec="seconds")
        db.execute(
            "UPDATE decisions SET row_json=?, status=?, actor=?, comment=?, updated_at=? WHERE row_id=?",
            (json.dumps(row_data, ensure_ascii=False), status, actor, comment, now, row_id),
        )
        db.execute(
            "INSERT INTO audit_log (entity_type, entity_id, action, actor, comment, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("decision", str(row_id), audit_action, actor, audit_comment, now),
        )
        db.commit()
        return {"row_id": row_id, "status": status, "decision": row_data}
    finally:
        db.close()


@router.get("/api/buyer/audit")
def buyer_audit():
    db = connect_db()
    try:
        rows = db.execute("SELECT * FROM audit_log ORDER BY audit_id DESC").fetchall()
        return {"actions": [dict(row) for row in rows]}
    finally:
        db.close()


@router.get("/api/batches/{batch_id}/export.csv")
def export_batch(batch_id: str):
    db = connect_db()
    try:
        rows = db.execute("SELECT row_json FROM decisions WHERE batch_id=? ORDER BY row_id", (batch_id,)).fetchall()
    finally:
        db.close()
    if not rows:
        raise HTTPException(status_code=404, detail="Batch not found.")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(json.loads(row["row_json"]) for row in rows)
    return csv_download(output.getvalue(), f"batch_{batch_id[:8]}.csv")


@router.get("/api/preferred-suppliers.csv")
def export_preferred_suppliers(as_of: str):
    try:
        content = preferred_export(as_of)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not build supplier export: {exc}") from exc
    return csv_download(content, f"preferred_suppliers_{as_of}.csv")


@router.get("/api/policy")
def get_policy():
    return load_policy()


@router.post("/api/policy/preview")
async def preview_policy(request: Request):
    try:
        candidate = await request.json()
        validate_policy(candidate)
        changes = compare_policies(load_policy(), candidate)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Policy could not be previewed: {exc}") from exc
    return {
        "version": candidate["version"],
        "effective_date": candidate["effective_date"],
        "changes": changes,
        "policy": candidate,
    }


@router.post("/api/policy/activate")
async def activate_policy(request: Request):
    payload = await request.json()
    candidate = payload.get("policy", {})
    actor = (payload.get("actor") or "Procurement admin").strip()
    try:
        info = save_and_activate_policy(candidate)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Policy was not activated: {exc}") from exc
    db = connect_db()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        db.execute(
            "INSERT INTO audit_log (entity_type, entity_id, action, actor, comment, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("policy", info["version"], "activate", actor, f"Effective {info['effective_date']}", now),
        )
        db.commit()
    finally:
        db.close()
    return {"status": "active", **info}


@router.post("/api/policy/extract-pdf")
async def extract_policy_pdf(request: Request):
    try:
        from pypdf import PdfReader
        pdf = PdfReader(io.BytesIO(await request.body()))
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        if not text.strip():
            raise ValueError("No text could be extracted from the PDF.")
        return {"extracted_text": text}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read this policy PDF: {exc}") from exc


@router.post("/api/data/import")
async def import_task_kit(request: Request):
    """Upload the supplied ZIP at runtime; task data stays out of the source repo."""
    raw = await request.body()
    root = DATA_ROOT.resolve()
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        extracted = 0
        for member in archive.infolist():
            target = (DATA_ROOT / member.filename).resolve()
            if root not in target.parents and target != root:
                continue
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
            extracted += 1
        archive.close()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not import task kit ZIP: {exc}") from exc
    return {"status": "imported", "files": extracted}


@router.post("/api/contracts/pdf")
async def upload_contract_pdf(request: Request, filename: str):
    safe_name = Path(filename).name
    if not safe_name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Upload a PDF contract.")
    destination = DATA_ROOT / "Cantill_FullStack_Task_Kit" / "contracts" / "pdf" / safe_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(await request.body())
    return {"status": "uploaded", "filename": safe_name, "note": "The decision engine reads PDF-only rate contracts when building its contract list."}

