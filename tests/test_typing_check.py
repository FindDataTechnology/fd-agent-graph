"""Tests for the port-type assignability checker.

Every TypeRef is built directly: these tests pin the checker's contract, not
the loader's parsing behaviour.
"""

import unittest

from fd_agent_graph import codes
from fd_agent_graph.model import Edge, Graph, Node, Port, TypeRef
from fd_agent_graph.typing_check import assignable, port_findings


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def scalar(kind: str) -> TypeRef:
    return TypeRef(kind=kind)


STRING = scalar("string")
NUMBER = scalar("number")
INTEGER = scalar("integer")
BOOLEAN = scalar("boolean")
ANY = scalar("any")
UNSUPPORTED = TypeRef(kind="unsupported", raw="mystery")


def obj(properties: dict, required=None) -> TypeRef:
    names = required if required is not None else tuple(properties)
    return TypeRef(
        kind="object",
        properties=tuple(sorted(properties.items())),
        required=tuple(sorted(names)),
    )


def array(items: TypeRef | None) -> TypeRef:
    return TypeRef(kind="array", items=items)


def enum(*values: str) -> TypeRef:
    return TypeRef(kind="enum", values=tuple(values))


def union(*variants: TypeRef) -> TypeRef:
    return TypeRef(kind="union", variants=tuple(variants))


def port(name: str, type_ref: TypeRef) -> Port:
    return Port(name=name, type=type_ref)


def node(node_id: str, inputs=(), outputs=()) -> Node:
    return Node(id=node_id, inputs=tuple(inputs), outputs=tuple(outputs))


def graph_of(*nodes: Node, edges=()) -> Graph:
    return Graph(name="g", nodes=tuple(nodes), edges=tuple(edges))


def one_edge(payload_out: TypeRef, payload_in: TypeRef, carries=("payload",)) -> Graph:
    return graph_of(
        node("a", outputs=(port("payload", payload_out),)),
        node("b", inputs=(port("payload", payload_in),)),
        edges=(Edge(source="a", target="b", carries=tuple(carries)),),
    )


class ScalarAssignabilityTests(unittest.TestCase):
    def test_integer_to_number_is_compatible(self):
        ok, reason = assignable(INTEGER, NUMBER)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_number_to_integer_is_incompatible_and_names_narrowing(self):
        ok, reason = assignable(NUMBER, INTEGER)
        self.assertFalse(ok)
        self.assertIn("narrow", reason.lower())
        self.assertIn("number", reason)
        self.assertIn("integer", reason)

    def test_identical_scalars_are_compatible(self):
        for kind in ("string", "number", "integer", "boolean"):
            with self.subTest(kind=kind):
                ok, reason = assignable(scalar(kind), scalar(kind))
                self.assertTrue(ok)
                self.assertEqual(reason, "")

    def test_scalars_accept_only_themselves(self):
        cases = [
            (STRING, NUMBER),
            (BOOLEAN, STRING),
            (INTEGER, BOOLEAN),
            (BOOLEAN, INTEGER),
        ]
        for producer, consumer in cases:
            with self.subTest(producer=producer.kind, consumer=consumer.kind):
                ok, reason = assignable(producer, consumer)
                self.assertFalse(ok)
                self.assertIn(producer.kind, reason)
                self.assertIn(consumer.kind, reason)

    def test_any_consumer_accepts_anything(self):
        for producer in (STRING, NUMBER, INTEGER, BOOLEAN, obj({"id": STRING})):
            with self.subTest(producer=producer.describe()):
                self.assertEqual(assignable(producer, ANY), (True, ""))

    def test_any_producer_is_not_compatible_with_a_concrete_type(self):
        ok, reason = assignable(ANY, STRING)
        self.assertFalse(ok)
        self.assertIn("any", reason)


class ObjectAssignabilityTests(unittest.TestCase):
    def test_wide_producer_satisfies_narrow_consumer(self):
        wide = obj({"id": STRING, "tenant": STRING})
        narrow = obj({"id": STRING})
        self.assertEqual(assignable(wide, narrow), (True, ""))

    def test_narrow_producer_cannot_satisfy_wider_requirement(self):
        ok, reason = assignable(obj({"id": STRING}), obj({"id": STRING, "tenant": STRING}))
        self.assertFalse(ok)
        self.assertIn("tenant", reason)

    def test_optional_producer_property_is_ignored_when_absent(self):
        consumer = obj({"id": STRING, "count": NUMBER}, required=("id",))
        self.assertEqual(assignable(obj({"id": STRING}), consumer), (True, ""))

    def test_optional_consumer_property_is_checked_when_present(self):
        consumer = obj({"id": STRING, "count": NUMBER}, required=("id",))
        producer = obj({"id": STRING, "count": STRING})
        ok, reason = assignable(producer, consumer)
        self.assertFalse(ok)
        self.assertIn("count", reason)

    def test_property_types_must_match(self):
        ok, reason = assignable(obj({"id": NUMBER}), obj({"id": STRING}))
        self.assertFalse(ok)
        self.assertIn("id", reason)

    def test_extra_producer_properties_are_ignored(self):
        producer = obj({"id": STRING, "a": NUMBER, "b": BOOLEAN})
        self.assertEqual(assignable(producer, obj({"id": STRING})), (True, ""))

    def test_nested_missing_property_is_located(self):
        producer = obj({"profile": obj({"id": STRING})})
        consumer = obj({"profile": obj({"id": STRING, "tenant": STRING})})
        ok, reason = assignable(producer, consumer)
        self.assertFalse(ok)
        self.assertIn("tenant", reason)


