"""AST diff: prove the hand-written helper functions are body-identical to
their counterparts in the monolith (which were copied verbatim).

Checks: leg_helpers.cross_facility_destination vs monolith._cross_facility_destination,
two_facilities_in_order vs _two_facilities_in_order,
facility_dock_from_text vs _facility_dock_from_text,
not_a_facility vs _not_a_facility (monolith closure inside commit_trip_leg).

Run from repo root: python3 tools/verify_helpers_ast.py
"""

import ast
import re


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def node_key(node):
    """Whitespace/comment-insensitive fingerprint of a function body."""
    return ast.dump(node, annotate_fields=False, include_attributes=False)


def find_func(tree_module, name):
    for n in tree_module.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def find_nested_func(fn, name):
    for n in ast.walk(fn):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def main():
    mono = ast.parse(read("bot/pre_split_state_machine.py"))
    helpers = ast.parse(read("bot/leg_helpers.py"))

    # monolith top-level helpers
    commit = find_func(mono, "commit_trip_leg")
    pairs = [
        ("cross_facility_destination", "_cross_facility_destination", "top"),
        ("two_facilities_in_order", "_two_facilities_in_order", "top"),
        ("facility_dock_from_text", "_facility_dock_from_text", "top"),
        ("not_a_facility", "_not_a_facility", "nested"),
    ]

    all_ok = True
    for new_name, old_name, kind in pairs:
        new_fn = find_func(helpers, new_name)
        old_fn = (find_nested_func(commit, old_name) if kind == "nested"
                  else find_func(mono, old_name))
        if new_fn is None or old_fn is None:
            print(f"{new_name}: MISSING (new={new_fn is not None} old={old_fn is not None})")
            all_ok = False
            continue

        # Compare body statement-by-statement with renames normalized.
        # For helpers the bodies are pure and variable names are unchanged,
        # so the AST fingerprints must match exactly (ignoring the def line).
        # Compare code statements only (skip leading docstring, whose internal
        # indentation legitimately differs after re-indenting the module).
        def code_body(fn):
            body = list(fn.body)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            return "|".join(
                ast.dump(s, annotate_fields=False, include_attributes=False)
                for s in body)

        same = code_body(new_fn) == code_body(old_fn)
        print(f"{new_name} (from _{old_name}): ", end="")
        if not same:
            all_ok = False
            # report where they diverge
            nb, ob = ast.dump(new_fn, annotate_fields=False), ast.dump(old_fn, annotate_fields=False)
            print("BODIES DIFFER")
            print("  new len", len(new_fn.body), "old len", len(old_fn.body))
            for i, (a, b) in enumerate(zip(new_fn.body, old_fn.body)):
                if node_key(a) != node_key(b):
                    print(f"  first diff at stmt {i}:")
                    print("    new:", ast.dump(a, annotate_fields=False)[:200])
                    print("    old:", ast.dump(b, annotate_fields=False)[:200])
                    break
        else:
            print("IDENTICAL")

    print("\nRESULT:", "ALL HELPERS IDENTICAL" if all_ok else "DIFFERENCES FOUND")


if __name__ == "__main__":
    main()