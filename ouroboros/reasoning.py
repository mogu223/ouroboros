"""
Ouroboros — Reasoning enhancement for non-frontier models.

Provides lightweight techniques to improve reasoning quality without
requiring frontier models:
- Self-consistency voting
- Reflection (critique → revise)
- Structured output enforcement
- Lightweight verification

Design constraints:
- Minimal memory footprint (VPS has ~84MB available)
- Cost-efficient (limit sampling)
- Model-agnostic
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Callable

log = logging.getLogger(__name__)

# ============================================================================
# Self-Consistency
# ============================================================================

def normalize_answer(s: str) -> str:
    """Normalize answer for comparison in self-consistency voting."""
    s = s.strip().lower()
    s = re.sub(r'[\s\-]+', ' ', s)
    s = re.sub(r'[^\w\s\.\,\-\%/]', '', s)
    # Numeric canonicalization
    try:
        if re.match(r'^[\$\€\£]?\s*\d+(\.\d+)?%?$', s):
            num = re.findall(r'[\d\.]+', s)
            if num:
                return str(float(num[0]))
    except Exception:
        pass
    return s


def self_consistency_vote(
    answers: List[str],
    confidences: Optional[List[float]] = None,
    early_quorum: float = 0.67,
) -> Tuple[str, float, Dict[str, Any]]:
    """
    Aggregate multiple answers using self-consistency voting.

    Args:
        answers: List of candidate answers
        confidences: Optional list of confidence scores (0-1)
        early_quorum: Fraction needed for early stopping

    Returns:
        (best_answer, confidence, metadata)
    """
    if not answers:
        return "", 0.0, {"samples": 0}

    # Normalize for voting
    norm_groups: Dict[str, List[Tuple[int, str, float]]] = {}
    for i, ans in enumerate(answers):
        key = normalize_answer(ans)
        conf = confidences[i] if confidences and i < len(confidences) else 0.5
        if key not in norm_groups:
            norm_groups[key] = []
        norm_groups[key].append((i, ans, conf))

    # Find largest group
    best_key = max(norm_groups.items(), key=lambda kv: len(kv[1]))[0]
    group = norm_groups[best_key]

    # Pick representative with highest confidence
    best_idx, best_ans, best_conf = max(group, key=lambda t: t[2])

    # Calculate actual confidence (weighted by group size)
    group_size = len(group)
    total_size = len(answers)
    vote_fraction = group_size / total_size
    adjusted_conf = best_conf * (0.5 + 0.5 * vote_fraction)

    return best_ans, round(adjusted_conf, 3), {
        "samples": total_size,
        "vote_fraction": round(vote_fraction, 3),
        "group_size": group_size,
        "early_stop": vote_fraction >= early_quorum,
    }


# ============================================================================
# Reflection (Critique → Revise)
# ============================================================================

CRITIC_PROMPT = """You are a strict reviewer. Return JSON only, no explanations.

Task:
Question: {question}
Proposed answer: {answer}

Checklist:
- Correctness (0 or 1)
- Follows constraints (0 or 1)
- Common error type: one of ["math", "logic", "grounding", "format", "missing assumption", "ambiguity", "none"]
- Suggested fix: max 30 words, or "none" if correct

Return JSON: {{"correct": 0|1, "constraints": 0|1, "error": "...", "fix": "..."}}"""

REVISE_PROMPT = """You are a careful solver. Think privately, then return JSON only.

Question:
{question}

Your previous answer:
{prev_answer}

Reviewer feedback:
{critic_json}

