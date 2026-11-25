#!/usr/bin/env python3
"""Kopiér server/static/hub.html ind i raspberry-pi/positionslogger_blueprint.py (HUB_TEMPLATE).

Kør efter hver ændring i hub.html:   python nest-hub/sync_hub_page.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
html = (ROOT / "server" / "static" / "hub.html").read_text(encoding="utf-8")
bp = ROOT / "raspberry-pi" / "positionslogger_blueprint.py"
src = bp.read_text(encoding="utf-8")

if '"""' in html or html.endswith("\\"):
    raise SystemExit('hub.html må ikke indeholde """ eller slutte med \\')
new, n = re.subn(r'HUB_TEMPLATE = r""".*?"""', lambda _: f'HUB_TEMPLATE = r"""{html}"""', src, flags=re.S)
if n != 1:
    raise SystemExit("fandt ikke HUB_TEMPLATE i blueprinten")
if new != src:
    bp.write_text(new, encoding="utf-8")
    print("opdateret", bp.relative_to(ROOT))
else:
    print("allerede ens")
