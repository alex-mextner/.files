"""Exit 0 when the 3d-cli PrintOps client has an `api`, 1 when it has none, 2 when the file is unreadable or malformed
(never raises, never prints: the file holds a token that an error message could echo).

Usage: printops_configured.py PATH-TO-printops.yaml

Mirrors lib/printops/client.py `load_config` of 3d-cli: `api` comes from $PRINTOPS_API, else from the file. The token is
optional: `3d init --env` accepts an api without one (a plain Spoolman server), so only the api decides. A value that is empty or null, a file
that is not a mapping, and a file that does not parse all leave the field unset, which is what the client reports as
unconfigured or as an error. No network request is made: a configured client whose server is down still counts.
PyYAML is used when installed (the client's own parser); a flat reader of `key: value` lines stands in otherwise,
because printops.yaml is a flat mapping and the module must not need a package.
"""

import os
import re
import sys

# The stand-in reader is deliberately stricter than YAML: it accepts a value only when it is certainly a non-empty
# string to PyYAML too (a plain token or URL, or one closed quote pair). Anything it is unsure of (flow collections,
# booleans, numbers, null words, anchors, tags, block scalars) counts as "not configured" or, when it may make the
# whole document unparsable, as unparsable: never a silently unusable credential. Fix the file (or install PyYAML).
PLAIN = re.compile(r"[A-Za-z][A-Za-z0-9._~:/@+%?=&,-]*(?<!:)")
HARMLESS = re.compile(r"~|-?[0-9]+(\.[0-9]+)?|\"\"|''")
NOT_STRINGS = {"true", "false", "yes", "no", "on", "off", "y", "n", "null"}


def scalar(raw):
    if len(raw) >= 3 and raw[0] == raw[-1] and raw[0] in "\"'" and raw[0] not in raw[1:-1] and "\\" not in raw:
        return raw[1:-1]
    if PLAIN.fullmatch(raw) and raw.lower() not in NOT_STRINGS:
        return raw
    return ""


def flat(text):
    data = {}
    if re.search("[^\n\r\x20-\x7e\xa0-\u2027\u202a-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]|\r(?!\n)", text):
        raise ValueError("tab, control character or a lone carriage return")  # PyYAML refuses tabs as separators and control characters, and breaks a line at a lone \r, U+0085, U+2028 and U+2029 (fail closed)
    for line in text.split("\n"):  # the characters that would break a line elsewhere are refused above
        if not line.strip(" \r") or line.lstrip(" ").startswith("#"):
            continue
        # YAML needs whitespace (or the end of the line) after the colon: `api:http://x` is one plain string, not a key
        found = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*):(?: +(.*?))?", re.sub(r"[ \t]+#.*$", "", line.rstrip(" \r")))
        if found is None:
            raise ValueError(line)
        raw = found.group(2) or ""
        value = scalar(raw)
        if raw and not value and raw.lower() not in NOT_STRINGS and not HARMLESS.fullmatch(raw):
            # anything this reader cannot judge (flow collections, aliases, anchors, tags, block scalars, unbalanced
            # quotes) may make the whole document unparsable for PyYAML and for the client: refuse the document
            raise ValueError(line)
        data[found.group(1)] = value
    return data


def load(path):
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    try:
        import yaml
    except ImportError:
        return flat(text)
    return yaml.safe_load(text) or {}


def field(data, key):
    value = data.get(key)  # only a non-blank string is usable: `api: yes` or `api: [x]` would reach the client as junk
    return value if isinstance(value, str) and value.strip() else None


def main(path):
    api = os.environ.get("PRINTOPS_API") or None
    token = os.environ.get("PRINTOPS_TOKEN") or None
    # like the client's load_config: the file is read (and must parse) while either value is still missing
    if (api is None or token is None) and os.path.isfile(path):
        data = load(path)
        if not isinstance(data, dict):
            return 2
        api = api or field(data, "api")
    return 0 if api else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1]))
    except Exception:  # an unreadable or malformed file is reported as such, never a crash of the check
        sys.exit(2)
