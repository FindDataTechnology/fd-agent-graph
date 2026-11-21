"""Memory declaration: four axes, one resolver, zero IO.

The declaration answers four different questions that used to share one name:

* ``store``   —  who shares this memory store (``fleet`` / ``member`` / ``person``)
* ``read``    —  which partitions feed recall, and how big its budget is
* ``write``   —  whose conclusion is authoritative for others (``private`` / ``shared``)
* ``process`` —  distill / promote / cold-store handling

Keeping them apart is the whole point. The measured failure that motivated this
module: the roster's ``memory_scope`` (write authority) was written into the
host's ``memory`` frontmatter key, whose value domain is a storage location
(``user|project|local``). The host rejected every such definition, and nothing
noticed because the two axes had the same name.

Two rules this module enforces that a plain schema would not:

* **``store.home`` is an enum, never an address.** A pack is a distributable
  artifact; letting it name a root key or a path would let it write into someone
  else's store. Resolution from ``home`` to a namespace belongs to identity, so
  anything resembling an address is refused here with its own code.
* **``executed`` is reported per sub-key.** A sub-key can be declared, resolve to
  a value, and still have no consumer in this version. ``cold_after_days`` is
  exactly that today. Silently reporting it as "in force" is how a declaration
  becomes decoration.

Nothing here reads a file, a database, or the environment: the same normalize
must serve offline roster validation and the running policy face, and IO would
make the two disagree whenever storage hiccups.
"""

from __future__ import annotations

import re
from typing import Any

from .binding import MEMORY_SCOPES

# ── Vocabulary ───────────────────────────────────────────────────────────────

STORE_HOMES = ("fleet", "member", "person")

SCOPE_SHARED = "shared"
SCOPE_BOTH = "own+shared"
SCOPE_OFF = "off"
#: `own` (own-partition only) is deliberately absent. yongyi-client's readDigest
#: branches on `scope === "own+shared"` alone, so a declared `own` would skip the
#: own probe and still read shared — reading MORE than it says, which is worse
#: than a loud refusal. A value earns its place in this tuple by having a consumer
#: that executes it literally, the same rule the process sub-keys follow.
READ_SCOPES = (SCOPE_SHARED, SCOPE_BOTH, SCOPE_OFF)

DISTILL_ON = "on"
DISTILL_OFF = "off"
DISTILL_INHERIT = "inherit"
DISTILL_VALUES = (DISTILL_ON, DISTILL_OFF, DISTILL_INHERIT)

#: Same bounds as the platform's knowledge profile: the floor keeps a degenerate
#: 0-token config from starving recall into noise, the ceiling is the memory
#: service's own digest clamp. A profile that passes here never needs re-clamping.
BUDGET_MIN = 64
BUDGET_MAX = 2000

AXES = ("store", "read", "write", "process", "schema")

AXIS_KEYS: dict[str, tuple[str, ...]] = {
    "store": ("home",),
    "read": ("scope", "budget"),
    "write": ("authority",),
    "process": ("distill", "promote_after", "cold_after_days"),
    # The fifth axis: which key groups same-subject memories. Separate from
    # `process` (lifecycle) and from `read` (visibility + budget) because it
    # answers a different question, and the spec forbids one axis standing in
    # for another. `key_by` must name a key from fd-memory's closed domain
    # vocabulary; `domain` is a label for the reader.
    "schema": ("key_by", "domain"),
}

#: Where an undeclared axis lands when nothing in the inheritance chain sets it.
PLATFORM_DEFAULTS: dict[str, dict[str, Any]] = {
    "store": {"home": "fleet"},
    "read": {"scope": SCOPE_BOTH, "budget": 1200},
    # No platform default for authority: "whose conclusion counts" is not a
    # question a platform can answer on an agent's behalf.
    "write": {},
    "process": {"distill": DISTILL_ON, "promote_after": 3, "cold_after_days": 0},
    # No platform default for schema: "what does this domain record" is a
    # question only the pack author can answer. An undefined key_by means the
    # memory is filed without a domain key — the pre-change behaviour.
    "schema": {},
}