class UnionAssignabilityTests(unittest.TestCase):
    def test_producer_union_assignable_to_wider_consumer_union(self):
        producer = union(STRING, NUMBER)
        consumer = union(STRING, NUMBER, BOOLEAN)
        self.assertEqual(assignable(producer, consumer), (True, ""))

    def test_producer_union_not_assignable_to_narrower_consumer_union(self):
        producer = union(STRING, NUMBER, BOOLEAN)
        consumer = union(STRING, NUMBER)
        ok, reason = assignable(producer, consumer)
        self.assertFalse(ok)
        self.assertIn("boolean", reason)

    def test_union_producer_requires_every_variant(self):
        ok, reason = assignable(union(STRING, NUMBER), STRING)
        self.assertFalse(ok)
        self.assertIn("number", reason)

    def test_non_union_producer_must_match_one_variant(self):
        self.assertEqual(assignable(STRING, union(NUMBER, STRING)), (True, ""))
        ok, reason = assignable(STRING, union(NUMBER, BOOLEAN))
        self.assertFalse(ok)
        self.assertIn("string", reason)

    def test_union_consumer_any_variant_accepts_unknown(self):
        self.assertEqual(assignable(ANY, union(STRING, ANY)), (True, ""))

    def test_union_producer_with_any_is_not_a_free_pass(self):
        ok, _ = assignable(union(STRING, ANY), NUMBER)
        self.assertFalse(ok)


class ArrayEnumUnsupportedTests(unittest.TestCase):
    def test_consumer_without_items_accepts_any_array(self):
        self.assertEqual(assignable(array(STRING), array(None)), (True, ""))

    def test_array_item_types_must_be_assignable(self):
        self.assertEqual(assignable(array(STRING), array(STRING)), (True, ""))
        ok, reason = assignable(array(STRING), array(NUMBER))
        self.assertFalse(ok)
        self.assertIn("string", reason)
        self.assertIn("number", reason)

    def test_producer_array_without_items_is_not_narrowed_silently(self):
        ok, reason = assignable(array(None), array(NUMBER))
        self.assertFalse(ok)
        self.assertIn("unknown", reason)

    def test_string_to_enum_is_incompatible(self):
        ok, reason = assignable(STRING, enum("a", "b"))
        self.assertFalse(ok)
        self.assertIn("string", reason)

    def test_enum_subset_is_compatible(self):
        self.assertEqual(assignable(enum("a"), enum("a", "b")), (True, ""))

    def test_enum_superset_is_incompatible(self):
        ok, reason = assignable(enum("a", "b"), enum("a"))
        self.assertFalse(ok)
        self.assertIn("'b'", reason)

    def test_enum_to_string_follows_the_catch_all_rule(self):
        # The contract enumerates enum -> enum only and says anything not
        # covered is incompatible, so widening to plain string is not allowed.
        ok, reason = assignable(enum("a"), STRING)
        self.assertFalse(ok)
        self.assertIn("string", reason)

    def test_unsupported_producer_is_never_compatible(self):
        for consumer in (STRING, ANY, obj({"id": STRING}), union(STRING, NUMBER)):
            with self.subTest(consumer=consumer.describe()):
                ok, reason = assignable(UNSUPPORTED, consumer)
                self.assertFalse(ok)
                self.assertIn("unsupported", reason)

    def test_unsupported_consumer_is_never_compatible(self):
        ok, reason = assignable(STRING, UNSUPPORTED)
        self.assertFalse(ok)
        self.assertIn("unsupported", reason)

    def test_unsupported_consumer_beats_a_concrete_producer(self):
        ok, _ = assignable(STRING, TypeRef(kind="unsupported", raw="?"))
        self.assertFalse(ok)


