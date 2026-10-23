#!/usr/bin/python3 -B
"""Make `control.enabled: false` true in tg-cli's config.yaml without touching any other key.

    config_control.py FILE check   exit 0 = already false, 3 = not (read-only), 4 = shape this script will not edit
    config_control.py FILE apply   set it: replace the key, add it to an existing block-style `control:` mapping,
                                   or add the mapping; exit 4 (file untouched) when `control` is written in any
                                   other style (flow mapping, quoted key, anchor, tag) or twice

Only plain block style is edited: anything else could be turned into invalid YAML (a duplicate `control` key
makes the daemon refuse its own config), so the script says so and leaves it to a human.
"""

import os
import re
import sys
import tempfile

EXIT_DRIFT, EXIT_UNSUPPORTED = 3, 4


def main(path: str, action: str) -> int:
    if action not in ("check", "apply"):
        print(f"usage: {sys.argv[0]} FILE check|apply", file=sys.stderr)
        return 2
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        text = ""
    if text.startswith("\ufeff") or any(line.startswith(("---", "%", "...")) for line in text.splitlines()):
        print("config.yaml has a BOM or YAML document markers/directives: set `control.enabled: false` by hand",
              file=sys.stderr)
        return EXIT_UNSUPPORTED
    lines = text.splitlines()

    heads = [i for i, line in enumerate(lines) if re.match(r"""\s*["']?control["']?\s*:""", line)]
    plain = [i for i in heads if re.fullmatch(r"control:\s*(#.*)?", lines[i])]
    if len(heads) > 1 or (heads and not plain):
        print("config.yaml writes `control` in a style this module does not edit (flow mapping, quotes, anchor, "
              "duplicate or indented): set `control.enabled: false` by hand", file=sys.stderr)
        return EXIT_UNSUPPORTED
    start = plain[0] if plain else None

    key = None
    indent = "  "
    if start is not None:
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][:1] in " \t#"):
            end += 1
        while end > start + 1 and not lines[end - 1].strip():  # trailing blank lines belong to the next block
            end -= 1
        children = [i for i in range(start + 1, end) if lines[i].strip() and not lines[i].lstrip().startswith("#")]
        if any(re.match(r"\s*-(\s|$)", lines[i]) for i in children):
            print("config.yaml: `control` is a list, not a mapping: fix it by hand", file=sys.stderr)
            return EXIT_UNSUPPORTED
        if any(not re.match(r"\s*[A-Za-z_][\w-]*\s*:(\s|$)", lines[i]) for i in children):
            print("config.yaml: `control` is not a plain block mapping of `key: value` lines: fix it by hand",
                  file=sys.stderr)
            return EXIT_UNSUPPORTED
        if children:
            indent = re.match(r"\s*", lines[children[0]]).group()
        key = next((i for i in children if re.match(rf"{indent}enabled\s*:", lines[i])), None)
    if key is not None and not re.fullmatch(rf"{indent}enabled\s*:\s*(true|false|yes|no|on|off)\s*(#.*)?", lines[key], re.IGNORECASE):
        print("config.yaml: `control.enabled` is not a plain true/false (anchor, tag or nested value): fix it by hand",
              file=sys.stderr)
        return EXIT_UNSUPPORTED
    if key is not None and re.fullmatch(rf"{indent}enabled\s*:\s*false\s*(#.*)?", lines[key]):
        return 0
    if action == "check":
        return EXIT_DRIFT

    if key is not None:
        lines[key] = f"{indent}enabled: false"
    elif start is not None:
        lines.insert(start + 1, f"{indent}enabled: false")
    else:
        lines = ["control:", "  enabled: false", *lines]
    path = os.path.realpath(path)   # a symlinked config (dotfiles) is edited in place, never replaced by a plain file
    directory = os.path.dirname(path)
    descriptor, temp = tempfile.mkstemp(dir=directory, prefix=".config.yaml.", suffix=".tmp")
    try:
        mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o600
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        os.unlink(temp)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
