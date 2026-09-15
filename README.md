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
| 2 | Scryfall API client | **done** |
| 3 | Cache-aside service (miss -> fetch -> write-back) + ranking | **ranking done**, cache-aside pending |
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
  scryfall.py        Scryfall API client - the only module that hits the network
  service.py         ranks Scryfall's raw printings into a short "most likely" list
scripts/
  fake_data.py       fill data/cards.db with sample cards; a stand-in for
                     Scryfall as the data source until it's wired in (Phase 3)
  try_scryfall.py    manual sanity check against the real Scryfall API + ranking
tests/
  test_store.py      round-trip tests for the data layer
  test_scryfall.py   Scryfall client tests (HTTP fully mocked)
  test_service.py    ranking tests (scryfall calls mocked)
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

Try the Scryfall client against the real API (a couple of real, rate-limited requests - not part of the test suite):

```bash
python -m scripts.try_scryfall "sol ring"
```

## Scryfall client

`app/scryfall.py` is the only module that makes outbound HTTP calls. It knows
nothing about the database - it just returns plain dicts shaped like Scryfall's
card objects.

- `search_by_name(text)` - fuzzy-resolves text to a card, then returns every
  printing of it (falls back to a plain search if the fuzzy match misses).
- `get_card(card_id)` - fetch one card by id.
- `get_cards_collection(ids)` - batch fetch, chunked at Scryfall's 75-id limit.

No API key or account is needed - Scryfall is fully open. The client sends a
descriptive `User-Agent` (`config.USER_AGENT`, currently name+version only, no
personal contact info), waits at least `config.SCRYFALL_MIN_INTERVAL` between
requests, and retries 429/503 responses with backoff before giving up and
raising `ScryfallError`. `tests/test_scryfall.py` mocks all HTTP so the suite
never touches the network.

## Ranking (`app/service.py`)

A popular card can have 100+ printings, so `search_by_name` alone isn't
useful to show a user directly. `service.search(text, limit=10)` scores every
printing's name against `text` with `rapidfuzz`, sorts by (score, most recent
release), and returns a short list of small dicts (id, name, set, collector
number, rarity, image, price) - never writes to the database.

If there are fewer than `limit` real printings, exactly that many are returned
- results are never padded (verified: a 6-printing card returns 6, not 10).

**Known limitation:** when the fuzzy match succeeds, every returned printing
shares one name, so the similarity score ties across all of them - recency is
just a tiebreak, not a real relevance signal. A heavily-reprinted card's actual
printing can fall outside the top 10 if it's an old one. That's expected: a
name alone can't identify an exact printing. The real fix is a picker UI
(Phase 4, compare thumbnail + set against the card in hand) and, later, an
explicit set/collector-number input once OCR can read it off the card (Phase 6)
- `search()` has no such parameter yet. Note this is not "more words help":
tested live, `search_by_name("sol ring commander legends")` returns zero
results, since Scryfall's fuzzy-name endpoint isn't a general search and
chokes on a combined name+set string.

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
