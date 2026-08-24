from __future__ import annotations

import re
from dataclasses import dataclass, field


class ReflectionPatchError(ValueError):
    pass


_GROUP = re.compile(r"^\s*规则组\s*([\w-]+)\s*[:：]?\s*$")
_COMMAND = re.compile(
    r"^(SET_C|DELETE_C|REPLACE_R|ADD_R|DELETE_R|REPLACE_GROUP|ADD_GROUP|DELETE_GROUP|REPLACE_ALL)"
    r"(?:\s+([^\s]+))?\s*$"
)


@dataclass(slots=True)
class _Group:
    group_id: str
    header: str
    conditions: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)

    def render(self) -> str:
        condition = self.conditions[0] if self.conditions else "C: 无"
        return "\n".join([self.header, condition, *self.rules])


def _parse_groups(dsl: str) -> list[_Group]:
    text = dsl.strip()
    if text == "无规则":
        return []
    groups: list[_Group] = []
    current: _Group | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _GROUP.match(line)
        if match:
            current = _Group(match.group(1), line)
            if any(group.group_id == current.group_id for group in groups):
                raise ReflectionPatchError(f"Duplicate rule group: {current.group_id}")
            groups.append(current)
        elif current is not None and re.match(r"^C\s*[:：]", line, re.I):
            if current.conditions:
                raise ReflectionPatchError(f"Rule group {current.group_id} contains more than one C line")
            if current.rules:
                raise ReflectionPatchError(f"C must appear before every R in rule group {current.group_id}")
            current.conditions.append(line)
        elif current is not None and re.match(r"^R\s*[:：]", line, re.I):
            current.rules.append(line)
        else:
            raise ReflectionPatchError(f"Draft is not canonical editable DSL: {line}")
    if not groups or any(not group.rules for group in groups):
        raise ReflectionPatchError("Every editable rule group must contain at least one R line")
    return groups


def addressed_draft(dsl: str) -> str:
    """Render a draft for the model with stable group/rule addresses."""
    groups = _parse_groups(dsl)
    if not groups:
        return "无规则"
    rendered: list[str] = []
    for group in groups:
        rendered.extend([group.header, group.conditions[0] if group.conditions else "C: 无"])
        rendered.extend(
            f"[{group.group_id}/{index}] {rule}"
            for index, rule in enumerate(group.rules, start=1)
        )
    return "\n".join(rendered)


def _payload_line(lines: list[str], index: int, prefix: str) -> tuple[str, int]:
    if index >= len(lines) or not re.match(rf"^{prefix}\s*[:：]", lines[index], re.I):
        raise ReflectionPatchError(f"Patch command requires one {prefix}: payload line")
    return lines[index].strip(), index + 1


def _block(lines: list[str], index: int, terminator: str) -> tuple[str, int]:
    content: list[str] = []
    while index < len(lines) and lines[index].strip() != terminator:
        content.append(lines[index])
        index += 1
    if index >= len(lines):
        raise ReflectionPatchError(f"Patch block is missing {terminator}")
    return "\n".join(content).strip(), index + 1


def _target(value: str | None, *, with_rule: bool = False) -> tuple[str, int | None]:
    if not value:
        raise ReflectionPatchError("Patch command is missing a target")
    if with_rule:
        match = re.fullmatch(r"([^/]+)/([1-9]\d*)", value)
        if not match:
            raise ReflectionPatchError(f"Expected group/rule target, got: {value}")
        return match.group(1), int(match.group(2))
    return value, None