class PortFindingsTests(unittest.TestCase):
    def test_missing_required_property_is_reported_with_a_concrete_fix(self):
        graph = one_edge(obj({"id": STRING}), obj({"id": STRING, "tenant": STRING}))
        findings = port_findings(graph)
        self.assertEqual(len(findings), 1)
        item = findings[0]
        self.assertEqual(item.code, codes.MISSING_REQUIRED_PROPERTY)
        self.assertEqual(item.severity, "error")
        self.assertEqual(item.location, "a->b:payload")
        self.assertIn("tenant", item.message)
        self.assertIn("tenant", item.fix)

    def test_scalar_mismatch_is_reported_as_type_incompatible(self):
        findings = port_findings(one_edge(STRING, NUMBER))
        self.assertEqual(len(findings), 1)
        item = findings[0]
        self.assertEqual(item.code, codes.TYPE_INCOMPATIBLE)
        self.assertEqual(item.location, "a->b:payload")
        self.assertIn("string", item.message)
        self.assertIn("number", item.message)
        self.assertTrue(item.fix)

    def test_number_to_integer_fix_mentions_the_direction(self):
        findings = port_findings(one_edge(NUMBER, INTEGER))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, codes.TYPE_INCOMPATIBLE)
        self.assertIn("integer is assignable to number but not the reverse", findings[0].fix)

    def test_unsupported_port_is_a_warning(self):
        for producer, consumer in ((UNSUPPORTED, STRING), (STRING, UNSUPPORTED)):
            with self.subTest(producer=producer.kind, consumer=consumer.kind):
                findings = port_findings(one_edge(producer, consumer))
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0].code, codes.UNSUPPORTED_TYPE)
                self.assertEqual(findings[0].severity, "warning")
                self.assertEqual(findings[0].severity, codes.severity(codes.UNSUPPORTED_TYPE))
                self.assertTrue(findings[0].fix)

    def test_missing_carried_port_emits_nothing(self):
        # Output missing on the source side.
        graph = graph_of(
            node("a", outputs=(port("other", STRING),)),
            node("b", inputs=(port("payload", NUMBER),)),
            edges=(Edge(source="a", target="b", carries=("payload",)),),
        )
        self.assertEqual(port_findings(graph), [])

        # Input missing on the target side.
        graph = graph_of(
            node("a", outputs=(port("payload", STRING),)),
            node("b", inputs=(port("other", NUMBER),)),
            edges=(Edge(source="a", target="b", carries=("payload",)),),
        )
        self.assertEqual(port_findings(graph), [])

    def test_unknown_endpoint_emits_nothing(self):
        graph = graph_of(
            node("b", inputs=(port("payload", NUMBER),)),
            edges=(Edge(source="ghost", target="b", carries=("payload",)),),
        )
        self.assertEqual(port_findings(graph), [])

    def test_compatible_ports_emit_nothing(self):
        self.assertEqual(port_findings(one_edge(INTEGER, NUMBER)), [])
        self.assertEqual(port_findings(one_edge(STRING, ANY)), [])
        self.assertEqual(port_findings(one_edge(obj({"id": STRING, "n": NUMBER}), obj({"id": STRING}))), [])

    def test_edges_without_carries_emit_nothing(self):
        self.assertEqual(port_findings(one_edge(STRING, NUMBER, carries=())), [])

    def test_findings_are_sorted_and_well_formed(self):
        graph = graph_of(
            node("a", outputs=(port("payload", STRING),)),
            node("b", inputs=(port("payload", NUMBER),)),
            node("c", inputs=(port("payload", STRING),)),
            node("d", inputs=(port("payload", obj({"id": STRING, "tenant": STRING})),)),
            edges=(
                Edge(source="a", target="d", carries=("payload",)),
                Edge(source="a", target="b", carries=("payload",)),
                Edge(source="a", target="c", carries=("payload",)),
            ),
        )
        # `a` has one output type, so the c edge is fine and only b/d report.
        findings = port_findings(graph)
        self.assertEqual([f.location for f in findings], ["a->b:payload", "a->d:payload"])
        self.assertEqual(
            findings,
            sorted(findings, key=lambda f: (f.location, f.code)),
        )
        for item in findings:
            self.assertTrue(item.fix, f"{item.code} without a fix")
            self.assertIn(item.code, codes.SEVERITIES)
            self.assertEqual(item.severity, codes.severity(item.code))

    def test_same_location_different_codes_sort_deterministically(self):
        # Two edges with identical visible location, guaranteed distinct codes,
        # to pin the (location, code) ordering tiebreak.
        graph = graph_of(
            node("a", outputs=(port("payload", UNSUPPORTED),)),
            node("b", inputs=(port("payload", STRING),)),
            edges=(
                Edge(source="a", target="b", carries=("payload",)),
                Edge(source="a", target="b", carries=("payload",)),
            ),
        )
        findings = port_findings(graph)
        self.assertEqual(len(findings), 2)
        self.assertEqual(
            findings,
            sorted(findings, key=lambda f: (f.location, f.code)),
        )


if __name__ == "__main__":
    unittest.main()
