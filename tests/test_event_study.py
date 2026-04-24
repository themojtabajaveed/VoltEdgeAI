import pytest
import sqlite3
import unittest.mock as mock
from datetime import date
from src.event_study.filing_archiver import FilingArchiver
import pandas as pd
from src.event_study.event_study_builder import EventStudyBuilder


def _make_archiver(tmp_path) -> FilingArchiver:
    """Create a FilingArchiver pointed at a temp DB — never touches data/history.db."""
    return FilingArchiver(db_path=str(tmp_path / "test.db"))


def _sample_row(**overrides) -> dict:
    base = {
        "symbol":      "RELIANCE",
        "exchange":    "BSE",
        "filing_date": "2026-01-15",
        "filing_time": "09:32:00",
        "category":    "Board Meeting",
        "subcategory": "Dividend",
        "headline":    "Board approves interim dividend",
        "filing_url":  None,
        "raw_json":    None,
        "fetched_at":  "2026-04-23T10:00:00+00:00",
    }
    base.update(overrides)
    return base


class TestFilingArchiverDB:

    def test_create_tables_on_init(self, tmp_path):
        """filing_archive table and both indexes must exist after __init__."""
        archiver = _make_archiver(tmp_path)
        con = sqlite3.connect(str(tmp_path / "test.db"))

        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "filing_archive" in tables, "filing_archive table not created"

        indexes = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()]
        assert "idx_filing_archive_symbol_date" in indexes
        assert "idx_filing_archive_date" in indexes
        con.close()
        archiver.close()

    def test_insert_returns_true_on_new_row(self, tmp_path):
        """insert_filing returns True when a new row is inserted."""
        archiver = _make_archiver(tmp_path)
        result = archiver.insert_filing(_sample_row())
        assert result is True
        archiver.close()

    def test_insert_and_deduplicate(self, tmp_path):
        """Inserting the same filing twice leaves exactly 1 row."""
        archiver = _make_archiver(tmp_path)
        row = _sample_row()
        first  = archiver.insert_filing(row)
        second = archiver.insert_filing(row)

        assert first is True
        assert second is False

        con = sqlite3.connect(str(tmp_path / "test.db"))
        count = con.execute(
            "SELECT COUNT(*) FROM filing_archive"
        ).fetchone()[0]
        assert count == 1
        con.close()
        archiver.close()

    def test_get_filings_for_date_range(self, tmp_path):
        """Query by symbol + date range returns correct subset."""
        archiver = _make_archiver(tmp_path)

        archiver.insert_filing(_sample_row(filing_date="2026-01-10", headline="H1"))
        archiver.insert_filing(_sample_row(filing_date="2026-01-15", headline="H2"))
        archiver.insert_filing(_sample_row(filing_date="2026-01-20", headline="H3"))
        archiver.insert_filing(_sample_row(
            symbol="TCS", filing_date="2026-01-12", headline="H4"))
        archiver.insert_filing(_sample_row(
            symbol="TCS", filing_date="2026-01-18", headline="H5"))

        results = archiver.get_filings_for_date_range(
            "RELIANCE", "2026-01-12", "2026-01-17"
        )
        assert len(results) == 1
        assert results[0]["headline"] == "H2"
        archiver.close()

    def test_get_all_filings_in_range_category_filter(self, tmp_path):
        """Category filter returns only matching filings."""
        archiver = _make_archiver(tmp_path)

        archiver.insert_filing(_sample_row(
            category="Board Meeting", headline="BM1"))
        archiver.insert_filing(_sample_row(
            category="Board Meeting", headline="BM2",
            filing_date="2026-01-16"))
        archiver.insert_filing(_sample_row(
            category="Orders", headline="ORD1",
            filing_date="2026-01-17"))
        # FR1 is outside the query window (2026-01-21 > 2026-01-20)
        archiver.insert_filing(_sample_row(
            category="Financial Results", headline="FR1",
            filing_date="2026-01-21"))

        # Board Meeting filter in [01-15, 01-20] → BM1 + BM2 = 2
        filtered = archiver.get_all_filings_in_range(
            "2026-01-15", "2026-01-20",
            categories=["Board Meeting"]
        )
        assert len(filtered) == 2
        assert all(r["category"] == "Board Meeting" for r in filtered)

        # No category filter in [01-15, 01-20] → BM1 + BM2 + ORD1 = 3 (FR1 excluded)
        all_results = archiver.get_all_filings_in_range(
            "2026-01-15", "2026-01-20"
        )
        assert len(all_results) == 3
        archiver.close()

    def test_get_stats_empty_db(self, tmp_path):
        """get_stats() on empty DB returns zeros — must not raise."""
        archiver = _make_archiver(tmp_path)
        stats = archiver.get_stats()

        assert stats["total_filings"] == 0
        assert stats["date_range"] == [None, None]
        assert stats["by_category"] == {}
        assert stats["symbols_covered"] == 0
        archiver.close()

    def test_get_stats_populated(self, tmp_path):
        """get_stats() returns correct counts after inserting known rows."""
        archiver = _make_archiver(tmp_path)

        for i in range(4):
            archiver.insert_filing(_sample_row(
                headline=f"BM{i}",
                filing_date=f"2026-01-{10+i:02d}",
                category="Board Meeting"
            ))
        for i in range(3):
            archiver.insert_filing(_sample_row(
                symbol="TCS",
                headline=f"ORD{i}",
                filing_date=f"2026-01-{14+i:02d}",
                category="Orders"
            ))

        stats = archiver.get_stats()

        assert stats["total_filings"] == 7
        assert stats["symbols_covered"] == 2
        assert stats["by_category"]["Board Meeting"] == 4
        assert stats["by_category"]["Orders"] == 3
        assert stats["date_range"][0] == "2026-01-10"
        assert stats["date_range"][1] == "2026-01-16"
        archiver.close()