Fix any issues. Return JSON: {{"answer": "<final>", "confidence": 0.0-1.0}}"""


def parse_json_from_text(text: str) -> Dict[str, Any]:
    """Extract JSON from potentially messy model output."""
    text = text.strip()
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting JSON object
    match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Try key-value extraction
    result = {}
    for key in ["correct", "constraints", "error", "fix", "answer", "confidence"]:
        m = re.search(rf'"{key}"\s*:\s*(?:"([^"]*)"|(\d+(?:\.\d+)?))', text)
        if m:
            result[key] = m.group(1) if m.group(1) else float(m.group(2)) if m.group(2) else ""
    return result


def reflection_pass(
    llm_chat_fn: Callable,
    question: str,
    answer: str,
    model: str = "glm-5",
) -> Tuple[str, float, Dict[str, Any]]:
    """
    Single reflection pass: critique -> revise.

    Args:
        llm_chat_fn: Function(messages, model, ...) -> (response, usage)
        question: Original question/task
        answer: Model's proposed answer
        model: Model to use

    Returns:
        (final_answer, confidence, metadata)
    """
    # Step 1: Critique
    critic_user = CRITIC_PROMPT.format(question=question[:500], answer=answer[:500])
    critic_msg, _ = llm_chat_fn(
        messages=[{"role": "user", "content": critic_user}],
        model=model,
        reasoning_effort="low",
        max_tokens=256,
    )
    critic_text = critic_msg.get("content", "")
    critic_json = parse_json_from_text(critic_text)

    is_correct = critic_json.get("correct", 0) == 1
    constraints_ok = critic_json.get("constraints", 0) == 1
    error_type = critic_json.get("error", "unknown")
    fix = critic_json.get("fix", "")

    # If correct and constraints OK, return as-is
    if is_correct and constraints_ok:
        return answer, 0.85, {
            "reflected": False,
            "critic": critic_json,
        }

    # Step 2: Revise
    revise_user = REVISE_PROMPT.format(
        question=question[:500],
        prev_answer=answer[:500],
        critic_json=json.dumps(critic_json, ensure_ascii=False),
    )
    revise_msg, _ = llm_chat_fn(
        messages=[{"role": "user", "content": revise_user}],
        model=model,
        reasoning_effort="medium",
        max_tokens=512,
    )
    revise_text = revise_msg.get("content", "")
    revise_json = parse_json_from_text(revise_text)

    revised_answer = revise_json.get("answer", answer)
    revised_conf = float(revise_json.get("confidence", 0.5))

    return revised_answer, revised_conf, {
        "reflected": True,
        "critic": critic_json,
        "error_type": error_type,
        "fix": fix[:50] if fix else "",
    }


# ============================================================================
# Structured Output Enforcement
# ============================================================================

STRUCTURED_PROMPT_TEMPLATE = """{task}

Important: Return your response as valid JSON with this structure:
{schema}

Return ONLY the JSON object, no additional text."""

def enforce_structured_output(
    llm_chat_fn: Callable,
    task: str,
    schema: Dict[str, str],
    model: str = "glm-5",
    max_retries: int = 2,
) -> Tuple[Dict[str, Any], bool]:
    """
    Enforce structured JSON output from the model.

    Args:
        llm_chat_fn: LLM chat function
        task: Task description
        schema: Dict of {field_name: description}
        model: Model to use
        max_retries: Maximum retry attempts

    Returns:
        (parsed_json, success)
    """
    schema_desc = "{\n"
    for key, desc in schema.items():
        schema_desc += f'  "{key}": {desc},\n'
    schema_desc += "}"

    prompt = STRUCTURED_PROMPT_TEMPLATE.format(task=task, schema=schema_desc)

    for attempt in range(max_retries + 1):
        msg, _ = llm_chat_fn(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            reasoning_effort="medium",
            max_tokens=1024,
        )
        text = msg.get("content", "")
        parsed = parse_json_from_text(text)

        # Check if all required fields present
        if all(k in parsed for k in schema.keys()):
            return parsed, True

        # Retry with stronger hint
        if attempt < max_retries:
            prompt = f"{prompt}\n\nYour previous response was missing required fields. Return ONLY valid JSON."

    return parsed, False


# ============================================================================
# Multi-Pass Verification
# ============================================================================

VERIFICATION_PROMPT = """Verify this answer before finalizing.

Question: {question}
Proposed answer: {answer}

Quick checks:
1. Does the answer directly address the question?
2. Are there any obvious errors or contradictions?
3. Is the format correct?

