import re

from .source_parser_models import (
    SourceExpressionIdentifier,
    SourceExpressionMatch,
    SourceExpressionSpan,
)

_C_KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "default",
    "break", "continue", "return", "goto", "sizeof", "typeof",
    "int", "char", "float", "double", "void", "long", "short",
    "signed", "unsigned", "const", "static", "extern", "inline",
    "struct", "union", "enum", "typedef", "volatile", "register",
    "auto", "restrict", "_Bool", "_Complex", "_Imaginary",
    "NULL", "true", "false",
}

_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_EXPRESSION_RE = re.compile(r"[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*")
_ACCESS_OPERATOR_RE = re.compile(r"\s*(?:->|\.)\s*")


def normalize_member_access_expression(expression: str) -> str:
    """Normalize C member/pointer access to dotted notation."""
    if not expression:
        return ""

    normalized = _ACCESS_OPERATOR_RE.sub(".", expression.strip())
    return normalized


def extract_source_expressions(source_line: str) -> tuple[SourceExpressionMatch, ...]:
    """Extract unique, typed identifier/member-access expressions from a line.

    Deprecated: use expression_tokenizer.extract_expressions() instead.
    Kept for backward compatibility with dwarf_core.api.parse_source_expression().
    """
    if not source_line:
        return ()

    seen: set[str] = set()
    matches: list[SourceExpressionMatch] = []
    for match in _EXPRESSION_RE.finditer(source_line):
        expression = match.group(0)
        normalized = normalize_member_access_expression(expression)
        if not normalized:
            continue

        root_identifier = normalized.split(".", 1)[0]
        if root_identifier in _C_KEYWORDS:
            continue
        if normalized in seen:
            continue

        parsed = parse_source_expression_match(expression, match.start())
        if parsed is None:
            continue

        seen.add(normalized)
        matches.append(parsed)

    return tuple(matches)


def parse_source_expression_match(
    expression: str,
    start_index: int,
) -> SourceExpressionMatch | None:
    """Parse one expression into a typed match with absolute spans."""
    normalized = normalize_member_access_expression(expression)
    if not normalized:
        return None

    identifier_matches = tuple(_IDENTIFIER_RE.finditer(expression))
    if not identifier_matches:
        return None

    identifiers: list[SourceExpressionIdentifier] = []
    for id_match in identifier_matches:
        identifier_span = SourceExpressionSpan(
            start=start_index + id_match.start(),
            end=start_index + id_match.end(),
        )
        identifiers.append(
            SourceExpressionIdentifier(name=id_match.group(0), span=identifier_span)
        )

    names = tuple(item.name for item in identifiers)
    uses_pointer_access = "->" in expression

    return SourceExpressionMatch(
        text=expression,
        normalized_text=normalized,
        span=SourceExpressionSpan(start=start_index, end=start_index + len(expression)),
        root_identifier=names[0],
        member_identifiers=names[1:],
        identifiers=tuple(identifiers),
        uses_pointer_access=uses_pointer_access,
    )