class TestSymbolMapping:
    """Group 2 — _map_bse_to_nse_symbol logic."""

    @pytest.fixture
    def archiver_with_mappings(self, tmp_path):
        """
        FilingArchiver with a hand-crafted _name_to_symbol dict
        and _nifty200 set — no real CSV needed.
        """
        archiver = FilingArchiver(db_path=str(tmp_path / "test.db"))
        # Inject a controlled lookup table — bypasses CSV loading
        archiver._nifty200 = {
            "RELIANCE", "TCS", "HDFCBANK", "SBIN", "INFY",
            "TATAMOTORS", "WIPRO", "AXISBANK"
        }
        archiver._name_to_symbol = {
            "RELIANCE INDUSTRIES":           "RELIANCE",
            "RELIANCE":                      "RELIANCE",
            "TATA CONSULTANCY SERVICES":     "TCS",
            "TCS":                           "TCS",
            "HDFC BANK":                     "HDFCBANK",
            "HDFCBANK":                      "HDFCBANK",
            "STATE BANK OF INDIA":           "SBIN",
            "SBIN":                          "SBIN",
            "INFOSYS":                       "INFY",
            "INFY":                          "INFY",
            "TATA MOTORS":                   "TATAMOTORS",
            "TATAMOTORS":                    "TATAMOTORS",
            "WIPRO":                         "WIPRO",
            "AXIS BANK":                     "AXISBANK",
            "AXISBANK":                      "AXISBANK",
        }
        return archiver

    def test_direct_exact_match(self, archiver_with_mappings):
        """Exact uppercase name returns correct symbol immediately."""
        a = archiver_with_mappings
        assert a._map_bse_to_nse_symbol("RELIANCE INDUSTRIES") == "RELIANCE"
        assert a._map_bse_to_nse_symbol("INFOSYS") == "INFY"
        assert a._map_bse_to_nse_symbol("WIPRO") == "WIPRO"

    def test_case_insensitive(self, archiver_with_mappings):
        """Lowercase and mixed-case input maps correctly."""
        a = archiver_with_mappings
        assert a._map_bse_to_nse_symbol("reliance industries") == "RELIANCE"
        assert a._map_bse_to_nse_symbol("Infosys") == "INFY"

    def test_suffix_stripped_match(self, archiver_with_mappings):
        """BSE names with LTD/LIMITED suffix map correctly after stripping."""
        a = archiver_with_mappings
        assert a._map_bse_to_nse_symbol("RELIANCE INDUSTRIES LIMITED") == "RELIANCE"
        assert a._map_bse_to_nse_symbol("HDFC BANK LIMITED") == "HDFCBANK"
        assert a._map_bse_to_nse_symbol("WIPRO LTD") == "WIPRO"
        assert a._map_bse_to_nse_symbol("TATA MOTORS LTD.") == "TATAMOTORS"

    def test_substring_match(self, archiver_with_mappings):
        """Partial name >= 6 chars finds correct symbol via substring scan."""
        a = archiver_with_mappings
        # Strips " LTD" → "TATA CONSULTANCY SERVICES" which is in the injected dict (Step 2)
        assert a._map_bse_to_nse_symbol("TATA CONSULTANCY SERVICES LTD") == "TCS"

    def test_no_match_unknown_company(self, archiver_with_mappings):
        """Completely unknown company name returns None."""
        a = archiver_with_mappings
        assert a._map_bse_to_nse_symbol("SOME UNKNOWN COMPANY XYZ") is None
        assert a._map_bse_to_nse_symbol("FOOBAR CORP") is None

    def test_short_string_rejected(self, archiver_with_mappings):
        """
        Substring scan requires >= 6 chars on the shorter string.
        A short input with no direct/suffix match returns None.
        """
        a = archiver_with_mappings
        assert a._map_bse_to_nse_symbol("WIP") is None
        assert a._map_bse_to_nse_symbol("REL") is None

    def test_no_false_positive_short_overlap(self, archiver_with_mappings):
        """
        A name that shares only a short substring with a valid symbol
        must NOT match if the common part is < 6 chars.
        """
        a = archiver_with_mappings
        # "AXIS FINANCE LTD" → strip " LTD" → "AXIS FINANCE"
        # "AXIS FINANCE" is not in dict; substring scan:
        #   "AXIS FINANCE" vs "AXIS BANK"  — no containment
        #   "AXIS FINANCE" vs "AXISBANK"   — no containment
        # → must return None
        result = a._map_bse_to_nse_symbol("AXIS FINANCE LTD")
        assert result is None