def apply_reflection_patch(draft: str, patch: str) -> str:
    """Apply a sparse, address-based reflection patch to canonical draft DSL.

    Rule addresses always refer to the original draft, so operation order cannot
    accidentally retarget a later rule after a deletion.
    """
    stripped = patch.strip()
    if stripped == "NO_CHANGES":
        groups = _parse_groups(draft)
        return "\n".join(group.render() for group in groups)
    lines = [line.rstrip() for line in stripped.splitlines() if line.strip()]
    if not lines:
        raise ReflectionPatchError("Reflection patch is empty")
    if lines[0].strip() == "REPLACE_ALL":
        replacement, end_index = _block(lines, 1, "END_ALL")
        if end_index != len(lines):
            raise ReflectionPatchError("REPLACE_ALL must be the only patch command")
        if replacement != "无规则":
            replacement_groups = _parse_groups(replacement)
            return "\n".join(group.render() for group in replacement_groups)
        return replacement

    groups = _parse_groups(draft)
    by_id = {group.group_id: group for group in groups}
    set_conditions: dict[str, list[str]] = {}
    replacements: dict[tuple[str, int], str] = {}
    deletions: set[tuple[str, int]] = set()
    additions: dict[str, list[str]] = {}
    group_replacements: dict[str, _Group] = {}
    group_deletions: set[str] = set()
    added_groups: list[_Group] = []
    replace_all: str | None = None

    index = 0
    while index < len(lines):
        command_line = lines[index].strip()
        index += 1
        match = _COMMAND.match(command_line)
        if not match:
            raise ReflectionPatchError(f"Unknown patch command: {command_line}")
        command, raw_target = match.groups()
        if command == "REPLACE_ALL":
            raise ReflectionPatchError("REPLACE_ALL must be the only patch command")
        if command in {"SET_C", "DELETE_C", "REPLACE_GROUP", "DELETE_GROUP"}:
            group_id, _ = _target(raw_target)
            if group_id not in by_id:
                raise ReflectionPatchError(f"Unknown rule group: {group_id}")
        if command in {"REPLACE_R", "DELETE_R"}:
            group_id, rule_index = _target(raw_target, with_rule=True)
            if group_id not in by_id or rule_index is None or rule_index > len(by_id[group_id].rules):
                raise ReflectionPatchError(f"Unknown rule address: {raw_target}")
        if command == "ADD_R":
            group_id, _ = _target(raw_target)
            if group_id not in by_id:
                raise ReflectionPatchError(f"Unknown rule group: {group_id}")

        if command == "SET_C":
            condition, index = _payload_line(lines, index, "C")
            current_condition = by_id[group_id].conditions[0] if by_id[group_id].conditions else "C: 无"
            if condition != current_condition:
                set_conditions[group_id] = [condition]
        elif command == "DELETE_C":
            current_condition = by_id[group_id].conditions[0] if by_id[group_id].conditions else "C: 无"
            if current_condition != "C: 无":
                set_conditions[group_id] = ["C: 无"]
        elif command == "REPLACE_R":
            rule, index = _payload_line(lines, index, "R")
            key = (group_id, rule_index)
            if key in replacements or key in deletions:
                raise ReflectionPatchError(f"Rule target edited more than once: {raw_target}")
            if rule != by_id[group_id].rules[rule_index - 1]:
                replacements[key] = rule
        elif command == "DELETE_R":
            key = (group_id, rule_index)
            if key in replacements or key in deletions:
                raise ReflectionPatchError(f"Rule target edited more than once: {raw_target}")
            deletions.add(key)
        elif command == "ADD_R":
            rule, index = _payload_line(lines, index, "R")
            additions.setdefault(group_id, []).append(rule)
        elif command == "REPLACE_GROUP":
            body, index = _block(lines, index, "END_GROUP")
            parsed = _parse_groups(body)
            if len(parsed) != 1 or parsed[0].group_id != group_id:
                raise ReflectionPatchError("REPLACE_GROUP block must contain the targeted group number")
            if parsed[0].render() != by_id[group_id].render():
                group_replacements[group_id] = parsed[0]
        elif command == "DELETE_GROUP":
            group_deletions.add(group_id)
        elif command == "ADD_GROUP":
            if raw_target is not None:
                raise ReflectionPatchError("ADD_GROUP does not accept a target")
            body, index = _block(lines, index, "END_GROUP")
            parsed = _parse_groups(body)
            reuses_live_group = parsed and parsed[0].group_id in by_id and parsed[0].group_id not in group_deletions
            if len(parsed) != 1 or reuses_live_group or any(g.group_id == parsed[0].group_id for g in added_groups):
                raise ReflectionPatchError("ADD_GROUP must contain one new, unique group")
            added_groups.append(parsed[0])

    if replace_all is not None:
        if replace_all != "无规则":
            _parse_groups(replace_all)
        return replace_all

    conflicting_groups = set(group_replacements) | group_deletions
    for group_id in conflicting_groups:
        edited_rules = set(replacements) | deletions
        if group_id in set_conditions or group_id in additions or any(key[0] == group_id for key in edited_rules):
            raise ReflectionPatchError(f"Whole-group and entry edits conflict for group {group_id}")

    result: list[_Group] = []
    for group in groups:
        if group.group_id in group_deletions:
            continue
        if group.group_id in group_replacements:
            result.append(group_replacements[group.group_id])
            continue
        conditions = set_conditions.get(group.group_id, list(group.conditions))
        rules = [
            replacements.get((group.group_id, position), rule)
            for position, rule in enumerate(group.rules, start=1)
            if (group.group_id, position) not in deletions
        ]
        rules.extend(additions.get(group.group_id, []))
        if rules:
            result.append(_Group(group.group_id, group.header, conditions, rules))
    result.extend(added_groups)
    return "\n".join(group.render() for group in result) if result else "无规则"
