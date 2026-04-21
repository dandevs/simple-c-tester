from bisect import bisect_right

from .models import (
    DwarfAddressLookupRequest,
    DwarfAddressLookupResponse,
    DwarfAddressRange,
    DwarfCompilationUnit,
    DwarfCoreError,
    DwarfLineIndex,
)


def build_line_index(compilation_units: tuple[DwarfCompilationUnit, ...]) -> DwarfLineIndex:
    ranges: list[DwarfAddressRange] = []
    for unit in compilation_units:
        ranges.extend(_build_cu_ranges(unit))

    sorted_ranges = tuple(sorted(ranges, key=lambda item: item.start_address))
    start_addresses = tuple(item.start_address for item in sorted_ranges)
    return DwarfLineIndex(ranges=sorted_ranges, start_addresses=start_addresses)


def lookup_address(request: DwarfAddressLookupRequest) -> DwarfAddressLookupResponse:
    if request.address < 0:
        return DwarfAddressLookupResponse(
            ok=False,
            error=DwarfCoreError(
                code="invalid_address",
                message="address must be >= 0",
                details={"address": str(request.address)},
            ),
        )

    if not request.line_index.ranges:
        return DwarfAddressLookupResponse(
            ok=False,
            error=DwarfCoreError(
                code="line_index_empty",
                message="line index is empty",
            ),
        )

    index = bisect_right(request.line_index.start_addresses, request.address) - 1
    if index < 0:
        return DwarfAddressLookupResponse(
            ok=False,
            error=DwarfCoreError(
                code="address_not_found",
                message="address not found in line index",
                details={"address": hex(request.address)},
            ),
        )

    range_entry = request.line_index.ranges[index]
    if request.address < range_entry.end_address:
        return DwarfAddressLookupResponse(ok=True, location=range_entry.location)

    return DwarfAddressLookupResponse(
        ok=False,
        error=DwarfCoreError(
            code="address_not_found",
            message="address not found in line index",
            details={"address": hex(request.address)},
        ),
    )


def _build_cu_ranges(compilation_unit: DwarfCompilationUnit) -> tuple[DwarfAddressRange, ...]:
    ranges: list[DwarfAddressRange] = []
    previous = None
    for entry in compilation_unit.line_entries:
        if previous is None:
            previous = entry
            if entry.end_sequence:
                previous = None
            continue

        if entry.address > previous.address and not previous.end_sequence:
            ranges.append(
                DwarfAddressRange(
                    start_address=previous.address,
                    end_address=entry.address,
                    location=previous.location,
                    cu_offset=compilation_unit.cu_offset,
                )
            )

        previous = None if entry.end_sequence else entry

    return tuple(ranges)
