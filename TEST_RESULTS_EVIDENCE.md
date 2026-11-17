# Capstone Test Results: Evidence Handover

Project: automated trading-card scanner. Raspberry Pi 4 + Pi Camera Module 3 + EasyOCR + local SQLite + Scryfall API + browser inventory UI.
Purpose of this file: evidence and honest findings for 4 slides (Tests 2.2-2.5) of the Test Results oral report, **due Nov 5, 2026**. Every test result must be backed by evidence (screenshots, logs, graphs, calculations).
Last updated: 2026-11-04. Branch `Surya_dev`.

Status keys: **DONE** = verified with evidence. **PENDING** = not captured yet. **NEEDS PI** = requires the Pi-side script or hardware, which is not in this repo.

---

## 1. Handover summary (start here)

| Test | Status | What is left |
|---|---|---|
| 2.2 Database and API | **DONE** (PC re-run, 11/4) | Optional: a Pi version of the same three cases (NEEDS PI). |
| 2.3 Inventory Management | **DONE** (PC re-run, 11/4; sorting built 11/4) | Optional: a clicked-sort-by-name picture. |
| 2.4 Processing Speed | **PENDING, NEEDS PI** | Per-stage timing, `free -h`/htop during OCR, a fresh 10-card run, final cards/min. Nothing measured yet. |
| 2.5 Card Feeding | **PENDING, NEEDS PI** | Feeder code, cycle log or tally, motor/feed settings, feeder photo. Nothing in this repo. |
| Final deliverables | 2.2 and 2.3 written (section 8). 2.4 and 2.5 placeholders. | Fill in after the Pi work. |

### What the next session must ask the user first
1. **Pi access.** The user said (11/4) they **can** access the Pi. Get the Pi's capture-and-OCR script (the one that POSTs to `/api/scan`): paste it, or save it in the repo folder and give the path. Is it the same as `scripts/scan.py` here, or its own file? (Neither `test_API.py` nor `test_EasyOCR.py` is it: those are early PC prototypes from Sept, in git history only, reading `~/Downloads/magic.jpg`.)
2. **Time.** Report is due Nov 5. A 10-card run takes about 7 minutes at the user's reported ~40 s/card; a 30-card run about 20-25 minutes.
3. **Is the Pi on wifi or Ethernet?** (Matters for any offline test on the Pi: do NOT run `ip link set wlan0 down` over an SSH session on wifi. On a keyboard/monitor, `sudo nmcli radio wifi off` then `on` is safe.)

