"""Assignability of port types, and the findings that follow from it.

An edge names the ports it carries. For each carried port the source node's
*output* type (the producer) must be usable by the target node's *input* type
(the consumer). This module answers that one question and turns a "no" into a
finding with a concrete fix.

The checker is conservative on purpose. A type it cannot understand is never
reported as compatible: pretending an unknown type matches would let a real
wiring error pass the gate silently.
"""

from __future__ import annotations

from typing import NamedTuple

from . import codes
from .model import ANY, Finding, Graph, TypeRef, finding

#: Kinds whose values are interchangeable exactly when the kinds are equal.
_SCALAR_KINDS = frozenset({"string", "number", "integer", "boolean"})

#: Codes this module may emit. A verdict from a nested check can only ever be
#: one of these three, so anything else would be an internal bug.
_EMITTABLE = frozenset(
    {codes.TYPE_INCOMPATIBLE, codes.UNSUPPORTED_TYPE, codes.MISSING_REQUIRED_PROPERTY}
)

_SUPPORTED_HINT = (
    "use a type from the supported subset "
    "(string, number, integer, boolean, object, array, enum, union, any)"
)


class _Verdict(NamedTuple):
    """Outcome of one assignability check.

    `code` is the finding code a failure should be reported under; `missing`
    names the property (dot-joined for nested objects) when the failure is a
    missing required property, so callers can write a concrete fix.
    """

    ok: bool
    reason: str = ""
    code: str | None = None
    missing: str | None = None


def _ok() -> _Verdict:
    return _Verdict(True)


def _incompatible(reason: str) -> _Verdict:
    return _Verdict(False, reason, codes.TYPE_INCOMPATIBLE)


def _missing_property(name: str, reason: str) -> _Verdict:
    return _Verdict(False, reason, codes.MISSING_REQUIRED_PROPERTY, name)


def _unsupported(reason: str) -> _Verdict:
    return _Verdict(False, reason, codes.UNSUPPORTED_TYPE)


def _unsupported_reason(ref: TypeRef, role: str) -> str:
    if ref.raw:
        return (
            f"{role} type {ref.raw!r} is unsupported "
            "(outside the supported subset)"
        )
    return f"{role} type is unsupported (outside the supported subset)"


def _check(producer: TypeRef, consumer: TypeRef) -> _Verdict:
    """Core assignability decision, carrying the finding code on failure.

    Order matters: an unsupported type wins over `any` (the contract is
    explicit that an unsupported type is never compatible, even opposite
    `any`); the producer-union branch comes before the consumer-union branch
    so that union-to-union requires every producer variant to land in some
    consumer variant; and the producer-`any` verdict comes after the
    consumer-union branch, so a union that contains `any` still accepts an
    unknown value.
    """
    if producer.kind == "unsupported" or consumer.kind == "unsupported":
        parts: list[str] = []
        if producer.kind == "unsupported":
            parts.append(_unsupported_reason(producer, "producer"))
        if consumer.kind == "unsupported":
            parts.append(_unsupported_reason(consumer, "consumer"))
        return _unsupported(
            "; ".join(parts)
            + "; the checker cannot decide assignability, so it does not assume compatibility"
        )

    if consumer.kind == "any":
        return _ok()

    if producer.kind == "union":
        # A union producer has no value outside its variants, so every variant
        # must be acceptable. An empty union denotes "no value" and is
        # therefore vacuously assignable.
        for variant in producer.variants:
            verdict = _check(variant, consumer)
            if not verdict.ok:
                return _Verdict(
                    False,
                    f"union variant {variant.describe()} is not assignable to "
                    f"{consumer.describe()}: {verdict.reason}",
                    verdict.code,
                    verdict.missing,
                )
        return _ok()

    if consumer.kind == "union":
        reasons: list[str] = []
        for variant in consumer.variants:
            verdict = _check(producer, variant)
            if verdict.ok:
                return _ok()
            reasons.append(f"{variant.describe()}: {verdict.reason}")
        detail = "; ".join(reasons) if reasons else "the union has no variants"
        return _incompatible(
            f"{producer.describe()} is not assignable to any variant of "
            f"{consumer.describe()} ({detail})"
        )

    if producer.kind == "any":
        # Reached only after the consumer-union branch: a union that includes
        # `any` is effectively `any`, so it accepts an unknown value. A
        # concrete consumer still must not be handed one.
        return _incompatible(
            f"'any' is not assignable to {consumer.describe()}: "
            "an unknown value cannot be assumed to satisfy a concrete type"
        )

    if producer.kind == "object" and consumer.kind == "object":
        return _check_object(producer, consumer)

    if producer.kind == "array" and consumer.kind == "array":
        return _check_array(producer, consumer)

    if producer.kind == "enum" and consumer.kind == "enum":
        return _check_enum(producer, consumer)

    if producer.kind == consumer.kind and producer.kind in _SCALAR_KINDS:
        return _ok()

    if producer.kind == "integer" and consumer.kind == "number":
        return _ok()

    if producer.kind == "number" and consumer.kind == "integer":
        return _incompatible(
            "number is not assignable to integer: narrowing would lose fractional "
            "values (integer is assignable to number, not the reverse)"
        )

    return _incompatible(
        f"{producer.describe()} is not assignable to {consumer.describe()}"
    )


