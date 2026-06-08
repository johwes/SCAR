"""
grep_scan.py — Danger-function pattern scanner (worked example for SCAR extension).

Ships inside the scar-agent image at /app/examples/grep_scan.py.
Invoke it via the osscrs-scan task's tool-cmd parameter:

  tkn pipeline start scar-v2 \\
    --param repo-url=https://github.com/johwes/scar-test-c \\
    --param tool-cmd="python3 /app/examples/grep_scan.py" \\
    --workspace name=shared-data,claimName=scar-pvc \\
    --use-param-defaults --showlog

Writes .scar/findings-grep-scan-<ts>.json which the repair loop discovers
automatically via the pluggable findings convention. No container rebuild
needed — the script is already in the scar-agent image.

To add your own patterns, extend the PATTERNS list. Each tuple is:
  (regex, CWE-ID, severity, human-readable message)
"""
import json
import os
import re
import time
from pathlib import Path

PATTERNS = [
    (r'\bgets\s*\(',    'CWE-120', 'high',   'Unbounded gets() — no length limit'),
    (r'\bstrcpy\s*\(',  'CWE-120', 'high',   'Unbounded strcpy() — no length check'),
    (r'\bstrcat\s*\(',  'CWE-120', 'high',   'Unbounded strcat() — no length check'),
    (r'\bsprintf\s*\(', 'CWE-134', 'medium', 'Unbounded sprintf() — prefer snprintf'),
    (r'\bscanf\s*\(',   'CWE-120', 'medium', 'scanf() without field-width limit'),
]

sandbox  = os.environ.get('SANDBOX_SRC', '.')
ws       = os.environ.get('SCAR_WORKSPACE', '.')
findings = []

for c_file in sorted(Path(sandbox).rglob('*.c')):
    try:
        lines = c_file.read_text(errors='replace').splitlines()
    except OSError:
        continue
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # Skip comment-only lines so labels in smoke-test files don't inflate results.
        if stripped.startswith('//') or stripped.startswith('*'):
            continue
        for pat, cwe, severity, msg in PATTERNS:
            if re.search(pat, line):
                findings.append({
                    'rule_id':   cwe,
                    'severity':  severity,
                    'file_path': str(c_file.relative_to(sandbox)),
                    'line':      i,
                    'column':    0,
                    'message':   f'{msg}: {stripped}',
                })
                break  # one finding per line

ts  = int(time.time())
out = Path(ws) / '.scar' / f'findings-grep-scan-{ts}.json'
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(findings, indent=2))
print(f'[grep-scan] {len(findings)} finding(s) -> {out}', flush=True)
