from .models import DwarfCoreError, DwarfLocationList, DwarfLocationListsResponse


def load_location_lists(dwarf_info) -> DwarfLocationListsResponse:
    if dwarf_info is None:
        return DwarfLocationListsResponse(
            ok=False,
            error=DwarfCoreError(
                code="missing_dwarf_info",
                message="DWARF info is unavailable",
            ),
        )

    try:
        raw_location_lists = dwarf_info.location_lists()
    except Exception as error:  # pragma: no cover - defensive for parser differences
        return DwarfLocationListsResponse(
            ok=False,
            error=DwarfCoreError(
                code="location_lists_read_failed",
                message="failed to read DWARF location lists",
                details={"reason": str(error)},
            ),
        )

    if raw_location_lists is None:
        return DwarfLocationListsResponse(
            ok=False,
            error=DwarfCoreError(
                code="location_lists_unavailable",
                message="DWARF location lists are unavailable",
            ),
        )

    return DwarfLocationListsResponse(ok=True, location_lists=())


def is_location_live_at_address(location_list: DwarfLocationList, address: int) -> bool:
    if address < 0:
        return False
    for range_entry in location_list.ranges:
        if range_entry.begin_address <= address < range_entry.end_address:
            return True
    return False
