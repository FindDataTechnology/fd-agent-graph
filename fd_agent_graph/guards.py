"""Guards: the closed grammar of routing conditions, with three-valued evaluation.

A guard answers one question - may this edge be taken, must this node run? - and
it answers it over *decisions*, never over free text. The grammar is closed on
purpose:

    atom  := <node>.<route> | <node>.done
    expr  := or
    or    := and ("or" and)*
    and   := unary ("and" unary)*
    unary := "not" unary | "(" expr ")" | atom

Why closed: an open expression (anything Python can evaluate) cannot be checked
statically - the checker cannot enumerate what a condition depends on - and it
turns a graph definition into executable code. Every atom names a decision node
and one of the routes that node declares, so the checker can answer "is this
guard even satisfiable" without running anything.

Why three values: a guard over a decision that has not settled is *unknown*,
never false. Collapsing unknown into false is how a branch that never ran comes
to look exactly like a branch that ran and was not taken - the failure this
whole change exists to make impossible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

#: Sentinel route used by `<node>.done`: the node settled, whichever route it chose.
DONE = None

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<dot>\.)
  | (?P<ident>[A-Za-z0-9_-]+)
    """,
    re.VERBOSE,
)

_KEYWORDS = frozenset({"not", "and", "or"})


class GuardSyntaxError(ValueError):
    """A guard that is not in the closed grammar.

    Carries the offending token and its offset so a report can point at it
    instead of quoting the whole condition back at the author.
    """

    def __init__(self, message: str, *, source: str, position: int, token: str = "") -> None:
        super().__init__(f"{message} at offset {position} in {source!r}")
        self.source = source
        self.position = position
        self.token = token


@dataclass(frozen=True, slots=True)
class Atom:
    """`<node>.<route>`, or `<node>.done` when `route` is None."""

    node: str
    route: str | None = None

    @property
    def is_done(self) -> bool:
        return self.route is None

    def to_source(self) -> str:
        return f"{self.node}.done" if self.route is None else f"{self.node}.{self.route}"


@dataclass(frozen=True, slots=True)
class Not:
    child: "Expr"

    def to_source(self) -> str:
        inner = self.child.to_source()
        # Keep the rendering unambiguous: `not a.b and c` must not come back
        # as `not (a.b and c)`.
        if isinstance(self.child, (And, Or)):
            return f"not ({inner})"
        return f"not {inner}"


@dataclass(frozen=True, slots=True)
class And:
    children: tuple["Expr", ...]

    def to_source(self) -> str:
        return " and ".join(_render_child(c, (Or,)) for c in self.children)


@dataclass(frozen=True, slots=True)
class Or:
    children: tuple["Expr", ...]

    def to_source(self) -> str:
        # `and` binds tighter, so an And child needs no parentheses here; Or
        # children were flattened at parse time, so none can nest directly.
        return " or ".join(child.to_source() for child in self.children)


Expr = Atom | Not | And | Or


def _render_child(child: "Expr", wrap: tuple[type, ...]) -> str:
    text = child.to_source()
    if wrap and isinstance(child, wrap):
        return f"({text})"
    return text


@dataclass(frozen=True, slots=True)
class Guard:
    """A parsed condition, keeping its source for round-trip fidelity."""

    source: str
    expr: Expr

    def to_source(self) -> str:
        return self.expr.to_source()

    def atoms(self) -> tuple[Atom, ...]:
        return _atoms(self.expr)


def _atoms(expr: Expr) -> tuple[Atom, ...]:
    if isinstance(expr, Atom):
        return (expr,)
    if isinstance(expr, Not):
        return _atoms(expr.child)
    out: list[Atom] = []
    for child in expr.children:
        out.extend(_atoms(child))
    return tuple(out)


# ---- parsing ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    value: str
    position: int


