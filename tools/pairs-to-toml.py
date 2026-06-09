"""
Read filter-coint.py output (stdin or file) and print a TOML pairs block.

Pairs are deduplicated so each ticker appears in at most one pair. When a
ticker would appear a second time, the later pair is dropped (input is
expected to be sorted by p-value ascending, so the first occurrence is always
the more cointegrated pair).

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

# Keep only the first (lowest p-value) pair per ticker.
seen: set[str] = set()
deduped = []
dropped = []
for t1, t2, pval in pairs:
    if t1 in seen or t2 in seen:
        dropped.append((t1, t2, pval))
        continue
    seen.add(t1)
    seen.add(t2)
    deduped.append((t1, t2, pval))

if dropped:
    print(
        f"# Dropped {len(dropped)} pair(s) with overlapping tickers "
        f"(ticker already used in a lower-p-value pair):",
        file=sys.stderr,
    )
    for t1, t2, pval in dropped:
        print(f"#   {t1}/{t2} (p={pval:.4f})", file=sys.stderr)

width = max(len(t1) + len(t2) for t1, t2, _ in deduped) + 7  # ["T1", "T2"]
print("pairs = [")
for t1, t2, pval in deduped:
    entry = f'    ["{t1}", "{t2}"],'
    print(f"{entry:<{width + 6}}  # p={pval:.4f}")
print("]")
