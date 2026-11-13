"""Parse a graph definition into the IR.

Raises only when the input cannot be turned into IR objects at all (bad YAML,
missing node ids, unreadable subgraph file). Semantic problems — a node without
ports, an edge carrying a port that does not exist — are *not* raised here: they
become error findings, so one run reports every problem instead of the first.
"""

from __future__ import annotations

from pathlib import Path

from . import yamlmini
from .guards import GuardSyntaxError, parse_guard
from .model import (
    CHECKPOINT_NODE,
    CHECKPOINT_POLICIES,
    EFFECT_ORDER,
    KIND_CODE,
    KINDS,
    Edge,
    Graph,
    GraphError,
    Node,
    Port,
    ToolRef,
    TypeRef,
)

_TYPE_ALIASES = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "dict": "object",
    "list": "array",
    "map": "object",
}

_SCALAR_KINDS = frozenset(
    {"string", "number", "integer", "boolean", "any", "unsupported"}
)


def parse_type(spec: object) -> TypeRef:
    """Turn a type spec into a TypeRef.

    A bare string is a scalar type. A mapping may carry `type`, plus
    `properties`/`required` for objects, `items` for arrays, `values` for enums
    and `variants` for unions. Anything unrecognised becomes `unsupported`
    rather than `any`, because "we could not understand this" and "anything
    goes" must not look the same to the checker.
    """
    if isinstance(spec, str):
        name = _TYPE_ALIASES.get(spec.strip(), spec.strip())
        if name in _SCALAR_KINDS:
            return TypeRef(kind=name, raw=spec)
        return TypeRef(kind="unsupported", raw=spec)
    if not isinstance(spec, dict):
        return TypeRef(kind="unsupported", raw=repr(spec))

    raw_name = str(spec.get("type", "")).strip()
    name = _TYPE_ALIASES.get(raw_name, raw_name)

    if name == "object":
        props_raw = spec.get("properties") or {}
        if not isinstance(props_raw, dict):
            return TypeRef(kind="unsupported", raw=raw_name)
        props = tuple(sorted((str(k), parse_type(v)) for k, v in props_raw.items()))
        required_raw = spec.get("required")
        if required_raw is None:
            required = tuple(sorted(props_raw))
        elif isinstance(required_raw, list):
            required = tuple(sorted(str(r) for r in required_raw))
        else:
            return TypeRef(kind="unsupported", raw=raw_name)
        return TypeRef(kind="object", properties=props, required=required, raw=raw_name)

    if name == "array":
        items = spec.get("items")
        return TypeRef(
            kind="array",
            items=parse_type(items) if items is not None else None,
            raw=raw_name,
        )

    if name == "enum":
        values = spec.get("values") or spec.get("enum") or []
        if not isinstance(values, list):
            return TypeRef(kind="unsupported", raw=raw_name)
        return TypeRef(kind="enum", values=tuple(sorted(str(v) for v in values)), raw=raw_name)

    if name == "union":
        variants_raw = spec.get("variants") or spec.get("anyOf") or []
        if not isinstance(variants_raw, list):
            return TypeRef(kind="unsupported", raw=raw_name)
        return TypeRef(
            kind="union",
            variants=tuple(parse_type(v) for v in variants_raw),
            raw=raw_name,
        )

    if name in _SCALAR_KINDS:
        return TypeRef(kind=name, raw=name)

    return TypeRef(kind="unsupported", raw=raw_name or repr(spec))


