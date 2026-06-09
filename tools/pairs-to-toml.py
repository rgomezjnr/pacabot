"""
Read filter-coint.py output (stdin or file) and print a TOML pairs block.

Usage:
    python filter-coint.py coint-output.txt | python pairs-to-toml.py
    python pairs-to-toml.py filtered.txt
"""
import re
import sys

pattern = re.compile(r"\('?(\w+)'?, '?(\w+)'?\): p-value = ([\d.]+)")

source = open(sys.argv[1]) if len(sys.argv) > 1 else sys.stdin

pairs = []
with source as f:
    for line in f:
        m = pattern.search(line)
        if m:
            pairs.append((m.group(1), m.group(2), float(m.group(3))))

if not pairs:
    print("No pairs found.", file=sys.stderr)
    sys.exit(1)

width = max(len(t1) + len(t2) for t1, t2, _ in pairs) + 7  # ["T1", "T2"]
print("pairs = [")
for t1, t2, pval in pairs:
    entry = f'    ["{t1}", "{t2}"],'
    print(f"{entry:<{width + 6}}  # p={pval:.4f}")
print("]")
