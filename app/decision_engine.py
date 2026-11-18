"""Decision and supplier-ranking logic for Corvo purchase requests.

The functions in this module use the task-kit CSV/PDF files as local reference
inputs. They do not contain answers keyed to PR numbers.
"""

import csv
import io
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.policy_engine import load_policy, first_matching_rule


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = PROJECT_ROOT / "data" / "Cantill_FullStack_Task_Kit"
MASTER_ROOT = DATA_ROOT / "master_data"
CONTRACT_ROOT = DATA_ROOT / "contracts"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).replace(tzinfo=None)


def number(value: str | None, default: float = 0.0) -> float:
    if value is None or not value.strip():
        return default
    return float(value.replace(",", "").strip())


def normalize_words(text: str) -> set[str]:
    aliases = {"brg": "bearing", "hyd": "hydraulic", "qty": "quantity"}
    stop = {"the", "a", "an", "for", "of", "to", "need", "needed", "please", "with", "at", "maintenance"}
    words = re.findall(r"[a-z0-9]+", text.lower())
    normalized = set()
    for word in words:
        if word in stop:
            continue
        word = aliases.get(word, word)
        if len(word) > 4 and word.endswith("s"):
            word = word[:-1]
        normalized.add(word)
    return normalized


def resolve_item(item_code: str, description: str, items: dict[str, dict[str, str]]) -> dict[str, str] | None:
    if item_code and item_code in items:
        return items[item_code]
    query = normalize_words(description)
    if not query:
        return None
    ranked = []
    for item in items.values():
        item_words = normalize_words(item["description"])
        score = len(query & item_words) / len(query)
        if score:
            ranked.append((score, item))
    ranked.sort(key=lambda result: result[0], reverse=True)
    if not ranked:
        return None
    best_score = ranked[0][0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    if best_score >= 0.6 and best_score - second_score >= 0.15:
        return ranked[0][1]
    return None


def load_contracts() -> list[dict[str, Any]]:
    """Combine the register with signed PDF-only contracts when pypdf is installed."""
    register_rows = read_csv(CONTRACT_ROOT / "contract_register.csv")
    lines = read_csv(CONTRACT_ROOT / "contract_lines.csv")
    line_map: dict[str, dict[str, float]] = {}
    for line in lines:
        line_map.setdefault(line["contract_id"], {})[line["item_code"]] = number(line["rate"])

    contracts = []
    known_ids = set()
    for row in register_rows:
        known_ids.add(row["contract_id"])
        contracts.append({
            "contract_id": row["contract_id"],
            "supplier_id": row["supplier_id"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "value_cap": number(row["value_cap"]),
            "consumed_value": number(row["consumed_value"]),
            "rates": line_map.get(row["contract_id"], {}),
        })

    try:
        from pypdf import PdfReader
    except ImportError:
        return contracts

    item_line = re.compile(
        r"\b(ITM-\d{4})\s+(.+?)\s+(NOS|PAIR|KG|MTR|LTR|BKT|JOB)\s+([\d,]+(?:\.\d+)?)\b",
        re.IGNORECASE,
    )
    for pdf_path in (CONTRACT_ROOT / "pdf").glob("*.pdf"):
        try:
            text = "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)
        except Exception:
            continue
        id_match = re.search(r"Contract No\.\s*(RC-\d{4}-\d+)", text, re.IGNORECASE)
        if not id_match or id_match.group(1) in known_ids:
            continue
        contract_id = id_match.group(1)
        supplier_match = re.search(r"Supplier ID\s+(S\d+)", text, re.IGNORECASE)
        term_match = re.search(r"valid from\s+(20\d{2}-\d{2}-\d{2})\s+to\s+(20\d{2}-\d{2}-\d{2})", text, re.IGNORECASE)
        ceiling_match = re.search(r"shall not exceed INR\s+([\d,]+(?:\.\d+)?)", text, re.IGNORECASE)
        if not (supplier_match and term_match and ceiling_match):
            continue
        rates = {}
        for match in item_line.finditer(text):
            rates[match.group(1)] = number(match.group(4))
        contracts.append({
            "contract_id": contract_id,
            "supplier_id": supplier_match.group(1),
            "start_date": term_match.group(1),
            "end_date": term_match.group(2),
            "value_cap": number(ceiling_match.group(1)),
            "consumed_value": 0.0,
            "rates": rates,
        })
        known_ids.add(contract_id)
    return contracts


def preferred_suppliers(
    item_code: str,
    as_of: date,
    history: list[dict[str, str]],
    suppliers: dict[str, dict[str, str]],
    contracts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rank active suppliers using frequency, recency, contracts, delivery, and quality."""
    window_start = as_of - timedelta(days=548)
    po_by_supplier: dict[str, list[dict[str, str]]] = {}
    for po in history:
        if po["item_code"] != item_code:
            continue
        if not (window_start <= parse_date(po["po_date"]) <= as_of):
            continue
        supplier = suppliers.get(po["supplier_id"])
        if supplier and supplier["status"].lower() == "active":
            po_by_supplier.setdefault(po["supplier_id"], []).append(po)

    contract_by_supplier: dict[str, list[dict[str, Any]]] = {}
    for contract in contracts:
        if item_code not in contract["rates"] or parse_date(contract["start_date"]) > as_of:
            continue
        supplier = suppliers.get(contract["supplier_id"])
        if supplier and supplier["status"].lower() == "active":
            contract_by_supplier.setdefault(contract["supplier_id"], []).append(contract)

    supplier_ids = set(po_by_supplier) | set(contract_by_supplier)
    ranked = []
    for supplier_id in supplier_ids:
        supplier = suppliers[supplier_id]
        pos = sorted(po_by_supplier.get(supplier_id, []), key=lambda po: po["po_date"], reverse=True)
        latest_po = pos[0] if pos else None
        latest_contract = max(
            contract_by_supplier.get(supplier_id, []),
            key=lambda c: c["start_date"],
            default=None,
        )
        if latest_po:
            days_old = (as_of - parse_date(latest_po["po_date"])).days
            recency = 1.0 if days_old <= 90 else (0.5 if days_old <= 365 else 0.0)
        else:
            recency = 0.0
        count = len(pos)
        has_contract = bool(contract_by_supplier.get(supplier_id))
        on_time = number(supplier.get("on_time_pct"))
        quality = number(supplier.get("quality_rating"))
        score = count * 10 + recency * 20 + (15 if has_contract else 0) + on_time * 0.2 + quality * 3
        reference_price = (
            number(latest_po["unit_price"]) if latest_po
            else (latest_contract["rates"][item_code] if latest_contract else None)
        )
        reasons = f"{count} POs in 548 days; recency {recency:g}; contract {'yes' if has_contract else 'no'}; on-time {on_time:g}%; quality {quality:g}/5"
        ranked.append({
            "item_code": item_code,
            "supplier_id": supplier_id,
            "rank": 0,
            "reference_price": reference_price,
            "score": score,
            "reason": reasons,
        })
    ranked.sort(key=lambda supplier: (-supplier["score"], supplier["supplier_id"]))
    for index, supplier in enumerate(ranked, start=1):
        supplier["rank"] = index
    return ranked


def suggested_suppliers(
    item: dict[str, str] | None,
    count: int,
    as_of: date,
    preferred: list[dict[str, Any]],
    suppliers: dict[str, dict[str, str]],
) -> list[str]:
    if item is None or count <= 0:
        return []
    selected = [row["supplier_id"] for row in preferred[:count]]
    category = item["category"].strip().lower()
    extras = []
    for supplier_id, supplier in suppliers.items():
        if supplier["status"].lower() != "active" or supplier_id in selected:
            continue
        categories = {value.strip().lower() for value in re.split(r"[;|]", supplier["categories"])}
        if category in categories:
            extras.append(supplier)
    extras.sort(key=lambda row: (-number(row["on_time_pct"]), -number(row["quality_rating"]), row["supplier_id"]))
    for supplier in extras:
        if len(selected) >= count:
            break
        selected.append(supplier["supplier_id"])
    return selected


def _latest_po_price(item_code: str, req_date: date, history: list[dict[str, str]]) -> tuple[float | None, str | None]:
    cutoff = req_date - timedelta(days=365)
    rows = [po for po in history if po["item_code"] == item_code and cutoff <= parse_date(po["po_date"]) <= req_date]
    if not rows:
        return None, None
    po = max(rows, key=lambda row: row["po_date"])
    return number(po["unit_price"]), po["po_number"]


def _usable_contracts(item_code: str, req_date: date, contracts: list[dict[str, Any]], suppliers: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    applicable = []
    for contract in contracts:
        supplier = suppliers.get(contract["supplier_id"])
        if item_code not in contract["rates"] or not supplier or supplier["status"].lower() != "active":
            continue
        if parse_date(contract["start_date"]) <= req_date <= parse_date(contract["end_date"]):
            applicable.append(contract)
    return sorted(applicable, key=lambda contract: (contract["rates"][item_code], contract["contract_id"]))


def _has_recent_active_purchase(item_code: str, req_date: date, history: list[dict[str, str]], suppliers: dict[str, dict[str, str]]) -> bool:
    cutoff = req_date - timedelta(days=365)
    return any(
        po["item_code"] == item_code
        and cutoff <= parse_date(po["po_date"]) <= req_date
        and (supplier := suppliers.get(po["supplier_id"])) is not None
        and supplier["status"].lower() == "active"
        for po in history
    )


def _value_requirement(requirement: dict[str, Any], items: dict[str, dict[str, str]], history: list[dict[str, str]], suppliers: dict[str, dict[str, str]], contracts: list[dict[str, Any]]) -> tuple[float, float, str, dict[str, Any] | None]:
    item = requirement["item"]
    if item is None:
        total = sum(line["quantity"] * line["estimated_unit_price"] for line in requirement["lines"])
        return total, 0.0, "requester estimate", None

    req_date = requirement["requirement_date"]
    # Capex is routed to sourcing before contract rules, so it must not consume a contract ceiling.
    applicable = [] if item.get("item_type") == "Capex" else _usable_contracts(item["item_code"], req_date, contracts, suppliers)
    for contract in applicable:
        rate = contract["rates"][item["item_code"]]
        value = requirement["quantity"] * rate
        consumed = contract["consumed_value"]
        if consumed + value <= contract["value_cap"]:
            contract["consumed_value"] = consumed + value
            used = {"contract_id": contract["contract_id"], "supplier_id": contract["supplier_id"], "rate": rate}
            return value, rate, f"contract {contract['contract_id']}", used

    if applicable:
        rate = applicable[0]["rates"][item["item_code"]]
        return requirement["quantity"] * rate, rate, f"lowest applicable contract rate {applicable[0]['contract_id']} (ceiling exhausted)", None

    po_price, po_number = _latest_po_price(item["item_code"], req_date, history)
    if po_price is not None:
        return requirement["quantity"] * po_price, po_price, f"most recent PO {po_number}", None

    standard = number(item.get("standard_price"))
    return requirement["quantity"] * standard, standard, "item master standard price", None


def process_batch(csv_text: str) -> dict[str, Any]:
    """Resolve, club, value, route, and consolidate an uploaded PR batch."""
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    input_rows = list(reader)
    if not input_rows:
        return {"rows": [], "csv": "", "requirements": []}

    item_rows = read_csv(MASTER_ROOT / "item_master.csv")
    items = {row["item_code"]: row for row in item_rows}
    suppliers = {row["supplier_id"]: row for row in read_csv(MASTER_ROOT / "supplier_master.csv")}
    history = read_csv(MASTER_ROOT / "po_history.csv")
    contracts = load_contracts()

    lines = []
    for row in input_rows:
        item = resolve_item(row.get("item_code", "").strip(), row.get("description", ""), items)
        created = parse_datetime(row["created_at"])
        need_by = parse_date(row["need_by_date"])
        lines.append({
            "pr_no": row["pr_no"],
            "pr_line": row["pr_line"],
            "created_at": created,
            "requester": row.get("requester", ""),
            "department": row.get("department", ""),
            "plant": row["plant"],
            "item": item,
            "resolved_item_code": item["item_code"] if item else "NEW",
            "description": row.get("description", ""),
            "quantity": number(row.get("quantity")),
            "uom": row.get("uom", ""),
            "estimated_unit_price": number(row.get("estimated_unit_price")),
            "need_by_date": need_by,
            "urgency": row.get("urgency", "Normal"),
        })

    lines.sort(key=lambda line: (line["created_at"], line["pr_no"], int(line["pr_line"])))
    requirements: list[dict[str, Any]] = []
    for line in lines:
        target = None
        if line["item"] is not None:
            possible = []
            for req in requirements:
                if req["item"] is None or req["item"]["item_code"] != line["item"]["item_code"] or req["plant"] != line["plant"]:
                    continue
                day_delta = (line["created_at"].date() - req["first_created_at"].date()).days
                need_delta = abs((line["need_by_date"] - req["first_need_by_date"]).days)
                if 0 <= day_delta <= 14 and need_delta <= 7:
                    possible.append(req)
            if possible:
                target = min(possible, key=lambda req: req["first_created_at"])
        if target is None:
            target = {
                "group_id": f"REQ-{len(requirements) + 1:04d}",
                "item": line["item"],
                "resolved_item_code": line["resolved_item_code"],
                "description": line["description"],
                "plant": line["plant"],
                "first_created_at": line["created_at"],
                "requirement_date": line["created_at"].date(),
                "first_need_by_date": line["need_by_date"],
                "quantity": 0.0,
                "lines": [],
                "emergency": False,
            }
            requirements.append(target)
        target["lines"].append(line)
        target["quantity"] += line["quantity"]
        target["emergency"] = target["emergency"] or line["urgency"].lower() == "emergency"

    for req in requirements:
        policy = load_policy(req["requirement_date"].isoformat())
        value, unit_price, price_source, contract = _value_requirement(req, items, history, suppliers, contracts)
        item = req["item"]
        recent = bool(item and _has_recent_active_purchase(item["item_code"], req["requirement_date"], history, suppliers))
        facts = {
            "is_new_item": item is None,
            "item_type": item["item_type"] if item else None,
            "category": item["category"] if item else None,
            "plant": req["plant"],
            "usable_contract": contract is not None,
            "urgency": "Emergency" if req["emergency"] else "Normal",
            "requirement_value": value,
            "recent_active_purchase": recent,
        }
        rule = next((rule for rule in policy["rules"] if all(
            _condition(condition, facts) for condition in rule["when"]["all"]
        )), None)
        req["value"] = value
        req["unit_price"] = unit_price
        req["price_source"] = price_source
        req["contract"] = contract
        req["rule"] = rule
        req["policy_version"] = policy["version"]
        req["preferred"] = preferred_suppliers(item["item_code"], req["requirement_date"], history, suppliers, contracts) if item else []
        missing_estimate = item is None and any(line["estimated_unit_price"] <= 0 for line in req["lines"])
        req["decision"] = "SOURCING_REQUEST" if missing_estimate else (rule["decision"] if rule else "SOURCING_REQUEST")
        req["route"] = "NOT_PROCESSED" if missing_estimate else (rule["route"] if rule else "RFQ")
        req["supplier_id"] = None
        req["unit_price_out"] = None
        req["suggested"] = []
        if req["route"] == "CONTRACT_PO" and contract:
            req["supplier_id"] = contract["supplier_id"]
            req["unit_price_out"] = contract["rate"]
        elif req["route"] in {"SPOT_PO", "EMERGENCY_PO"} and req["preferred"]:
            req["supplier_id"] = req["preferred"][0]["supplier_id"]
            req["unit_price_out"] = req["preferred"][0]["reference_price"] or unit_price
        elif req["decision"] == "PO":
            req["decision"] = "SOURCING_REQUEST"
            req["route"] = "RFQ"
        if req["decision"] == "SOURCING_REQUEST":
            suggestion_count = rule.get("suggestion_count", 3) if rule else 3
            req["suggested"] = suggested_suppliers(item, suggestion_count, req["requirement_date"], req["preferred"], suppliers)
        req["reason"] = (
            f"Policy v{policy['version']} clause {rule['clause'] if rule else '5.5/5.6'}; "
            f"valued using {price_source}; "
            f"requirement date {req['requirement_date'].isoformat()}"
        )
        if missing_estimate:
            req["reason"] += "; NOT_PROCESSED because the new-item estimate is missing"
        if req["preferred"]:
            req["reason"] += f"; supplier ranking: {req['preferred'][0]['reason']}"

    # Consolidate PO requirements by supplier, plant, route, and 24-hour window.
    po_groups: list[dict[str, Any]] = []
    for req in requirements:
        req["po_id"] = ""
        if req["decision"] != "PO" or not req["supplier_id"]:
            continue
        match = next((po for po in po_groups
                      if po["supplier_id"] == req["supplier_id"]
                      and po["plant"] == req["plant"]
                      and po["route"] == req["route"]
                      and timedelta(0) <= req["first_created_at"] - po["first_created_at"] <= timedelta(hours=24)), None)
        if match is None:
            match = {
                "po_id": f"PO-{len(po_groups) + 1:04d}",
                "supplier_id": req["supplier_id"],
                "plant": req["plant"],
                "route": req["route"],
                "first_created_at": req["first_created_at"],
            }
            po_groups.append(match)
        req["po_id"] = match["po_id"]

    rows = []
    for req in requirements:
        for line in req["lines"]:
            rows.append({
                "pr_no": line["pr_no"],
                "pr_line": line["pr_line"],
                "resolved_item_code": req["resolved_item_code"],
                "group_id": req["group_id"],
                "po_id": req["po_id"],
                "decision": req["decision"],
                "route": req["route"],
                "supplier_id": req["supplier_id"] or "",
                "unit_price": f"{req['unit_price_out']:.2f}" if req["unit_price_out"] is not None else "",
                "group_value_inr": f"{req['value']:.2f}",
                "suggested_supplier_ids": ";".join(req["suggested"]),
                "policy_version": req["policy_version"],
                "reason": req["reason"],
            })

    columns = ["pr_no", "pr_line", "resolved_item_code", "group_id", "po_id", "decision", "route", "supplier_id", "unit_price", "group_value_inr", "suggested_supplier_ids", "policy_version", "reason"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return {"rows": rows, "csv": output.getvalue(), "requirements": requirements}


def _condition(condition: dict[str, Any], facts: dict[str, Any]) -> bool:
    actual = facts.get(condition["field"])
    expected = condition["value"]
    operator = condition["operator"]
    if operator == "equals":
        return actual == expected
    if actual is None:
        return False
    if operator == "less_than":
        return actual < expected
    if operator == "less_than_or_equal":
        return actual <= expected
    if operator == "greater_than_or_equal":
        return actual >= expected
    raise ValueError(f"Unsupported policy operator: {operator}")


def preferred_export(as_of_text: str) -> str:
    """Build the policy-required preferred-supplier CSV for every catalogue item."""
    as_of = parse_date(as_of_text)
    items = read_csv(MASTER_ROOT / "item_master.csv")
    suppliers = {row["supplier_id"]: row for row in read_csv(MASTER_ROOT / "supplier_master.csv")}
    history = read_csv(MASTER_ROOT / "po_history.csv")
    contracts = load_contracts()
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["item_code", "rank", "supplier_id", "reference_price", "ranking_reason"],
    )
    writer.writeheader()
    for item in items:
        top_suppliers = preferred_suppliers(
            item["item_code"], as_of, history, suppliers, contracts
        )[:3]
        for supplier in top_suppliers:
            writer.writerow({
                "item_code": supplier["item_code"],
                "rank": supplier["rank"],
                "supplier_id": supplier["supplier_id"],
                "reference_price": supplier["reference_price"],
                "ranking_reason": supplier["reason"],
            })
    return output.getvalue()




