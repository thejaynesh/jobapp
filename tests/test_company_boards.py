from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.company_board import CompanyBoard
from app.services.company_boards import (
    DEFAULT_MAX_EMPTY_CYCLES,
    board_slugs,
    record_boards,
    record_fetch_results,
    registry_slugs,
    summary,
)


@pytest.fixture(autouse=True)
def _validation_on(monkeypatch):
    """
    This module is the one that tests validation, so it runs with it on.

    The suite turns it off globally (conftest) because probing is a live
    request per board and a fetch cycle records hundreds; here the probe itself
    is stubbed, so the cost is nil and the behaviour is the subject.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "ATS_BOARD_VALIDATION", True)


def _board(db, ats="greenhouse", slug="acme", validated=True, **kwargs) -> CompanyBoard:
    """
    A board in the registry, already past validation unless asked otherwise.

    Discovered boards now arrive inactive until a probe confirms the slug is
    real (see TestValidationBeforePolling). Everything about ranking and
    retirement is downstream of that, so those tests get a board that has
    already been through it.
    """
    record_boards(db, {ats: [slug]}, origin=kwargs.pop("origin", "discovered"), **kwargs)
    db.flush()
    board = (
        db.query(CompanyBoard)
        .filter(CompanyBoard.ats == ats, CompanyBoard.slug == slug)
        .one()
    )
    if validated and board.validated_at is None:
        board.validated_at = datetime.now(timezone.utc)
        board.active = True
        board.inactive_reason = None
        db.flush()
    return board


class TestRecordBoards:
    def test_stores_a_new_board(self, db):
        assert record_boards(db, {"greenhouse": ["acme"]}, origin="discovered") == 1
        board = _board(db)
        assert board.ats == "greenhouse"
        assert board.slug == "acme"
        assert board.origin == "discovered"

    def test_a_discovered_board_waits_for_a_probe_before_being_polled(self, db):
        """
        Discovery reads a slug out of a link and files it as a company, which
        is a guess. `greenhouse/linkedin` and `greenhouse/appcast` were both
        polled every cycle for months on exactly that guess.
        """
        record_boards(db, {"greenhouse": ["guessed"]}, origin="discovered")
        db.flush()
        board = (
            db.query(CompanyBoard)
            .filter(CompanyBoard.slug == "guessed").one()
        )
        assert board.active is False
        assert board.validated_at is None
        assert board.inactive_reason == "awaiting validation"

    def test_a_configured_or_seeded_board_is_trusted_without_a_probe(self, db):
        # A slug the user typed is a claim somebody made deliberately.
        for origin in ("configured", "seed"):
            record_boards(db, {"lever": [f"typed-{origin}"]}, origin=origin)
        db.flush()
        for origin in ("configured", "seed"):
            board = db.query(CompanyBoard).filter(
                CompanyBoard.slug == f"typed-{origin}").one()
            assert board.active is True, origin
            assert board.validated_at is not None, origin

    def test_validation_switched_off_activates_everything_immediately(self, db):
        # A board held for a probe that will never run is a board lost.
        from app.config import settings

        with patch.object(settings, "ATS_BOARD_VALIDATION", False):
            record_boards(db, {"greenhouse": ["nocheck"]}, origin="discovered")
        db.flush()
        board = db.query(CompanyBoard).filter(CompanyBoard.slug == "nocheck").one()
        assert board.active is True

    def test_reseeing_a_board_is_not_a_new_board(self, db):
        record_boards(db, {"lever": ["acme"]}, origin="discovered")
        db.flush()
        assert record_boards(db, {"lever": ["acme"]}, origin="sniffed") == 0
        assert db.query(CompanyBoard).filter(CompanyBoard.ats == "lever").count() == 1

    def test_replaying_a_stored_list_does_not_revive(self, db):
        """Seeds and the legacy blob are replayed every cycle; if they revived
        boards, retirement would never stick."""
        from app.services.company_boards import backfill_from_slugs
        board = _board(db, slug="retired")
        board.active = False
        db.flush()

        backfill_from_slugs(db, {"greenhouse": ["retired"]}, origin="discovered")
        db.flush()
        db.refresh(board)
        assert board.active is False

    def test_reseeing_revives_a_retired_board(self, db):
        board = _board(db, slug="dormant")
        board.active = False
        board.consecutive_empty = 20
        board.inactive_reason = None  # retired for going quiet, not rejected
        db.flush()

        record_boards(db, {"greenhouse": ["dormant"]}, origin="discovered")
        db.flush()
        db.refresh(board)
        assert board.active is True
        assert board.consecutive_empty == 0

    def test_backfills_company_and_host_without_overwriting(self, db):
        board = _board(db, company="Acme Inc", source_host="careers.acme.com")
        assert board.company == "Acme Inc"
        record_boards(db, {"greenhouse": ["acme"]}, origin="sniffed", company="Other")
        db.flush()
        db.refresh(board)
        assert board.company == "Acme Inc"  # first name wins

    def test_ignores_blank_slugs(self, db):
        assert record_boards(db, {"greenhouse": ["", "   "]}, origin="discovered") == 0

    def test_empty_input_is_a_no_op(self, db):
        assert record_boards(db, {}, origin="discovered") == 0


class TestBoardRanking:
    def test_producers_come_before_quiet_boards(self, db):
        for slug in ("quiet", "busy", "modest"):
            _board(db, slug=slug)
        record_fetch_results(db, "greenhouse", ["quiet", "busy", "modest"],
                             {"busy": 40, "modest": 5})
        db.flush()
        assert board_slugs(db, "greenhouse", 10)[:2] == ["busy", "modest"]

    def test_retired_boards_are_not_polled(self, db):
        board = _board(db, slug="dead")
        _board(db, slug="alive")
        board.active = False
        db.flush()
        assert board_slugs(db, "greenhouse", 10) == ["alive"]

    def test_respects_the_cap(self, db):
        for i in range(5):
            _board(db, slug=f"co{i}")
        assert len(board_slugs(db, "greenhouse", 3)) == 3

    def test_registry_slugs_applies_a_cap_per_ats(self, db):
        _board(db, ats="greenhouse", slug="gh")
        _board(db, ats="lever", slug="lv")
        result = registry_slugs(db, {"greenhouse": 5, "lever": 0})
        assert result["greenhouse"] == ["gh"]
        assert result["lever"] == []


class TestFetchResults:
    def test_yield_is_accumulated(self, db):
        board = _board(db)
        record_fetch_results(db, "greenhouse", ["acme"], {"acme": 7})
        record_fetch_results(db, "greenhouse", ["acme"], {"acme": 3})
        db.flush()
        db.refresh(board)
        assert board.last_job_count == 3
        assert board.total_job_count == 10
        assert board.last_fetched_at is not None

    def test_a_silent_board_is_retired_eventually(self, db):
        board = _board(db, slug="silent")
        for _ in range(DEFAULT_MAX_EMPTY_CYCLES):
            record_fetch_results(db, "greenhouse", ["silent"], {})
        db.flush()
        db.refresh(board)
        assert board.active is False

    def test_one_good_cycle_resets_the_streak(self, db):
        board = _board(db, slug="seasonal")
        for _ in range(DEFAULT_MAX_EMPTY_CYCLES - 1):
            record_fetch_results(db, "greenhouse", ["seasonal"], {})
        record_fetch_results(db, "greenhouse", ["seasonal"], {"seasonal": 1})
        record_fetch_results(db, "greenhouse", ["seasonal"], {})
        db.flush()
        db.refresh(board)
        assert board.active is True
        assert board.consecutive_empty == 1

    @pytest.mark.parametrize("origin", ["configured", "seed"])
    def test_user_chosen_boards_are_never_retired(self, db, origin):
        board = _board(db, slug=f"kept-{origin}", origin=origin)
        for _ in range(DEFAULT_MAX_EMPTY_CYCLES * 2):
            record_fetch_results(db, "greenhouse", [board.slug], {})
        db.flush()
        db.refresh(board)
        assert board.active is True

    def test_a_whole_ats_outage_counts_against_nobody(self, db):
        board = _board(db, slug="innocent")
        for _ in range(DEFAULT_MAX_EMPTY_CYCLES * 2):
            record_fetch_results(db, "greenhouse", ["innocent"], {}, had_errors=True)
        db.flush()
        db.refresh(board)
        assert board.active is True
        assert board.consecutive_empty == 0

    def test_nothing_attempted_is_a_no_op(self, db):
        board = _board(db)
        record_fetch_results(db, "greenhouse", [], {})
        db.flush()
        db.refresh(board)
        assert board.last_fetched_at is None


class TestSummary:
    def test_counts_per_ats(self, db):
        _board(db, ats="greenhouse", slug="a")
        _board(db, ats="greenhouse", slug="b")
        _board(db, ats="lever", slug="c")
        record_fetch_results(db, "greenhouse", ["a", "b"], {"a": 4})
        db.query(CompanyBoard).filter(CompanyBoard.slug == "b").one().active = False
        db.flush()

        result = summary(db)
        assert result["greenhouse"] == {
            "total": 2, "active": 1, "retired": 1, "pending": 0, "rejected": 0,
            "jobs": 4,
        }
        assert result["lever"]["total"] == 1
        assert result["lever"]["retired"] == 0


class TestRetiredBoards:
    def test_lists_only_retired_boards(self, db):
        from app.services.company_boards import retired_boards
        _board(db, slug="alive")
        dead = _board(db, slug="dead")
        dead.active = False
        db.flush()

        result = retired_boards(db)
        assert [b.slug for b in result] == ["dead"]

    def test_a_board_retired_by_silence_shows_up(self, db):
        """End to end: going quiet is what makes a board visible as broken."""
        from app.services.company_boards import retired_boards
        _board(db, slug="wentquiet")
        for _ in range(DEFAULT_MAX_EMPTY_CYCLES):
            record_fetch_results(db, "greenhouse", ["wentquiet"], {})
        db.flush()

        result = retired_boards(db)
        assert len(result) == 1
        assert result[0].slug == "wentquiet"
        assert result[0].consecutive_empty == DEFAULT_MAX_EMPTY_CYCLES

    def test_empty_when_everything_is_healthy(self, db):
        from app.services.company_boards import retired_boards
        _board(db, slug="fine")
        assert retired_boards(db) == []

    def test_respects_the_limit(self, db):
        from app.services.company_boards import retired_boards
        for i in range(5):
            board = _board(db, slug=f"dead{i}")
            board.active = False
        db.flush()
        assert len(retired_boards(db, limit=2)) == 2


class TestReactivate:
    def test_puts_a_board_back_with_a_clean_slate(self, db):
        from app.services.company_boards import board_slugs, reactivate
        board = _board(db, slug="revived")
        board.active = False
        board.consecutive_empty = 9
        db.flush()

        result = reactivate(db, board.id)
        db.flush()
        assert result is not None
        assert board.active is True
        assert board.consecutive_empty == 0
        assert "revived" in board_slugs(db, "greenhouse", 10)

    def test_unknown_id_returns_none(self, db):
        import uuid
        from app.services.company_boards import reactivate
        assert reactivate(db, uuid.uuid4()) is None


class TestSlugBlocklist:
    """
    The registry held gems next to junk: an auto-discovered `ionq` yielding
    5,460 jobs beside `greenhouse/linkedin`, `greenhouse/appcast` and
    `greenhouse/stepstone` — slugs read off pages that were never a company.
    """

    def test_job_boards_are_refused_as_companies(self, db):
        from app.services.company_boards import is_blocked_slug

        for slug in ("linkedin", "appcast", "stepstone", "indeed", "glassdoor",
                     "justjoin", "ziprecruiter", "recruitics"):
            assert is_blocked_slug(slug) is True, slug

    def test_url_path_segments_are_refused(self, db):
        from app.services.company_boards import is_blocked_slug

        for slug in ("careers", "jobs", "www", "api", "embed", "search", "apply"):
            assert is_blocked_slug(slug) is True, slug

    def test_real_companies_are_not(self, db):
        from app.services.company_boards import is_blocked_slug

        for slug in ("ionq", "stripe", "airbnb", "acme", "a", "nvidia"):
            assert is_blocked_slug(slug) is False, slug

    def test_a_workday_triple_is_judged_on_its_tenant(self, db):
        from app.services.company_boards import is_blocked_slug

        assert is_blocked_slug("linkedin:wd5:Careers") is True
        assert is_blocked_slug("nvidia:wd5:NVIDIAExternalCareerSite") is False

    def test_blocked_slugs_never_reach_the_registry(self, db):
        # The second line of defence: boards also arrive from the legacy blob,
        # community lists and the one-time backfill, and every one of those
        # paths bypasses extraction.
        stored = record_boards(
            db, {"greenhouse": ["linkedin", "appcast", "ionq"]}, origin="discovered"
        )
        db.flush()
        assert stored == 1
        slugs = {b.slug for b in db.query(CompanyBoard).all()}
        assert slugs == {"ionq"}


class _Probe:
    def __init__(self, exists=True, company=None, error=None):
        self.exists = exists
        self.company = company
        self.error = error


class TestValidationBeforePolling:
    def _pending(self, db, slug="guessed", ats="greenhouse", company=None):
        record_boards(db, {ats: [slug]}, origin="discovered", company=company)
        db.flush()
        return db.query(CompanyBoard).filter(CompanyBoard.slug == slug).one()

    def _validate(self, db, probe, **kwargs):
        from app.services.company_boards import validate_pending

        with patch("app.services.ats_validation.probe_board", return_value=probe):
            return validate_pending(db, **kwargs)

    def test_a_real_board_is_activated(self, db):
        board = self._pending(db)
        counts = self._validate(db, _Probe(exists=True))
        db.refresh(board)

        assert counts["activated"] == 1
        assert board.active is True
        assert board.validated_at is not None
        assert board.inactive_reason is None

    def test_a_slug_with_no_board_behind_it_is_rejected(self, db):
        board = self._pending(db, slug="appcast-lookalike")
        counts = self._validate(db, _Probe(exists=False, error="greenhouse has no board"))
        db.refresh(board)

        assert counts["rejected"] == 1
        assert board.active is False
        assert "no board" in board.inactive_reason

    def test_a_rejected_board_is_not_revived_by_being_linked_again(self, db):
        """
        It was linked in the first place — that is exactly the evidence the
        probe disproved.
        """
        board = self._pending(db, slug="ghost")
        self._validate(db, _Probe(exists=False, error="no such board"))
        db.refresh(board)

        record_boards(db, {"greenhouse": ["ghost"]}, origin="discovered")
        db.flush()
        db.refresh(board)
        assert board.active is False

    def test_a_board_belonging_to_someone_else_is_refiled_not_dropped(self, db):
        # The board is real and worth polling; only the company column was
        # wrong, and leaving it wrong is what made the registry untrustworthy.
        # A real board filed under the wrong employer — which happens when the
        # slug was read off a page belonging to somebody else.
        board = self._pending(db, slug="bigco", company="Some Startup")
        counts = self._validate(db, _Probe(exists=True, company="BigCo Holdings"))
        db.refresh(board)

        assert counts["wrong_company"] == 1
        assert board.company == "BigCo Holdings"
        assert board.active is True

    def test_a_cosmetic_name_difference_is_not_a_mismatch(self, db):
        board = self._pending(db, slug="stripe", company="Stripe")
        counts = self._validate(db, _Probe(exists=True, company="Stripe, Inc."))
        db.refresh(board)

        assert counts["wrong_company"] == 0
        assert board.company == "Stripe"

    def test_an_unreachable_ats_does_not_condemn_the_board(self, db):
        """
        Not being able to reach the ATS is not evidence that the company is
        fictional, and one timeout must not retire a real board.
        """
        board = self._pending(db)
        self._validate(db, _Probe(exists=True, error="probe failed: timeout"))
        db.refresh(board)
        assert board.active is True

    def test_only_unvalidated_boards_are_probed(self, db):
        from app.services.company_boards import validate_pending

        settled = _board(db, slug="already-known")
        self._pending(db, slug="new-one")

        with patch("app.services.ats_validation.probe_board",
                   return_value=_Probe(exists=True)) as probe:
            counts = validate_pending(db)

        assert counts["probed"] == 1
        assert probe.call_args[0][1] == "new-one"
        assert settled.active is True

    def test_the_batch_is_bounded(self, db):
        from app.services.company_boards import validate_pending

        for i in range(10):
            self._pending(db, slug=f"pending-{i}")
        counts = self._validate(db, _Probe(exists=True), limit=4)
        assert counts["probed"] == 4

    def test_an_unvalidated_board_is_not_polled(self, db):
        self._pending(db, slug="unproven")
        assert board_slugs(db, "greenhouse", 10) == []


class TestSummaryDistinguishesInactiveStates:
    """
    Inactive means three different things now, and one number for all of them
    would report "400 boards retired" about a registry where most are simply
    waiting for a first probe.
    """

    def test_the_three_are_counted_separately(self, db):
        _board(db, slug="producing")

        quiet = _board(db, slug="quiet")
        quiet.active = False  # retired for going silent; no reason recorded
        record_boards(db, {"greenhouse": ["unproven"]}, origin="discovered")

        rejected = _board(db, slug="ghost")
        rejected.active = False
        rejected.inactive_reason = "greenhouse has no board for this slug"
        db.flush()

        counts = summary(db)["greenhouse"]
        assert counts["active"] == 1
        assert counts["pending"] == 1
        assert counts["rejected"] == 1
        assert counts["retired"] == 1
        assert counts["total"] == 4

    def test_a_board_awaiting_a_probe_is_not_listed_as_retired(self, db):
        from app.services.company_boards import retired_boards

        record_boards(db, {"greenhouse": ["unproven"]}, origin="discovered")
        db.flush()
        assert retired_boards(db) == []
