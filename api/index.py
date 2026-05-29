import sys
import os

# Resolve project root absolutely so Vercel's sandbox can find the `app` package
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from app.main import app  # noqa: F401 — Vercel expects `app`