def _wrap_property(name: str, verdict: _Verdict) -> _Verdict:
    missing = f"{name}.{verdict.missing}" if verdict.missing else None
    return _Verdict(
        False,
        f"property '{name}': {verdict.reason}",
        verdict.code or codes.TYPE_INCOMPATIBLE,
        missing,
    )


def _check_object(producer: TypeRef, consumer: TypeRef) -> _Verdict:
    """Width subtyping: the consumer's requirements must be a subset.

    Extra producer properties are ignored, which is the point of the model: a
    downstream step may legitimately read only part of an upstream payload.
    """
    declared = dict(consumer.properties)

    for name in sorted(consumer.required):
        consumer_prop = declared.get(name, ANY)
        producer_prop = producer.prop(name)
        if producer_prop is None:
            return _missing_property(
                name, f"missing required property '{name}'"
            )
        verdict = _check(producer_prop, consumer_prop)
        if not verdict.ok:
            return _wrap_property(name, verdict)

    # A consumer property that is present but optional still has to line up
    # when the producer actually supplies it.
    for name in sorted(declared):
        if name in consumer.required:
            continue
        producer_prop = producer.prop(name)
        if producer_prop is None:
            continue
        verdict = _check(producer_prop, declared[name])
        if not verdict.ok:
            return _wrap_property(name, verdict)

    return _ok()


def _check_array(producer: TypeRef, consumer: TypeRef) -> _Verdict:
    if consumer.items is None:
        return _ok()
    if producer.items is None:
        return _incompatible(
            f"array without an element type is not assignable to "
            f"{consumer.describe()}: the element type is unknown"
        )
    verdict = _check(producer.items, consumer.items)
    if verdict.ok:
        return _ok()
    return _Verdict(
        False,
        f"array element type: {verdict.reason}",
        verdict.code or codes.TYPE_INCOMPATIBLE,
        verdict.missing,
    )


def _check_enum(producer: TypeRef, consumer: TypeRef) -> _Verdict:
    allowed = set(consumer.values)
    extra = [value for value in producer.values if value not in allowed]
    if extra:
        listed = ", ".join(repr(value) for value in extra)
        return _incompatible(
            f"enum value(s) {listed} are not members of {consumer.describe()}"
        )
    return _ok()


def assignable(producer: TypeRef, consumer: TypeRef) -> tuple[bool, str]:
    """(True, "") when a value of `producer` may be fed to a port typed `consumer`.

    Second element is a human-readable reason when False, e.g.
    "string is not assignable to number" or "missing required property 'tenant'".
    """
    verdict = _check(producer, consumer)
    return (True, "") if verdict.ok else (False, verdict.reason)


def _fix_for(
    code: str,
    verdict: _Verdict,
    edge_source: str,
    edge_target: str,
    port_name: str,
    producer_type: TypeRef,
    consumer_type: TypeRef,
) -> str:
    if code == codes.UNSUPPORTED_TYPE:
        return (
            "the checker cannot reason about this type; "
            f"declare the port with a supported kind: {_SUPPORTED_HINT}"
        )
    if code == codes.MISSING_REQUIRED_PROPERTY:
        prop = verdict.missing or "?"
        return (
            f"producer must also output '{prop}': add it to the '{port_name}' "
            f"output of {edge_source}"
        )
    if producer_type.kind == "number" and consumer_type.kind == "integer":
        return "align the port types; integer is assignable to number but not the reverse"
    return (
        f"align the port types: make {edge_source} output '{port_name}' as "
        f"{consumer_type.describe()} or make {edge_target} accept "
        f"{producer_type.describe()} on input '{port_name}'"
    )


def port_findings(graph: Graph) -> list[Finding]:
    """Report every carried port whose producer type cannot satisfy its consumer.

    Ports that do not exist on one side are skipped: that is a structural
    problem owned by `graph.edge.unknown-port`, and duplicating it here would
    produce two findings for one mistake.
    """
    findings: list[Finding] = []
    for edge in graph.edges:
        source = graph.node(edge.source)
        target = graph.node(edge.target)
        if source is None or target is None:
            # Unknown nodes are the structural checker's finding, not ours.
            continue
        for name in edge.carries:
            out_port = source.output(name)
            in_port = target.input(name)
            if out_port is None or in_port is None:
                continue

            verdict = _check(out_port.type, in_port.type)
            if verdict.ok:
                continue

            code = verdict.code if verdict.code in _EMITTABLE else codes.TYPE_INCOMPATIBLE
            location = f"{edge.source}->{edge.target}:{name}"
            findings.append(
                finding(
                    code,
                    location,
                    f"port '{name}' from {edge.source} to {edge.target}: {verdict.reason}",
                    _fix_for(
                        code,
                        verdict,
                        edge.source,
                        edge.target,
                        name,
                        out_port.type,
                        in_port.type,
                    ),
                    severity=codes.severity(code),
                )
            )

    return sorted(findings, key=lambda item: (item.location, item.code))
