# OCR Card Scanner

Look up Magic: The Gathering card information and track a collection, backed by a
local cache so the card API is only queried for cards it hasn't seen before.

## How it works

```
          search / browse
                |
                v
        +----------------+      hit       +------------------+
        |  web UI (Jinja | -------------> |  SQLite (local)  |
        |   + HTMX)      | <------------- |  primary read    |
        +----------------+                +------------------+
                |                                  ^
                | miss / stale price               | write-back
                v                                  |
        +----------------+                         |
        |  Scryfall API  | ------------------------+
        +----------------+
```

- **SQLite is the primary read.** Every lookup checks the local database first.
- **Scryfall is queried only on a cache miss**, or when a stored price is older
  than the freshness window (`PRICE_TTL_DAYS`, default 7 days). The result is
  written back so the next read is local.
- **Static card data** (name, type, text, image) is effectively immutable and
  cached indefinitely. **Prices** are refreshed on the TTL and by a manual
  "refresh" action. Scryfall itself only re-prices about once a day.
- One row per **printing**, keyed by the Scryfall card UUID. Because OCR usually
  only yields a card *name*, lookups return a ranked list of printings and the
  user picks the exact one; only the picked card is stored.

## Status

| Phase | Scope | State |
|------:|-------|-------|
| 1 | Local data layer (SQLite, network-free) | **done** |
| 2 | Scryfall API client | planned |
| 3 | Cache-aside service (miss -> fetch -> write-back) + ranking | planned |
| 4 | Web UI: search + browse (FastAPI, Jinja, HTMX) | planned |
| 5 | Collection UI + weekly price refresh | planned |
| 6 | Point the OCR script at the local API + accuracy tuning | planned |

Phase-by-phase detail lives in `ROADMAP.md` (local, not committed).

## Project layout

```
app/
  config.py          config knobs (DB path, price TTL, Scryfall settings)
  db.py              SQLite connection helpers + schema
  store/             data access - plain SQL, no network
    cards.py           the `cards` table
    prices.py          the `card_prices` table
    collection.py      the `collection` table
scripts/
  fake_data.py       fill data/cards.db with sample cards; stands in for
                     Scryfall as the data source until Phase 2
tests/
  test_store.py      round-trip tests for the data layer
data/
  cards.db           the local database (created on first run, git-ignored)
```

`store/` is a package that re-exports every function, so `from app import store`
then `store.get_card(...)`, `store.list_collection(...)` etc. all work; the
submodules (`store.cards`, `store.prices`, `store.collection`) can also be
imported directly.

## Setup

Requires Python 3.11+. The app code is pure standard library so far, so the only
dependency is `pytest`, for the test suite:

```bash
python -m pip install -r requirements.txt
```

Later phases add their own dependencies (FastAPI, httpx, rapidfuzz, ...) - those
get added to `requirements.txt` as each phase lands. See `ROADMAP.md`.

## Usage

Build a sample database and print a valuation:

```bash
python -m scripts.fake_data
```

```
Seeded 5 cards into .../data/cards.db

Collection:
   1x Lightning Bolt   2x2  foil        $3.20
   4x Llanowar Elves   dom  nonfoil     $0.80
   2x Sol Ring         cmr  nonfoil     $2.58
   1x The One Ring     ltr  etched    $120.00

Collection value: $126.58
Stale price rows (TTL 7d): 1 -> ['33333333-3333-4333-8333-333333333333']
```

Run the tests:

```bash
python -m pytest
```

Use the data layer directly:

```python
from app import store, config

card = store.get_card("55555555-5555-4555-8555-555555555555")
prices = store.get_prices(card["id"])
age = store.price_age_days(card["id"])          # days since we last refreshed
owned = store.list_collection()                 # rows + grand total
stale = store.stale_card_ids(config.PRICE_TTL_DAYS, only_collection=True)
```

## Data model

**`cards`** - one row per printing (Scryfall UUID `id`), plus `oracle_id`,
`name`, `set_code`, `set_name`, `collector_number`, `rarity`, `type_line`,
`mana_cost`, `oracle_text`, `image_uri`, `lang`, `released_at`, the full
`raw_json`, and `fetched_at` (first cached).

**`card_prices`** - `card_id`, `usd`, `usd_foil`, `usd_etched`, `eur`, and
`prices_updated_at` - the time *this app* last refreshed the price, which is what
the TTL checks.

**`collection`** - `card_id`, `quantity`, `finish` (`nonfoil` / `foil` /
`etched`), `condition`, `notes`, `added_at`, unique on
`(card_id, finish, condition)`. `list_collection()` values each row against the
price column matching its finish and skips rows with an unknown price in the
total.

## OCR

OCR is a separate step from this app. The original `test_EasyOCR.py` /
`test_API.py` spike scripts were removed (they were hand-run experiments, not
tests). Phase 6 adds a `scan.py` that OCRs an image and queries this app's local
API instead of calling Scryfall directly.
