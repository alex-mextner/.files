#!/usr/bin/env python3
"""Merge rig/config.yaml into the machine's rig global config.

Keys declared in the source win; keys only the target has stay. Lists of plain values are the
union (source items first, then the target's own extras), so an item a machine added is not dropped
and a second run changes nothing. Lists of mappings are replaced by the source.

Prints `ok` when the target already has everything, `changed` when it would (or, with --apply, did)
change. Exit 0 on either; non-zero with a message on stderr when a file cannot be read or written.
YAML comments in the target are not kept (the file is rewritten by PyYAML); with --apply and
--backup-suffix the previous file is first copied to <target><SUFFIX>.
"""
import argparse
import os
import shutil
import sys

import yaml


def is_plain_list(value):
    return isinstance(value, list) and not any(isinstance(item, (dict, list)) for item in value)


def merge(have, want):
    if isinstance(have, dict) and isinstance(want, dict):
        out = dict(have)
        for key, value in want.items():
            out[key] = merge(have[key], value) if key in have else value
        return out
    if is_plain_list(have) and is_plain_list(want):
        return want + [item for item in have if item not in want]
    return want


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path) as handle:
        return yaml.safe_load(handle) or {}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="the repository's rig/config.yaml")
    parser.add_argument("target", help="the machine's rig global config")
    parser.add_argument("--apply", action="store_true", help="write the merged config (default: only report)")
    parser.add_argument("--backup-suffix", default="", help="copy the previous target to <target><suffix> first")
    args = parser.parse_args()

    have = load(args.target)
    merged = merge(have, load(args.source))
    if merged == have:
        print("ok")
        return 0
    if args.apply:
        os.makedirs(os.path.dirname(args.target) or ".", exist_ok=True)
        if args.backup_suffix and os.path.exists(args.target):
            shutil.copy2(args.target, args.target + args.backup_suffix)
        with open(args.target, "w") as handle:
            yaml.safe_dump(merged, handle, sort_keys=False, default_flow_style=False)
    print("changed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, yaml.YAMLError) as error:
        print(f"merge-config: {error}", file=sys.stderr)
        sys.exit(1)
