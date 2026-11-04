"""Run the real web UI on the COPY of the DB made by t22_harness.py (data/evidence_copy.db).
The real data/cards.db is never touched.  Run from anywhere:

    python evidence/tools/run_ui.py   ->  http://localhost:8000/collection   and   /scan

Run `python evidence/tools/t22_harness.py reset` first if the copy does not exist yet.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COPY = REPO / "data" / "evidence_copy.db"
sys.path.insert(0, str(REPO))

if not COPY.exists():
    sys.exit("No copy yet - run: python evidence/tools/t22_harness.py reset")

from app import config
config.DB_PATH = COPY

import uvicorn
from app.main import app

uvicorn.run(app, host="127.0.0.1", port=8000)