Return JSON: {{"verified": true|false, "issue": "description or null"}}"""

def verify_answer(
    llm_chat_fn: Callable,
    question: str,
    answer: str,
    model: str = "glm-5",
) -> Tuple[bool, Optional[str]]:
    """
    Quick verification pass for an answer.

    Returns:
        (is_verified, issue_description)
    """
    prompt = VERIFICATION_PROMPT.format(question=question[:300], answer=answer[:300])
    msg, _ = llm_chat_fn(
        messages=[{"role": "user", "content": prompt}],
        model=model,
        reasoning_effort="low",
        max_tokens=128,
    )
    text = msg.get("content", "")
    parsed = parse_json_from_text(text)

    verified = parsed.get("verified", True)
    issue = parsed.get("issue") if not verified else None

    return bool(verified), issue


# ============================================================================
# Composite Reasoning Strategies
# ============================================================================

def enhanced_reasoning(
    llm_chat_fn: Callable,
    question: str,
    model: str = "glm-5",
    strategy: str = "reflect",
    samples: int = 3,
) -> Tuple[str, float, Dict[str, Any]]:
    """
    Enhanced reasoning with configurable strategy.

    Strategies:
        - "simple": Single pass (baseline)
        - "reflect": Critique -> Revise
        - "vote": Self-consistency voting
        - "reflect_vote": Vote + reflection on best

    Args:
        llm_chat_fn: LLM chat function
        question: Task/question
        model: Model to use
        strategy: Strategy name
        samples: Number of samples for voting

    Returns:
        (answer, confidence, metadata)
    """
    if strategy == "simple":
        # Baseline: single pass
        msg, _ = llm_chat_fn(
            messages=[{"role": "user", "content": question}],
            model=model,
            reasoning_effort="medium",
            max_tokens=1024,
        )
        return msg.get("content", ""), 0.5, {"strategy": "simple"}

    elif strategy == "reflect":
        # Single pass + reflection
        msg, _ = llm_chat_fn(
            messages=[{"role": "user", "content": question}],
            model=model,
            reasoning_effort="medium",
            max_tokens=1024,
        )
        initial = msg.get("content", "")
        return reflection_pass(llm_chat_fn, question, initial, model)

    elif strategy == "vote":
        # Self-consistency voting
        answers = []
        for i in range(samples):
            msg, _ = llm_chat_fn(
                messages=[{"role": "user", "content": question}],
                model=model,
                reasoning_effort="medium",
                max_tokens=512,
            )
            answers.append(msg.get("content", ""))
        return self_consistency_vote(answers)

    elif strategy == "reflect_vote":
        # Vote -> Reflect on winner
        answers = []
        for i in range(samples):
            msg, _ = llm_chat_fn(
                messages=[{"role": "user", "content": question}],
                model=model,
                reasoning_effort="medium",
                max_tokens=512,
            )
            answers.append(msg.get("content", ""))

        best, conf, meta = self_consistency_vote(answers)

        # Reflect on best answer
        final, final_conf, reflect_meta = reflection_pass(llm_chat_fn, question, best, model)
        meta["reflection"] = reflect_meta
        meta["vote_fraction"] = meta.get("vote_fraction", 0)

        return final, final_conf, meta

    else:
        log.warning(f"Unknown strategy '{strategy}', falling back to simple")
        return enhanced_reasoning(llm_chat_fn, question, model, "simple")


# ============================================================================
# Strategy Selection
# ============================================================================

REASONING_STRATEGIES = {
    "simple": "Single pass - fastest, lowest quality",
    "reflect": "Critique then revise - good for accuracy",
    "vote": "Multi-sample voting - good for consistency",
    "reflect_vote": "Vote + reflection - highest quality, most expensive",
}

def get_strategy_for_task(task_type: str, complexity: str = "medium") -> str:
    """
    Recommend reasoning strategy based on task type and complexity.

    Args:
        task_type: "code", "analysis", "decision", "creative", "simple"
        complexity: "low", "medium", "high"

    Returns:
        Strategy name
    """
    # Simple tasks: no enhancement needed
    if task_type == "simple" or complexity == "low":
        return "simple"

    # Code tasks: reflection helps catch bugs
    if task_type == "code":
        return "reflect" if complexity == "medium" else "reflect_vote"

    # Analysis/decision: voting helps consistency
    if task_type in ("analysis", "decision"):
        return "vote" if complexity == "medium" else "reflect_vote"

    # Creative: simple is usually fine
    if task_type == "creative":
        return "simple"

    # Default: medium complexity gets reflection
    return "reflect" if complexity == "medium" else "vote"