### How the user wants to work (follow this)
- **One test at a time, one step at a time.** Wait for the user to say "done" or paste output before moving on.
- **Do NOT modify code, DB, or config unless the user explicitly asks.** Read-only exploration first. If a command could change data (e.g. scanning writes to the inventory), say so before the user runs it.
- Give exact file paths with line numbers, exact commands, and what the user should see.
- **Pictures must be really easy to understand:** either one line for the slide or a browser picture. For each screenshot give: what to open/run, exactly what should be visible, a filename (format `test2-2_01_db_before.png`), and a checklist (large font, crop tight, no personal info or API keys). The user takes screenshots (Win+Shift+S); Windows saves them to `C:\Users\Owner\OneDrive\Pictures\Screenshots\` where Claude can read them. **Crop out the browser tab bar and bookmarks bar** (they show personal tabs).
- **If code contradicts a slide, say so plainly and say which version to trust.** Do not smooth over mismatches.
- When a log or timing number is needed, show how to produce it from the code or existing logs, not estimate.
- Final tasks when all four are done: (1) table: slide / claim / verified in code (yes/no/partly) / evidence files / slide text to change; (2) 3 likely instructor questions per test with short honest answers grounded in the code; (3) flag anything unbacked so wording can be adjusted instead of overclaiming.

---

## 2. Ground rules (decisions made in the working session)

1. **Dates matter.** Two things were added AFTER the original tests: sorting by name/rarity/price (built 2026-11-04; the 10/6 test ran on code with no sorting, and the Pi did not sort either) and the `ScryfallError` handling in `app/scans.py` (2026-11-04; the 10/1 test predates it). Never present either as something that passed on the original test date. Say "added and verified 11/4".
2. **The 10/8 result stays as recorded.** 13.5 min for 10 cards = 810 s = 81 s/card = 0.74 cards/min, FAIL against the 1 card/min minimum (ideal 3). The test sheet says 74 s/card (about 0.81 cards/min); the plan called for 30 cards. **No log from that run exists** (user confirmed 11/4), so 74 vs 81 is unresolved. A fresh re-run goes in as a SEPARATE dated row, never over the original.
3. **Causes are "not measured yet" until measured on the Pi.** The earlier git-history lead (the PC script `scripts/scan.py` loaded the model per card before 10/13) is NOT applicable to the Pi: the Pi runs its own script, which loads the model once (received 11/5, section 6). The user reports about **40 s/card now**; why it was 81 s on 10/8 is unexplained.
4. **Label what each piece of evidence is.** Everything in `evidence/` was produced on a PC against a COPY of the dev DB (`data/evidence_copy.db`) on 2026-11-04. It is a re-run, not the original Pi runs of 10/1, 10/6, 10/8, 10/16. It proves the code behaves that way; it does not prove what happened on those days.
5. **No estimating.** If a number or claim can't be backed by a log, screenshot, or calculation, reword the slide down ("recorded on the test sheet", "not measured yet").
6. **Never change data silently.** The harness works on a copy; the real `data/cards.db` was verified unchanged (MD5) after each run set. Do not use `rm -f $VAR/*` patterns: a safety check blocks them (nothing was deleted in this session).

---

## 3. Repo map (this repo = PC/web half only)

The Pi-side code is NOT in this repo: no camera capture, no GPIO/motor/feeder code, no scan-trigger button handler, no Pi OCR script. Anything depending on it is NEEDS PI.

| Stage | File / function |
|---|---|
| Camera capture | NEEDS PI (not in repo) |
| Preprocessing (offline photo path) | scripts/scan.py:75 `_load_image` (resize to 1200 px wide) |
| EasyOCR | scripts/scan.py:66 `_get_reader` (Reader created once per process, `gpu=False`); scripts/scan.py:93 `_read_all`. Pi's own OCR code: NEEDS PI. Comment at scripts/scan.py:54-57 says ~5-6 s per photo on a PC CPU: NOT a Pi measurement. |
| Receive scan from Pi | app/main.py `POST /api/scan` -> app/scans.py `record` |
| Parse OCR into a card | app/scans.py `identify`, `find_set_and_number`, `pick_name_box`, `clean_name` |
| Local DB lookup | app/service.py:85 `get_card` (cache-aside); SQL in app/store/cards.py |
| Scryfall call | app/scryfall.py:69 `_request` (min 0.1 s between requests, 3 retries, backoff 0.5/1/2 s, raises `ScryfallOffline`); service.search app/service.py:15 |
| Inventory update | app/service.py:116 `add_owned` -> app/store/collection.py:12 `add_to_collection` (UPSERT; UNIQUE(card_id, finish, condition) at app/db.py:51) |
| Inventory UI + sorting | app/main.py `GET /collection`; app/store/collection.py `list_collection`, `sort_rows`; templates/_collection.html |
| Live scan view | app/main.py `GET /scan`, templates/_scan.html |
| Per-scan timing | Only the Pi-supplied `elapsed_seconds` field (app/main.py `ScanPayload`), stored by scans.record and shown on /scan. No other timing or logging code exists. |

The real dev DB `data/cards.db` (dated Oct 29, gitignored) is NOT the Pi's DB from the test runs: 9 cards, 4 collection rows (Sheltered Thicket, Seedship Impact, Sol Ring, Hullbreaker Horror x3).

### Mismatches found between slides and code (summary)
1. **2.2:** "matched against the local DB first" is not how scanning works. `service.search()` always calls Scryfall and never reads or writes `cards`. Only `get_card(card_id)` is cache-aside, and it runs when a card is viewed or added. `/api/scan` caches nothing.
2. **2.2:** there is no "unidentified" flag. Offline scan -> `offline=True`, empty candidates, page says "You're offline". A non-offline miss -> "couldn't identify a card".
3. **2.3:** sorting did not exist at test time (only `ORDER BY c.name, col.finish`). Built 11/4.
4. **2.4:** no timing instrumentation exists; 74 s vs 81 s unresolved.
5. **2.5:** nothing in this repo supports 100 cycles / 85 successes.

---

## 4. Test 2.2 Database and API Test (tested 10/1/26) : DONE (PC re-run)

**Slide claim:** a scanned card is matched against the local SQLite DB first; if not found, Scryfall is queried and the result stored locally; if the API fails, the card is flagged unidentified without crashing. Slide table: Swamp (in DB) expected "retrieved from local DB", actual "database card count added", Pass. Dig Through Time (not in DB) expected "API request sent, info returned", actual "new card added to DB", Pass. Consign (Pi offline) expected "no crash, card flagged unidentified", actual "threw an error", marked Pass.

### Code path
- Local lookup, app/service.py:98-103: `card = store.get_card(card_id)`; if None -> `data = scryfall.get_card(card_id)`; `store.upsert_card(data)`; `store.upsert_prices(data)`.
- Scryfall request: app/scryfall.py:69-89 `_request` (3 retries; connection failure -> `ScryfallOffline`).
- DB insert: app/store/cards.py:53-58 `INSERT INTO cards ... ON CONFLICT(id) DO UPDATE`.
- Error handling on scan: app/scans.py `record` catches `ScryfallError` (offline is a subclass) and returns empty candidates; templates/_scan.html shows "You're offline - connect to wifi to look up scanned cards." for offline, "couldn't identify a card" otherwise.

### Code fix made 11/4 (committed 3f694fd, NOT part of the 10/1 test)
Before: `record` caught only `ScryfallOffline`, so a persistent Scryfall 429/503 raised `ScryfallError` -> HTTP 500 to the Pi. Now:
```diff
-from .scryfall import ScryfallOffline
+from .scryfall import ScryfallError, ScryfallOffline
-    except ScryfallOffline:
+    except ScryfallError as exc:   # ScryfallOffline is a subclass
-        offline = True
+        offline = isinstance(exc, ScryfallOffline)
```
New test `test_api_scan_scryfall_error_is_flagged_not_a_500` failed before the fix and passes after. Full suite: **101 passed**.

### Results (PC re-run on a copy of the dev DB, 2026-11-04)

**Case 1, Swamp (already cached).** Run 1 primes the cache (cards 9 -> 10, collection 4 -> 5, log shows `GET /cards/b233b117-...`). Run 2 is the evidence: quantity 1 -> 2, cards total stays 10, collection rows stay 5, and the log shows only `cards/named` and `cards/search`, **no `GET /cards/<uuid>`**. So the card record came from SQLite. Caveat: the name search still went to Scryfall. The slide's "database card count added" actually matches the collection quantity increment (1 -> 2), not the cards table (unchanged at 10).

**Case 2, Dig Through Time (not cached).** `scan` (simulated Pi POST): `/api/scan -> 200 {'candidates': 10, 'offline': False}`, cards stay 10 -> 10 (a scan stores nothing). `add`: log shows `GET https://api.scryfall.com/cards/66608ea9-d9d2-4fda-9c96-7ce132ee5da9` 200 OK, new `cards` row (Dig Through Time, slz #258), cards 10 -> 11, collection 5 -> 6.

**Case 3, Consign (offline).** `ping api.scryfall.com` fails (host not found). `POST /api/scan` -> 200 `{'id': 1, 'exact': False, 'offline': True, 'candidates': 0}`, no traceback, nothing stored (cards 11, collection 6). The Scan page shows the red "You're offline" notice (scan #1 at 16:56:20 UTC). Online control for Consign at 11:37: 200, `offline: False`, 3 candidates.
- Verdict: "no crash, flagged" is met. The slide's "actual: threw an error" is **not supported**: not reproduced anywhere; source unknown (could be the Pi script, `scripts/scan.py`'s RuntimeError at line 101, or `/api/search` returning 503). It is flagged as offline, not "unidentified"; the scan lives in memory only (`scans._latest`) and is lost on restart.

### Evidence files (Test 2.2)
Screenshots (`evidence/`; `*.png` is gitignored so they are local only):
- `test2-2_A_swamp_qty2.png`: Collection page, Swamp qty 2.
- `test2-2_B_dig_saved.png`: Collection page, Dig Through Time (SLZ #258, qty 1) and Swamp (MDD #296, qty 2).
- `test2-2_E_dig_card_detail_crop.png`: Dig Through Time detail page (name, SLZ 0258 EN, rarity U, Delve text, Scryfall image). Cropped to remove browser tabs/bookmarks.
- `test2-2_06_ping_fail.png`: ping fails (network down).
- `test2-2_07_api_scan_offline_true.png`: `offline = True, candidates = 0`.
- `test2-2_C_offline_notice.png`: the Scan page offline notice.
- `test2-2_05_offline_scan.png`: harness output of the offline scan (backup).

Logs (`evidence/logs/`, real output from 2026-11-04 14:08 EST, true time order, header line gives command and time):
- `test2-2_01_swamp_run1_prime.txt`
- `test2-2_02_swamp_run2_cache_hit.txt` (**the key one**)
- `test2-2_03_dig_scan.txt`
- `test2-2_04_dig_add.txt`

Not used on purpose: a screenshot showing "Unable to connect to the remote server" (the web server was not running; a localhost error, not offline evidence), a VS Code screenshot with the chat sidebar and a run_ui.py error, and cut-off crops.

### Slide wording to change
- Swamp row: expected "card already cached; not re-fetched from Scryfall"; actual "quantity 1 -> 2; card total unchanged".
- Dig Through Time row: add "on add to collection" after "new card added to DB". Do not claim a scan stores the card.
- Consign row: actual "no error; scan answered offline: true; page shows 'You're offline'". Drop "threw an error" unless its source is found. Drop "unidentified" or explain it means "no card identified".
- Do not claim "scan checks local DB first": search always queries Scryfall.

---

## 5. Test 2.3 Inventory Management Test (tested 10/6/26) : DONE (PC re-run; sorting built 11/4)

**Slide claim:** the inventory adds new cards with correct name/set/rarity, increases quantity instead of creating a duplicate, records multiple different cards, and sorts by name, rarity, and price. All 6 cases passed.

### Finding (honest)
The code tested on 10/6 had **no sorting**: only a hardcoded `ORDER BY c.name, col.finish`, plain-text headers, no Rarity column. The Pi did not sort either (user confirmed). The user decided on 11/4 to build it. **Word the slide: "sorting gap found during evidence review; implemented and verified 11/4"**, not "passed on 10/6".

### What was built (commit 004774c)
- `app/store/collection.py`: `sort_rows(rows, sort, direction)` + `SORT_KEYS`. Name: case-insensitive. Rarity: by scarcity (common < uncommon < rare < mythic < special < bonus), not alphabet. Price: numeric `unit_price` (float; prices are REAL in the DB and go through `to_float`), so $29.72 sorts above $6.85. Missing price / unknown rarity always sort LAST in either direction. Bad `sort`/`dir` values fall back to name/asc.
- `app/main.py`: `GET /collection?sort=name|rarity|price&dir=asc|desc`, server-side. +/-/Remove/Refresh buttons carry the current sort (hidden fields + `hx-include`).
- `templates/_collection.html`: clickable headers Card, Rarity (new column), Unit with arrow; click again to reverse. `static/style.css`: link styling.
- `tests/test_main.py`: 6 new tests (numeric price + unknown last, rarity by scarcity, default name order, bad params, sort survives a quantity click, header links).

### Claim vs code
| Claim | Verified? |
|---|---|
| Add new card, correct name/set/rarity | Yes (name+set on collection page; rarity is a column since 11/4, was detail-page only) |
| Same card again -> quantity up, no duplicate | Yes: `UNIQUE(card_id, finish, condition)` + `ON CONFLICT ... quantity = quantity + excluded.quantity` (app/db.py:51, app/store/collection.py:33-42). Foil and nonfoil are separate rows by design. |
| Multiple different cards | Yes |
| Sort by name | Partly: it was the fixed default; now selectable. |
| Sort by price / rarity | Yes since 11/4, not on 10/6 |
| Price compared as a number; missing price handled | Yes (float compare; `None` last; excluded from total; counted in the "unknown price" stat) |

### Evidence files (Test 2.3)
Use the `_crop` versions on slides (the uncropped originals show personal browser tabs and bookmarks; keep them off slides).

**Sorted by price, highest first** (`evidence/test2-3_02_sorted_price_crop.png`, `/collection?sort=price&dir=desc`): $29.72 (Sol Ring), $6.85, $1.93, $0.26, then the three no-price cards ("n/a": Jace, Lightning Bolt, Swamp) last. $6.85 after $29.72 shows numeric comparison. Total check: 29.72 + 20.55 (3 x 6.85) + 1.93 + 0.26 = $52.46, matching the stat card. Also shows qty 3 and 2, and the Rarity column.

![Sorted by price](evidence/test2-3_02_sorted_price_crop.png)

**Sorted by rarity, ascending** (`evidence/test2-3_03_sorted_rarity_crop.png`, `/collection?sort=rarity&dir=asc`): Common (Swamp) -> Uncommon (Seedship Impact) -> Rare (Hullbreaker Horror, Lightning Bolt, Sheltered Thicket, Sol Ring, alphabetical among ties) -> Mythic (Jace). Scarcity order, not alphabetical.

![Sorted by rarity](evidence/test2-3_03_sorted_rarity_crop.png)

Quantity above 1 / no duplicate row: `test2-2_A_swamp_qty2.png`. Multiple different cards: `test2-2_B_dig_saved.png`.

Note: Lightning Bolt shows as Rare because the printing picked was a Secret Lair one; Jace's printing has no Scryfall price. Neither is a bug.

---

## 6. Test 2.4 Processing Speed Test (tested 10/8/26) : PENDING, NEEDS PI

**Slide claim (currently FAIL):** 10 cards processed in 13.5 minutes, about 81 s per card (0.74 cards/min) against a 1 card/min minimum (ideal 3). Original test sheet says 74 s per card; the plan called for 30 cards.

### Pi script received 2026-11-05 (saved as `evidence/pi/pi_scan_script_as_pasted_2026-11-05.py`, exact copy)
Path on the Pi: `/home/pi-model-b/` (env `ocr-env`). It is its own self-contained script, NOT `scripts/scan.py` from this repo (it does not import it). What it does, verified by reading it:
- **EasyOCR Reader is created ONCE at startup** (`reader = easyocr.Reader(["en"])` before the loop, prints "Loading OCR model once..."). No `gpu=` argument; the Pi 4 has no CUDA, so it runs on CPU. => The "model loaded per card" lead in ground rule 3 does NOT apply to this script. (It applied to the old PC `scripts/scan.py`, which the Pi does not use.)
- **Capture:** `rpicam-still --timeout 1000 --width 1920 --height 1080 --quality 85` per card, so at least ~1 s of fixed preview delay plus camera start-up is built into every card.
- **OCR runs on the top half only:** crop `[0:540, 0:1920]` of the 1920x1080 image (Y_END = 540). One `readtext` call on 1920x540.
- **Interactive:** it waits for a human to press Enter for every card (`input(...)`), so wall-clock time per card includes the person's time unless measured separately. No feeder/GPIO code is in this script either (2.5 code is still elsewhere).
- **The only timing it records** is `elapsed_seconds` = `reader.readtext(...)` only (OCR), printed as "OCR time" and POSTed to the PC. It does NOT include capture, load/crop, the POST, or the human wait.
- **The POST is synchronous** (`urlopen`, 30 s timeout): the Pi waits for the PC, and the PC does its Scryfall lookups (fuzzy match + search, each at least 0.1 s apart, with retries) before replying. So network/Scryfall time is inside the Pi's per-card wall time.
- **Footer is never sent:** because only the top half of the image is OCR'd, the footer (set code and collector number, at the bottom of the card) is not in the detections, so the PC's footer-first exact match (`find_set_and_number`) cannot fire from this script; every scan takes the title/name-search path. (Observation for 2.2/2.3 slides: scans from the Pi are never "exact" matches.)

Timing instrumentation prepared (NOT applied to the Pi; review first): `evidence/pi/pi_scan_timed.py` (drop-in copy of the script) and the diff `evidence/pi/pi_scan_timing.diff` (102 lines added, 6 removed). It times capture, load+crop, OCR, POST and total per card, records time between cards (human wait), MemAvailable and swap used just before OCR, appends a CSV row to `/home/pi-model-b/scan_timing.csv`, and prints a summary (avg per stage, cards/min with and without the human wait) when you enter `q`. OCR semantics and the payload sent to the PC are unchanged. Tested here with camera/OCR/network faked (3 simulated cards, CSV and summary correct); not yet run on the Pi.

### Footer-reading version (2026-11-10): `evidence/pi/pi_scan_footer.py` (NOT yet run on the Pi)
Why: a Pi photo sent by the user (Vampiric Tutor, CMR #656, foil, sleeved) had the **title cut off the top of the frame** while the footer (`656`, `CMR*EN`, `M`) was fully visible, so a title-only scan would read nothing useful. Scryfall `cards/cmr/656` returns Vampiric Tutor (mythic), the right card.
What it does: same as `pi_scan_timed.py` plus a second OCR band at the bottom of the photo (`FOOTER_BAND = (0.65, 1.0)`, fractions of photo height; footer text in that photo was ~77-81% down). Box y-coordinates are shifted to full-photo coordinates so the PC's existing footer-first logic (`find_set_and_number`) works unchanged. `READ_FOOTER = False` gives the old title-only behaviour so both modes can be timed. It also saves `footer_crop.jpg` so you can see what the camera gave OCR. CSV adds `mode`, `ocr_title_s`, `ocr_footer_s`.
Tested here with camera/OCR faked and simulated footer text from that photo: the PC found `('cmr', '656')`, `identify` returned `exact: True`, and searched set `cmr` number `656`. **Not tested:** whether real EasyOCR can read that small footer text on a foil, sleeved card at 1920x1080. That needs a real photo run (OCR on the PC from a copy of `magic_small.jpg`, or on the Pi). Expect footer OCR to roughly add the cost of the extra band to `ocr_s`.

**Real-photo test, 2026-11-10 (PC CPU, not Pi timing)** of `magic_small.jpg` (the Pi photo, Vampiric Tutor CMR #656, foil, sleeved), real EasyOCR through the PC's real pipeline:
- Title-only (what the Pi sent before): 1 empty detection, **0 candidates**.
- Footer band: number `656` read at **1.00**, but the set code `CMR*EN` at only **0.16** (the foil star confuses OCR). The PC threw away anything under 0.4, so the set was dropped -> `(None, '656')` -> fell back to a garbage name guess (`on top`, from the rules text) and returned wrong cards (Monument to Perfection, ...).
- **Fix made on the PC (app/scans.py, uncommitted at time of writing):** new `_MIN_SET_CONF = 0.1` used ONLY for the set-code lookup next to a confident number (the number itself still needs 0.4). Safe because set + number must resolve to exactly one printing on Scryfall; a wrong set guess finds nothing and falls back to the title. Result on the same photo: `find_set_and_number -> ('cmr', '656')`, `exact: True`, **Vampiric Tutor cmr 656 mythic**.
- 3 regression tests added to tests/test_scan.py using the real boxes from this photo (including that a set code at 0.05 is still ignored). Full suite: 104 passed.
- OCR timing on the PC for reference only: title band 3.5 s, footer band 2.6 s (model load 3.1 s). Pi timing still not measured.
- Still untested: the Pi's own capture feeding `pi_scan_footer.py` (the footer band logic was tested here on the saved photo, not on a live Pi run). Date: this fix is from 11/10, after all the original tests.

### Known facts
- Arithmetic: 13.5 min = 810 s; 810 / 10 = 81 s/card = 0.74 cards/min. FAIL against 1 card/min stands as recorded.
- **No log or timestamps from the 10/8 run exist** (user, 11/4). The 74 s on the test sheet cannot be reconciled with 81 s without a source. Ask the user which is written down and where; if it cannot be resolved, put both on the slide as "recorded" values.
- The user reports it now takes **about 40 s per card** (not yet measured/recorded). 40 s/card = 1.5 cards/min, which would meet the 1 card/min minimum. This must be shown as a separate, dated re-run row, never as a change to the 10/8 result.
- Git-history lead: SUPERSEDED. It concerned the PC script; the Pi's script loads the model once (see above). Do not present it as a cause. If the Pi script differed on 10/8, check the script's modified time on the Pi (`ls -l --time-style=full-iso /home/pi-model-b/*.py`) and `~/.bash_history`.
- Bottleneck: **NOT MEASURED YET.** Five to six seconds of OCR per photo (PC CPU comment) cannot by itself explain 40-81 s per card; the rest is unknown (capture, model load, Pi CPU speed, network/Scryfall, swap).

### To do on the Pi (the user's original asks)
a) Resolve the numbers (above).
b) Find where time is spent: show temporary timing instrumentation (`time.perf_counter` around image capture, preprocessing, EasyOCR `readtext`, DB lookup, API call, inventory update) **as a diff for the user to review, not applied**, or get the same numbers without editing code (`python -X importtime`, `cProfile`, existing logging, the `elapsed_seconds` the Pi already posts to `/scan`). Check specifically whether the EasyOCR Reader is created once at startup or re-created for every card; whether the model runs on CPU within the Pi's RAM (`free -h`, swap usage, whether the Pi swaps during a scan). Is the 30-second capture time in the Test 2.6 notes related?
c) Design a clean re-run, **10 cards** (about 7 min at ~40 s/card; 30 cards if time allows), with per-stage timing printed and saved to a file the user can screenshot or chart (CSV).
d) Bottleneck from real measurements only; otherwise write "not measured yet".

Evidence wanted: timing log or per-stage breakdown, a `free -h` or htop screenshot during OCR, and the final cards/min calculation.

---

## 7. Test 2.5 Card Feeding Test (tested 10/16/26) : PENDING, NEEDS PI

**Slide claim:** 100 feeding cycles, 85 successful single-card feeds, 85% against an 85% minimum, so Pass with zero margin. (The test sheet appendix is mislabeled "User Interface Test" but contains the feeding procedure and results.)

Nothing in this repo: no GPIO/motor code, no scan-trigger handler, no cycle log, no motor/feed settings. To do on the Pi (the user's original asks):
a) Find where the feeding mechanism is driven (GPIO or motor control, timing, scan-trigger button handler) and whether the code records success/failure per cycle, or whether 85/100 was counted by hand.
b) Find any log, CSV, or counter supporting 100 cycles and 85 successes. If none, say so, and propose a small logging approach plus a repeatable procedure **as a diff for review**, or help present it honestly as manually observed.
c) Check motor voltage and timing parameters in code against the test-plan findings (3 V motors, 2.0 V ideal bias, reservoir angle 40-45 degrees) and report whether code and plan disagree.
d) Note any link between the card-isolation problem found in Test 1.4 and the failures counted here.

Evidence wanted: a log or tally of cycles, a photo or short video of the feeder mid-cycle, and a screenshot of the motor/feed settings in code. If it was counted by hand, the slide should say "manually counted".

---

## 8. Final deliverables

Scope: 2.2 and 2.3 written from PC re-runs (11/4). 2.4 and 2.5 are placeholders until the Pi work.

### 8.1 Slide-by-slide table

| Slide / row | Claim | Verified in code? | Evidence files | Slide text to change |
|---|---|---|---|---|
| 2.2 Swamp (in local DB) | "retrieved from local DB" | **Partly.** The card record comes from SQLite (no `GET /cards/<id>` on the second add). The name search still goes to Scryfall every time. | `test2-2_A_swamp_qty2.png`; logs `test2-2_01_swamp_run1_prime.txt`, `test2-2_02_swamp_run2_cache_hit.txt` | Expected: "card already cached; not re-fetched". Actual: "quantity 1 -> 2; card total unchanged". |
| 2.2 Dig Through Time (not in DB) | "API request sent, info returned"; "new card added to DB" | **Yes, but** the card is stored when added or viewed, NOT when scanned. | `test2-2_B_dig_saved.png`, `test2-2_E_dig_card_detail_crop.png`; logs `test2-2_03_dig_scan.txt`, `test2-2_04_dig_add.txt` | Add "on add to collection". |
| 2.2 Consign (offline) | "no crash, card flagged unidentified" | **Yes** for no crash and flagged-as-offline. **No** for "threw an error" (not reproduced) and for "unidentified". | `test2-2_06_ping_fail.png`, `test2-2_07_api_scan_offline_true.png`, `test2-2_C_offline_notice.png`, `test2-2_05_offline_scan.png` | Actual: "no error; scan answered offline: true; page shows 'You're offline'". |
| 2.3 New card has correct name/set/rarity | adds correctly | **Yes** (rarity column since 11/4) | `test2-3_02_sorted_price_crop.png`, `test2-3_03_sorted_rarity_crop.png` | none |
| 2.3 Same card again -> quantity, no duplicate | quantity increases | **Yes** | `test2-2_A_swamp_qty2.png` | none |
| 2.3 Multiple different cards | recorded correctly | **Yes** | `test2-2_B_dig_saved.png`, `test2-3_02_sorted_price_crop.png` | none |
| 2.3 Sort by name | works | **Partly** (fixed default before 11/4, selectable now) | default order visible in `test2-2_B_dig_saved.png` | "Sort by name (default)" |
| 2.3 Sort by price | works | **Yes since 11/4, not 10/6** | `test2-3_02_sorted_price_crop.png` | "added and verified 11/4" |
| 2.3 Sort by rarity | works | **Yes since 11/4, not 10/6** | `test2-3_03_sorted_rarity_crop.png` | "added and verified 11/4" |
| 2.4 Processing speed | 10 cards / 13.5 min / 81 s per card / 0.74 cards/min / FAIL | **Not verifiable here.** Arithmetic checks; 74 vs 81 s unresolved. | none yet (NEEDS PI) | Keep as recorded; add a separate dated re-run row if measured. |
| 2.5 Card feeding | 85/100 cycles, 85% vs 85% minimum | **Not verifiable here.** | none (NEEDS PI) | Say "manually counted" unless a log exists. |

### 8.2 Likely instructor questions (2.2 and 2.3)

**Test 2.2 Database and API**
1. *Does a scan check the local database first?* No. Identifying a scanned card always queries Scryfall (`service.search`, app/service.py:41,53,57). The local DB caches the per-card record and prices, used when a card is viewed or added (`get_card`, app/service.py:98-103), and lets the app keep showing owned cards offline. On a repeat add, the card was not re-fetched; the log shows it.
2. *What happens when the Pi is offline?* `/api/scan` returns 200 with `offline: true`, no crash, and the Scan page shows "You're offline". Nothing is stored; the latest scan lives in memory only and is lost on restart. Owned and cached cards still display. A non-offline Scryfall failure (429/503 after retries) shows "couldn't identify a card" (fixed 11/4, commit 3f694fd; before that it was a 500).
3. *How do you avoid hammering Scryfall or creating duplicates?* The client waits at least 0.1 s between requests and retries 3 times with 0.5/1/2 s backoff (app/scryfall.py:15-17, 64-89). Caching uses `INSERT ... ON CONFLICT(id) DO UPDATE`, so re-caching never duplicates (app/store/cards.py:53-58).

**Test 2.3 Inventory**
1. *How do you stop duplicate rows when the same card is scanned twice?* `UNIQUE(card_id, finish, condition)` plus `ON CONFLICT ... DO UPDATE SET quantity = quantity + excluded.quantity` (app/db.py:51, app/store/collection.py:33-42). Foil and nonfoil are separate rows by design. Swamp went 1 -> 2 with the row count unchanged.
2. *Is sorting done in the browser, and are prices compared as text?* Server-side Python (`store.sort_rows`) via `/collection?sort=...&dir=...`. Prices are REAL, converted with `to_float`, compared as numbers ($29.72 above $6.85 in the screenshot). Be upfront that sorting was added 11/4 after the original test found no sort.
3. *What about cards with no price or unknown rarity?* They sort last in either direction, show "n/a", are left out of the total, and are counted in the "unknown price" stat (3 in the screenshot). Covered by tests; full suite 101 passed.

**Tests 2.4 and 2.5:** write after the Pi work.

### 8.3 Claims you cannot back (reword, or capture more evidence)
- That the 10/1, 10/6, 10/8 or 10/16 results themselves happened as the slides say: no logs from those runs exist; the PC re-runs prove the code, not those days.
- "Threw an error" on Consign: not reproduced anywhere. Source unknown.
- Sorting passing on 10/6: the code then had none.
- 74 s vs 81 s per card, and any cause of slowness: not measured.
- 85/100 feeding successes, motor voltage and reservoir-angle settings: not in this repo.
- Optional nice-to-have: a clicked-sort-by-name picture.

---

## 9. How to reproduce / tooling

Run from the repo root (`C:\Users\Owner\projects\OCR-Card-Scanner`). The harness works on `data/evidence_copy.db` (gitignored copy of `data/cards.db`):
```
python evidence/tools/t22_harness.py reset                 # fresh copy of data/cards.db
python evidence/tools/t22_harness.py add "Swamp"           # run twice: second run = cache hit
python evidence/tools/t22_harness.py scan "Dig Through Time"
python evidence/tools/t22_harness.py add "Dig Through Time"
python evidence/tools/t22_harness.py scan "Consign"        # network OFF -> offline: True
python evidence/tools/run_ui.py                            # UI on the copy: localhost:8000/collection and /scan
```
For clean log files use `python -u -W ignore ...` (unbuffered, so request lines appear in true time order when output is redirected).
Offline scan through the running UI (PowerShell): `Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/scan -ContentType "application/json" -Body '{"detections":[{"text":"Consign","confidence":0.9}]}'` then open http://localhost:8000/scan. Note: airplane mode does not block localhost, but the web server must be running.
Tests: `python -m pytest -q` -> 101 passed.

### Commits made this session (branch `Surya_dev`, NOT pushed)
- `004774c` Add sortable collection (name, rarity, price) with Rarity column
- `3f694fd` Don't 500 on a non-offline Scryfall failure during /api/scan
- `d5e5f12` Add evidence harness scripts for the capstone test report

Untracked (local only): this file, `evidence/*.png` (gitignored), `evidence/logs/*.txt`. Offer to commit the notes and logs if the user wants them in git.

### Evidence folder index
```
evidence/
  tools/    t22_harness.py, run_ui.py                 (committed d5e5f12)
  logs/     test2-2_01..04_*.txt                       (real run output, 2026-11-04)
  test2-2_A_swamp_qty2.png        test2-2_B_dig_saved.png        test2-2_E_dig_card_detail_crop.png
  test2-2_05_offline_scan.png     test2-2_06_ping_fail.png       test2-2_07_api_scan_offline_true.png
  test2-2_C_offline_notice.png
  test2-3_02_sorted_price_crop.png     test2-3_03_sorted_rarity_crop.png     (use these on slides)
  test2-3_02_sorted_price.png, test2-3_03_sorted_rarity.png   (UNCROPPED originals: show personal tabs, keep off slides)
```
