"""Node implementations for the CLI tests.

Kept as a real importable module rather than a temp file so `--impl` exercises
the same loading path a user's own module would.
"""


def factory():
    table = {
        "intake": lambda _inputs: {"draft": "v1"},
        "review": lambda _inputs: {"draft": "v1", "route": "approved"},
        "ship": lambda _inputs: {"out": "sent"},
        "rework": lambda _inputs: {"out": "redone"},
        "planner": lambda _inputs: {"tasks": ["a", "b"]},
        "w01": lambda _inputs: {
            "patch": "p1",
            "file_ownership": "fd-x",
            "review_note": "ok",
        },
        "w02": lambda _inputs: {
            "patch": "p2",
            "file_ownership": "fd-y",
            "review_note": "ok",
        },
        "join": lambda _inputs: {"merged": "all"},
        "verifier": lambda _inputs: {"verdict": True},
    }
    # fanout-12 has twelve workers; a table missing w03..w12 would make a
    # *permitted* dispatch fail during execution, which is a different property
    # from the verdict this fixture is used to test.
    for index in range(3, 13):
        table[f"w{index:02d}"] = (
            lambda _inputs, i=index: {
                "patch": f"p{i}",
                "file_ownership": f"fd-{i}",
                "review_note": "ok",
            }
        )
    return table


def revise_factory():
    """Same graph, opposite decision: proves the branch is data, not wiring."""
    table = factory()
    table["review"] = lambda _inputs: {"draft": "v1", "route": "revise"}
    return table


def broken_factory():
    table = factory()
    del table["ship"]
    return table
