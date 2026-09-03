"""
Sweeping a board on a schedule instead of when a browser happens to be open.

The extension reads Tsenta's API from inside a tab using the ID token the site
publishes to extensions. That works, and it only happens while somebody is
browsing — so the board is as current as the last time it was visited, which is
not a schedule.

The durable half of the credential is a Firebase refresh token, and Google's
public `securetoken` endpoint mints fresh hour-long ID tokens from it. Handing
that over once is what turns a browser-bound sweep into a scheduled one, and
what these tests defend is the handful of ways that can go quietly wrong.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx

import pytest

from app.models.linked_account import LinkedAccount
from app.services import linked_auth
from app.services.sources import tsenta


def _resp(status=200, payload=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = text
    return r


def _linked(db, refresh="refresh-1", api_key="key-1"):
    db.query(LinkedAccount).delete()
    row = LinkedAccount(site="tsenta", api_key=api_key, refresh_token=refresh,
                        linked_at=datetime.now(timezone.utc))
    db.add(row)
    db.commit()
    return row


def _card(n):
    # A distinct company per card: the dedupe hash is company + title +
    # location, so twenty cards from one employer with one title are one job
    # and the counts would be measuring deduplication rather than the sweep.
    return {"id": f"t{n}", "title": "Backend Engineer", "company": f"Acme {n}",
            "location": "Boston, MA", "url": f"https://tsenta.com/jobs/{n}",
            "description": "We need a backend engineer with Python and Go. " * 6}


class TestMintingATokenFromWhatTheBrowserHandedOver:
    def setup_method(self):
        linked_auth.forget("tsenta")

    def test_a_stored_refresh_token_becomes_an_id_token(self, db):
        _linked(db)
        with patch("httpx.post", return_value=_resp(
            payload={"id_token": "ID-1", "refresh_token": "refresh-1",
                     "expires_in": "3600"},
        )):
            assert linked_auth.id_token(db, "tsenta") == "ID-1"

    def test_a_rotated_refresh_token_is_stored_before_anything_else_can_fail(self, db):
        """
        Google may hand back a *new* refresh token, after which the old one may
        stop working. Losing the new one is losing the credential.
        """
        _linked(db, refresh="old")
        with patch("httpx.post", return_value=_resp(
            payload={"id_token": "ID-1", "refresh_token": "new", "expires_in": "3600"},
        )):
            linked_auth.id_token(db, "tsenta")

        assert db.get(LinkedAccount, "tsenta").refresh_token == "new"

    def test_the_token_is_reused_rather_than_minted_per_page(self, db):
        # An ID token lasts an hour and a sweep is a minute; minting per page
        # would be forty round trips to Google for one board.
        _linked(db)
        with patch("httpx.post", return_value=_resp(
            payload={"id_token": "ID-1", "expires_in": "3600"},
        )) as post:
            linked_auth.id_token(db, "tsenta")
            linked_auth.id_token(db, "tsenta")
        assert post.call_count == 1

    def test_a_dead_refresh_token_records_why_rather_than_raising(self, db):
        """
        A refresh token dies when the user signs out everywhere or changes a
        password. That is not a bug and the server cannot fix it — the repair
        is to open the board in a browser, which re-links. So it has to be
        legible, not a stack trace.
        """
        _linked(db)
        with patch("httpx.post", return_value=_resp(
            status=400, payload={"error": {"message": "TOKEN_EXPIRED"}},
        )):
            assert linked_auth.id_token(db, "tsenta") is None

        row = db.get(LinkedAccount, "tsenta")
        assert "TOKEN_EXPIRED" in row.last_error
        assert row.last_error_at is not None

    def test_an_unreachable_token_service_is_not_a_dead_credential(self, db):
        _linked(db)
        with patch("httpx.post", side_effect=httpx.ConnectError("no route")):
            assert linked_auth.id_token(db, "tsenta") is None
        # The credential is untouched: nothing about it was refused.
        assert db.get(LinkedAccount, "tsenta").refresh_token == "refresh-1"

    def test_a_site_never_linked_is_not_an_error(self, db):
        db.query(LinkedAccount).delete()
        db.commit()
        with patch("httpx.post") as post:
            assert linked_auth.id_token(db, "tsenta") is None
        post.assert_not_called()

    def test_relinking_clears_an_error_the_new_token_has_not_earned(self, db):
        row = _linked(db, refresh="dead")
        row.last_error = "TOKEN_EXPIRED"
        row.last_error_at = datetime.now(timezone.utc)
        db.commit()

        linked_auth.link(db, "tsenta", "key-1", "fresh")

        row = db.get(LinkedAccount, "tsenta")
        assert row.refresh_token == "fresh"
        assert row.last_error is None

    def test_relinking_drops_the_cached_token(self, db):
        _linked(db)
        with patch("httpx.post", return_value=_resp(
            payload={"id_token": "ID-1", "expires_in": "3600"})):
            linked_auth.id_token(db, "tsenta")

        linked_auth.link(db, "tsenta", "key-2", "refresh-2")

        with patch("httpx.post", return_value=_resp(
            payload={"id_token": "ID-2", "expires_in": "3600"})) as post:
            assert linked_auth.id_token(db, "tsenta") == "ID-2"
        assert post.call_count == 1

    def test_an_expired_cache_entry_mints_again(self, db):
        _linked(db)
        with patch("httpx.post", return_value=_resp(
            payload={"id_token": "ID-1", "expires_in": "3600"})):
            linked_auth.id_token(db, "tsenta")
        # Push the cached entry into the past rather than sleeping an hour.
        token, _ = linked_auth._cache["tsenta"]
        linked_auth._cache["tsenta"] = (token, 0.0)

        with patch("httpx.post", return_value=_resp(
            payload={"id_token": "ID-2", "expires_in": "3600"})):
            assert linked_auth.id_token(db, "tsenta") == "ID-2"


class TestTheServerSideSweep:
    def setup_method(self):
        linked_auth.forget("tsenta")

    def _client(self, responses):
        client = MagicMock()
        client.get.side_effect = responses
        return client

    def _with_token(self):
        return patch.object(linked_auth, "id_token", return_value="ID-1")

    def test_it_pages_until_a_short_one_and_stores_what_it_finds(self, db):
        pages = [
            _resp(payload={"jobs": [_card(n) for n in range(20)]}),
            _resp(payload={"jobs": [_card(n) for n in range(20, 33)]}),
        ]
        with self._with_token(), patch("time.sleep"):
            out = tsenta.sweep(db, client=self._client(pages))

        assert out["pages"] == 2
        assert out["rows"] == 33
        # "end of list" is about the run, not the last page: every slice it was
        # asked for ran out of postings, which is the whole board collected.
        assert out["stopped"] == "end of list"
        assert out["inserted"] > 0

    def test_the_page_size_reported_is_the_one_the_board_served(self, db):
        """
        The board caps `limit` at 20 however large a number is sent. Comparing
        a later page against the size *requested* is how a silent cap reads as
        the end of the list — the mistake the browser-side sweep was written to
        avoid, and it applies identically here.
        """
        pages = [
            _resp(payload={"jobs": [_card(n) for n in range(20)]}),
            _resp(payload={"jobs": [_card(n) for n in range(20, 25)]}),
        ]
        with self._with_token(), patch("time.sleep"):
            out = tsenta.sweep(db, client=self._client(pages))

        assert out["limit"] == 20
        assert out["pages"] == 2, "a served page smaller than requested is not the end"

    def test_an_unlinked_board_says_so_instead_of_failing(self, db):
        db.query(LinkedAccount).delete()
        db.commit()
        with patch.object(linked_auth, "id_token", return_value=None):
            out = tsenta.sweep(db)
        assert out["stopped"] == "not linked"
        assert "browser" in out["detail"]

    def test_a_refused_credential_names_what_the_mint_said(self, db):
        row = _linked(db)
        row.last_error = "TOKEN_EXPIRED"
        db.commit()
        with patch.object(linked_auth, "id_token", return_value=None):
            out = tsenta.sweep(db)
        assert out["stopped"] == "credential refused"
        assert out["detail"] == "TOKEN_EXPIRED"

    def test_a_401_mid_sweep_drops_the_cached_token(self, db):
        # The token was good when minted and is not now. Reusing it next run
        # would fail the same way for the same hour.
        _linked(db)
        linked_auth._cache["tsenta"] = ("ID-1", 9e12)
        pages = [_resp(status=401)]
        with self._with_token(), patch("time.sleep"):
            out = tsenta.sweep(db, client=self._client(pages))

        assert out["stopped"] == "not signed in"
        assert "tsenta" not in linked_auth._cache

    def test_a_page_with_nothing_job_shaped_ends_the_sweep(self, db):
        pages = [_resp(payload={"jobs": []})]
        with self._with_token(), patch("time.sleep"):
            out = tsenta.sweep(db, client=self._client(pages))
        assert out["stopped"] == "end of list"
        assert out["pages"] == 0

    def test_a_network_failure_mid_sweep_keeps_the_pages_already_stored(self, db):
        pages = [
            _resp(payload={"jobs": [_card(n) for n in range(20)]}),
            httpx.ConnectError("dropped"),
        ]
        with self._with_token(), patch("time.sleep"):
            out = tsenta.sweep(db, client=self._client(pages))

        assert out["pages"] == 1
        assert out["stopped"] == "request failed"
        assert out["inserted"] > 0, "the first page was real and is worth keeping"

    def test_the_query_leaves_off_the_filters_their_client_pins_on(self, db):
        pages = [_resp(payload={"jobs": [_card(1)]})]
        client = self._client(pages)
        with self._with_token(), patch("time.sleep"):
            tsenta.sweep(db, client=client)

        params = client.get.call_args.kwargs["params"]
        assert "autoApplyOnly" not in params
        assert "datePosted" not in params
        assert params["locations"] == "country:US"

    def test_the_token_rides_on_every_request(self, db):
        pages = [_resp(payload={"jobs": [_card(1)]})]
        client = self._client(pages)
        with self._with_token(), patch("time.sleep"):
            tsenta.sweep(db, client=client)

        headers = client.get.call_args.kwargs["headers"]
        assert headers["authorization"] == "Bearer ID-1"

    def test_the_offset_cap_is_reported_as_capped_not_as_the_end(self, db):
        """
        The API answers HTTP 400 from page 21 on. That is an offset cap at 400
        rows, and reading it as the end of a list is how the sweep came to
        collect 216 postings out of at least 1,845: a query that genuinely runs
        out ends on a short page long before it.
        """
        pages = [_resp(payload={"jobs": [_card(n) for n in range(20)]})
                 for _ in range(20)] + [_resp(status=400)]
        with self._with_token(), patch("time.sleep"):
            out = tsenta.sweep(db, client=self._client(pages),
                               slices=[{"locations": "state:CA"}])

        assert out["capped_slices"] == 1
        assert "capped" in out["stopped"]
        assert "state:CA" in out["detail"], "name the slice we cannot see behind"

    def test_a_400_on_the_very_first_page_is_a_real_error(self, db):
        # The cap cannot fire on page one — there is no offset yet — so a 400
        # there is the board refusing the query and should say so.
        with self._with_token(), patch("time.sleep"):
            out = tsenta.sweep(db, client=self._client([_resp(status=400)]),
                               slices=[{"locations": "state:CA"}])
        assert out["stopped"] == "HTTP 400"
        assert out["capped_slices"] == 0

    def test_every_slice_is_swept_and_the_totals_add_up(self, db):
        # Two pages per slice, two slices: the mock has to answer all four or
        # the second slice fails on a missing response rather than on anything
        # the code did.
        pages = [
            _resp(payload={"jobs": [_card(n) for n in range(5)]}),
            _resp(payload={"jobs": [_card(n) for n in range(5, 8)]}),
            _resp(payload={"jobs": [_card(n) for n in range(8, 13)]}),
            _resp(payload={"jobs": [_card(n) for n in range(13, 16)]}),
        ]
        with self._with_token(), patch("time.sleep"):
            out = tsenta.sweep(db, client=self._client(pages),
                               slices=[{"locations": "state:WA"},
                                       {"locations": "state:MA"}])

        assert out["slices"] == 2
        assert out["rows"] == 16
        assert out["stopped"] == "end of list"

    def test_a_dead_credential_stops_the_whole_run_not_just_one_slice(self, db):
        # Fifty more slices would be fifty more ways to say the same thing,
        # and fifty more requests at a board that has already refused us.
        _linked(db)
        with self._with_token(), patch("time.sleep"):
            out = tsenta.sweep(db, client=self._client([_resp(status=401)]),
                               slices=[{"locations": f"state:{s}"}
                                       for s in ("CA", "NY", "TX")])
        assert out["slices"] == 1
        assert out["stopped"] == "not signed in"

    def test_the_feed_and_the_index_ask_different_questions(self):
        # `datePosted` is not a date filter here: absent it returns their
        # recommendation feed, present with any value it returns their whole
        # index. Getting that backwards is what capped the sweep at 216.
        feed = tsenta.recommendation_slice()
        index = tsenta.index_slices()

        assert all("datePosted" not in q for q in feed)
        assert len(index) > 40, "the index is swept state by state"
        assert sum(1 for q in index if q.get("datePosted")) >= 40

    def test_a_second_sweep_of_the_same_board_inserts_nothing_new(self, db):
        # The board returns the same recommendation set every few hours. The
        # value is the delta, and the dedupe layers are what make repeating
        # cheap rather than duplicative.
        def fresh():
            return [_resp(payload={"jobs": [_card(n) for n in range(5)]})]

        with self._with_token(), patch("time.sleep"):
            first = tsenta.sweep(db, client=self._client(fresh()))
            second = tsenta.sweep(db, client=self._client(fresh()))

        assert first["inserted"] == 5
        assert second["inserted"] == 0
        assert second["skipped"] == 5


class TestTheLinkEndpoint:
    @pytest.fixture
    def agent(self, client, monkeypatch):
        """The same shape as the agent fixture in test_agent_api."""
        from app.config import settings
        from tests.test_agent_api import TOKEN

        monkeypatch.setattr(settings, "AUTH_ENABLED", True)
        monkeypatch.setattr(settings, "AGENT_TOKEN", TOKEN)
        monkeypatch.setattr(settings, "APP_PASSWORD", "irrelevant-but-required")
        monkeypatch.setattr(settings, "SECRET_KEY", "not-the-placeholder-value")
        return client

    def test_a_credential_is_stored(self, agent, db):
        from tests.test_agent_api import auth_header

        db.query(LinkedAccount).delete()
        db.commit()
        r = agent.post("/api/agent/link", headers=auth_header(), json={
            "site": "tsenta", "api_key": "key", "refresh_token": "refresh"})

        assert r.status_code == 200
        assert db.get(LinkedAccount, "tsenta").refresh_token == "refresh"

    def test_the_token_is_not_echoed_back(self, agent, db):
        from tests.test_agent_api import auth_header

        r = agent.post("/api/agent/link", headers=auth_header(), json={
            "site": "tsenta", "api_key": "key", "refresh_token": "secret-value"})
        assert "secret-value" not in r.text

    def test_a_site_the_server_cannot_sweep_is_refused(self, agent):
        # Not a gate on anything — a typo would store a credential nothing ever
        # reads and report success for it.
        from tests.test_agent_api import auth_header

        r = agent.post("/api/agent/link", headers=auth_header(), json={
            "site": "linkedin", "api_key": "key", "refresh_token": "refresh"})
        assert r.status_code == 400
        assert "tsenta" in r.json()["detail"]

    def test_half_a_credential_is_refused(self, agent):
        from tests.test_agent_api import auth_header

        r = agent.post("/api/agent/link", headers=auth_header(), json={
            "site": "tsenta", "api_key": "key"})
        assert r.status_code == 400

    def test_it_needs_the_agent_token_like_everything_else(self, agent):
        r = agent.post("/api/agent/link", json={
            "site": "tsenta", "api_key": "k", "refresh_token": "r"})
        assert r.status_code in (401, 403)
