"""Test 2.2 / 2.3 evidence harness. Edits no app file.

Works on a COPY of data/cards.db (data/evidence_copy.db, gitignored), so the real DB is never touched.
Logs every outbound Scryfall request with a timestamp (httpx INFO logs).
Run from anywhere:  python evidence/tools/t22_harness.py <command> ["Card Name"]

  counts "Swamp"            # DB rows for a name + totals
  add "Swamp"               # search Scryfall, add top hit via service.add_owned
  scan "Dig Through Time"   # simulate Pi POST /api/scan with a title detection
  reset                     # re-copy the DB from data/cards.db (undo everything)
"""
import logging, shutil, sqlite3, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORK = REPO / "data" / "evidence_copy.db"
sys.path.insert(0, str(REPO))

if len(sys.argv) < 2:
    sys.exit(__doc__)

if sys.argv[1] == "reset" or not WORK.exists():
    shutil.copy(REPO / "data" / "cards.db", WORK)
    print(f"[harness] copied data/cards.db -> {WORK}")
    if sys.argv[1] == "reset":
        sys.exit()

from app import config
config.DB_PATH = WORK  # app.db reads config.DB_PATH at connect time

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.INFO)

from app import db, service, store
db.init_db()


def counts(name):
    c = sqlite3.connect(WORK)
    print("cards rows matching:", c.execute("select id,name,set_code,collector_number,fetched_at from cards where name=?", (name,)).fetchall())
    print("collection rows    :", c.execute("select col.id,c.name,col.quantity,col.finish from collection col join cards c on c.id=col.card_id where c.name=?", (name,)).fetchall())
    print("TOTAL cards =", c.execute("select count(*) from cards").fetchone()[0],
          "| TOTAL collection rows =", c.execute("select count(*) from collection").fetchone()[0])


cmd, name = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
if cmd == "counts":
    counts(name)
elif cmd == "add":
    print("--- BEFORE ---"); counts(name)
    top = service.search(name)[0]
    print(f"[harness] chose {top['name']} {top['set_code']} #{top['collector_number']} id={top['id']}")
    service.add_owned(top["id"])
    print("--- AFTER ---"); counts(name)
elif cmd == "scan":
    from fastapi.testclient import TestClient
    from app import main
    print("--- BEFORE ---"); counts(name)
    r = TestClient(main.app).post("/api/scan", json={"detections": [{"text": name, "confidence": 0.9}], "elapsed_seconds": 0})
    print("[harness] /api/scan ->", r.status_code, r.json())
    print("--- AFTER ---"); counts(name)
else:
    sys.exit(__doc__)
