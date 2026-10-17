"""Repair the mangled replace filters across all templates.

Rewrites the two-character sequence that should be a space inside the string
literal. Built by character code so no quoting layer can strip it.
"""

import io
from pathlib import Path

SPACE = chr(32)
OPEN = 'replace("_"' + ", "
BROKEN_TAIL = '")|title'
FIXED_TAIL = '"' + SPACE + '")|title'

total = 0
for path in Path("app/templates").rglob("*.html"):
    text = io.open(path, encoding="utf-8").read()
    if OPEN + BROKEN_TAIL in text:
        count = text.count(OPEN + BROKEN_TAIL)
        text = text.replace(OPEN + BROKEN_TAIL, OPEN + FIXED_TAIL)
        io.open(path, "w", encoding="utf-8", newline="\n").write(text)
        print("fixed " + str(count) + " in " + str(path))
        total += count

print("total fixed: " + str(total))

remaining = []
for path in Path("app/templates").rglob("*.html"):
    for number, line in enumerate(io.open(path, encoding="utf-8").read().splitlines(), 1):
        if OPEN + BROKEN_TAIL in line:
            remaining.append(str(path) + ":" + str(number))

print("remaining: " + (", ".join(remaining) if remaining else "none"))
