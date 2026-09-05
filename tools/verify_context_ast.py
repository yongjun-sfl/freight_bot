"""AST diff: prove leg_context.LegContext methods behave identically to the
nested helper closures they were extracted from in the monolith.

The closure bodies captured locals (cur, conn, did, msg_timestamp, intent,
origin, destination, trailer, where, position, arrival_leg); in the methods
those same values read via self.X. So we normalize `self.attr` -> `attr`
before comparing. SQL string-literal whitespace is normalized too (MySQL is
whitespace-insensitive). Docstrings (first statement) are skipped; their
internal indentation legitimately differs after re-indentation.

Run from repo root: python3 tools/verify_context_ast.py
"""

import ast
import re


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Normalize(ast.NodeTransformer):
    """One pass: self.attr -> attr; collapse whitespace in string constants."""

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return node

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            node.value = re.sub(r"\s+", " ", node.value.strip())
        return node

    def visit_JoinedStr(self, node):
        self.generic_visit(node)
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                v.value = re.sub(r"\s+", " ", v.value.strip())
        return node


def code_body(fn):
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) \
            and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    body = [ast.fix_missing_locations(Normalize().visit(s)) for s in body]
    return "|".join(
        ast.dump(s, annotate_fields=False, include_attributes=False) for s in body)


def find_func(tree, name):
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def find_any_func(container, name):
    """Find a function/method by name anywhere inside a class or function."""
    for n in ast.walk(container):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def main():
    mono = ast.parse(read("bot/pre_split_state_machine.py"))
    ctx_mod = ast.parse(read("bot/leg_context.py"))

    commit = find_func(mono, "commit_trip_leg")
    closures = {}
    for n in ast.walk(commit):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.name in {
                    "find_duplicate_bol_leg", "resolve_round",
                    "current_round_number", "stamp_finished",
                    "open_departure_leg", "start_hooked_load",
                    "pickup_with_no_destination", "open_new_round"}:
            if n.name not in closures:
                closures[n.name] = n

    cls = next(n for n in ctx_mod.body if isinstance(n, ast.ClassDef)
               and n.name == "LegContext")

    pairs = [
        "find_duplicate_bol_leg",
        "resolve_round",
        "current_round_number",
        "stamp_finished",
        "open_departure_leg",
        "start_hooked_load",
        "pickup_with_no_destination",
        "open_new_round",
    ]

    all_ok = True
    for name in pairs:
        new_fn = find_any_func(cls, name)
        old_fn = closures.get(name)
        if new_fn is None or old_fn is None:
            print(f"{name}: MISSING (new={new_fn is not None} old={old_fn is not None})")
            all_ok = False
            continue
        if code_body(new_fn) == code_body(old_fn):
            print(f"{name}: IDENTICAL")
        else:
            all_ok = False
            print(f"{name}: BODIES DIFFER")

    print("\nRESULT:", "ALL CONTEXT METHODS IDENTICAL" if all_ok else "DIFFERENCES FOUND")


if __name__ == "__main__":
    main()