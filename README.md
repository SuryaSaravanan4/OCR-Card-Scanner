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

**Deployment target:** intended to run on a Raspberry Pi that may not always
be on wifi. Viewing the UI itself never needs a network (it's served locally);
only a genuinely new lookup or a price refresh needs the internet. See
"Working offline" below for exactly what does and doesn't require connectivity.

## Status

| Phase | Scope | State |
|------:|-------|-------|
| 1 | Local data layer (SQLite, network-free) | **done** |
| 2 | Scryfall API client | **done** |
| 3 | Cache-aside service (miss -> fetch -> write-back) + ranking | **done** |
| 4 | Web UI: search + browse (FastAPI, Jinja, HTMX) | **done** |
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
  main.py            FastAPI app + routes - the browse/search UI
templates/           Jinja templates rendered by app/main.py
static/              style.css, served at /static
scripts/
  fake_data.py       fill data/cards.db with sample cards; a stand-in for
                     Scryfall as the data source now that fake_data isn't the
                     only way in (kept for tests/demos)
  try_scryfall.py    manual sanity check against the real Scryfall API + ranking
tests/
  conftest.py        shared `db` fixture (throwaway temp SQLite per test)
  test_store.py      round-trip tests for the data layer
  test_scryfall.py   Scryfall client tests (HTTP fully mocked)
  test_service.py    ranking + cache-aside tests (scryfall calls mocked)
data/
  cards.db           the local database (created on first run, git-ignored)
```

`store/` is a package that re-exports every function, so `from app import store`
then `store.get_card(...)`, `store.list_collection(...)` etc. all work; the
submodules (`store.cards`, `store.prices`, `store.collection`) can also be
imported directly.

## Setup

Requires Python 3.11+.

```bash
python -m pip install -r requirements.txt
```

Current dependencies: `pytest` (test suite), `httpx` (Scryfall client),
`rapidfuzz` (ranking), `fastapi` + `uvicorn[standard]` + `jinja2` +
`python-multipart` (web UI).

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

## Cache-aside service (`app/service.py`)

This is the seam between the Scryfall client and the database - routes and
templates call these functions and never know whether a read came from
SQLite or Scryfall.

- `get_card(card_id)` - serves from SQLite if cached and the price is still
  within `PRICE_TTL_DAYS`; otherwise fetches from Scryfall and writes back
  (a miss writes both the card and its price; a stale hit re-fetches and
  rewrites only the price). Returns the card merged with its current prices.
- `add_owned(card_id, quantity, finish, condition)` - calls `get_card` first
  so the card is always cached before being recorded as owned, then adds it
  to the collection.
- `refresh_prices()` - the weekly job. Refetches prices for every stale card
  that's actually in the collection (via Scryfall's batch endpoint) and
  returns how many were refreshed. Never touches uncached or unowned cards.

Verified against the real API: a fresh card id triggers exactly one Scryfall
call and gets cached; reading it again makes none.

### Working offline

Browsing already-cached data (your collection, a card you've opened before,
even a stale price) needs no network at all. `scryfall.py` raises a distinct
`ScryfallOffline` when it can't reach the network (as opposed to a normal
`ScryfallError`, which means Scryfall responded but rejected the request).
`service.get_card` treats those two cases differently:

- **Cache miss + offline** - nothing to fall back to, so `ScryfallOffline`
  propagates. This is the future UI's cue to prompt "connect to wifi" instead
  of a generic error (not built yet - see ROADMAP.md Phase 4).
- **Stale price + offline** - the card is already cached, so the stale price
  refresh is skipped silently and the last-known data is served. No error.

Verified against the live cache: an already-seen card served correctly with
`scryfall.get_card` forced to simulate offline, while a never-seen id raised
`ScryfallOffline` as expected.

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

## Web UI (`app/main.py`, `templates/`)

Server-rendered Jinja + HTMX, no build step or JS bundler - just FastAPI
rendering templates. Run it with:

```bash
python -m uvicorn app.main:app --reload
```

Then open `http://localhost:8000`. Every endpoint can also be poked directly
at `http://localhost:8000/docs`.

| Method + path | Does |
|---|---|
| `GET /` | search box |
| `GET /search?q=` | ranks candidates via `service.search`; returns just the results fragment for an HTMX request (`HX-Request` header), or the full page otherwise |
| `GET /cards/{id}` | card detail + "add to collection" form, via `service.get_card` |
| `GET /collection` | the owned-cards table + grand total |
| `POST /collection` | add a card (`card_id, finish, quantity, condition`) via `service.add_owned` |
| `POST /collection/{id}` | set a row's quantity (`store.set_quantity`; below 1 deletes it) |
| `POST /collection/{id}/delete` | remove a row |
| `POST /refresh-prices` | run `service.refresh_prices()`, redirect back with a "refreshed N" flash |
| `GET /api/search?q=` | JSON variant of `/search`, for Phase 6's OCR script |

Only `/search` uses HTMX (`hx-get` + `hx-target="#results"`) for
search-as-you-type; the collection forms are plain HTML forms (no JS
required) since per-row swap polish is Phase 5.

**Offline handling:** `/search` and `/cards/{id}` are the two places a lookup
can genuinely fail with no network (a cache miss has nothing to fall back
to) - they catch `scryfall.ScryfallOffline` specifically and render a "you're
offline" notice instead of a stack trace. Everything already cached
(`/collection`, a previously-opened card, a stale-but-cached price) needs no
network and renders normally regardless of connectivity, same as
`service.get_card` already handles - see "Working offline" above.

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
