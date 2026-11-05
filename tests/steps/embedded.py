"""Python aus einem Schritt-Template (Heredoc <<'PY' … PY im bash-Skript) für Tests.

Die Schritte im Build-Job bringen ihr Python inline mit: Ein Checkout von seed-pipelines im
Build-Job verschöbe den Checkout des Projekts nach s/<repo>. So testet die CI genau den Code, den
die Pipeline ausführt, ohne zweite Kopie. Nur Standardbibliothek.
"""

import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def source(template: str, marker: str) -> str:
    """Das Python ab der Zeile # <marker> bis zur Zeile PY, ohne die Einrückung des YAML-Blocks."""
    lines = (ROOT / template).read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == f"# {marker}"), None)
    if start is None or "<<'PY'" not in lines[start - 1]:
        raise LookupError(f"{template}: kein Heredoc <<'PY' mit # {marker}")
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = []
    for line in lines[start:]:
        if line.strip() == "PY":
            break
        body.append(line[indent:] if line.strip() else "")
    return "\n".join(body) + "\n"


def load(template: str, marker: str) -> types.ModuleType:
    """Dasselbe als Modul; der Block unter __main__ läuft dabei nicht."""
    module = types.ModuleType(f"seed_{marker.replace('-', '_')}")
    module.__file__ = str(ROOT / template)
    exec(compile(source(template, marker), f"{template} ({marker})", "exec"), module.__dict__)
    return module
