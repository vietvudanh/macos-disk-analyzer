#!/usr/bin/env python3
"""
Draw an interactive disk map (treemap) from an ncdu scan and suggest what to delete.

Suggestions come from two rule files:
  my_cleanup_rules.toml  YOUR list of things you usually delete (checked first, listed first)
  cleanup_rules.toml     built-in rules (caches, build output, SDKs, models, ...)

Run with: uvx python disk_map.py        (Python 3.11+, standard library only)
Writes:   data/<host>/disk_map.html     open it in a browser
          data/<host>/cleanup_suggested.sh
"""

import argparse
import json
import re
import shlex
import shutil
import socket
import sys
import time
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIG_FILE = 20 * 1024**2  # files at least this big are kept as their own map tiles
MARKERS = {"Cargo.toml", "package.json", "pyproject.toml", "go.mod", "pom.xml", "build.gradle"}
NO_MATCH = 1 << 30


class Node:
    __slots__ = ("name", "own", "kids", "size", "is_file", "markers", "match", "best")

    def __init__(self, name, is_file=False):
        self.name = name
        self.own = 0  # bytes of small files directly inside (or the file itself)
        self.kids = {}
        self.size = 0
        self.is_file = is_file
        self.markers = None
        self.match = NO_MATCH  # rule index matching this path
        self.best = NO_MATCH  # lowest rule index matching anywhere in the subtree


def join(parent, name):
    return f"/{name}" if parent == "/" else f"{parent}/{name}"


# --- tree building -----------------------------------------------------------


def build(entry, path, seen_inodes, skip):
    """Turn an ncdu directory entry into a Node tree, keeping only big files."""
    node = Node(entry[0]["name"])
    node.own = entry[0].get("dsize", 0)
    for child in entry[1:]:
        if isinstance(child, list):
            child_path = join(path, child[0]["name"])
            if not skip(child_path):
                add_child(node, build(child, child_path, seen_inodes, skip))
            continue
        if child.get("hlnkc"):  # count hard-linked files once
            ino = child.get("ino")
            if ino in seen_inodes:
                continue
            seen_inodes.add(ino)
        name, size = child["name"], child.get("dsize", 0)
        if name in MARKERS:
            node.markers = (node.markers or set()) | {name}
        if size >= BIG_FILE:
            leaf = Node(name, is_file=True)
            leaf.own = size
            add_child(node, leaf)
        else:
            node.own += size
    return node


def add_child(parent, child):
    existing = parent.kids.get(child.name)
    if existing is None:
        parent.kids[child.name] = child
    else:
        merge(existing, child)


def merge(a, b):
    """Fold b into a. Both describe the same folder seen through two mount points."""
    a.own = max(a.own, b.own)
    if b.markers:
        a.markers = (a.markers or set()) | b.markers
    for name, kid in b.kids.items():
        mine = a.kids.get(name)
        if mine is None:
            a.kids[name] = kid
        elif mine.is_file or kid.is_file:
            if kid.own > mine.own:
                a.kids[name] = kid
        else:
            merge(mine, kid)


def fold_data_volume(root):
    """macOS shows the data volume both at / (firmlinks) and /System/Volumes/Data."""
    volumes = root.kids.get("System", Node("")).kids.get("Volumes")
    data = volumes.kids.pop("Data", None) if volumes else None
    if data:
        for kid in data.kids.values():
            add_child(root, kid)


def compute_sizes(node):
    node.size = node.own + sum(compute_sizes(k) for k in node.kids.values())
    return node.size


# --- rules -------------------------------------------------------------------


def glob_to_regex(pattern, home):
    if pattern.startswith("~"):
        pattern = home + pattern[1:]
    elif not pattern.startswith("/"):
        pattern = "**/" + pattern
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "{" and "}" in pattern[i:]:
            end = pattern.index("}", i)
            options = pattern[i + 1 : end].split(",")
            out.append("(?:" + "|".join(re.escape(o) for o in options) + ")")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


class Matcher:
    """First-match-wins glob list compiled into one regex (one match per path)."""

    def __init__(self, entries, home):
        # entries: [(owner_index, glob)]
        self.owners = [owner for owner, _ in entries]
        self.parts = [re.compile(glob_to_regex(g, home)) for _, g in entries]
        alternation = "|".join(
            f"(?P<g{k}>{glob_to_regex(g, home)})" for k, (_, g) in enumerate(entries)
        )
        self.combined = re.compile(alternation) if entries else None

    def first(self, path, accept=None):
        if self.combined is None:
            return None
        m = self.combined.fullmatch(path)
        if not m:
            return None
        k = int(m.lastgroup[1:])
        if accept is None or accept(self.owners[k]):
            return self.owners[k]
        for j in range(k + 1, len(self.parts)):  # rare: a `needs` check failed
            if self.parts[j].fullmatch(path) and accept(self.owners[j]):
                return self.owners[j]
        return None


