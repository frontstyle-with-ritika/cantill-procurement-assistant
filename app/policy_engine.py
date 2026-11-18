"""Load, validate, compare, and activate versioned procurement policies."""

import json
import re
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
DEFAULT_POLICY_PATH = APP_ROOT / "policies" / "policy_v5.json"
DATA_ROOT = PROJECT_ROOT / "data"
TASK_POLICY_PATH = DATA_ROOT / "Cantill_FullStack_Task_Kit" / "policy_v5.json"
ACTIVE_POLICY_PATH = DATA_ROOT / "active_policy.json"
POLICY_VERSIONS_DIR = DATA_ROOT / "policies"
SUPPORTED_OPERATORS = {
    "equals",
    "less_than",
    "less_than_or_equal",
    "greater_than_or_equal",
}


def load_policy(as_of_date: str | None = None) -> dict[str, Any]:
    """Choose the latest policy effective on the requested date."""
    baseline_path = DEFAULT_POLICY_PATH if DEFAULT_POLICY_PATH.exists() else TASK_POLICY_PATH
    if as_of_date is None:
        path = ACTIVE_POLICY_PATH if ACTIVE_POLICY_PATH.exists() else baseline_path
        with path.open(encoding="utf-8-sig") as file:
            return json.load(file)

    candidates = [baseline_path]
    if POLICY_VERSIONS_DIR.exists():
        candidates.extend(POLICY_VERSIONS_DIR.glob("policy_*.json"))
    eligible = []
    for candidate_path in candidates:
        if not candidate_path.exists():
            continue
        with candidate_path.open(encoding="utf-8-sig") as file:
            candidate = json.load(file)
        if candidate.get("effective_date", "") <= as_of_date:
            eligible.append(candidate)
    if not eligible:
        with baseline_path.open(encoding="utf-8-sig") as file:
            return json.load(file)
    return max(eligible, key=lambda candidate: (candidate["effective_date"], candidate["version"]))

def validate_policy(policy: dict[str, Any]) -> None:
    if not isinstance(policy.get("version"), str) or not policy["version"].strip():
        raise ValueError("Policy must include a version string.")
    if not isinstance(policy.get("effective_date"), str):
        raise ValueError("Policy must include an effective_date in YYYY-MM-DD format.")
    if not isinstance(policy.get("rules"), list) or not policy["rules"]:
        raise ValueError("Policy must include an ordered rules list.")
    for rule in policy["rules"]:
        if not all(key in rule for key in ("clause", "when", "decision", "route")):
            raise ValueError("Each rule needs clause, when, decision, and route fields.")
        conditions = rule.get("when", {}).get("all")
        if not isinstance(conditions, list) or not conditions:
            raise ValueError(f"Rule {rule['clause']} must have at least one condition.")
        for condition in conditions:
            if condition.get("operator") not in SUPPORTED_OPERATORS:
                raise ValueError(f"Unsupported operator in rule {rule['clause']}.")
            if "field" not in condition or "value" not in condition:
                raise ValueError(f"Condition in rule {rule['clause']} needs field and value.")


def save_and_activate_policy(policy: dict[str, Any]) -> dict[str, str]:
    """Store an approved version and make it the current policy for new runs."""
    validate_policy(policy)
    safe_version = re.sub(r"[^A-Za-z0-9._-]", "_", policy["version"])
    POLICY_VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    version_path = POLICY_VERSIONS_DIR / f"policy_{safe_version}.json"
    encoded = json.dumps(policy, indent=2, ensure_ascii=False)
    version_path.write_text(encoded, encoding="utf-8")
    ACTIVE_POLICY_PATH.write_text(encoded, encoding="utf-8")
    return {"version": policy["version"], "effective_date": policy["effective_date"]}


def compare_policies(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Create a readable summary of rule additions, removals, or changes."""
    old_rules = {rule["clause"]: rule for rule in old.get("rules", [])}
    new_rules = {rule["clause"]: rule for rule in new.get("rules", [])}
    changes = []
    for clause in sorted(new_rules.keys() - old_rules.keys()):
        changes.append(f"Adds rule {clause}: route {new_rules[clause]['route']}.")
    for clause in sorted(old_rules.keys() - new_rules.keys()):
        changes.append(f"Removes rule {clause} (previous route {old_rules[clause]['route']}).")
    for clause in sorted(old_rules.keys() & new_rules.keys()):
        if old_rules[clause] != new_rules[clause]:
            changes.append(f"Changes rule {clause}: route {old_rules[clause]['route']} to {new_rules[clause]['route']} or its conditions/outcome.")
    if not changes:
        changes.append("No rule content changed; only policy version or effective date may differ.")
    return changes


def condition_matches(condition: dict[str, Any], facts: dict[str, Any]) -> bool:
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


def rule_matches(rule: dict[str, Any], facts: dict[str, Any]) -> bool:
    return all(condition_matches(condition, facts) for condition in rule["when"]["all"])


def first_matching_rule(facts: dict[str, Any]) -> dict[str, Any] | None:
    policy = load_policy()
    for rule in policy["rules"]:
        if rule_matches(rule, facts):
            return {"policy_version": policy["version"], **rule}
    return None

