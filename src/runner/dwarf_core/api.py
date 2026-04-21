from dataclasses import dataclass, field

from .loader import load_dwarf_data
from .models import (
    DwarfCoreError,
    DwarfLoaderRequest,
    DwarfLoaderResponse,
    DwarfResolveRequest,
    DwarfResolveResponse,
    SourceExpressionParseRequest,
    SourceExpressionParseResponse,
)
from .resolver import resolve_inline_annotations
from .source_parser import (
    extract_source_expressions,
    normalize_member_access_expression,
)


@dataclass
class DwarfCoreApi:
    """Typed public API contract for DWARF core operations."""

    cache: dict[str, DwarfLoaderResponse] = field(default_factory=dict)

    def load(self, request: DwarfLoaderRequest) -> DwarfLoaderResponse:
        """Load DWARF units and line index for a binary path."""
        if not request.binary_path:
            return DwarfLoaderResponse(
                ok=False,
                error=DwarfCoreError(
                    code="missing_binary_path",
                    message="binary_path is required",
                ),
            )

        if request.binary_path in self.cache:
            return self.cache[request.binary_path]

        response = load_dwarf_data(request)
        self.cache[request.binary_path] = response
        return response

    def parse_source_expression(
        self,
        request: SourceExpressionParseRequest,
    ) -> SourceExpressionParseResponse:
        """Parse and normalize source expressions from text or line context."""
        has_expression = bool(request.expression and request.expression.strip())
        has_source_line = bool(request.source_line and request.source_line.strip())
        if not has_expression and not has_source_line:
            return SourceExpressionParseResponse(
                ok=False,
                error=DwarfCoreError(
                    code="empty_source_expression",
                    message="expression or source_line is required",
                ),
            )

        if has_source_line:
            parsed_expressions = extract_source_expressions(request.source_line)
            return SourceExpressionParseResponse(
                ok=True,
                normalized_expression="",
                variables=tuple(item.normalized_text for item in parsed_expressions),
                tokens=tuple(
                    identifier.name
                    for item in parsed_expressions
                    for identifier in item.identifiers
                ),
                expressions=parsed_expressions,
            )

        assert request.expression is not None
        normalized = normalize_member_access_expression(request.expression)
        parsed_expression = extract_source_expressions(request.expression)
        variables: tuple[str, ...]
        tokens: tuple[str, ...]
        expressions = parsed_expression
        if parsed_expression:
            variables = tuple(item.normalized_text for item in parsed_expression)
            tokens = tuple(
                identifier.name
                for item in parsed_expression
                for identifier in item.identifiers
            )
        else:
            variables = (normalized,) if normalized else ()
            tokens = ()

        return SourceExpressionParseResponse(
            ok=True,
            normalized_expression=normalized,
            variables=variables,
            tokens=tokens,
            expressions=expressions,
        )

    def resolve(self, request: DwarfResolveRequest) -> DwarfResolveResponse:
        """Resolve location and inline annotations from stop context."""
        return resolve_inline_annotations(request, load=self.load)


def create_dwarf_core_api() -> DwarfCoreApi:
    """Factory for a reusable DWARF core API instance."""
    return DwarfCoreApi()