def _parse_ports(raw: object, node_id: str = "", malformed: list | None = None) -> tuple[Port, ...]:
    """Parse a port map.

    `required` is overloaded by design: at the port level it is a boolean, and
    inside an `object` type it is the list of required property names. Disambiguate
    on the value's type, and strip the port-level keys before handing the rest to
    the type parser — otherwise `{type: object, required: true}` looks like an
    object whose `required` is a boolean and degrades to `unsupported`.

    A port section that is not a mapping is recorded in `malformed` rather than
    returned as "no ports": a node whose contract could not be read is not the same
    as a node that declares none.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        if malformed is not None:
            malformed.append((node_id, f"port section is {type(raw).__name__}, not a mapping"))
        return ()
    ports: list[Port] = []
    for name, spec in raw.items():
        required = True
        constraint = False
        type_spec: object = spec
        if isinstance(spec, dict):
            type_spec = dict(spec)
            port_required = spec.get("required")
            if isinstance(port_required, bool):
                required = port_required
                type_spec.pop("required", None)
            constraint_raw = spec.get("constraint", False)
            if isinstance(constraint_raw, bool):
                constraint = constraint_raw
            else:
                constraint = False
                if malformed is not None:
                    malformed.append(
                        (node_id, f"port {name!r} constraint is not a boolean")
                    )
            type_spec.pop("constraint", None)
        ports.append(
            Port(
                name=str(name),
                type=parse_type(type_spec),
                required=required,
                constraint=constraint,
            )
        )
    return tuple(sorted(ports, key=lambda p: p.name))


def _parse_effects(raw: object) -> tuple[str, ...] | None:
    """Preserve the declared names verbatim; validity is the checker's job.

    Collapsing an unrecognised name to "no declaration" here would erase the
    difference between "wrote something we do not understand" and "wrote
    nothing", and the checker needs that difference to pick its finding code.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, list):
        items = [str(item) for item in raw]
    else:
        return None
    cleaned = tuple(sorted({item.strip() for item in items if item.strip()}))
    return cleaned


def _parse_tools(raw: object) -> tuple[ToolRef, ...]:
    if not isinstance(raw, list):
        return ()
    tools: list[ToolRef] = []
    for item in raw:
        if isinstance(item, str):
            tools.append(ToolRef(name=item))
        elif isinstance(item, dict):
            effects = _parse_effects(item.get("effects")) or ("read",)
            tools.append(ToolRef(name=str(item.get("name", "?")), effects=effects))
    return tuple(tools)


def _parse_node(raw: object, where: str, malformed: list | None = None) -> Node:
    if not isinstance(raw, dict):
        raise GraphError(f"{where}: node entry is not a mapping")
    node_id = raw.get("id")
    if not node_id:
        raise GraphError(f"{where}: node entry without an 'id'")
    kind = str(raw.get("kind", KIND_CODE)).strip()
    checkpoint = str(raw.get("checkpoint", CHECKPOINT_NODE)).strip()
    bound = raw.get("bound")
    cost = raw.get("cost", 1)
    try:
        bound_value = int(bound) if bound is not None else None
    except (TypeError, ValueError) as exc:
        raise GraphError(f"{where}: node {node_id!r} has a non-integer 'bound'") from exc
    try:
        cost_value = int(cost)
    except (TypeError, ValueError) as exc:
        raise GraphError(f"{where}: node {node_id!r} has a non-integer 'cost'") from exc

    idempotent = raw.get("idempotent")
    if idempotent is not None and not isinstance(idempotent, bool):
        # A non-boolean here is almost always an unresolved placeholder. Treat it
        # as "not declared" so the checker reports MISSING_IDEMPOTENCY, which is
        # an actionable finding; raising would turn it into an unreadable file
        # (exit 2) and hide every other problem in the definition.
        idempotent = None

    subgraph = raw.get("subgraph")
    select = raw.get("select")
    when_raw = raw.get("when", raw.get("route"))
    when_guard, when_source, when_error = _parse_guard_value(when_raw)
    return Node(
        id=str(node_id),
        kind=kind if kind in KINDS else kind,  # unknown kinds reported later
        inputs=_parse_ports(raw.get("inputs"), str(node_id), malformed),
        outputs=_parse_ports(raw.get("outputs"), str(node_id), malformed),
        effects=_parse_effects(raw.get("effects")) or (),
        idempotent=idempotent,
        checkpoint=checkpoint if checkpoint in CHECKPOINT_POLICIES else checkpoint,
        bound=bound_value,
        cost=cost_value,
        tools=_parse_tools(raw.get("tools")),
        subgraph=str(subgraph) if subgraph else None,
        description=str(raw.get("description", "")),
        routes=_parse_routes(raw.get("routes")),
        select=str(select).strip() if select is not None else None,
        when=when_guard,
        when_source=when_source,
        when_error=when_error,
    )