def _tokenize(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    for match in _TOKEN_RE.finditer(source):
        kind = match.lastgroup or ""
        if kind == "ws":
            continue
        tokens.append(_Token(kind, match.group(), match.start()))
    consumed = "".join(t.value for t in tokens)
    positions = [i for i, ch in enumerate(source) if not ch.isspace()]
    if "".join(source[i] for i in positions) != consumed:
        # Something in the text is not a token at all (a quote, a comparison
        # operator, a literal). Point at the exact byte that does not belong,
        # rather than at the end of a partial parse.
        for index, position in enumerate(positions):
            char = source[position]
            if index >= len(consumed) or consumed[index] != char:
                raise GuardSyntaxError(
                    f"character {char!r} is not part of the guard grammar",
                    source=source,
                    position=position,
                    token=char,
                )
    return tokens


class _Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = _tokenize(source)
        self.index = 0

    # -- cursor -----------------------------------------------------------
    def _peek(self) -> _Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _advance(self) -> _Token:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def _at_keyword(self, word: str) -> bool:
        token = self._peek()
        return token is not None and token.kind == "ident" and token.value == word

    def _fail(self, message: str, token: _Token | None = None) -> GuardSyntaxError:
        if token is None:
            token = self._peek()
        position = token.position if token is not None else len(self.source)
        return GuardSyntaxError(
            message,
            source=self.source,
            position=position,
            token=token.value if token is not None else "<end>",
        )

    # -- grammar ----------------------------------------------------------
    def parse(self) -> Expr:
        if not self.tokens:
            raise GuardSyntaxError(
                "empty guard", source=self.source, position=0, token="<empty>"
            )
        expr = self._parse_or()
        token = self._peek()
        if token is not None:
            raise self._fail(f"unexpected token {token.value!r} after a complete expression", token)
        return expr

    def _parse_or(self) -> Expr:
        children = [self._parse_and()]
        while self._at_keyword("or"):
            self._advance()
            children.append(self._parse_and())
        if len(children) == 1:
            return children[0]
        return Or(tuple(_flatten(children, Or)))

    def _parse_and(self) -> Expr:
        children = [self._parse_unary()]
        while self._at_keyword("and"):
            self._advance()
            children.append(self._parse_unary())
        if len(children) == 1:
            return children[0]
        return And(tuple(_flatten(children, And)))

    def _parse_unary(self) -> Expr:
        if self._at_keyword("not"):
            token = self._advance()
            if self._peek() is None:
                raise self._fail("'not' is missing its operand", token)
            return Not(self._parse_unary())
        token = self._peek()
        if token is not None and token.kind == "lparen":
            self._advance()
            inner = self._parse_or()
            closer = self._peek()
            if closer is None or closer.kind != "rparen":
                raise self._fail("missing closing parenthesis")
            self._advance()
            return inner
        return self._parse_atom()

    def _parse_atom(self) -> Expr:
        token = self._peek()
        if token is None:
            raise self._fail("expected <node>.<route>")
        if token.kind != "ident":
            raise self._fail(f"expected a node name, found {token.value!r}", token)
        if token.value in _KEYWORDS:
            raise self._fail(f"{token.value!r} is an operator, not a node name", token)
        node = self._advance().value

        dot = self._peek()
        if dot is None or dot.kind != "dot":
            raise self._fail(f"expected '.' after node name {node!r}")
        self._advance()

        route_token = self._peek()
        if route_token is None or route_token.kind != "ident":
            raise self._fail(f"expected a route name after {node!r}.", route_token)
        if route_token.value in _KEYWORDS:
            raise self._fail(
                f"expected a route name after {node!r}., found the operator "
                f"{route_token.value!r}",
                route_token,
            )
        route = self._advance().value
        return Atom(node=node, route=None if route == "done" else route)


def _flatten(children: list[Expr], kind: type) -> list[Expr]:
    """Collapse `a or (b or c)` to `a or b or c` so equality is structural."""
    out: list[Expr] = []
    for child in children:
        if isinstance(child, kind):
            out.extend(child.children)  # type: ignore[attr-defined]
        else:
            out.append(child)
    return out


def parse_guard(source: str) -> Guard:
    """Parse a guard, raising `GuardSyntaxError` on anything outside the grammar."""
    text = source.strip()
    return Guard(source=text, expr=_Parser(text).parse())


# ---- evaluation ---------------------------------------------------------


@dataclass(frozen=True)
class RouteBoard:
    """What the decision nodes of one run have settled on.

    A node present in `settled` has decided; the value is the set of routes it
    selected (empty is legitimate for `select: many`). A node absent from
    `settled` has not decided, which is what makes an atom over it unknown.
    """

    settled: Mapping[str, frozenset[str]]

    def is_settled(self, node: str) -> bool:
        return node in self.settled

    def selected(self, node: str) -> frozenset[str]:
        return self.settled.get(node, frozenset())


def evaluate(guard: Guard, board: RouteBoard) -> bool | None:
    """Three-valued: True, False, or None for unknown."""
    return _evaluate(guard.expr, board)


def _evaluate(expr: Expr, board: RouteBoard) -> bool | None:
    if isinstance(expr, Atom):
        if not board.is_settled(expr.node):
            return None
        if expr.route is None:
            return True
        return expr.route in board.selected(expr.node)
    if isinstance(expr, Not):
        value = _evaluate(expr.child, board)
        return None if value is None else not value
    values = [_evaluate(child, board) for child in expr.children]
    if isinstance(expr, And):
        if any(v is False for v in values):
            return False
        return None if any(v is None for v in values) else True
    if any(v is True for v in values):
        return True
    return None if any(v is None for v in values) else False


def referenced_nodes(guard: Guard) -> tuple[str, ...]:
    """Decision nodes the guard names, in first-appearance order."""
    seen: list[str] = []
    for atom in guard.atoms():
        if atom.node not in seen:
            seen.append(atom.node)
    return tuple(seen)


def referenced_routes(guard: Guard, node: str) -> tuple[str, ...]:
    """Routes the guard names on `node`; `done` is reported as the literal 'done'."""
    out: list[str] = []
    for atom in guard.atoms():
        if atom.node != node:
            continue
        route = "done" if atom.route is None else atom.route
        if route not in out:
            out.append(route)
    return tuple(out)