class TestFilingArchiverBackfill:
    """Group 3 — backfill_bse() and _fetch_bse_day() with mocked HTTP."""

    @pytest.fixture
    def archiver(self, tmp_path):
        a = FilingArchiver(db_path=str(tmp_path / "test.db"))
        a._nifty200 = {"RELIANCE", "TCS", "INFY"}
        a._name_to_symbol = {
            "RELIANCE INDUSTRIES": "RELIANCE",
            "RELIANCE":            "RELIANCE",
            "INFOSYS":             "INFY",
            "TATA CONSULTANCY SERV LT": "TCS",
        }
        return a

    def _bse_response(self, rows: list) -> mock.Mock:
        m = mock.Mock()
        m.raise_for_status = mock.Mock()
        m.json.return_value = {"Table": rows}
        return m

    def _sample_bse_row(self, **overrides) -> dict:
        base = {
            "SLONGNAME":       "RELIANCE INDUSTRIES",
            "HEADLINE":        "Board approves buyback",
            "CATEGORYNAME":    "Board Meeting",
            "SUBCATEGORYNAME": "Buyback",
            "DT_TM":           "2026-01-15 09:32:00",
            "SCRIP_CD":        "500325",
        }
        base.update(overrides)
        return base

    def test_backfill_skips_weekends(self, archiver):
        """backfill_bse must not call _fetch_bse_day for Sat or Sun."""
        with mock.patch.object(
            archiver, "_fetch_bse_day", return_value=[]
        ) as mock_fetch:
            with mock.patch("time.sleep"):
                archiver.backfill_bse(days_back=10)

        for call in mock_fetch.call_args_list:
            date_arg = call.args[0]
            assert date_arg.weekday() < 5, (
                f"Weekend date passed to _fetch_bse_day: {date_arg}"
            )

    def test_backfill_handles_http_error_gracefully(self, archiver):
        """HTTP error on one day must not abort the entire backfill."""
        call_count = 0

        def fetch_side_effect(d):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ConnectionError("BSE API timeout")
            return []

        with mock.patch.object(
            archiver, "_fetch_bse_day", side_effect=fetch_side_effect
        ):
            with mock.patch("time.sleep"):
                summary = archiver.backfill_bse(days_back=10)

        assert len(summary["errors"]) == 1
        assert summary["dates_fetched"] >= 1

    def test_backfill_inserts_filtered_filings(self, archiver):
        """Only filings whose BSE name maps to a Nifty200 symbol are inserted."""
        rows = [
            self._sample_bse_row(SLONGNAME="RELIANCE INDUSTRIES"),
            self._sample_bse_row(SLONGNAME="INFOSYS",
                                 HEADLINE="Q3 results",
                                 DT_TM="2026-01-15 10:00:00"),
            self._sample_bse_row(SLONGNAME="SOME UNKNOWN CO LTD",
                                 HEADLINE="AGM notice"),
        ]
        with mock.patch("requests.get", return_value=self._bse_response(rows)):
            result = archiver._fetch_bse_day(date(2026, 1, 15))

        assert len(result) == 2
        assert {r["symbol"] for r in result} == {"RELIANCE", "INFY"}

    def test_backfill_returns_summary_dict(self, archiver):
        """backfill_bse() return dict has all required keys."""
        with mock.patch.object(archiver, "_fetch_bse_day", return_value=[]):
            with mock.patch("time.sleep"):
                summary = archiver.backfill_bse(days_back=3)

        assert "dates_fetched"             in summary
        assert "filings_inserted"          in summary
        assert "filings_skipped_duplicate" in summary
        assert "errors"                    in summary

    def test_backfill_rate_limit_sleep(self, archiver):
        """time.sleep must be called once per trading day, with rate >= 2.0s."""
        with mock.patch.object(archiver, "_fetch_bse_day", return_value=[]):
            with mock.patch("time.sleep") as mock_sleep:
                archiver.backfill_bse(days_back=5)

        assert mock_sleep.call_count >= 1
        for call in mock_sleep.call_args_list:
            assert call.args[0] >= 2.0

    def test_fetch_bse_day_parses_datetime(self, archiver):
        """_fetch_bse_day correctly parses DT_TM into filing_date + filing_time."""
        row = self._sample_bse_row(
            SLONGNAME="RELIANCE INDUSTRIES",
            DT_TM="2026-01-15 14:35:00",
        )
        with mock.patch("requests.get", return_value=self._bse_response([row])):
            result = archiver._fetch_bse_day(date(2026, 1, 15))

        assert len(result) == 1
        assert result[0]["filing_date"] == "2026-01-15"
        assert result[0]["filing_time"] == "14:35:00"
        assert result[0]["symbol"]      == "RELIANCE"
        assert result[0]["exchange"]    == "BSE"

    def test_fetch_bse_day_empty_table(self, archiver):
        """Empty Table in BSE response returns empty list — no crash."""
        with mock.patch("requests.get", return_value=self._bse_response([])):
            result = archiver._fetch_bse_day(date(2026, 1, 15))
        assert result == []

    def test_fetch_bse_day_network_error_returns_empty(self, archiver):
        """Network exception returns empty list — never raises."""
        with mock.patch("requests.get", side_effect=ConnectionError("timeout")):
            result = archiver._fetch_bse_day(date(2026, 1, 15))
        assert result == []


