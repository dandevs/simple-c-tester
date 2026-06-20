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
    # gdb expression for this field (e.g. ``head->data``). Populated during
    # build and the inlining pass so any field line can be expanded on its own.
    expr: str = ""


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
    # gdb expression that produced this node (e.g. ``head->next``). Lets the UI
    # re-expand any single node as an artificial array via ``*(expr)@N``.
    expr: str = ""
    # True when this node is a scalar pointer expanded into array elements.
    # The compaction pass keeps such nodes as their own boxes (not inlined).
    is_expanded_array: bool = False
    # True when this node is a pointer to a struct/union (a graph edge).
    # Set during ``_build_node`` so the compaction pass can keep it as a
    # box even when the type name is a typedef (e.g. ``t_ast *``) that
    # ``_points_to_aggregate`` cannot recognise from the name alone.
    is_graph_edge: bool = False


# Pointer values that represent NULL and should not be expanded.
_NULL_VALUES = {"0x0", "(nil)", "nullptr", "NULL", "null", ""}


def _is_pointer_type(type_hint: str) -> bool:
    return type_hint.strip().endswith("*")


def _looks_pointer_address(value: str) -> bool:
    v = value.strip()
    if v in _NULL_VALUES:
        return False
    return v.startswith("0x")


def _safe_base(expr: str) -> str:
    """Parenthesise *expr* before an index operator if it needs grouping.

    Indexing binds tighter than dereference in C, so ``*p[i]`` parses as
    ``*(p[i])``. For our array elements we want ``(*p)[i]``, hence the wrap
    when *expr* is not a bare token. Member access (``.`` / ``->``) binds tight
    enough that no wrapping is needed there.
    """
    e = expr.strip()
    if not e:
        return e
    if e.startswith("(") and e.endswith(")"):
        return e
    if e.startswith("*") or "@" in e:
        return f"({e})"
    return e


def _child_expr(parent_expr: str, parent_type: str, child_exp: str) -> str:
    """Reconstruct a child's gdb expression from its parent's.

    Pointer parents dereference via ``->`` (gdb varobj auto-derefs pointer
    children); by-value aggregates use ``.``; numeric ``child_exp`` is an
    array index.
    """
    idx = child_exp.strip().strip("[]")
    if idx.lstrip("-").isdigit():
        return f"{_safe_base(parent_expr)}[{idx}]"
    if parent_type.strip().endswith("*"):
        return f"{parent_expr}->{child_exp}"
    return f"{parent_expr}.{child_exp}"


