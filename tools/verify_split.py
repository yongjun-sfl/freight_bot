"""Post-split verification: diff the extracted modules against the monolith.

Compares real SQL statements and user-facing strings, whitespace-normalized,
between the whole original monolith (pre_split_state_machine.py) and the
whole new production code (leg_helpers + leg_context + leg_cases +
state_machine). Anything LOST would be dropped behavior; anything ADDED is a
behavior change introduced by the split.

Run from the repo root:  python3 tools/verify_split.py
"""

import re
from collections import Counter


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def stable(s):
    return re.sub(r"\s+", "", s)


def sql_blocks(src):
    """Real f-string SQL statements (not docstrings)."""
    out = []
    for m in re.finditer(r'f?"""(.*?)"""', src, re.S):
        s = m.group(1).strip()
        if re.match(r"(?is)(select|insert|update|alter|create|drop)\b", s):
            out.append(stable(s))
    return out


def ui_strings(src):
    """Double-quoted string contents that read like text (UI cards, replies,
    log messages with spaces or colons), whitespace-normalized."""
    out = []
    for m in re.finditer(r'f?"((?:[^"\\]|\\.)*)"', src):
        s = m.group(1)
        if len(s) >= 15 and (":" in s or " " in s):
            out.append(stable(s))
    return out


def diff(name, o_items, n_items):
    o, n = Counter(o_items), Counter(n_items)
    lost = {k: v - n.get(k, 0) for k, v in o.items() if v > n.get(k, 0)}
    added = {k: v - o.get(k, 0) for k, v in n.items() if v > o.get(k, 0)}
    print(f"[{name}]\n  orig={sum(o.values())} new={sum(n.values())}")
    ok = True
    if lost:
        ok = False
        print("  LOST from original (dropped behavior):")
        for k, v in sorted(lost.items()):
            print(f"    x{v} {k[:140]!r}")
    if added:
        ok = False
        print("  ADDED in new code (behavior change):")
        for k, v in sorted(added.items()):
            print(f"    x{v} {k[:140]!r}")
    if ok:
        print("  OK - identical")
    return ok


def main():
    orig = read("bot/pre_split_state_machine.py")
    production = "\n".join(read(f) for f in (
        "bot/leg_helpers.py", "bot/leg_context.py",
        "bot/leg_cases.py", "bot/state_machine.py"))

    ok = True
    ok &= diff("SQL statements: monolith vs new modules",
               sql_blocks(orig), sql_blocks(production))
    ok &= diff("UI/log strings: monolith vs new modules",
               ui_strings(orig), ui_strings(production))

    print("\nRESULT:", "ALL MATCH" if ok else "SOME DIFFERENCES FOUND")


if __name__ == "__main__":
    main()