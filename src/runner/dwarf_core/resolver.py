from .line_index import lookup_address
from .models import (
    DwarfAddressLookupRequest,
    DwarfCoreError,
    DwarfLineIndex,
    DwarfLoaderRequest,
    DwarfResolveRequest,
    DwarfResolveResponse,
    DwarfSourceLocation,
    ResolvedVariableAnnotation,
)
from .resolver_models import DwarfResolverInput, DwarfResolverOutput
from .source_parser import extract_source_expressions, normalize_member_access_expression


def resolve_inline_annotations(
    request: DwarfResolveRequest,
    *,
    load,
    liveness_checker=None,
) -> DwarfResolveResponse:
    """Resolve source annotations from DWARF line mapping + expression parsing."""
    if not request.binary_path:
        return DwarfResolveResponse(
            ok=False,
            availability="unavailable",
            dwarf_available=False,
            symbols_available=False,
            error=DwarfCoreError(
                code="missing_binary_path",
                message="binary_path is required",
            ),
        )
    if request.address < 0:
        return DwarfResolveResponse(
            ok=False,
            availability="unavailable",
            dwarf_available=False,
            symbols_available=False,
            error=DwarfCoreError(
                code="invalid_address",
                message="address must be >= 0",
                details={"address": str(request.address)},
            ),
        )

    loader_response = load(DwarfLoaderRequest(binary_path=request.binary_path))
    resolver_input = DwarfResolverInput(
        file_path=request.file_path,
        line=request.line,
        program_counter=request.program_counter or request.address,
        source_line=request.source_line,
        runtime_variables=request.runtime_variables,
    )
    if not loader_response.ok:
        return _unavailable_response(loader_response, resolver_input)

    resolver_output = _resolve_with_loaded_data(
        line_index=loader_response.line_index,
        resolver_input=resolver_input,
        liveness_checker=liveness_checker,
    )
    return DwarfResolveResponse(
        ok=True,
        location=resolver_output.location,
        annotations=resolver_output.annotations,
        availability=resolver_output.availability,
        dwarf_available=resolver_output.dwarf_available,
        symbols_available=resolver_output.symbols_available,
    )


def _resolve_with_loaded_data(
    *,
    line_index: DwarfLineIndex,
    resolver_input: DwarfResolverInput,
    liveness_checker,
) -> DwarfResolverOutput:
    location = _resolve_location(line_index, resolver_input)
    runtime_map = _build_runtime_variable_map(resolver_input.runtime_variables)
    if not runtime_map:
        return DwarfResolverOutput(location=location, annotations=(), symbols_available=False)

    expression_source = resolver_input.source_line
    if not expression_source:
        return DwarfResolverOutput(location=location, annotations=(), symbols_available=True)

    expressions = extract_source_expressions(expression_source)
    if not expressions:
        return DwarfResolverOutput(location=location, annotations=(), symbols_available=True)

    annotations = _build_annotations(
        expressions=tuple(item.text for item in expressions),
        runtime_map=runtime_map,
        program_counter=resolver_input.program_counter,
        liveness_checker=liveness_checker,
    )
    return DwarfResolverOutput(
        location=location,
        annotations=annotations,
        symbols_available=True,
    )


def _resolve_location(line_index: DwarfLineIndex, resolver_input: DwarfResolverInput) -> DwarfSourceLocation | None:
    lookup = lookup_address(
        DwarfAddressLookupRequest(
            line_index=line_index,
            address=resolver_input.program_counter,
        )
    )
    if lookup.ok and lookup.location is not None:
        return lookup.location
    if resolver_input.file_path and resolver_input.line > 0:
        return DwarfSourceLocation(file_path=resolver_input.file_path, line=resolver_input.line)
    return None


def _build_runtime_variable_map(
    runtime_variables: tuple[tuple[str, str], ...],
) -> dict[str, tuple[str, str]]:
    mapped: dict[str, tuple[str, str]] = {}
    for name, value in runtime_variables:
        normalized_name = normalize_member_access_expression(name)
        if not normalized_name:
            continue
        mapped[normalized_name] = (name, value)
    return mapped


def _build_annotations(
    *,
    expressions: tuple[str, ...],
    runtime_map: dict[str, tuple[str, str]],
    program_counter: int,
    liveness_checker,
) -> tuple[ResolvedVariableAnnotation, ...]:
    seen: set[str] = set()
    annotations: list[ResolvedVariableAnnotation] = []
    for expression in expressions:
        normalized_expression = normalize_member_access_expression(expression)
        if not normalized_expression or normalized_expression in seen:
            continue
        seen.add(normalized_expression)

        runtime_entry = runtime_map.get(normalized_expression)
        if runtime_entry is None:
            continue

        runtime_name, runtime_value = runtime_entry
        is_live, liveness_status, liveness_assumed = _check_liveness(
            normalized_expression=normalized_expression,
            program_counter=program_counter,
            liveness_checker=liveness_checker,
        )
        availability = "available" if is_live else "unavailable"
        if liveness_assumed:
            availability = "available"

        annotations.append(
            ResolvedVariableAnnotation(
                name=runtime_name,
                value=runtime_value,
                source_expression=expression,
                is_live=is_live,
                liveness_status=liveness_status,
                liveness_assumed=liveness_assumed,
                availability=availability,
            )
        )

    return tuple(annotations)


def _check_liveness(
    *,
    normalized_expression: str,
    program_counter: int,
    liveness_checker,
) -> tuple[bool, str, bool]:
    if liveness_checker is None:
        return True, "unknown", True

    try:
        live = liveness_checker(normalized_expression, program_counter)
    except Exception:
        return True, "unknown", True

    if live is True:
        return True, "live", False
    if live is False:
        return False, "not_live", False
    return True, "unknown", True


def _unavailable_response(loader_response, resolver_input: DwarfResolverInput) -> DwarfResolveResponse:
    fallback_location = None
    if resolver_input.file_path and resolver_input.line > 0:
        fallback_location = DwarfSourceLocation(
            file_path=resolver_input.file_path,
            line=resolver_input.line,
        )

    return DwarfResolveResponse(
        ok=True,
        location=fallback_location,
        annotations=(),
        availability="unavailable",
        dwarf_available=bool(getattr(loader_response, "dwarf_info_available", False)),
        symbols_available=False,
        error=loader_response.error,
    )
