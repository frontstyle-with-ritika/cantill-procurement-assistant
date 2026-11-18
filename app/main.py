"""Backend for the Corvo procurement assistant."""

import re
import csv
import io
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from typing import Any
from pydantic import BaseModel, Field
from app.api import router, save_chat_request
from app.decision_engine import process_batch

ROOT = Path(__file__).parent
app = FastAPI(title="Corvo Procurement Assistant", version="0.2.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.include_router(router)

ITEM_MASTER_PATH = (
    ROOT.parent
    / "data"
    / "Cantill_FullStack_Task_Kit"
    / "master_data"
    / "item_master.csv"
)

class IntakeMessage(BaseModel):
    message: str
    previous: dict[str, Any] = Field(default_factory=dict)


def find_quantity_and_unit(message: str) -> tuple[float | None, str | None]:
    """Find an amount followed by a familiar unit, such as '100 pairs'."""

    units = {
        "pair": "PAIR",
        "pairs": "PAIR",
        "nos": "NOS",
        "pcs": "NOS",
        "pieces": "NOS",
        "kg": "KG",
        "kgs": "KG",
        "ltr": "LTR",
        "litre": "LTR",
        "litres": "LTR",
        "liter": "LTR",
        "liters": "LTR",
        "mtr": "MTR",
        "meter": "MTR",
        "meters": "MTR",
        "job": "JOB",
        "jobs": "JOB",
        "bkt": "BKT",
    }
    unit_pattern = "|".join(sorted(units, key=len, reverse=True))
    match = re.search(
        rf"\b([\d,]+(?:\.\d+)?)\s*({unit_pattern})\b",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return None, None

    quantity = float(match.group(1).replace(",", ""))
    if quantity.is_integer():
        quantity = int(quantity)

    unit = units[match.group(2).lower()]
    return quantity, unit


def find_plant(message: str) -> tuple[str | None, str | None]:
    """Recognize the two Corvo plants and their common names."""

    plants = [
        (r"\b(CHK1|Chakan)\b", "CHK1", "Chakan"),
        (r"\b(PUN1|Pune|Bhosari)\b", "PUN1", "Bhosari"),
    ]
    for pattern, plant_code, plant_name in plants:
        if re.search(pattern, message, flags=re.IGNORECASE):
            return plant_code, plant_name
    return None, None


def find_needed_by_date(message: str) -> str | None:
    """Recognize an ISO date or a date like 'by December 1'."""

    iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", message)
    if iso_match:
        try:
            return date.fromisoformat(iso_match.group(1)).isoformat()
        except ValueError:
            return None

    month_names = (
        "January|February|March|April|May|June|July|August|"
        "September|October|November|December|"
        "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    )
    match = re.search(
        rf"\b(?:by|before|on)?\s*({month_names})\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    try:
        month = datetime.strptime(match.group(1)[:3].title(), "%b").month
        year = int(match.group(3)) if match.group(3) else date.today().year
        return date(year, month, int(match.group(2))).isoformat()
    except ValueError:
        return None
def find_catalog_item(request_text: str) -> dict:
    """Find a likely catalogue item, or report that the match is unclear."""

    text = request_text.lower()

    # Remove quantity and unit, plants, and dates so they don't affect item matching.
    text = re.sub(
        r"\b[\d,]+(?:\.\d+)?\s*(?:pairs?|nos|pcs|pieces?|kgs?|"
        r"ltr|litres?|liters?|mtr|meters?|jobs?|bkt)\b",
        " ",
        text,
    )
    text = re.sub(
        r"\b(?:january|february|march|april|may|june|july|august|"
        r"september|october|november|december|jan|feb|mar|apr|jun|"
        r"jul|aug|sep|sept|oct|nov|dec)\s+\d{1,2}"
        r"(?:st|nd|rd|th)?(?:,?\s+20\d{2})?\b",
        " ",
        text,
    )
    text = re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", " ", text)
    text = re.sub(
        r"\b(?:chakan|chk1|pune|bhosari|pun1|we|need|i|want|please|"
        r"buy|order|for|by|the|a|an|of|to|at|on|before|this|is|"
        r"normal|urgent|urgently|emergency|urgency|asap|immediately|critical)\b",
        " ",
        text,
    )

    ignored_words = {
        "pair", "pairs", "nos", "pcs", "piece", "pieces",
        "kg", "kgs", "ltr", "mtr", "meter", "meters",
        "job", "jobs", "bkt",
    }

    def words(value: str) -> set[str]:
        found = re.findall(r"[a-z0-9]+", value.lower())
        cleaned = []
        for word in found:
            if word in ignored_words:
                continue
            # Treat simple plurals like "gloves" and "glove" as the same word.
            if len(word) > 4 and word.endswith("s"):
                word = word[:-1]
            cleaned.append(word)
        return set(cleaned)

    query_words = words(text)
    if not query_words:
        return {"status": "no_description", "item": None, "candidates": []}
    if not ITEM_MASTER_PATH.exists():
        return {"status": "catalog_missing", "item": None, "candidates": []}

    with ITEM_MASTER_PATH.open(encoding="utf-8-sig", newline="") as file:
        items = list(csv.DictReader(file))

    ranked = []
    for item in items:
        item_words = words(item["description"])
        overlap = query_words & item_words
        score = len(overlap) / len(query_words)
        if score > 0:
            ranked.append((score, item))

    ranked.sort(key=lambda result: result[0], reverse=True)
    if not ranked:
        return {"status": "not_found", "item": None, "candidates": []}

    top_score, top_item = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0

    candidates = [
        {
            "item_code": item["item_code"],
            "description": item["description"],
            "item_type": item["item_type"],
        }
        for _, item in ranked[:3]
    ]

    # Require a reasonably strong and clearly better match.
    if top_score >= 0.6 and top_score - second_score >= 0.15:
        return {"status": "matched", "item": candidates[0], "candidates": candidates}

    return {"status": "ambiguous", "item": None, "candidates": candidates}

@app.get("/api/health")
def health_check() -> dict[str, str]:
    """Confirm that the backend is running."""
    return {"status": "ok", "app": "Corvo Procurement Assistant"}


def find_estimated_unit_price(message: str) -> float | None:
    match = re.search(
        r"(?:estimated\s+(?:unit\s+)?price|unit\s+price|price)\s*(?:inr|rs\.?|₹)?\s*([\d,]+(?:\.\d+)?)",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def find_urgency(message: str) -> str | None:
    if re.search(r"\b(emergency|urgent)\b", message, flags=re.IGNORECASE):
        return "Emergency"
    if re.search(r"\b(normal|not urgent)\b", message, flags=re.IGNORECASE):
        return "Normal"
    return None


@app.post("/api/intake")
def receive_intake(payload: IntakeMessage) -> dict:
    """Collect a chat request, run it through the same engine, and ask for confirmation."""
    message = payload.message.strip()

    if message.casefold() in {"confirm", "yes", "confirm request"} and payload.previous.get("ready_for_confirmation"):
        saved = save_chat_request(payload.previous)
        return {
            "reply": f"Purchase request {saved['pr_no']} was created and added to the buyer queue.",
            "parsed": {},
            "missing": [],
        }

    quantity, unit = find_quantity_and_unit(message)
    plant_code, plant_name = find_plant(message)
    needed_by = find_needed_by_date(message)
    estimated_price = find_estimated_unit_price(message)
    urgency = find_urgency(message)

    parsed = payload.previous.copy()
    parsed.setdefault("requirement_date", date.today().isoformat())
    parsed.setdefault("created_at", datetime.now().isoformat(timespec="minutes"))
    parsed.setdefault("requester", "Chat requester")
    parsed["original_message"] = message
    earlier_text = parsed.get("request_text", "")
    parsed["request_text"] = f"{earlier_text} {message}".strip()

    item_match = find_catalog_item(parsed["request_text"])
    parsed["item_status"] = item_match["status"]
    parsed["resolved_item_code"] = item_match["item"]["item_code"] if item_match["item"] else None
    parsed["resolved_item_description"] = item_match["item"]["description"] if item_match["item"] else None

    if quantity is not None:
        parsed["quantity"] = quantity
        parsed["unit"] = unit
    if plant_code is not None:
        parsed["plant_code"] = plant_code
        parsed["plant_name"] = plant_name
    if needed_by is not None:
        parsed["needed_by_date"] = needed_by
    if estimated_price is not None:
        parsed["estimated_unit_price"] = estimated_price
    if urgency is not None:
        parsed["urgency"] = urgency

    missing = []
    if parsed.get("quantity") is None or parsed.get("unit") is None:
        missing.append("the quantity and unit")
    if parsed.get("plant_code") is None:
        missing.append("which plant")
    if parsed.get("needed_by_date") is None:
        missing.append("the date you need it")
    if parsed.get("urgency") is None:
        missing.append("whether this is normal or an emergency")
    if item_match["status"] in {"ambiguous", "no_description", "catalog_missing"}:
        missing.append("a clearer item description or item code")
    if item_match["status"] == "not_found" and parsed.get("estimated_unit_price") is None:
        missing.append("an estimated unit price for this new item")

    if missing:
        reply = "I still need " + ", ".join(missing) + ". What should I use?"
        return {"reply": reply, "parsed": parsed, "missing": missing}

    # Build one CSV-shaped request and use the same engine as batch processing.
    fields = [
        "pr_no", "pr_line", "created_at", "requester", "department", "plant",
        "item_code", "description", "quantity", "uom", "estimated_unit_price",
        "need_by_date", "urgency",
    ]
    synthetic = io.StringIO()
    writer = csv.DictWriter(synthetic, fieldnames=fields)
    writer.writeheader()
    chat_pr_no = "CHAT-" + datetime.now().strftime("%Y%m%d%H%M%S")
    writer.writerow({
        "pr_no": chat_pr_no,
        "pr_line": 1,
        "created_at": parsed["created_at"],
        "requester": parsed["requester"],
        "department": "General",
        "plant": parsed["plant_code"],
        "item_code": parsed["resolved_item_code"] or "",
        "description": parsed["resolved_item_description"] or "new item from chat",
        "quantity": parsed["quantity"],
        "uom": parsed["unit"],
        "estimated_unit_price": parsed.get("estimated_unit_price", ""),
        "need_by_date": parsed["needed_by_date"],
        "urgency": parsed["urgency"],
    })
    decision = process_batch(synthetic.getvalue())["rows"][0]
    parsed["decision_result"] = decision
    parsed["ready_for_confirmation"] = True
    parsed["resolved_item_code"] = decision["resolved_item_code"]

    reply = (
        f"I understood {parsed['quantity']} {parsed['unit']} for {parsed['plant_name']} "
        f"by {parsed['needed_by_date']}. The catalogue item is "
        f"{parsed['resolved_item_code']}. Proposed route: {decision['route']} "
        f"({decision['decision']}). {decision['reason']} Reply CONFIRM to create this PR."
    )
    return {"reply": reply, "parsed": parsed, "missing": []}


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    """Show the browser page."""
    return FileResponse(ROOT / "static" / "index.html")

