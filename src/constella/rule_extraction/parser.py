from __future__ import annotations

import hashlib
import re

from .models import StateExpression, StateTransition, StructuredRule, StructuredRuleSet
from .normalizer import normalize_state_text


class RuleParseError(ValueError):
    pass


_GROUP = re.compile(r"^\s*(?:规则组|rule\s*group)\s*([\w-]+)?\s*[:：]?\s*$", re.I)
_ENTRY = re.compile(r"^\s*([CR])\s*[:：]\s*(.*)$", re.I)
# The bracketed relation may itself be a formula containing square brackets.
# Stop only at the closing bracket immediately followed by the DSL arrow.
_ARROW = re.compile(
    r"\s*(?:(?:—|－|-)\s*(?:\[\s*(.*?)\s*\](?=\s*(?:→|->|⇒|⟶))|([^\s→]+))\s*)?(?:→|->|⇒|⟶)\s*"
)
_NO_RULE = re.compile(r"(?:^|\s)(?:no[_ -]?rule|无规则|没有(?:可抽取)?规则)(?:\s|$)", re.I)


def _stable_id(prefix: str, *parts: str) -> str:
    value = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(value).hexdigest()[:16]}"


def _clean(text: str) -> str:
    value = text.strip().replace("```dsl", "").replace("```text", "").replace("```", "").strip()
    # Qwen frequently renders a complete group in one natural-language line:
    # “规则组1，C: …，R: …”. Preserve its DSL fields while making them line-oriented.
    value = re.sub(r"(?m)^\s*((?:规则组|rule\s*group)\s*[\w-]+)\s*[，,]\s*", r"\1\n", value, flags=re.I)
    value = re.sub(r"[，,]\s*([CR])\s*[:：]", r"\n\1: ", value, flags=re.I)
    normalized_lines = []
    for line in value.splitlines():
        line = re.sub(r"^\s*[*+-]\s*", "", line.strip())
        # Formatting marks are not part of the DSL. Removing them here also
        # handles nested list/italic forms such as "*   *依据：...*".
        line = line.replace("**", "").replace("__", "").replace("*", "")
        normalized_lines.append(line)
    return "\n".join(normalized_lines)


def _state_expressions(raw: str) -> list[StateExpression]:
    text = re.sub(r"\s*\n\s*", " ", raw).strip()
    if not text or text in {"无", "none", "None", "-"}:
        return []
    result: list[StateExpression] = []
    # `` + `` separates DSL fields only when the following fragment starts a
    # new object|state (or object=state) expression.  A plus sign inside one
    # state must remain literal, notably for shielding-gas compositions such as
    # ``保护气体成分|He 90% + Ar 7.5% + CO2 2.5%``.
    for item in re.split(r"\s+\+\s+(?=[^+\n]*(?:\||=))", text):
        item = item.strip()
        if not item:
            continue
        if "|" in item:
            obj, state = (part.strip(" \t。；;") for part in item.split("|", 1))
        elif "=" in item:
            obj, state = (part.strip(" \t。；;") for part in item.split("=", 1))
        else:
            # A narrow, source-observed shorthand used by Qwen for table results.
            # It remains deterministic and intentionally does not try to infer arbitrary subjects.
            hit = re.fullmatch(r"(无|少量|大量)\s*气孔", item.strip(" \t。；;"))
            if hit:
                obj, state = "气孔", hit.group(1)
            else:
                # DSL is intentionally permissive in the first release.  A
                # multimodal model often emits a compact source phrase such as
                # “弧光辐射强烈” or “必须采取防风措施”.  Preserve it losslessly
                # instead of rejecting an otherwise parseable rule merely because
                # it does not choose the optional object|state notation.
                obj, state = item.strip(" \t。；;"), "提及"
        if not obj or not state:
            raise RuleParseError(f"State expression has an empty object or state: {item}")
        normalized = normalize_state_text(state)
        result.append(StateExpression(_stable_id("state", obj, state, normalized), obj, state, normalized))
    if not result:
        raise RuleParseError("Rule side has no state expressions")
    return result


