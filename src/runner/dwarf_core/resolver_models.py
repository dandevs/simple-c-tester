from dataclasses import dataclass

from .models import DwarfSourceLocation, ResolvedVariableAnnotation


@dataclass(frozen=True)
class DwarfResolverInput:
    """Resolver input contract for source+PC+runtime context."""

    file_path: str = ""
    line: int = 0
    program_counter: int = 0
    source_line: str = ""
    runtime_variables: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class DwarfResolverOutput:
    """Stable resolver output for inline annotations."""

    location: DwarfSourceLocation | None = None
    annotations: tuple[ResolvedVariableAnnotation, ...] = ()
    availability: str = "available"
    dwarf_available: bool = True
    symbols_available: bool = True