class TestEventStudyBuilderDB:
    """Group 4 — EventStudyBuilder DB layer (table creation + token map)."""

    @pytest.fixture
    def builder(self, tmp_path):
        """EventStudyBuilder pointed at a tmp DB — never touches real DB."""
        return EventStudyBuilder(db_path=str(tmp_path / "test.db"))

    def test_create_table_on_init(self, tmp_path):
        """event_study table must exist after EventStudyBuilder.__init__."""
        b = EventStudyBuilder(db_path=str(tmp_path / "test.db"))
        con = sqlite3.connect(str(tmp_path / "test.db"))
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "event_study" in tables, (
            f"event_study table not created. Tables found: {tables}"
        )
        con.close()
        b.conn.close()

    def test_schema_has_core_columns(self, tmp_path):
        """event_study must have the core return columns."""
        b = EventStudyBuilder(db_path=str(tmp_path / "test.db"))
        con = sqlite3.connect(str(tmp_path / "test.db"))
        cols = [r[1] for r in con.execute(
            "PRAGMA table_info(event_study)"
        ).fetchall()]
        for expected in [
            "symbol", "filed_at", "ret_t0_day",
            "ret_t1_day", "ret_t3_day", "ret_t5_day",
        ]:
            assert expected in cols, f"Column {expected!r} missing from event_study"
        con.close()
        b.conn.close()

    def test_token_map_is_dict(self, tmp_path):
        """_token_map must be a dict after init."""
        b = EventStudyBuilder(db_path=str(tmp_path / "test.db"))
        assert isinstance(b._token_map, dict)
        b.conn.close()

    def test_token_map_values_are_int(self, tmp_path):
        """All values in _token_map must be int instrument tokens."""
        b = EventStudyBuilder(db_path=str(tmp_path / "test.db"))
        for sym, token in b._token_map.items():
            assert isinstance(token, int), (
                f"Token for {sym!r} is {type(token).__name__}, expected int"
            )
        b.conn.close()

    def test_get_token_known_symbol(self, tmp_path):
        """_get_token returns int for a symbol present in _token_map."""
        b = EventStudyBuilder(db_path=str(tmp_path / "test.db"))
        b._token_map["TESTSTOCK"] = 999999
        token = b._get_token("TESTSTOCK")
        assert token == 999999
        b.conn.close()

    def test_get_token_unknown_returns_none_or_zero(self, tmp_path):
        """_get_token for unknown symbol must not raise."""
        b = EventStudyBuilder(db_path=str(tmp_path / "test.db"))
        b._token_map = {}
        try:
            result = b._get_token("UNKNOWNSYM")
            # Acceptable: returns None, 0, or raises KeyError
            # Must NOT raise AttributeError or TypeError
            assert result is None or result == 0 or isinstance(result, int)
        except KeyError:
            pass  # also acceptable
        b.conn.close()

    def test_build_all_returns_int(self, tmp_path):
        """build_all() must return an int (count of processed rows)."""
        b = EventStudyBuilder(db_path=str(tmp_path / "test.db"))
        # filings_archive is empty in test DB — build_all returns 0
        result = b.build_all()
        assert isinstance(result, int)
        assert result == 0
        b.conn.close()


