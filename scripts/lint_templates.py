"""Lint the Jinja templates for the classes of mistake that break rendering:
malformed replace filters, unbalanced block tags, and references to undefined
partials.
"""

import io
import re
from pathlib import Path

BROKEN_REPLACE = '|replace("_", ")'
TAG_RE = re.compile(r"\{%-?\s*(block|endblock|if|endif|for|endfor|macro|endmacro|with|endwith|call|endcall)\b")

problems = []

for path in sorted(Path("app/templates").rglob("*.html")):
    text = io.open(path, encoding="utf-8").read()
    for number, line in enumerate(text.splitlines(), 1):
        if BROKEN_REPLACE in line:
            problems.append(f"{path}:{number}: malformed replace filter")

    # Track block nesting. end* tags pop; opening tags push.
    stack = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in TAG_RE.finditer(line):
            tag = match.group(1)
            if tag.startswith("end"):
                expected = tag[3:]
                if not stack:
                    problems.append(f"{path}:{number}: {tag} with nothing open")
                elif stack[-1][0] != expected:
                    problems.append(
                        f"{path}:{number}: {tag} closes {expected} "
                        f"but {stack[-1][0]} was open (line {stack[-1][1]})"
                    )
                    stack.pop()
                else:
                    stack.pop()
            else:
                stack.append((tag, number))
    if stack:
        for tag, number in stack:
            problems.append(f"{path}:{number}: {tag} never closed")

# Every include/extends target must exist.
template_root = Path("app/templates")
ref_re = re.compile(r'\{%-?\s*(?:include|extends|import|from)\s+"([^"]+)"')
for path in sorted(template_root.rglob("*.html")):
    text = io.open(path, encoding="utf-8").read()
    for match in ref_re.finditer(text):
        target = match.group(1)
        if not (template_root / target).is_file():
            problems.append(f"{path}: missing template {target}")

if problems:
    print(f"TEMPLATE PROBLEMS: {len(problems)}")
    for item in problems:
        print(" ", item)
    raise SystemExit(1)

count = len(list(template_root.rglob("*.html")))
print(f"template lint passed for {count} files")
