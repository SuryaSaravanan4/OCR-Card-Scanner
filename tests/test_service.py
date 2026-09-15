"""Tests for the ranking layer. scryfall.search_by_name is monkeypatched so
these never touch the network.
"""
from __future__ import annotations

from app import service


def _card(id, name, released_at="2020-01-01", **overrides):
    data = {"id": id, "name": name, "released_at": released_at, "prices": {"usd": "1.00"}}
    data.update(overrides)
    return data


def test_ties_break_by_most_recent_release(monkeypatch):
    # Same name (a real fuzzy match) -> identical similarity score -> recency decides.
    older = _card("old", "Sol Ring", released_at="2010-01-01")
    newer = _card("new", "Sol Ring", released_at="2023-01-01")
    monkeypatch.setattr(service.scryfall, "search_by_name", lambda text: [older, newer])

    results = service.search("sol ring")
    assert [c["id"] for c in results] == ["new", "old"]


def test_closer_name_match_ranks_first(monkeypatch):
    exact = _card("exact", "Lightning Bolt")
    unrelated = _card("far", "Lightning Strike")
    monkeypatch.setattr(service.scryfall, "search_by_name", lambda text: [unrelated, exact])

    results = service.search("Lightning Bolt")
    assert results[0]["id"] == "exact"


def test_respects_limit(monkeypatch):
    cards = [_card(str(i), "Sol Ring", released_at=f"20{i:02d}-01-01") for i in range(20)]
    monkeypatch.setattr(service.scryfall, "search_by_name", lambda text: cards)

    results = service.search("sol ring", limit=5)
    assert len(results) == 5


def test_does_not_pad_when_fewer_than_limit(monkeypatch):
    # Only 3 real printings exist - the result must not be padded up to `limit`.
    cards = [_card(str(i), "Aeve, Progenitor Ooze") for i in range(3)]
    monkeypatch.setattr(service.scryfall, "search_by_name", lambda text: cards)

    results = service.search("aeve", limit=10)
    assert len(results) == 3


def test_candidate_shape(monkeypatch):
    card = _card(
        "abc", "Sol Ring", set="cmr", set_name="Commander Legends",
        collector_number="472", rarity="uncommon",
        image_uris={"normal": "https://example.test/sol.jpg"},
    )
    monkeypatch.setattr(service.scryfall, "search_by_name", lambda text: [card])

    result = service.search("sol ring")[0]
    assert result == {
        "id": "abc",
        "name": "Sol Ring",
        "set_code": "cmr",
        "set_name": "Commander Legends",
        "collector_number": "472",
        "rarity": "uncommon",
        "released_at": "2020-01-01",
        "image_uri": "https://example.test/sol.jpg",
        "price_usd": "1.00",
    }


def test_empty_search_returns_empty_list(monkeypatch):
    monkeypatch.setattr(service.scryfall, "search_by_name", lambda text: [])
    assert service.search("gibberish") == []
