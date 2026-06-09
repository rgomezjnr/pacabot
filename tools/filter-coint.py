import re
import sys

input_file = sys.argv[1] if len(sys.argv) > 1 else 'coint-output.txt'
threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05

pattern = re.compile(r"\('(\w+)', '(\w+)'\): p-value = ([\d.]+)")

results = []
with open(input_file) as f:
    for line in f:
        m = pattern.match(line.strip())
        if m:
            t1, t2, pval = m.group(1), m.group(2), float(m.group(3))
            if pval < threshold:
                results.append((t1, t2, pval))

results.sort(key=lambda x: x[2])

print(f"Cointegrated pairs (p < {threshold}):\n")
for t1, t2, pval in results:
    print(f"  ({t1}, {t2}): p-value = {pval:.4f}")
print(f"\n{len(results)} pair(s) found.")
