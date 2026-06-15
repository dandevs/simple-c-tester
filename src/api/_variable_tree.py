"""Async gdb traversal that builds a ``VarTreeNode`` tree for the variable
tree view.

Given a live :class:`GdbMIController` and a C expression (typically a
variable name), this module recursively walks the data structure via
``var_create`` / ``var_list_children`` / ``var_delete``, classifying each
field as either a *scalar* (shown inside the node box) or a *child* (a tree
edge leading to a sub-node).

Cycle detection uses pointer-address tracking so circular linked lists and
graphs are rendered with a ``(cycle)`` marker instead of looping forever.
A hard node-count cap (``MAX_NODES``) prevents pathological runaway.

All public functions are ``async`` and are pure w.r.t. their arguments —
the only side effect is gdb MI traffic via ``controller``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.debugger import GdbMIController

MAX_NODES = 500


@dataclass
class VarField:
    """A scalar field displayed inside a node box (e.g. ``val = 42``)."""

    name: str
    value: str
    type_hint: str


@dataclass
class VarTreeNode:
    """One node in the visualised data-structure tree."""

    name: str
    value: str
    type_hint: str
    fields: list[VarField] = field(default_factory=list)
    children: list["VarTreeNode"] = field(default_factory=list)
    is_cycle: bool = False
    is_null: bool = False
    address: str = ""


# Pointer values that represent NULL and should not be expanded.
_NULL_VALUES = {"0x0", "(nil)", "nullptr", "NULL", "null", ""}


def _is_pointer_type(type_hint: str) -> bool:
    return type_hint.strip().endswith("*")


def _looks_pointer_address(value: str) -> bool:
    v = value.strip()
    if v in _NULL_VALUES:
        return False
    return v.startswith("0x")


async def build_variable_tree(
    controller: GdbMIController,
    expression: str,
    max_nodes: int = MAX_NODES,
) -> VarTreeNode | None:
    """Build a :class:`VarTreeNode` tree rooted at *expression*.

    Returns ``None`` if gdb cannot create a variable object for the
    expression.  Cycles are detected by pointer address and rendered as
    leaf nodes with ``is_cycle=True``.
    """
    created = await controller.var_create(expression, frame="*", timeout=2.0)
    if created is None:
        return None

    var_name = str(created.get("name", ""))
    value = str(created.get("value", "?"))
    type_hint = str(created.get("type", ""))
    numchild = int(created.get("numchild", 0))

    # Can't build a tree from a NULL or unresolved root.
    stripped = value.strip()
    if _is_pointer_type(type_hint) and stripped in _NULL_VALUES:
        return None
    if stripped == "?":
        return None

    visited: set[str] = set()
    node_count = [0]

    try:
        return await _build_node(
            controller,
            var_name,
            expression,
            value,
            type_hint,
            numchild,
            visited,
            node_count,
            max_nodes,
        )
    finally:
        await controller.var_delete(var_name, timeout=1.0)


async def _build_node(
    controller: GdbMIController,
    var_name: str,
    display_name: str,
    value: str,
    type_hint: str,
    numchild: int,
    visited: set[str],
    node_count: list[int],
    max_nodes: int,
) -> VarTreeNode:
    """Classify the gdb variable object's children into fields vs. edges.

    A single ``var_create`` / ``var_delete`` pair brackets the whole tree
    because ``var_list_children`` returns child var-objects that persist
    until the root is deleted.
    """
    node_count[0] += 1
    if node_count[0] > max_nodes:
        return VarTreeNode(name=display_name, value="...", type_hint="")

    address = value.strip() if _looks_pointer_address(value) else ""

    # NULL pointer — leaf node, no expansion.
    if _is_pointer_type(type_hint) and value.strip() in _NULL_VALUES:
        return VarTreeNode(
            name=display_name,
            value=value,
            type_hint=type_hint,
            is_null=True,
        )

    # Cycle detection — we've already visited this address.
    if address and address in visited:
        return VarTreeNode(
            name=display_name,
            value=value,
            type_hint=type_hint,
            is_cycle=True,
            address=address,
        )
    if address:
        visited.add(address)

    node = VarTreeNode(
        name=display_name,
        value=value,
        type_hint=type_hint,
        address=address,
    )

    if numchild <= 0:
        return node

    children_data = await controller.var_list_children(var_name, timeout=1.5)
    if not children_data:
        return node

    for child in children_data:
        child_var_name = str(child.get("name", ""))
        child_exp = str(child.get("exp", ""))
        child_value = str(child.get("value", "?"))
        child_type = str(child.get("type", ""))
        child_numchild = int(child.get("numchild", 0))

        if not child_exp:
            continue

        # Resolve lazy values.
        if child_value in {"?", "", "{...}"}:
            evaluated = await controller.var_evaluate(child_var_name, timeout=1.0)
            if evaluated is not None:
                child_value = evaluated
        if child_value in {"", "{...}"}:
            child_value = "?"

        # Skip NULL/unresolved pointer children — they add visual noise
        # without structural value.
        if child_numchild > 0:
            child_stripped = child_value.strip()
            if _is_pointer_type(child_type) and child_stripped in _NULL_VALUES:
                continue
            if child_stripped == "?":
                continue

        # Scalar fields (numchild == 0) are shown inside the node box.
        # Pointer/aggregate fields (numchild > 0) become tree edges.
        if child_numchild <= 0:
            node.fields.append(
                VarField(name=child_exp, value=child_value, type_hint=child_type)
            )
        else:
            child_display = f"{display_name}.{child_exp}"
            child_node = await _build_node(
                controller,
                child_var_name,
                child_display,
                child_value,
                child_type,
                child_numchild,
                visited,
                node_count,
                max_nodes,
            )
            if child_node is not None:
                node.children.append(child_node)

    return node


# ---------------------------------------------------------------------------
# Build from pre-captured variables (no live gdb needed)
# ---------------------------------------------------------------------------


def build_tree_from_captured(
    variables: list[tuple[str, str, str]],
    root_name: str,
) -> VarTreeNode | None:
    """Build a :class:`VarTreeNode` tree from pre-captured dotted-path variables.

    Used when no live gdb controller is available (auto-story mode).  The
    ``variables`` list comes from ``TimelineEvent.variables`` — flattened
    ``(dotted_name, value, type_hint)`` tuples produced by
    ``_capture_scope_variables`` during the trace.

    Paths are split on ``"."`` to reconstruct the tree hierarchy.  A path
    that has longer paths extending it becomes a tree edge (child node);
    a path with no extensions is a scalar field displayed inside the box.
    NULL/``"?"`` children are skipped.
    """
    path_map: dict[tuple[str, ...], tuple[str, str]] = {}
    for var_tuple in variables:
        if len(var_tuple) >= 3:
            name, value, type_hint = var_tuple
        elif len(var_tuple) == 2:
            name, value = var_tuple
            type_hint = ""
        else:
            continue
        parts = tuple(p for p in name.split(".") if p)
        if not parts or parts[0] != root_name:
            continue
        path_map[parts] = (value, type_hint)

    if not path_map:
        return None

    all_paths = frozenset(path_map)

    # A path is "internal" if any other path extends it.
    internal: set[tuple[str, ...]] = set()
    for path in all_paths:
        n = len(path)
        for other in all_paths:
            if len(other) > n and other[:n] == path:
                internal.add(path)
                break

    def _build(path: tuple[str, ...], display: str) -> VarTreeNode:
        value, type_hint = path_map.get(path, ("?", ""))
        node = VarTreeNode(name=display, value=value, type_hint=type_hint)

        n = len(path)
        for child_path in sorted(all_paths):
            if len(child_path) != n + 1 or child_path[:n] != path:
                continue
            seg = child_path[n]
            child_val, child_type = path_map.get(child_path, ("?", ""))
            child_display = f"{display}.{seg}"

            if child_path in internal:
                # Skip NULL/unresolved children
                if _is_pointer_type(child_type) and child_val.strip() in _NULL_VALUES:
                    continue
                if child_val.strip() == "?":
                    continue
                node.children.append(_build(child_path, child_display))
            else:
                node.fields.append(
                    VarField(name=seg, value=child_val, type_hint=child_type)
                )
        return node

    return _build((root_name,), root_name)


__all__ = [
    "VarField",
    "VarTreeNode",
    "build_variable_tree",
    "build_tree_from_captured",
    "MAX_NODES",
]
