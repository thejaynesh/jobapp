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


def _board(db, ats="greenhouse", slug="acme", **kwargs) -> CompanyBoard:
    record_boards(db, {ats: [slug]}, origin=kwargs.pop("origin", "discovered"), **kwargs)
    db.flush()
    return (
        db.query(CompanyBoard)
        .filter(CompanyBoard.ats == ats, CompanyBoard.slug == slug)
        .one()
    )


class TestRecordBoards:
    def test_stores_a_new_board(self, db):
        assert record_boards(db, {"greenhouse": ["acme"]}, origin="discovered") == 1
        board = _board(db)
        assert board.ats == "greenhouse"
        assert board.slug == "acme"
        assert board.origin == "discovered"
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
            "total": 2, "active": 1, "retired": 1, "jobs": 4,
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
