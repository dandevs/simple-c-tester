from dataclasses import dataclass


@dataclass(frozen=True)
class SourceExpressionSpan:
    """Half-open [start, end) character span inside a source line."""

    start: int
    end: int


@dataclass(frozen=True)
class SourceExpressionIdentifier:
    """Identifier token extracted from a member-access expression."""

    name: str
    span: SourceExpressionSpan


@dataclass(frozen=True)
class SourceExpressionMatch:
    """Typed parse result for a C identifier/member-access expression."""

    text: str
    normalized_text: str
    span: SourceExpressionSpan
    root_identifier: str
    member_identifiers: tuple[str, ...]
    identifiers: tuple[SourceExpressionIdentifier, ...]
    uses_pointer_access: bool = False
