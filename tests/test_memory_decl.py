"""Tests for the four-axis memory declaration (stdlib unittest).

Every negative case asserts the SPECIFIC code and the offending path — "returns
not-ok" is the assertion that lets a wrong rejection pass as a right one.
"""

from __future__ import annotations

import unittest

from fd_agent_graph.binding import GraphError, roster_from_dict
from fd_agent_graph.memory_decl import (
    BUDGET_MAX,
    BUDGET_MIN,
    PLATFORM_DEFAULTS,
    normalize,
    resolve_effective,
    unexecuted,
)


def codes(result: dict) -> set[str]:
    return {e["code"] for e in result["errors"]}


def paths(result: dict) -> set[str]:
    return {e["path"] for e in result["errors"]}


class NormalizeAcceptsTests(unittest.TestCase):
    def test_all_four_axes_accepted(self) -> None:
        result = normalize(
            {
                "store": {"home": "member"},
                "read": {"scope": "shared", "budget": BUDGET_MAX},
                "write": {"authority": "private"},
                "process": {"distill": "off", "promote_after": 5, "cold_after_days": 90},
            }
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["declaration"]["store"]["home"], "member")
        self.assertEqual(result["declaration"]["read"]["budget"], BUDGET_MAX)
        self.assertEqual(result["declaration"]["write"]["authority"], "private")
        self.assertEqual(result["declaration"]["process"]["cold_after_days"], 90)

    def test_omitted_axes_are_omitted_not_defaulted(self) -> None:
        """Omission is what lets the next level inherit; filling it here would
        make an agent silently override its team."""
        result = normalize({"read": {"scope": "off"}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(set(result["declaration"]), {"read"})

    def test_none_is_an_empty_declaration(self) -> None:
        self.assertEqual(normalize(None), {"ok": True, "declaration": {}})

    def test_every_axis_value_is_accepted(self) -> None:
        for home in ("fleet", "member", "person"):
            with self.subTest(home=home):
                self.assertTrue(normalize({"store": {"home": home}})["ok"])
        # `own` is absent on purpose: no consumer executes it literally.
        for scope in ("shared", "own+shared", "off"):
            with self.subTest(scope=scope):
                self.assertTrue(normalize({"read": {"scope": scope}})["ok"])
        for authority in ("private", "shared"):
            with self.subTest(authority=authority):
                self.assertTrue(normalize({"write": {"authority": authority}})["ok"])
        for distill in ("on", "off", "inherit"):
            with self.subTest(distill=distill):
                self.assertTrue(normalize({"process": {"distill": distill}})["ok"])


class NormalizeRejectsTests(unittest.TestCase):
    def test_bare_scalar_is_ambiguous_and_names_the_axes(self) -> None:
        for value in ("shared", "private", "own+shared", "off"):
            with self.subTest(value=value):
                result = normalize(value)
                self.assertEqual(codes(result), {"memory_ambiguous_scalar"})
                message = result["errors"][0]["message"]
                # The point of the error is that it does NOT pick an axis.
                self.assertIn("write.authority", message)
                self.assertIn("read.scope", message)

    def test_unknown_axis_is_named(self) -> None:
        result = normalize({"memory": {"store": {"home": "fleet"}}})
        self.assertEqual(codes(result), {"memory_axis_unknown"})
        self.assertEqual(paths(result), {"memory.memory"})

    def test_misspelled_subkey_is_named_with_known_keys(self) -> None:
        result = normalize({"process": {"distil": "off"}})
        self.assertEqual(codes(result), {"memory_key_unknown"})
        self.assertEqual(paths(result), {"memory.process.distil"})
        self.assertIn("distill", result["errors"][0]["message"])

    def test_address_shaped_home_is_refused_with_its_own_code(self) -> None:
        for value in ("d-mypack", "u-alice/p/proj", "/var/lib/fd-memory", "sqlite:///x", "d/pack/member"):
            with self.subTest(value=value):
                result = normalize({"store": {"home": value}})
                self.assertEqual(codes(result), {"store_home_addressed"})

    def test_plain_wrong_home_is_invalid_not_addressed(self) -> None:
        result = normalize({"store": {"home": "team"}})
        self.assertEqual(codes(result), {"store_home_invalid"})

    def test_budget_outside_bounds_is_rejected_not_clamped(self) -> None:
        for value in (BUDGET_MIN - 1, BUDGET_MAX + 1, 0, -5):
            with self.subTest(value=value):
                result = normalize({"read": {"budget": value}})
                self.assertEqual(codes(result), {"read_budget_invalid"})
                self.assertIn("refused rather than clamped", result["errors"][0]["message"])

    def test_budget_must_be_an_integer(self) -> None:
        for value in ("1200", 1200.5, True):
            with self.subTest(value=value):
                self.assertEqual(codes(normalize({"read": {"budget": value}})), {"read_budget_invalid"})

    def test_authority_rejects_location_values(self) -> None:
        """The exact conflation this module exists to stop."""
        for value in ("user", "project", "local"):
            with self.subTest(value=value):
                result = normalize({"write": {"authority": value}})
                self.assertEqual(codes(result), {"write_authority_invalid"})
                self.assertIn("not a storage location", result["errors"][0]["message"])

    def test_own_is_refused_as_unexecutable_not_unknown(self) -> None:
        """The value is meaningful; it just has no consumer that would honour it.

        yongyi-client branches on `scope === "own+shared"` alone, so `own` would
        skip the own probe and still read shared — reading more than it says.
        """
        result = normalize({"read": {"scope": "own"}})
        self.assertEqual(codes(result), {"read_scope_unexecutable"})
        message = result["errors"][0]["message"]
        self.assertIn("no consumer", message)
        self.assertIn("own+shared", message)

    def test_read_scope_rejects_authority_values(self) -> None:
        for value in ("private", "public"):
            with self.subTest(value=value):
                self.assertEqual(codes(normalize({"read": {"scope": value}})), {"read_scope_invalid"})

    def test_process_bounds(self) -> None:
        self.assertEqual(
            codes(normalize({"process": {"promote_after": 0}})), {"process_promote_after_invalid"}
        )
        self.assertEqual(
            codes(normalize({"process": {"cold_after_days": -1}})),
            {"process_cold_after_days_invalid"},
        )


class ResolveTests(unittest.TestCase):
    def rows(self, *levels):
        return resolve_effective(list(levels))

    def test_child_overrides_parent(self) -> None:
        rows = self.rows(
            ("agent", {"process": {"distill": "off"}}),
            ("team", {"process": {"distill": "on"}}),
        )
        self.assertEqual(rows["process.distill"]["effective"], "off")
        self.assertEqual(rows["process.distill"]["inherited_from"], "agent")
        self.assertEqual(rows["process.distill"]["declared"], "off")

    def test_inherit_takes_the_parent_value(self) -> None:
        rows = self.rows(
            ("agent", {"process": {"distill": "inherit"}}),
            ("team", {"process": {"distill": "on"}}),
        )
        self.assertEqual(rows["process.distill"]["declared"], "inherit")
        self.assertEqual(rows["process.distill"]["effective"], "on")
        self.assertEqual(rows["process.distill"]["inherited_from"], "team")

    def test_nothing_declared_anywhere_lands_on_platform(self) -> None:
        rows = self.rows(("agent", {}), ("team", {}))
        for sub in ("store.home", "read.scope", "read.budget", "process.distill"):
            with self.subTest(sub=sub):
                self.assertEqual(rows[sub]["inherited_from"], "platform")
                self.assertIsNone(rows[sub]["declared"])
        self.assertEqual(rows["read.scope"]["effective"], PLATFORM_DEFAULTS["read"]["scope"])

    def test_authority_has_no_platform_default(self) -> None:
        """Nobody can answer "whose conclusion counts" on an agent's behalf."""
        rows = self.rows(("agent", {"memory": {}}))
        self.assertIsNone(rows["write.authority"]["effective"])
        self.assertIsNone(rows["write.authority"]["inherited_from"])

    def test_declared_but_unexecuted_subkeys_are_reported(self) -> None:
        rows = self.rows(
            ("agent", {}),
            ("team", {"process": {"cold_after_days": 90, "promote_after": 5}}),
        )
        for sub, expected in (("process.cold_after_days", 90), ("process.promote_after", 5)):
            with self.subTest(sub=sub):
                row = rows[sub]
                self.assertEqual(row["effective"], expected)
                self.assertEqual(row["inherited_from"], "team")
                self.assertFalse(row["executed"])
        self.assertEqual(
            unexecuted(rows), ["process.promote_after", "process.cold_after_days"]
        )

    def test_only_distill_has_a_consumer_today(self) -> None:
        rows = self.rows(("agent", {}))
        self.assertTrue(rows["process.distill"]["executed"])
        # No per-page evidence counter exists to threshold against, and no eviction
        # pass exists. Reporting these as executed would be the defect this change
        # removes, so they are declared-and-resolved but explicitly unexecuted.
        self.assertFalse(rows["process.promote_after"]["executed"])
        self.assertFalse(rows["process.cold_after_days"]["executed"])

    def test_author_side_axes_have_no_consumer_and_say_so(self) -> None:
        """The three author-side axes are recorded, not executed (measured 2026-11-13).

        An earlier version of ``EXECUTED_SUBKEYS`` listed them on the belief that the
        platform's injection ladder and host reconciliation read them. The audit found
        no such consumer: ``memoryNamespaceFor`` ignores ``store.home``, the ladder
        reads ``fleets.knowledge_profile`` rather than ``manifest.memory.read``, and
        there is no runtime gate for ``write.authority``. Marking them executed made
        the read face claim a policy was in force; this test pins the honest answer so
        the claim cannot creep back without a consumer.
        """
        rows = self.rows(
            (
                "agent",
                {
                    "store": {"home": "fleet"},
                    "read": {"scope": "off", "budget": 800},
                    "write": {"authority": "shared"},
                },
            )
        )
        for sub in ("store.home", "read.scope", "read.budget", "write.authority"):
            with self.subTest(sub=sub):
                self.assertFalse(rows[sub]["executed"], sub)
        self.assertEqual(
            unexecuted(rows),
            ["store.home", "read.scope", "read.budget", "write.authority"],
        )
        # …while the declared values are still reported faithfully.
        self.assertEqual(rows["store.home"]["declared"], "fleet")
        self.assertEqual(rows["read.scope"]["effective"], "off")
        self.assertTrue(rows["process.distill"]["executed"])

    def test_row_order_is_stable(self) -> None:
        # Row order is a contract: the read face renders rows in this sequence,
        # so an operator's muscle memory ("schema is last") depends on it. The
        # schema axis was appended rather than inserted, so every pre-existing
        # row keeps its position and the new ones land at the end.
        rows = self.rows(("agent", {}))
        self.assertEqual(
            list(rows),
            [
                "store.home",
                "read.scope",
                "read.budget",
                "write.authority",
                "process.distill",
                "process.promote_after",
                "process.cold_after_days",
                "schema.key_by",
                "schema.domain",
            ],
        )


class RosterWiringTests(unittest.TestCase):
    def test_memory_scope_is_the_write_axis_shorthand(self) -> None:
        roster = roster_from_dict({"roster": "r", "agents": [{"id": "a", "memory_scope": "shared"}]})
        self.assertEqual(roster.agents[0].memory["write"]["authority"], "shared")

    def test_write_authority_declaration_sets_the_scope(self) -> None:
        roster = roster_from_dict(
            {"roster": "r", "agents": [{"id": "a", "memory": {"write": {"authority": "private"}}}]}
        )
        self.assertEqual(roster.agents[0].memory_scope, "private")

    def test_conflicting_spellings_are_rejected(self) -> None:
        with self.assertRaises(GraphError) as ctx:
            roster_from_dict(
                {
                    "roster": "r",
                    "agents": [
                        {
                            "id": "a",
                            "memory_scope": "private",
                            "memory": {"write": {"authority": "shared"}},
                        }
                    ],
                }
            )
        message = str(ctx.exception)
        self.assertIn("private", message)
        self.assertIn("shared", message)

    def test_roster_level_memory_is_the_team_layer(self) -> None:
        roster = roster_from_dict(
            {
                "roster": "r",
                "memory": {"process": {"distill": "off"}},
                "agents": [{"id": "a", "memory_scope": "private"}],
            }
        )
        self.assertEqual(roster.memory["process"]["distill"], "off")
        rows = resolve_effective([("agent", roster.agents[0].memory), ("team", roster.memory)])
        self.assertEqual(rows["process.distill"]["effective"], "off")
        self.assertEqual(rows["process.distill"]["inherited_from"], "team")

    def test_invalid_declaration_names_the_field_path(self) -> None:
        with self.assertRaises(GraphError) as ctx:
            roster_from_dict(
                {"roster": "r", "agents": [{"id": "a", "memory": {"store": {"home": "d-p"}}}]}
            )
        message = str(ctx.exception)
        self.assertIn("a", message)
        self.assertIn("store.home", message)
        self.assertIn("store_home_addressed", message)


if __name__ == "__main__":
    unittest.main()
