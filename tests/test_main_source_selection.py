"""Tests for source_mode dispatch in main.py (Plan B Phase B5)."""

from __future__ import annotations

import queue

import pytest

from src.event_intelligence.config import EventIntelConfig
from src.event_intelligence.main import _build_producers
from src.event_intelligence.official_client import OfficialClient
from src.event_intelligence.truedata_client import TrueDataClient


def _cfg(mode: str) -> EventIntelConfig:
    return EventIntelConfig(
        truedata_api_key="",
        truedata_ws_url="",
        gemini_api_key="",
        source_mode=mode,
    )


def test_official_mode_picks_official_client():
    producers = _build_producers(_cfg("official"), queue.Queue())
    assert len(producers) == 1
    assert isinstance(producers[0], OfficialClient)


def test_truedata_mode_picks_truedata_client():
    producers = _build_producers(_cfg("truedata"), queue.Queue())
    assert len(producers) == 1
    assert isinstance(producers[0], TrueDataClient)


def test_dual_mode_picks_both():
    producers = _build_producers(_cfg("dual"), queue.Queue())
    assert len(producers) == 2
    types = {type(p).__name__ for p in producers}
    assert "OfficialClient" in types
    assert "TrueDataClient" in types


def test_unknown_mode_falls_back_to_official():
    producers = _build_producers(_cfg("invalid"), queue.Queue())
    assert len(producers) == 1
    assert isinstance(producers[0], OfficialClient)


def test_empty_mode_falls_back_to_official():
    producers = _build_producers(_cfg(""), queue.Queue())
    assert len(producers) == 1
    assert isinstance(producers[0], OfficialClient)


def test_mode_is_case_insensitive():
    for mode in ("OFFICIAL", "Official", "official"):
        producers = _build_producers(_cfg(mode), queue.Queue())
        assert isinstance(producers[0], OfficialClient)


def test_default_mode_is_official():
    """The default config (no env var set) must produce OfficialClient."""
    cfg = EventIntelConfig(
        truedata_api_key="",
        truedata_ws_url="",
        gemini_api_key="",
    )
    assert cfg.source_mode == "official"
    producers = _build_producers(cfg, queue.Queue())
    assert isinstance(producers[0], OfficialClient)