def _parse_routes(raw: object) -> tuple[tuple[str, str], ...]:
    """Route declarations, from either a mapping or a list of entries.

    A route that is not a `name: condition` pair is kept as `name: ""` rather
    than dropped: the checker then reports the missing condition, which names
    the route, instead of the node looking as if it never declared it.
    """
    if raw is None:
        return ()
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        for name, condition in raw.items():
            pairs.append((str(name), "" if condition is None else str(condition).strip()))
        return tuple(pairs)
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                name = item.get("name", item.get("route"))
                condition = item.get("condition", item.get("when", ""))
                if name is None:
                    continue
                pairs.append((str(name), "" if condition is None else str(condition).strip()))
            else:
                pairs.append((str(item), ""))
        return tuple(pairs)
    return ((str(raw), ""),)


def _parse_guard_value(raw: object) -> tuple[object, str | None, str | None]:
    """Parse a guard without making the file unreadable when it is malformed.

    Returns `(guard, source, error)`. A malformed guard keeps its source and its
    error so `checks` can report it as a finding with a location; raising here
    would turn one bad condition into exit 2 and hide every other problem.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, None, None
    source = str(raw).strip()
    try:
        return parse_guard(source), source, None
    except GuardSyntaxError as exc:
        return None, source, str(exc)


def _parse_edge(raw: object, where: str) -> Edge:
    if not isinstance(raw, dict):
        raise GraphError(f"{where}: edge entry is not a mapping")
    source = raw.get("from", raw.get("source"))
    target = raw.get("to", raw.get("target"))
    if not source or not target:
        raise GraphError(f"{where}: edge entry needs 'from' and 'to'")
    carries = raw.get("carries") or []
    if isinstance(carries, str):
        carries = [carries]
    when = raw.get("when", raw.get("route"))
    when_guard, when_source, when_error = _parse_guard_value(when)
    return Edge(
        source=str(source),
        target=str(target),
        carries=tuple(sorted(str(c) for c in carries)),
        when=when_guard,
        when_source=when_source,
        when_error=when_error,
    )


def graph_from_dict(data: object, where: str = "<memory>") -> Graph:
    if not isinstance(data, dict):
        raise GraphError(f"{where}: graph definition is not a mapping")

    raw_nodes = data.get("nodes") or []
    if not isinstance(raw_nodes, list):
        raise GraphError(f"{where}: 'nodes' must be a list")
    malformed: list[tuple[str, str]] = []
    nodes = tuple(_parse_node(item, where, malformed) for item in raw_nodes)

    raw_edges = data.get("edges") or []
    if not isinstance(raw_edges, list):
        raise GraphError(f"{where}: 'edges' must be a list")
    edges = tuple(_parse_edge(item, where) for item in raw_edges)

    def _ids(key: str) -> tuple[str, ...]:
        value = data.get(key) or []
        if isinstance(value, str):
            value = [value]
        return tuple(sorted(str(v) for v in value))

    metadata = tuple(
        sorted((str(k), str(v)) for k, v in (data.get("metadata") or {}).items())
    )

    return Graph(
        name=str(data.get("graph", data.get("name", ""))),
        nodes=nodes,
        edges=edges,
        entries=_ids("entries"),
        terminals=_ids("terminals"),
        graph_inputs=_parse_ports(data.get("inputs"), "graph", malformed),
        version=int(data.get("version", 1) or 1),
        metadata=metadata,
        malformed=tuple(malformed),
    )


def load_graph(path: Path, subgraph_root: Path | None = None) -> Graph:
    """Load a definition, resolving one level of subgraph port references.

    A subgraph that cannot be read marks the referencing node rather than
    failing the whole load: the checker then reports `unresolved-subgraph`
    against that node, which is actionable, while a hard raise would hide every
    other problem in the file.
    """
    path = Path(path)
    try:
        data = yamlmini.parse(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GraphError(f"{path}: cannot read: {exc}") from exc
    except yamlmini.YamlError as exc:
        raise GraphError(f"{path}: {exc}") from exc

    graph = graph_from_dict(data, str(path))

    root = subgraph_root or path.parent
    unresolved: list[str] = []
    resolved_nodes: list[Node] = []
    for node in graph.nodes:
        if not node.subgraph:
            resolved_nodes.append(node)
            continue
        candidate = Path(node.subgraph)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            child = load_graph(candidate, candidate.parent)
        except GraphError:
            unresolved.append(node.id)
            resolved_nodes.append(node)
            continue
        # The child's declared inputs become this node's inputs; its terminals'
        # outputs become this node's outputs. Internals stay opaque.
        child_inputs = child.graph_inputs or tuple(
            port
            for entry in child.effective_entries()
            for port in (child.node(entry).inputs if child.node(entry) else ())
        )
        child_outputs = tuple(
            port
            for terminal in child.effective_terminals()
            for port in (child.node(terminal).outputs if child.node(terminal) else ())
        )
        merged_ins = {p.name: p for p in child_inputs}
        merged_ins.update({p.name: p for p in node.inputs})
        merged_outs = {p.name: p for p in child_outputs}
        merged_outs.update({p.name: p for p in node.outputs})
        resolved_nodes.append(
            Node(
                id=node.id,
                kind=node.kind,
                inputs=tuple(sorted(merged_ins.values(), key=lambda p: p.name)),
                outputs=tuple(sorted(merged_outs.values(), key=lambda p: p.name)),
                effects=node.effects,
                idempotent=node.idempotent,
                checkpoint=node.checkpoint,
                bound=node.bound,
                cost=node.cost,
                tools=node.tools,
                subgraph=node.subgraph,
                description=node.description,
            )
        )

    return Graph(
        name=graph.name,
        nodes=tuple(resolved_nodes),
        edges=graph.edges,
        entries=graph.entries,
        terminals=graph.terminals,
        graph_inputs=graph.graph_inputs,
        version=graph.version,
        metadata=graph.metadata,
        unresolved=tuple(unresolved),
        malformed=graph.malformed,
    )


def graph_to_dict(graph: Graph) -> dict:
    """Serialise back to the YAML-facing shape (used for round-trip tests)."""

    def ports(items: tuple[Port, ...]) -> dict:
        out: dict[str, object] = {}
        for port in items:
            spec = type_to_dict(port.type)
            if not port.required:
                spec["required"] = False
            if port.constraint:
                spec["constraint"] = True
            out[port.name] = spec
        return out

    data: dict[str, object] = {"graph": graph.name, "version": graph.version}
    if graph.entries:
        data["entries"] = list(graph.entries)
    if graph.terminals:
        data["terminals"] = list(graph.terminals)
    if graph.graph_inputs:
        data["inputs"] = ports(graph.graph_inputs)
    if graph.metadata:
        data["metadata"] = dict(graph.metadata)
    data["nodes"] = [
        {
            k: v
            for k, v in {
                "id": node.id,
                "kind": node.kind,
                "inputs": ports(node.inputs) or None,
                "outputs": ports(node.outputs) or None,
                "effects": list(node.effects) or None,
                "idempotent": node.idempotent,
                "checkpoint": node.checkpoint,
                "bound": node.bound,
                "cost": node.cost if node.cost != 1 else None,
                "tools": [
                    {"name": t.name, "effects": list(t.effects)} for t in node.tools
                ]
                or None,
                "subgraph": node.subgraph,
                "description": node.description or None,
                "routes": dict(node.routes) or None,
                "select": node.select,
                "when": node.when_source,
            }.items()
            if v is not None
        }
        for node in graph.nodes
    ]
    data["edges"] = [
        {
            k: v
            for k, v in {
                "from": edge.source,
                "to": edge.target,
                "carries": list(edge.carries) or None,
                "when": edge.when_source,
            }.items()
            if v is not None
        }
        for edge in graph.edges
    ]
    return data


def type_to_dict(ref: TypeRef) -> dict:
    if ref.kind == "object":
        return {
            "type": "object",
            "properties": {name: type_to_dict(t) for name, t in ref.properties},
            "required": list(ref.required),
        }
    if ref.kind == "array":
        return {"type": "array", "items": type_to_dict(ref.items) if ref.items else {"type": "any"}}
    if ref.kind == "enum":
        return {"type": "enum", "values": list(ref.values)}
    if ref.kind == "union":
        return {"type": "union", "variants": [type_to_dict(v) for v in ref.variants]}
    return {"type": ref.kind}