def load_rules(rule_files):
    rules, categories = [], []
    for path, source in rule_files:
        if not path.exists():
            continue
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            sys.exit(f"Error in {path}: {e}")
        for r in data.get("rule", []):
            paths = r.get("paths") or r.get("path") or []
            if isinstance(paths, str):
                paths = [paths]
            tier = r.get("tier", "mine" if source == "mine" else "safe")
            if tier not in ("mine", "safe", "review", "keep"):
                sys.exit(f"{path}: rule {r.get('label')!r} has unknown tier {tier!r}")
            needs = r.get("needs") or []
            rules.append(
                {
                    "label": r.get("label", paths[0] if paths else "?"),
                    "tier": tier,
                    "source": source,
                    "paths": paths,
                    "note": r.get("note", ""),
                    "command": r.get("command", ""),
                    "needs": [needs] if isinstance(needs, str) else needs,
                    "min_size": int(r.get("min_size_mb", 1) * 1024**2),
                }
            )
        for c in data.get("category", []):
            categories.append({"name": c["name"], "paths": c.get("paths", [])})
    return rules, categories


def annotate(node, path, parent, matcher, rules):
    """Record the rule matching each path and the best rule anywhere below it."""

    def accept(idx):
        needs = rules[idx]["needs"]
        if not needs:
            return True
        have = set(parent.kids) | (parent.markers or set()) if parent else set()
        return any(n in have for n in needs)

    hit = matcher.first(path, accept)
    node.match = NO_MATCH if hit is None else hit
    best = node.match if hit is not None and rules[hit]["source"] == "mine" else NO_MATCH
    for kid in node.kids.values():
        best = min(best, annotate(kid, join(path, kid.name), node, matcher, rules))
    node.best = best  # only your own rules may carve pieces out of a bigger match
    return best


def collect(node, path, governing, rules, out):
    """Turn rule matches into deletion candidates without overlaps.

    A folder is governed by the closest ancestor (or itself) matching a rule;
    nested matches of built-in rules are ignored (no __pycache__ inside a
    .venv). If something deeper matches one of YOUR rules, the folder is split
    into its children so that part follows your rule instead.
    """
    if node.match != NO_MATCH and (
        governing == NO_MATCH or (node.match < governing and rules[node.match]["source"] == "mine")
    ):
        governing = node.match
    if governing == NO_MATCH:
        for kid in node.kids.values():
            collect(kid, join(path, kid.name), NO_MATCH, rules, out)
        return
    if node.best >= governing:
        rule = rules[governing]
        if rule["tier"] != "keep" and node.size >= rule["min_size"]:
            out.append((path, node.size, governing, node.match == governing))
        return
    for kid in node.kids.values():
        collect(kid, join(path, kid.name), governing, rules, out)


# --- output ------------------------------------------------------------------


def serialize(node, path, parent_cat, cat_matcher, cand_rule, min_abs, max_kids=60):
    cat = cat_matcher.first(path)
    cat = parent_cat if cat is None else cat
    out = {"n": node.name, "s": node.size}
    if cat != parent_cat:
        out["c"] = cat
    if node.is_file:
        out["f"] = 1
    if path in cand_rule:
        out["r"] = cand_rule[path]
    floor = max(min_abs, node.size * 0.002)
    kids = sorted(
        (k for k in node.kids.values() if k.size >= floor),
        key=lambda k: k.size,
        reverse=True,
    )[:max_kids]
    if kids:
        out["k"] = [
            serialize(k, join(path, k.name), cat, cat_matcher, cand_rule, min_abs, max_kids)
            for k in kids
        ]
    return out


def shell_script(rules, candidates, home):
    lines = [
        "#!/bin/sh",
        "# Generated by disk_map.py. Review every line before running:",
        "# this permanently deletes data. 'review' items are commented out.",
        "set -u",
    ]
    by_rule = {}
    for path, size, idx, whole in candidates:
        by_rule.setdefault(idx, []).append((path, size, whole))
    order = sorted(by_rule, key=lambda i: (TIER_ORDER[rules[i]["tier"]], -sum(it[1] for it in by_rule[i])))
    for idx in order:
        rule, items = rules[idx], by_rule[idx]
        prefix = "# " if rule["tier"] == "review" else ""
        lines += ["", f"# {rule['label']} [{rule['tier']}] - {fmt_gb(sum(it[1] for it in items))}"]
        if rule["note"]:
            lines.append(f"# {rule['note']}")
        cmd = rule["command"]
        # A command cleans the whole matched folder, so pieces split off around
        # one of your rules are removed individually with rm instead.
        if cmd and "{path}" not in cmd and "{parent}" not in cmd and all(it[2] for it in items):
            lines.append(prefix + cmd)
            continue
        for path, size, whole in items:
            if cmd and whole and ("{path}" in cmd or "{parent}" in cmd):
                line = cmd.replace("{path}", shlex.quote(path)).replace(
                    "{parent}", shlex.quote(path.rsplit("/", 1)[0] or "/")
                )
            else:
                line = f"rm -rf -- {shlex.quote(path)}"
            lines.append(f"{prefix}{line}   # {fmt_gb(size)}")
    return "\n".join(lines) + "\n"


