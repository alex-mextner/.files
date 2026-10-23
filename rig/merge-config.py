#!/usr/bin/env python3
"""Merge rig/config.yaml into the machine's rig global config.

usage: merge-config.py <source.yaml> <target.yaml> [--apply] [--backup-suffix SUFFIX]

Keys declared in the source win; keys only the target has stay. Lists of plain values are the
union (source items first, then the target's own extras), so an item a machine added is not dropped
and a second run changes nothing. Lists of mappings are replaced by the source.

Prints `ok` when the target already has everything, `changed` when it would (or, with --apply, did)
change. Exit 0 on either; non-zero with a message on stderr when a file cannot be read or written.
YAML comments in the target are not kept (the file is rewritten by PyYAML); with --apply the previous
file is first copied to <target><SUFFIX>.
"""
import os
import shutil
import sys

import yaml


def merge(have, want):
    if isinstance(have, dict) and isinstance(want, dict):
        out = dict(have)
        for key, value in want.items():
            out[key] = merge(have[key], value) if key in have else value
        return out
    if isinstance(have, list) and isinstance(want, list) and not any(isinstance(i, (dict, list)) for i in have + want):
        return want + [item for item in have if item not in want]
    return want


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path) as handle:
        return yaml.safe_load(handle) or {}


def main(argv):
    apply = "--apply" in argv
    suffix = ""
    if "--backup-suffix" in argv:
        suffix = argv[argv.index("--backup-suffix") + 1]
    args = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] != "--backup-suffix")]
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    source, target = args
    have = load(target)
    merged = merge(have, load(source))
    if merged == have:
        print("ok")
        return 0
    if apply:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        if os.path.exists(target):
            shutil.copy2(target, target + suffix)
        with open(target, "w") as handle:
            yaml.safe_dump(merged, handle, sort_keys=False, default_flow_style=False)
    print("changed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, yaml.YAMLError) as error:
        print(f"merge-config: {error}", file=sys.stderr)
        sys.exit(1)