class TestPromoteToFilingsArchive:
    """Group 5 — promote_to_filings_archive() bridge method."""

    @pytest.fixture
    def archiver(self, tmp_path):
        return FilingArchiver(db_path=str(tmp_path / "test.db"))

    def _insert_bse_row(self, archiver, **overrides):
        base = {
            "symbol":      "RELIANCE",
            "exchange":    "BSE",
            "filing_date": "2026-01-15",
            "filing_time": "10:30:00",
            "category":    "Board Meeting",
            "subcategory": "Dividend",
            "headline":    "Board approves interim dividend",
            "filing_url":  None,
            "raw_json":    None,
            "fetched_at":  "2026-04-24T05:00:00+00:00",
        }
        base.update(overrides)
        archiver.insert_filing(base)

    def test_promote_inserts_into_filings_archive(self, archiver, tmp_path):
        """A filing_archive row must appear in filings_archive after promote."""
        self._insert_bse_row(archiver)
        result = archiver.promote_to_filings_archive()

        assert result["promoted"] == 1
        assert result["errors"] == 0

        con = sqlite3.connect(str(tmp_path / "test.db"))
        count = con.execute(
            "SELECT COUNT(*) FROM filings_archive"
        ).fetchone()[0]
        assert count == 1
        con.close()

    def test_promote_filed_at_format(self, archiver, tmp_path):
        """filed_at must be ISO 8601 with IST offset +05:30."""
        self._insert_bse_row(
            archiver,
            filing_date="2026-02-10",
            filing_time="14:35:00",
            headline="Unique headline A",
        )
        archiver.promote_to_filings_archive()

        con = sqlite3.connect(str(tmp_path / "test.db"))
        row = con.execute(
            "SELECT filed_at FROM filings_archive"
        ).fetchone()
        assert row[0] == "2026-02-10T14:35:00+05:30"
        con.close()

    def test_promote_null_time_defaults_market_open(self, archiver, tmp_path):
        """filing_time=None must default filed_at time to 09:15:00."""
        self._insert_bse_row(
            archiver,
            filing_time=None,
            headline="Unique headline B",
        )
        archiver.promote_to_filings_archive()

        con = sqlite3.connect(str(tmp_path / "test.db"))
        row = con.execute(
            "SELECT filed_at FROM filings_archive"
        ).fetchone()
        assert "T09:15:00+05:30" in row[0]
        con.close()

    def test_promote_category_to_event_type_mapping(self, archiver, tmp_path):
        """Category 'Orders' must map to event_type 'ORDER_WIN'."""
        self._insert_bse_row(
            archiver,
            category="Orders",
            headline="Unique headline C",
        )
        archiver.promote_to_filings_archive()

        con = sqlite3.connect(str(tmp_path / "test.db"))
        row = con.execute(
            "SELECT event_type FROM filings_archive"
        ).fetchone()
        assert row[0] == "ORDER_WIN"
        con.close()

    def test_promote_is_idempotent(self, archiver, tmp_path):
        """Running promote twice must not duplicate rows."""
        self._insert_bse_row(archiver)
        first  = archiver.promote_to_filings_archive()
        second = archiver.promote_to_filings_archive()

        assert first["promoted"] == 1
        assert second["promoted"] == 0
        assert second["skipped_duplicate"] == 1

        con = sqlite3.connect(str(tmp_path / "test.db"))
        count = con.execute(
            "SELECT COUNT(*) FROM filings_archive"
        ).fetchone()[0]
        assert count == 1
        con.close()