#: Sub-keys this version actually has a consumer for. Everything else resolves to
#: a value and is reported as NOT executed, so a reader is never told that a
#: declared policy is in force when no code reads it.
#:
#: Measured 2026-11-13 across paas, fd-memory and agent-runner — the判据 is "which
#: code reads it", not "does it resolve":
#:
#: * ``process.distill`` — fd-memory's scheduler reads the pack root's policy row.
#: * ``store.home`` — nobody: ``yongyi-client``'s ``memoryNamespaceFor`` returns
#:   ``d/<pack>/<agent>`` unconditionally and never looks at ``home``.
#: * ``read.scope`` / ``read.budget`` — nobody reads the PACK's declaration; the
#:   injection ladder reads the member's knowledge profile (cell default plus
#:   ``fleets.knowledge_profile``), never ``manifest.memory.read``.
#: * ``write.authority`` — no runtime gate; the spec carries it by prose discipline
#:   (``agents[].memory.guidance``) and manifest validation. That is a deliberate
#:   design for this axis, but it is not a consumer reading the declaration.
#: * ``promote_after`` / ``cold_after_days`` — no evidence counter, no eviction pass.
#:
#: An earlier version of this set listed the three author-side axes as executed on
#: the belief that the platform consumed them; the audit above found no such
#: consumer. Marking them unexecuted is the honest reading, and it is what the
#: shared fixture's own note says ("store/read/write are author-side axes the
#: memory service does not implement").
EXECUTED_SUBKEYS = frozenset({"process.distill", "schema.key_by"})

#: `schema.key_by` joined this set with add-memory-schema-axis, and the claim is
#: auditable in fd-memory: `domainKeysForValue` (recall by value), `domainFillRates`
#: (the adoption metric the operator reads) and the injection face's domain
#: folding all read it. `schema.domain` is NOT here — it is a label carried into
#: the read face, not something any code branches on.

#: The closed domain-key vocabulary (add-memory-schema-axis).
#:
#: MUST equal fd-memory's RootDb.DOMAIN_KEYS. That vocabulary is closed on
#: purpose: every domain key carries an expression index in the memory store, so
#: letting a distributed pack name a new one would let it change the shape of a
#: database it does not own — the same boundary `store.home` enforces by refusing
#: every address form. Adding a key is a release, not a declaration.
#:
#: The parity check (paas scripts/check-memory-vocabulary-parity.mjs) compares
#: this set against fd-memory's; edit one, edit the other.
DOMAIN_KEY_VOCABULARY = frozenset(
    {"instrument", "party", "case_no", "supplier", "sku"}
)

#: Shapes that mean "an address", refused before the enum check so the error can
#: explain the rule instead of just listing three allowed words.
_ADDRESS_HINTS = re.compile(r"(/|\\\\|://|^[du]-|\.sqlite\b|\.db\b)")


class DeclarationError(dict):
    """One rejection: ``{path, code, message}`` — same shape as the profile face."""

    def __init__(self, path: str, code: str, message: str) -> None:
        super().__init__(path=path, code=code, message=message)


def _err(errors: list[dict], path: str, code: str, message: str) -> None:
    errors.append(DeclarationError(path, code, message))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ── Normalize ────────────────────────────────────────────────────────────────