async def build_variable_tree(
    controller: GdbMIController,
    expression: str,
    max_nodes: int = MAX_NODES,
) -> VarTreeNode | None:
    """Build a :class:`VarTreeNode` tree rooted at *expression*.

    Returns ``None`` if gdb cannot create a variable object for the
    expression.  Cycles are detected by pointer address and rendered as
    leaf nodes with ``is_cycle=True``.

    Pointer/array expansion is left entirely to the user (hover a pointer
    line in the tree view and press ``a``); nothing is auto-expanded here.
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
        built = await _build_node(
            controller,
            var_name,
            expression,
            value,
            type_hint,
            numchild,
            visited,
            node_count,
            max_nodes,
            expr=expression,
        )
    finally:
        await controller.var_delete(var_name, timeout=1.0)
    return _compact_node(built) if built is not None else None


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
    expr: str = "",
) -> VarTreeNode:
    """Classify the gdb variable object's children into fields vs. edges.

    A single ``var_create`` / ``var_delete`` pair brackets the whole tree
    because ``var_list_children`` returns child var-objects that persist
    until the root is deleted.  *expr* is the gdb expression for this node,
    threaded through so any node can later be re-expanded as an array.
    """
    node_count[0] += 1
    if node_count[0] > max_nodes:
        return VarTreeNode(name=display_name, value="...", type_hint="", expr=expr)

    address = value.strip() if _looks_pointer_address(value) else ""

    # NULL pointer — leaf node, no expansion.
    if _is_pointer_type(type_hint) and value.strip() in _NULL_VALUES:
        return VarTreeNode(
            name=display_name,
            value=value,
            type_hint=type_hint,
            is_null=True,
            expr=expr,
        )

    # Cycle detection — we've already visited this address.
    if address and address in visited:
        return VarTreeNode(
            name=display_name,
            value=value,
            type_hint=type_hint,
            is_cycle=True,
            address=address,
            expr=expr,
        )
    if address:
        visited.add(address)

    node = VarTreeNode(
        name=display_name,
        value=value,
        type_hint=type_hint,
        address=address,
        expr=expr,
        # A pointer whose target has multiple fields is a graph edge.
        # Covers both explicit ``struct Foo *`` and typedef'd types like
        # ``t_ast *`` that ``_points_to_aggregate`` cannot detect by name.
        is_graph_edge=(
            _points_to_aggregate(type_hint)
            or (_is_pointer_type(type_hint) and numchild > 1)
        ),
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

        child_expr = _child_expr(expr, type_hint, child_exp)
        child_display = f"{display_name}.{child_exp}"

        # Resolve lazy values.
        if child_value in {"?", "", "{...}"}:
            evaluated = await controller.var_evaluate(child_var_name, timeout=1.0)
            if evaluated is not None:
                child_value = evaluated
        # Only "" is truly unresolved. "{...}" is the legitimate value gdb
        # returns for compound children (struct/union/array); preserving it
        # keeps their node boxes readable instead of degrading to "?".
        if child_value == "":
            child_value = "?"

        # Skip NULL/unresolved POINTER children only — they add visual noise
        # without structural value. Aggregate children (struct/union/array)
        # are always expandable via their own children even when their scalar
        # value is "{...}", so they must never be skipped here (this is what
        # previously hid union data from the tree).
        if child_numchild > 0:
            child_stripped = child_value.strip()
            if _is_pointer_type(child_type) and (
                child_stripped in _NULL_VALUES or child_stripped == "?"
            ):
                continue

        # Classify the child as a tree edge (sub-box) or a scalar field.
        if child_numchild <= 0:
            # gdb reports no children for this varobj.  Some gdb builds do
            # not auto-dereference struct/union pointers — including
            # typedef'd types like ``t_ast *`` — leaving numchild at 0.
            # Force a dereference via ``*(expr)`` for any non-NULL pointer;
            # the deref'd numchild tells us whether the target is an
            # aggregate (struct → numchild > 0) or a scalar (int*/char*
            # → numchild == 0).  Same dereferencing the manual ``a``
            # expansion does, but done automatically during tree building.
            if (
                _is_pointer_type(child_type)
                and child_value.strip() not in _NULL_VALUES
                and child_value.strip() != "?"
            ):
                deref_created = await controller.var_create(
                    f"*({child_expr})", frame="*", timeout=2.0
                )
                if deref_created is not None:
                    deref_name = str(deref_created.get("name", ""))
                    deref_numchild = int(deref_created.get("numchild", 0))
                    try:
                        if deref_numchild > 0:
                            deref_node = await _build_node(
                                controller,
                                deref_name,
                                child_display,
                                child_value,
                                child_type,
                                deref_numchild,
                                visited,
                                node_count,
                                max_nodes,
                                expr=child_expr,
                            )
                            if deref_node is not None:
                                node.children.append(deref_node)
                                continue
                    finally:
                        await controller.var_delete(deref_name, timeout=1.0)
            # Not dereferenceable — treat as a scalar field.
            node.fields.append(
                VarField(
                    name=child_exp,
                    value=child_value,
                    type_hint=child_type,
                    expr=child_expr,
                )
            )
        else:
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
                expr=child_expr,
            )
            if child_node is not None:
                node.children.append(child_node)

    return node


# ---------------------------------------------------------------------------
# Compaction pass — inline non-graph content as dotted fields
# ---------------------------------------------------------------------------
#
# Only **graph nodes** (pointers to a struct/union, and by-value aggregates
# that contain one) are drawn as their own boxes. Everything else — scalars,
# scalar pointers (char *, char **, …), arrays, and pure-data aggregates such
# as a union whose members only hold scalars — is flattened into the nearest
# graph-node ancestor as dotted-name fields. This keeps the diagram focused on
# the data-structure graph instead of drawing a box for every char * deref.


def _points_to_aggregate(type_hint: str) -> bool:
    """True for pointers whose target is a struct/union (a graph edge)."""
    t = type_hint.strip()
    if not t.endswith("*"):
        return False
    target = t[:-1].strip()
    for qualifier in ("const ", "volatile ", "restrict "):
        if target.startswith(qualifier):
            target = target[len(qualifier):].strip()
    return (
        target.startswith("struct")
        or target.startswith("union")
        or target.endswith("{...}")
    )


def _has_graph_node(node: VarTreeNode) -> bool:
    """True if *node* or any descendant is drawn as its own box.

    A node's own ``struct s_node *`` type qualifies it as a box even when its
    pointer children are all NULL (a leaf in the graph), so leaves are never
    collapsed away.  A scalar pointer expanded into an array
    (``is_expanded_array``) is also kept as a box so its element fields stay
    visible and the node stays hoverable for re-expansion.

    The ``is_graph_edge`` flag covers typedef'd pointer types (e.g.
    ``t_ast *``) that ``_points_to_aggregate`` cannot recognise by name
    alone — it is set during ``_build_node`` when the pointer's target is
    confirmed to have multiple fields.
    """
    if node.is_expanded_array or node.is_graph_edge or _points_to_aggregate(node.type_hint):
        return True
    return any(_has_graph_node(child) for child in node.children)


def _last_segment(name: str) -> str:
    """Final dotted-path component (e.g. ``n.u_data.cmd`` → ``cmd``)."""
    return name.rsplit(".", 1)[-1] if "." in name else name


def _join_path(prefix: str, segment: str) -> str:
    """Join path segments; array-index segments like ``[0]`` attach without a dot."""
    if not segment:
        return prefix
    if segment.startswith("["):
        return f"{prefix}{segment}"
    return f"{prefix}.{segment}"


def _marked_value(node: VarTreeNode) -> str:
    """Value with a trailing (cycle)/(null) marker when relevant."""
    value = node.value or ""
    if node.is_cycle:
        return f"{value} (cycle)".strip()
    if node.is_null:
        return f"{value} (null)".strip()
    return value


def _inline_subtree(node: VarTreeNode, prefix: str) -> list[VarField]:
    """Flatten a graph-node-free subtree into dotted-name fields.

    Scalar pointers collapse to a single field showing their address (gdb
    already includes the string preview for ``char *``); deref levels are not
    chased. By-value aggregates emit their members with growing dotted
    prefixes. Only ever called on subtrees with no pointer-to-aggregate.

    Each field carries the gdb ``expr`` of the variable it represents, so a
    hovered field line can be expanded on its own.
    """
    if _is_pointer_type(node.type_hint):
        return [VarField(
            name=prefix, value=_marked_value(node),
            type_hint=node.type_hint, expr=node.expr,
        )]

    fields: list[VarField] = []
    for f in node.fields:
        fields.append(VarField(
            name=_join_path(prefix, f.name), value=f.value,
            type_hint=f.type_hint, expr=f.expr,
        ))
    for child in node.children:
        child_prefix = _join_path(prefix, _last_segment(child.name))
        fields.extend(_inline_subtree(child, child_prefix))
    # Empty / unresolved aggregate — keep it visible as a single field.
    if not fields:
        fields.append(VarField(
            name=prefix, value=_marked_value(node),
            type_hint=node.type_hint, expr=node.expr,
        ))
    return fields


def _compact_node(node: VarTreeNode) -> VarTreeNode:
    """Return a copy with non-graph children inlined as dotted fields.

    Children of an expanded array are always kept as their own boxes: an
    array slot is a meaningful position the user asked to see, so even a
    struct element with no live graph edge (e.g. a trailing ``next = NULL``)
    must not be flattened into the array node's field list.
    """
    fields = list(node.fields)
    children: list[VarTreeNode] = []
    keep_all_children = node.is_expanded_array
    for child in node.children:
        if keep_all_children or _has_graph_node(child):
            children.append(_compact_node(child))
        else:
            fields.extend(_inline_subtree(child, _last_segment(child.name)))
    return VarTreeNode(
        name=node.name,
        value=node.value,
        type_hint=node.type_hint,
        fields=fields,
        children=children,
        is_cycle=node.is_cycle,
        is_null=node.is_null,
        address=node.address,
        expr=node.expr,
        is_expanded_array=node.is_expanded_array,
        is_graph_edge=node.is_graph_edge,
    )


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
                # Skip NULL/unresolved POINTER children only. Aggregate
                # children (struct/union/array) are expandable even when
                # their captured value degraded to "?" (a compound value),
                # so skipping them here would hide union data from the tree.
                if _is_pointer_type(child_type) and (
                    child_val.strip() in _NULL_VALUES or child_val.strip() == "?"
                ):
                    continue
                node.children.append(_build(child_path, child_display))
            else:
                node.fields.append(
                    VarField(name=seg, value=child_val, type_hint=child_type)
                )

        # Set is_graph_edge after children are known so the compaction
        # pass keeps typedef'd struct/union pointers (e.g. ``t_ast *``)
        # as boxes when they have captured sub-fields.
        node.is_graph_edge = (
            _points_to_aggregate(type_hint)
            or (_is_pointer_type(type_hint) and len(node.children) > 1)
        )
        return node

    built = _build((root_name,), root_name)
    return _compact_node(built) if built is not None else None


# ---------------------------------------------------------------------------
# Array expansion — turn a pointer into a visible element array on demand
# ---------------------------------------------------------------------------
#
# A bare scalar pointer (``int *``, ``char *``, ...) carries no length info in
# its type, so gdb's varobj reports ``numchild`` of 0 (or 1 for the single
# dereferenced element). To show ``arr[0] .. arr[N-1]`` we tell gdb to treat
# the memory as an artificial array via the ``@`` operator: ``*(expr)@N``.
#
# Expansion is entirely user-driven: in the tree view you hover a pointer line
# and press ``a``, then type the element count. :func:`build_array_subtree`
# rebuilds that one variable as ``*(expr)@count`` and the result splices back
# into the existing tree. Nothing is auto-expanded at build time.

# Default count pre-filled in the expand prompt (the user can change it).
DEFAULT_EXPAND_COUNT = 8


async def _build_array_node(
    controller: GdbMIController,
    base_expr: str,
    base_display: str,
    base_value: str,
    base_type: str,
    count: int,
    visited: set[str],
    node_count: list[int],
    max_nodes: int,
) -> VarTreeNode | None:
    """Build an ``is_expanded_array`` node for *base_expr* as *count* elements.

    Creates a throwaway ``*(base_expr)@count`` varobj, lists its element
    children, and classifies each as a field (scalar) or a recursed sub-node.
    The returned node keeps *base_value* / *base_type* (the pointer's own
    identity) so it splices cleanly into an existing tree.
    """
    safe = max(1, min(count, max_nodes))
    arr_expr = f"*({base_expr})@{safe}"
    created = await controller.var_create(arr_expr, frame="*", timeout=2.0)
    if created is None:
        return None
    arr_var = str(created.get("name", ""))
    try:
        children_data = await controller.var_list_children(arr_var, timeout=1.5)
        # NOTE: the element loop below may recurse via _build_node, which calls
        # var_list_children on the element varobjs — so the array varobj (and
        # its children) must stay alive until the loop finishes. Deletion is
        # deferred to the finally block.
        node_count[0] += 1
        node = VarTreeNode(
            name=base_display,
            value=base_value,
            type_hint=base_type,
            address=base_value.strip() if _looks_pointer_address(base_value) else "",
            expr=base_expr,
            is_expanded_array=True,
        )
        if children_data:
            await _populate_array_elements(
                controller, node, children_data, base_expr, base_display,
                visited, node_count, max_nodes,
            )
        return node
    finally:
        await controller.var_delete(arr_var, timeout=1.0)


async def _populate_array_elements(
    controller: GdbMIController,
    node: VarTreeNode,
    children_data: list[dict],
    base_expr: str,
    base_display: str,
    visited: set[str],
    node_count: list[int],
    max_nodes: int,
) -> None:
    """Classify each artificial-array element as a field or recursed sub-node."""
    for child in children_data:
        idx = str(child.get("exp", ""))
        child_var_name = str(child.get("name", ""))
        child_value = str(child.get("value", "?"))
        child_type = str(child.get("type", ""))
        child_numchild = int(child.get("numchild", 0))
        if not idx:
            continue

        if child_value in {"?", "", "{...}"}:
            evaluated = await controller.var_evaluate(child_var_name, timeout=1.0)
            if evaluated is not None:
                child_value = evaluated
        if child_value == "":
            child_value = "?"

        element_expr = f"{_safe_base(base_expr)}[{idx}]"
        element_display = f"{base_display}[{idx}]"
        if child_numchild <= 0:
            node.fields.append(
                VarField(
                    name=f"[{idx}]", value=child_value,
                    type_hint=child_type, expr=element_expr,
                )
            )
        else:
            element_node = await _build_node(
                controller,
                child_var_name,
                element_display,
                child_value,
                child_type,
                child_numchild,
                visited,
                node_count,
                max_nodes,
                expr=element_expr,
            )
            if element_node is not None:
                node.children.append(element_node)


async def build_array_subtree(
    controller: GdbMIController,
    base_expr: str,
    base_display: str,
    base_value: str,
    base_type: str,
    count: int,
    max_nodes: int = MAX_NODES,
) -> VarTreeNode | None:
    """Rebuild a single variable as an artificial array of *count* elements.

    Used by the per-variable ``a`` action.  Returns a fresh
    :class:`VarTreeNode` with ``is_expanded_array=True``, keeping the
    pointer's *base_value* / *base_type* so it splices into the existing tree
    at the node named *base_display*.  Returns ``None`` if gdb rejects the
    ``*(base_expr)@count`` expression.

    The result is run through the compaction pass so an expanded array shows
    scalar pointers inlined as fields and struct/union edges as sub-boxes.
    """
    visited: set[str] = set()
    node_count = [0]
    built = await _build_array_node(
        controller,
        base_expr,
        base_display,
        base_value,
        base_type,
        count,
        visited,
        node_count,
        max_nodes,
    )
    return _compact_node(built) if built is not None else None


__all__ = [
    "VarField",
    "VarTreeNode",
    "build_variable_tree",
    "build_array_subtree",
    "build_tree_from_captured",
    "MAX_NODES",
    "DEFAULT_EXPAND_COUNT",
]