def _parse_rule(raw: str, package_id: str, group_id: str, index: int, conditions: list[StateExpression]) -> StructuredRule:
    match = _ARROW.search(raw)
    if not match:
        # Treat a bare R: expression as a source-stated fact. This remains
        # reviewable via raw_expression and avoids silently discarding real model
        # output while the DSL grammar is deliberately not constrained.
        consequents = _state_expressions(raw)
        consequent_keys = {(item.object, item.raw_state, item.normalized_state) for item in consequents}
        conditions = [item for item in conditions if (item.object, item.raw_state, item.normalized_state) not in consequent_keys]
        antecedents = conditions or _state_expressions("原文上下文|存在")
        return StructuredRule(
            id=_stable_id("rule", package_id, group_id, str(index), raw), context_package_id=package_id,
            rule_group_id=group_id, rule_index=index, conditions=conditions, antecedents=antecedents,
            consequents=consequents, relation="陈述", transitions=[], raw_expression=raw,
        )
    left, right = raw[:match.start()].strip(), raw[match.end():].strip()
    antecedents, consequents = _state_expressions(left), _state_expressions(right)
    # C: is an applicability constraint, not a duplicate spelling of the rule
    # input. Keep only constraints which are not exactly present on R:'s left.
    antecedent_keys = {(item.object, item.raw_state, item.normalized_state) for item in antecedents}
    conditions = [item for item in conditions if (item.object, item.raw_state, item.normalized_state) not in antecedent_keys]
    transitions = [
        StateTransition(before.object, before.id, after.id)
        for before in antecedents for after in consequents
        if before.object == after.object and before.raw_state != after.raw_state
    ]
    return StructuredRule(
        id=_stable_id("rule", package_id, group_id, str(index), raw), context_package_id=package_id,
        rule_group_id=group_id, rule_index=index, conditions=conditions, antecedents=antecedents,
        consequents=consequents, relation=(match.group(1) or match.group(2)).strip() if (match.group(1) or match.group(2)) else None,
        transitions=transitions, raw_expression=raw,
    )


def parse_final_expression(expression: str, package_id: str, *, prompt_id: str, prompt_version: str, model: str) -> StructuredRuleSet:
    cleaned = _clean(expression)
    if not cleaned:
        raise RuleParseError("Model returned an empty final expression")
    if _NO_RULE.search(cleaned) and not re.search(r"^\s*R\s*[:：]", cleaned, re.M | re.I):
        return StructuredRuleSet(package_id, [], _improvement_notes(cleaned), expression, prompt_id, prompt_version, model)

    # Narrative follow-up sections can themselves quote C:/R: examples. They are
    # deliberately not formal rules and must not be consumed by the DSL parser.
    parseable = re.split(r"(?im)^\s*(?:#+\s*)?(?:后续改进|improvements?)\s*(?:[:：]\s*)?$", cleaned, maxsplit=1)[0]

    groups: list[tuple[str, list[tuple[str, list[StateExpression]]]]] = []
    group_id = "group_1"
    conditions: list[StateExpression] = []
    rules: list[tuple[str, list[StateExpression]]] = []
    active_kind: str | None = None
    active_value: list[str] = []

    def flush_entry() -> None:
        nonlocal conditions, rules, active_kind, active_value
        if active_kind is None:
            return
        value = " ".join(active_value).strip()
        if active_kind == "C":
            # A real Qwen output occasionally labels a complete arrow rule as
            # C:.  It cannot be an applicability constraint; recover it as an
            # R: entry rather than storing an arrow string as a state value.
            if _ARROW.search(value):
                rules.append((value, list(conditions)))
            else:
                conditions = _state_expressions(value)
        else:
            # Models may repeat C:/R: pairs inside a named group. Capture the
            # condition snapshot at each R: rather than incorrectly assigning
            # the group's final C: line to every rule.
            rules.append((value, list(conditions)))
        active_kind, active_value = None, []

    def flush_group() -> None:
        nonlocal conditions, rules
        flush_entry()
        if rules:
            groups.append((group_id, rules))
        conditions, rules = [], []

    for line in parseable.splitlines():
        if re.fullmatch(r"\s*-{2,}\s*", line):
            continue
        if re.match(r"^\s*(?:依据|证据)\s*[:：]", line):
            continue
        group_match = _GROUP.match(line)
        entry_match = _ENTRY.match(line)
        if group_match:
            flush_group()
            group_id = f"group_{group_match.group(1) or len(groups) + 1}"
            continue
        if entry_match:
            flush_entry()
            active_kind, active_value = entry_match.group(1).upper(), [entry_match.group(2)]
            continue
        if active_kind is not None and line.strip():
            active_value.append(line.strip())
    flush_group()
    if not groups:
        raise RuleParseError("No complete R: rule entries found")
    result: list[StructuredRule] = []
    for group, items in groups:
        for item, constraints in items:
            result.append(_parse_rule(item, package_id, group, len(result) + 1, constraints))
    return StructuredRuleSet(package_id, result, _improvement_notes(cleaned), expression, prompt_id, prompt_version, model)


def _improvement_notes(text: str) -> list[str]:
    match = re.search(r"(?:后续改进|improvement(?:s)?)\s*(?:[:：]\s*|\n)(.+)", text, re.I | re.S)
    if not match:
        return []
    return [line.strip(" -•\t") for line in match.group(1).splitlines() if line.strip(" -•\t")]