def normalize(raw: object, where: str = "memory") -> dict:
    """Validate + canonicalise one declaration level.

    Returns ``{"ok": True, "declaration": {...}}`` or ``{"ok": False, "errors": [...]}``.
    The canonical declaration contains ONLY the sub-keys that were mentioned —
    omission is meaningful (it is what lets the next level inherit).
    """
    if raw is None:
        return {"ok": True, "declaration": {}}

    if isinstance(raw, str) or not isinstance(raw, dict):
        # A bare scalar is the classic shape of the confusion this module exists to
        # prevent: `memory: shared` could mean read visibility or write authority,
        # and picking one silently is how the two got conflated in the first place.
        return {
            "ok": False,
            "errors": [
                DeclarationError(
                    where,
                    "memory_ambiguous_scalar",
                    f"{where} must be a mapping of {list(AXES)} — a bare value cannot be "
                    f"assigned to one axis (shared/private reads as write.authority, "
                    f"own+shared/shared/off reads as read.scope); name the axis explicitly",
                )
            ],
        }

    errors: list[dict] = []
    declaration: dict[str, dict[str, Any]] = {}

    for axis, value in raw.items():
        if axis not in AXES:
            _err(
                errors,
                f"{where}.{axis}",
                "memory_axis_unknown",
                f"{where}.{axis} is not a memory axis (known: {list(AXES)})",
            )
            continue
        if not isinstance(value, dict):
            _err(
                errors,
                f"{where}.{axis}",
                "memory_axis_invalid",
                f"{where}.{axis} must be a mapping of {list(AXIS_KEYS[axis])}",
            )
            continue
        for key, sub in value.items():
            path = f"{where}.{axis}.{key}"
            if key not in AXIS_KEYS[axis]:
                _err(
                    errors,
                    path,
                    "memory_key_unknown",
                    f"{path} is not a known key (known: {list(AXIS_KEYS[axis])})",
                )
                continue
            canonical = _check_value(axis, key, sub, path, errors)
            if canonical is not None:
                declaration.setdefault(axis, {})[key] = canonical

    if errors:
        return {"ok": False, "errors": errors}
    return {"ok": True, "declaration": declaration}


def _check_value(
    axis: str, key: str, value: object, path: str, errors: list[dict]
) -> Any:
    if axis == "store" and key == "home":
        if not isinstance(value, str):
            _err(errors, path, "store_home_invalid", f"{path} must be one of {list(STORE_HOMES)}")
            return None
        if value not in STORE_HOMES and _ADDRESS_HINTS.search(value):
            _err(
                errors,
                path,
                "store_home_addressed",
                f"{path} = {value!r} looks like an address. A declaration may only name "
                f"which store ({list(STORE_HOMES)}); the namespace is resolved from "
                f"identity, because a distributable pack that can name an address can "
                f"write into someone else's store",
            )
            return None
        if value not in STORE_HOMES:
            _err(
                errors,
                path,
                "store_home_invalid",
                f"{path} must be one of {list(STORE_HOMES)} (got {value!r})",
            )
            return None
        return value

    if axis == "read" and key == "scope":
        if value == "own":
            # Named separately from "not in the vocabulary": the value is
            # meaningful, it just has no consumer that would execute it, and
            # saying so stops the next reader from re-adding it hopefully.
            _err(
                errors,
                path,
                "read_scope_unexecutable",
                f"{path} = 'own' has no consumer that executes it: the read face only "
                f"distinguishes 'own+shared' from everything else, so 'own' would read "
                f"shared too (more than declared); use one of {list(READ_SCOPES)}",
            )
            return None
        if value not in READ_SCOPES:
            _err(errors, path, "read_scope_invalid", f"{path} must be one of {list(READ_SCOPES)}")
            return None
        return value

    if axis == "read" and key == "budget":
        if not _is_int(value) or not (BUDGET_MIN <= value <= BUDGET_MAX):
            _err(
                errors,
                path,
                "read_budget_invalid",
                f"{path} must be an integer in {BUDGET_MIN}..{BUDGET_MAX} (got {value!r}); "
                f"an out-of-range budget is refused rather than clamped, because a silent "
                f"clamp makes the declared value and the effective one differ",
            )
            return None
        return value

    if axis == "write" and key == "authority":
        if value not in MEMORY_SCOPES:
            _err(
                errors,
                path,
                "write_authority_invalid",
                f"{path} must be one of {list(MEMORY_SCOPES)} — this is write authority, "
                f"not a storage location (locations live on store.home)",
            )
            return None
        return value

    if axis == "process" and key == "distill":
        if value not in DISTILL_VALUES:
            _err(errors, path, "process_distill_invalid", f"{path} must be one of {list(DISTILL_VALUES)}")
            return None
        return value

    if axis == "process" and key == "promote_after":
        if not _is_int(value) or value < 1:
            _err(errors, path, "process_promote_after_invalid", f"{path} must be an integer >= 1")
            return None
        return value

    if axis == "process" and key == "cold_after_days":
        if not _is_int(value) or value < 0:
            _err(
                errors,
                path,
                "process_cold_after_days_invalid",
                f"{path} must be an integer >= 0 (0 = never cold-store)",
            )
            return None
        return value

    if axis == "schema" and key == "key_by":
        if not isinstance(value, str) or value not in DOMAIN_KEY_VOCABULARY:
            _err(
                errors,
                path,
                "schema_key_by_invalid",
                f"{path} must be one of {list(DOMAIN_KEY_VOCABULARY)} "
                f"(domain keys are a closed set — each one carries an index in the memory store)",
            )
            return None
        return value

    if axis == "schema" and key == "domain":
        if not isinstance(value, str) or not value.strip():
            _err(errors, path, "schema_domain_invalid", f"{path} must be a non-empty string")
            return None
        return value.strip()

    _err(errors, path, "memory_key_unknown", f"{path} is not a known key")
    return None


