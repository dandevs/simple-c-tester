from __future__ import annotations

import asyncio

from .expression_tokenizer import extract_expressions
from .debugger import GdbMIController


def _has_side_effects(expression: str) -> bool:
    return "++" in expression or "--" in expression


async def _evaluate_single_expression(
    debugger: GdbMIController, expression: str
) -> str | None:
    if _has_side_effects(expression):
        return None
    try:
        value = await debugger.evaluate_expression(expression, timeout=1.0)
    except Exception:
        return None
    return value


async def resolve_line_annotations(
    line_text: str, line_number: int, debugger: GdbMIController
) -> dict[int, dict[str, str]]:
    """Extract expressions from *line_text* and evaluate them via gdb MI.

    Returns a mapping of {line_number: {expression: value, ...}}.
    Expressions containing ``++`` or ``--`` are skipped for side-effect safety.
    Failed gdb evaluations are silently ignored.
    """
    expressions = extract_expressions(line_text)
    if not expressions:
        return {line_number: {}}

    annotations: dict[str, str] = {}
    seen: set[str] = set()

    for expr in expressions:
        if expr in seen:
            continue
        seen.add(expr)
        value = await _evaluate_single_expression(debugger, expr)
        if value is not None:
            annotations[expr] = value

    return {line_number: annotations}


def resolve_line_annotations_sync(
    line_text: str, line_number: int, debugger: GdbMIController
) -> dict[int, dict[str, str]]:
    """Synchronous wrapper for :func:`resolve_line_annotations`.

    Safe when no event loop is running. If a loop is already active,
    returns an empty annotation dict to avoid blocking the loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(resolve_line_annotations(line_text, line_number, debugger))
    return {line_number: {}}