TIER_ORDER = {"mine": 0, "safe": 1, "review": 2, "keep": 3}


def fmt_gb(n):
    return f"{n / 1024**3:.2f} GB"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=socket.gethostname(), help="data/<host>/ to read and write")
    parser.add_argument("--home", default=str(Path.home()), help="home folder of the scanned machine")
    parser.add_argument("--rules", default=str(HERE / "cleanup_rules.toml"))
    parser.add_argument("--my-rules", default=str(HERE / "my_cleanup_rules.toml"))
    parser.add_argument("--include-cloudstorage", action="store_true",
                        help="Include ~/Library/CloudStorage (cloud placeholders)")
    parser.add_argument("--min-tile-mb", type=float, default=5, help="smallest folder drawn on the map")
    args = parser.parse_args()
    sys.setrecursionlimit(100_000)

    data_dir = HERE / "data" / args.host
    input_file = data_dir / "disk_usage.json"
    if not input_file.exists():
        sys.exit(f"{input_file} not found. Run: sudo ncdu -o {input_file} /")

    t0 = time.time()
    print(f"Loading {input_file} ...")
    with input_file.open() as f:
        raw = json.load(f)
    scanned_at = raw[2].get("timestamp")

    def skip(path):
        return not args.include_cloudstorage and "/Library/CloudStorage" in path

    print("Building tree ...")
    root = build(raw[3], "/", set(), skip)
    del raw
    fold_data_volume(root)
    compute_sizes(root)

    rules, categories = load_rules([(Path(args.my_rules), "mine"), (Path(args.rules), "builtin")])
    print(f"Matching {len(rules)} rules ...")
    matcher = Matcher([(i, g) for i, r in enumerate(rules) for g in r["paths"]], args.home)
    annotate(root, "/", None, matcher, rules)
    candidates = []
    collect(root, "/", NO_MATCH, rules, candidates)
    candidates.sort(key=lambda c: -c[1])

    cat_matcher = Matcher([(i, g) for i, c in enumerate(categories) for g in c["paths"]], args.home)
    categories.append({"name": "Other", "paths": []})
    tree = serialize(root, "/", len(categories) - 1, cat_matcher,
                     {c[0]: c[2] for c in candidates}, int(args.min_tile_mb * 1024**2))
    tree["c"] = len(categories) - 1

    disk = None
    if args.host == socket.gethostname():
        target = "/System/Volumes/Data" if Path("/System/Volumes/Data").exists() else "/"
        usage = shutil.disk_usage(target)
        disk = {"total": usage.total, "free": usage.free}

    payload = {
        "meta": {"host": args.host, "home": args.home, "scanned_at": scanned_at,
                 "generated_at": int(time.time()), "disk": disk},
        "categories": [c["name"] for c in categories],
        "rules": [{k: r[k] for k in ("label", "tier", "source", "paths", "note", "command")} for r in rules],
        "candidates": candidates,
        "tree": tree,
    }
    template = (HERE / "disk_map_template.html").read_text()
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html_file = data_dir / "disk_map.html"
    html_file.write_text(template.replace("/*__DATA__*/null", blob))
    script_file = data_dir / "cleanup_suggested.sh"
    script_file.write_text(shell_script(rules, candidates, args.home))
    script_file.chmod(0o755)

    totals = {}
    for _, size, idx, _ in candidates:
        totals[rules[idx]["tier"]] = totals.get(rules[idx]["tier"], 0) + size
    print(f"\nScanned {fmt_gb(root.size)} in {time.time() - t0:.1f}s")
    for tier, name in (("mine", "Your list"), ("safe", "Safe to remove"), ("review", "Needs review")):
        print(f"  {name:<15} {fmt_gb(totals.get(tier, 0)):>10}")
    print(f"\nMap:    {html_file}\nScript: {script_file}")
    print(f"Open with: open '{html_file}'")


if __name__ == "__main__":
    main()
