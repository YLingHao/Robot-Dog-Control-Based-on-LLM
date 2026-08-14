#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import logging
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

KB_DATA_PATH = Path(__file__).with_name("action_kb_data.json")


def _normalize_text(text: str) -> str:
    normalized = text.lower().strip()
    replacements = {
        "°": "度",
        "（": "(",
        "）": ")",
        "，": ",",
        "。": ".",
        "；": ";",
        "：": ":",
    }
    for src, dst in replacements.items():
        normalized = normalized.replace(src, dst)
    normalized = re.sub(r"[\s,.;:!?！？、]+", "", normalized)
    return normalized


def _build_base_prompt(user_text: str, extra_hint: Optional[str] = None) -> str:
    instruction = (
        "你是一个机器狗控制助手，将用户的自然语言命令转换为中间语义JSON。"
        "如果用户命令包含多个动作，必须完整输出全部动作并保持执行顺序。"
        "输出必须是严格JSON，顶层只允许 action_count 和 action_sequence。"
        "action_sequence 中每个步骤必须包含 step_id、action_type、action_name，并按需要包含 direction 和 target。"
    )
    if extra_hint:
        instruction += extra_hint
    return (
        "你将收到一条用户命令。你的任务是只输出一个严格的 JSON 对象，且在 JSON 结束后立即停止。\n\n"
        "[系统要求]\n"
        f"{instruction}\n\n"
        "[用户命令]\n"
        f"{user_text}\n\n"
        "[输出]\n"
    )


@lru_cache(maxsize=1)
def load_action_kb() -> Dict[str, Any]:
    with KB_DATA_PATH.open("r", encoding="utf-8") as f:
        kb = json.load(f)

    action_index: Dict[str, Dict[str, Any]] = {}

    for action in kb.get("actions", []):
        aliases = action.get("aliases", [])
        normalized_aliases = []
        seen = set()
        for alias in aliases:
            normalized = _normalize_text(alias)
            if normalized and normalized not in seen:
                normalized_aliases.append(normalized)
                seen.add(normalized)
        action["_normalized_aliases"] = normalized_aliases
        action_index[action.get("id", "")] = action

    kb["action_index"] = action_index
    return kb


def retrieve_actions(query: str, k: Optional[int] = None) -> Dict[str, Any]:
    kb = load_action_kb()
    normalized_query = _normalize_text(query)
    top_k = k or kb.get("top_k_default", 6)
    scored_actions: List[Tuple[int, Dict[str, Any], List[str]]] = []
    ordered_matches: List[Dict[str, Any]] = []

    for action in kb.get("actions", []):
        score = 0
        matched_aliases: List[str] = []
        earliest_pos = None
        for alias, normalized_alias in zip(action.get("aliases", []), action.get("_normalized_aliases", [])):
            if normalized_alias and normalized_alias in normalized_query:
                pos = normalized_query.find(normalized_alias)
                if pos != -1 and (earliest_pos is None or pos < earliest_pos):
                    earliest_pos = pos
                score += max(len(normalized_alias), 2) * 10
                matched_aliases.append(alias)
                ordered_matches.append({
                    "id": action.get("id", ""),
                    "alias": alias,
                    "normalized_alias": normalized_alias,
                    "position": pos,
                })

        if score > 0:
            if earliest_pos is not None:
                score += max(0, 50 - earliest_pos)
            scored_actions.append((score, action, matched_aliases))

    scored_actions.sort(key=lambda item: (-item[0], item[1].get("id", "")))
    ordered_matches.sort(key=lambda item: (item["position"], item["id"], item["alias"]))

    selected = scored_actions[:top_k]
    selected_ids = {item[1].get("id", "") for item in selected}
    ordered_selected_matches = [item for item in ordered_matches if item["id"] in selected_ids]

    occurrence_sequence: List[str] = []
    for match in ordered_selected_matches:
        if not occurrence_sequence or occurrence_sequence[-1] != match["id"]:
            occurrence_sequence.append(match["id"])

    return {
        "query": query,
        "normalized_query": normalized_query,
        "actions": [item[1] for item in selected],
        "matched_aliases": {item[1].get("id", ""): item[2] for item in selected},
        "ordered_matches": ordered_selected_matches,
        "occurrence_sequence": occurrence_sequence,
        "expected_action_count": len(occurrence_sequence),
        "top_k": top_k,
    }


def render_kb_snippet(retrieved: Dict[str, Any], max_chars: Optional[int] = None) -> str:
    kb = load_action_kb()
    budget = max_chars or kb.get("snippet_char_budget", 1400)
    actions = retrieved.get("actions", [])

    if not actions:
        return (
            "- 未命中显式动作知识。你可以使用微调后的理解能力解释自然语言，但只能映射到你明确知道的动作语义；"
            "如果无法安全映射，请不要输出错误JSON。"
        )

    lines: List[str] = []
    for action in actions:
        aliases = "/".join(action.get("aliases", [])[:4])
        action_type = action.get("action_type", "unknown")
        action_name = action.get("action_name", "unknown")
        target_schema = action.get("target_schema", {})
        target_bits = [f"{k}={v}" for k, v in target_schema.items()]

        line = (
            f"- id={action.get('id')}; action_type={action_type}; action_name={action_name}; "
            f"aliases={aliases}; target_schema={', '.join(target_bits) if target_bits else '{}'}"
        )
        prerequisite = action.get("prerequisite_state")
        if prerequisite:
            line += f"; prerequisite={prerequisite}"
        note = action.get("note")
        if note:
            line += f"; note={note}"

        if sum(len(existing) + 1 for existing in lines) + len(line) > budget:
            break
        lines.append(line)

    return "\n".join(lines)


def get_action_output_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "action_count": {"type": "integer"},
            "action_sequence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "step_id": {"type": "integer"},
                        "action_type": {
                            "type": "string",
                            "enum": ["locomotion", "gait_switch", "posture_adjust", "trick", "state_control", "safety"]
                        },
                        "action_name": {"type": "string"},
                        "direction": {"type": "string"},
                        "target": {"type": "object"}
                    },
                    "required": ["step_id", "action_type", "action_name", "target"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["action_count", "action_sequence"],
        "additionalProperties": False
    }


def build_augmented_prompt(user_text: str, top_k: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
    start = time.perf_counter()
    try:
        kb = load_action_kb()
        prompt = _build_base_prompt(
            user_text,
            "只输出一个 JSON 对象；不要输出示例、不要续写下一条命令、不要输出多份 JSON。"
        )
        elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
        debug = {
            "matched_action_ids": [],
            "matched_count": 0,
            "matched_aliases": {},
            "expected_action_count": 0,
            "occurrence_sequence": [],
            "retrieve_ms": elapsed_ms,
            "snippet_chars": 0,
            "used_fallback": False,
            "system_instruction": kb.get("system_instruction", ""),
        }
        return prompt, debug
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
        logging.error(f"加载动作知识库失败，已回退到基础提示词: {e}")
        prompt = _build_base_prompt(
            user_text,
            "如果无法安全映射，请不要输出JSON。"
        )
        return prompt, {
            "matched_action_ids": [],
            "matched_count": 0,
            "matched_aliases": {},
            "expected_action_count": 0,
            "occurrence_sequence": [],
            "retrieve_ms": elapsed_ms,
            "snippet_chars": 0,
            "used_fallback": True,
            "error": str(e),
            "system_instruction": "",
        }