# ── Resolve ──────────────────────────────────────────────────────────────────


#: Canonical sub-key order for output. Stable order matters: the read face is
#: compared in tests and by operators, and set iteration would reshuffle it.
SUBKEYS = tuple(f"{axis}.{key}" for axis in AXES for key in AXIS_KEYS[axis])


def resolve_effective(
    levels: list[tuple[str, dict]],
    deployment_defaults: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """Resolve the inheritance chain into one row per sub-key.

    ``levels`` is most-specific first, each ``(label, canonical_declaration)`` —
    typically ``[("agent", a), ("team", t)]``. ``deployment_defaults`` is NOT a
    level: it is what the deployment would use if nobody declared anything, so it
    fills ``effective`` without becoming anyone's ``declared`` value.

    Each row carries all four columns an operator needs to catch "declared but not
    in force" without reading code:

    * ``declared``   — the value a LEVEL declared (``None`` when every level was
      silent; a deployment default is not a declaration)
    * ``inherited_from`` — which level supplied ``effective``, ``"deployment"``
      when a deployment default did, ``"platform"`` when the built-in did
    * ``effective``  — the value in force (``None`` for write authority, which has
      no default: nobody can answer it on the agent's behalf)
    * ``executed``   — whether this version has a consumer for that sub-key

    This is mirrored verbatim in fd-memory's ``src/policy.ts``; a shared fixture
    compares the two, because two implementations of one inheritance rule drift
    unless something checks.
    """
    defaults = deployment_defaults or {}
    rows: dict[str, dict] = {}
    for sub in SUBKEYS:
        axis, key = sub.split(".", 1)
        declared = None
        for _label, declaration in levels:
            if key in (declaration.get(axis) or {}):
                declared = declaration[axis][key]
                break
        effective = None
        source = None
        for label, declaration in levels:
            value = (declaration.get(axis) or {}).get(key)
            # `inherit` is not a value — it is an instruction to keep looking up.
            if value is None or (axis == "process" and key == "distill" and value == DISTILL_INHERIT):
                continue
            effective, source = value, label
            break
        if effective is None:
            deployment_value = (defaults.get(axis) or {}).get(key)
            if deployment_value is not None:
                effective, source = deployment_value, "deployment"
            else:
                platform_value = (PLATFORM_DEFAULTS.get(axis) or {}).get(key)
                if platform_value is not None:
                    effective, source = platform_value, "platform"
        rows[sub] = {
            "declared": declared,
            "inherited_from": source,
            "effective": effective,
            "executed": sub in EXECUTED_SUBKEYS,
        }
    return rows


def unexecuted(rows: dict[str, dict]) -> list[str]:
    """Sub-keys someone DECLARED that no consumer will act on.

    Filtered to declared values on purpose: each row's ``executed`` flag already
    tells the truth about every key, including ones that resolve from a default.
    This list is the actionable subset — "you asked for this and nothing does it"
    — so it stays empty until someone declares something the version cannot honour.
    """
    return [
        sub
        for sub in SUBKEYS
        if rows[sub]["declared"] is not None and not rows[sub]["executed"]
    ]
