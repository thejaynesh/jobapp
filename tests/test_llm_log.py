"""
The prompt and the reply, stored together.

Every log line that existed said a call happened and how it ended, which is the
one thing the result already tells you. When a resume comes out empty the
question is whether the prompt asked for nothing useful or the model gave
nothing back — and the prompt was assembled from a profile and a job description
that have both moved on since. Unless it was written down at the time, it is
gone.

What these tests defend most is that the log cannot make anything worse: it
never raises, it writes on its own session so a caller's rollback cannot take
the evidence with it, and it has a ceiling so it cannot fill the disk.
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.models.llm_call import LLMCall
from app.services import llm_log

MESSAGES = [
    {"role": "system", "content": "You score jobs."},
    {"role": "user", "content": "Job: Backend Engineer"},
]


@pytest.fixture
def logged(db, monkeypatch):
    """Route the log's own session at the test session."""
    monkeypatch.setattr("app.database.SessionLocal", lambda: db)
    # The log commits; the fixture's outer transaction still rolls back.
    monkeypatch.setattr(db, "close", lambda: None)
    return db


def rows(db) -> list[LLMCall]:
    return db.query(LLMCall).order_by(LLMCall.created_at).all()


class TestWhatIsRecorded:
    def test_a_successful_call_stores_both_halves(self, logged):
        with llm_log.call("nim", "glm-5.2", MESSAGES) as entry:
            entry.finish("score: 82")
        call = rows(logged)[0]
        assert call.messages[1]["content"] == "Job: Backend Engineer"
        assert call.response == "score: 82"
        assert call.ok is True

    def test_a_failed_call_keeps_its_prompt(self, logged):
        # Often the most useful row in the table: the failure with the exact
        # input that produced it.
        with pytest.raises(RuntimeError):
            with llm_log.call("nim", "glm-5.2", MESSAGES):
                raise RuntimeError("429 rate limited")
        call = rows(logged)[0]
        assert call.ok is False
        assert "429" in call.error
        assert call.messages, "a failure without its prompt is half a record"

    def test_reasoning_is_kept_apart_from_the_answer(self, logged):
        # An empty content beside a full reasoning field is the signature of a
        # token ceiling that was too low. Merging them hides exactly that.
        with llm_log.call("nim", "glm-5.2", MESSAGES) as entry:
            entry.finish("", reasoning="Let me think about this...",
                         finish_reason="length")
        call = rows(logged)[0]
        assert call.response == ""
        assert "think" in call.reasoning
        assert call.finish_reason == "length"

    def test_token_counts_come_from_the_response(self, logged):
        raw = MagicMock()
        raw.usage.prompt_tokens = 1200
        raw.usage.completion_tokens = 340
        raw.choices = [MagicMock(finish_reason="stop")]
        with llm_log.call("nim", "m", MESSAGES) as entry:
            entry.finish("ok", raw=raw)
        call = rows(logged)[0]
        assert call.prompt_tokens == 1200
        assert call.completion_tokens == 340

    def test_it_times_the_call(self, logged):
        with llm_log.call("nim", "m", MESSAGES) as entry:
            entry.finish("ok")
        assert rows(logged)[0].duration_ms >= 0


class TestStages:
    def test_calls_are_labelled(self, logged):
        # A document generation is six calls with six different jobs; unlabelled
        # they are six anonymous rows.
        with llm_log.stage("resume_bullets"):
            with llm_log.call("nim", "m", MESSAGES) as entry:
                entry.finish("ok")
        assert rows(logged)[0].stage == "resume_bullets"

    def test_the_label_carries_the_application(self, logged):
        app_id = uuid.uuid4()
        with llm_log.stage("cover_letter", application_id=app_id):
            with llm_log.call("nim", "m", MESSAGES) as entry:
                entry.finish("ok")
        assert rows(logged)[0].application_id == app_id

    def test_a_nested_stage_keeps_the_outer_ids(self, logged):
        job_id = uuid.uuid4()
        with llm_log.stage("generation", job_id=job_id):
            with llm_log.stage("resume_summary"):
                with llm_log.call("nim", "m", MESSAGES) as entry:
                    entry.finish("ok")
        call = rows(logged)[0]
        assert call.stage == "resume_summary"
        assert call.job_id == job_id

    def test_the_label_is_restored_afterwards(self, logged):
        with llm_log.stage("match"):
            pass
        assert llm_log.current_stage() == "unknown"

    def test_an_unlabelled_call_still_records(self, logged):
        with llm_log.call("nim", "m", MESSAGES) as entry:
            entry.finish("ok")
        assert rows(logged)[0].stage == "unknown"


class TestItCannotMakeThingsWorse:
    def test_a_logging_failure_does_not_break_the_call(self, monkeypatch):
        # Breaking a document generation to record that it happened would be a
        # strictly worse outcome than the missing log.
        def explode():
            raise RuntimeError("database is down")

        monkeypatch.setattr("app.database.SessionLocal", explode)
        with llm_log.call("nim", "m", MESSAGES) as entry:
            entry.finish("the answer survives")
        # No exception is the assertion.

    def test_the_original_exception_is_not_replaced(self, monkeypatch):
        monkeypatch.setattr("app.database.SessionLocal",
                            lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        with pytest.raises(ValueError, match="the real problem"):
            with llm_log.call("nim", "m", MESSAGES):
                raise ValueError("the real problem")

    def test_it_writes_on_its_own_session(self, db, monkeypatch):
        # The caller is usually mid-transaction; a rollback there must not take
        # the record of what went wrong with it.
        opened = []

        def session():
            opened.append(True)
            return db

        monkeypatch.setattr("app.database.SessionLocal", session)
        monkeypatch.setattr(db, "close", lambda: None)
        with llm_log.call("nim", "m", MESSAGES) as entry:
            entry.finish("ok")
        assert opened, "the log must not borrow the caller's session"

    def test_it_can_be_switched_off(self, logged, monkeypatch):
        monkeypatch.setattr(settings, "LLM_LOG_ENABLED", False)
        with llm_log.call("nim", "m", MESSAGES) as entry:
            entry.finish("ok")
        assert rows(logged) == []


class TestSize:
    def test_long_prompts_are_truncated(self, logged, monkeypatch):
        # Prompts carry whole job descriptions and profiles. Without a ceiling
        # this table outgrows everything else in the schema.
        monkeypatch.setattr(settings, "LLM_LOG_MAX_CHARS", 500)
        huge = [{"role": "user", "content": "x" * 50_000}]
        with llm_log.call("nim", "m", huge) as entry:
            entry.finish("y" * 50_000)
        call = rows(logged)[0]
        assert len(call.messages[0]["content"]) < 1000
        assert llm_log.TRUNCATION_NOTE in call.messages[0]["content"]
        assert llm_log.TRUNCATION_NOTE in call.response

    def test_truncation_says_so_rather_than_silently_cutting(self, logged, monkeypatch):
        # A prompt that looks complete but is not would send the investigation
        # somewhere false.
        monkeypatch.setattr(settings, "LLM_LOG_MAX_CHARS", 500)
        with llm_log.call("nim", "m", [{"role": "user", "content": "x" * 5000}]) as entry:
            entry.finish("ok")
        assert "truncated" in rows(logged)[0].messages[0]["content"]

    def test_pruning_keeps_the_newest(self, logged):
        from datetime import datetime, timedelta, timezone

        base = datetime.now(timezone.utc)
        for i in range(6):
            logged.add(LLMCall(stage=f"s{i}", messages=[],
                               created_at=base + timedelta(seconds=i)))
        logged.commit()
        removed = llm_log.prune(logged, keep=2)
        assert removed == 4
        remaining = [c.stage for c in rows(logged)]
        assert remaining == ["s4", "s5"]

    def test_pruning_below_the_limit_does_nothing(self, logged):
        with llm_log.call("nim", "m", MESSAGES) as entry:
            entry.finish("ok")
        assert llm_log.prune(logged, keep=100) == 0


class TestTheCallSitesAreWired:
    def test_the_matching_path_is_logged(self, logged):
        from app.services.matcher import chat_completion

        reply = MagicMock()
        reply.choices = [MagicMock(finish_reason="stop")]
        reply.choices[0].message.content = '{"score": 80}'
        reply.choices[0].message.reasoning_content = None
        reply.usage.prompt_tokens = 10
        reply.usage.completion_tokens = 5

        with patch("app.services.matcher.OpenAI") as client:
            client.return_value.chat.completions.create.return_value = reply
            chat_completion(MESSAGES, "k", "http://nim", "glm-5.2")
        assert rows(logged)[0].response == '{"score": 80}'

    def test_the_provider_chain_is_logged(self, logged):
        from app.llm.providers import Provider, call_provider

        reply = MagicMock()
        reply.choices = [MagicMock(finish_reason="stop")]
        reply.choices[0].message.content = "hello"
        reply.choices[0].message.reasoning_content = None
        reply.usage.prompt_tokens = 3
        reply.usage.completion_tokens = 1

        with patch("app.llm.providers.OpenAI", create=True):
            with patch("app.llm.providers._call_openai_compatible",
                       side_effect=lambda p, m, t, mt, to, entry=None:
                       (entry.finish("hello") if entry else None) or "hello"):
                call_provider(Provider(name="fi", api_key="k", model="glm-5.1",
                                       base_url="https://x/v1"), MESSAGES)
        call = rows(logged)[0]
        assert call.provider == "fi"
        assert call.response == "hello"

    def test_a_provider_failure_is_logged_with_its_prompt(self, logged):
        from app.llm.providers import Provider, call_provider

        with patch("app.llm.providers._call_openai_compatible",
                   side_effect=RuntimeError("401 bad key")):
            with pytest.raises(RuntimeError):
                call_provider(Provider(name="fi", api_key="k", model="m",
                                       base_url="https://x/v1"), MESSAGES)
        call = rows(logged)[0]
        assert call.ok is False
        assert "401" in call.error
        assert call.messages[0]["content"] == "You score jobs."


class TestThePage:
    def _call(self, db, **kwargs):
        record = LLMCall(**{"stage": "match", "provider": "nim", "model": "m",
                            "messages": MESSAGES, "response": "ok", **kwargs})
        db.add(record)
        db.commit()
        return record

    def test_it_lists_calls(self, client, db):
        self._call(db)
        body = client.get("/llm").text
        assert "match" in body

    def test_an_empty_log_says_what_will_appear(self, client, db):
        assert "No calls recorded yet" in client.get("/llm").text

    def test_it_filters_by_stage(self, client, db):
        self._call(db, stage="match")
        self._call(db, stage="cover_letter", response="Dear hiring manager")
        body = client.get("/llm?stage=cover_letter").text
        assert "Dear hiring manager" in body

    def test_it_filters_to_failures(self, client, db):
        self._call(db, response="fine")
        self._call(db, ok=False, error="429 rate limited", response=None)
        body = client.get("/llm?status=failed").text
        assert "429" in body
        assert "fine" not in body

    def test_calls_that_returned_nothing_have_their_own_filter(self, client, db):
        # The failure that looks like success everywhere else in the app, and
        # the exact shape of a blank generated document.
        self._call(db, response="")
        self._call(db, response="plenty of words")
        body = client.get("/llm?status=empty").text
        assert "returned nothing" in body
        assert "plenty of words" not in body

    def test_the_detail_page_shows_the_whole_prompt(self, client, db):
        record = self._call(db)
        body = client.get(f"/llm/{record.id}").text
        assert "You score jobs." in body
        assert "Job: Backend Engineer" in body

    def test_the_detail_page_shows_reasoning_separately(self, client, db):
        record = self._call(db, response="", reasoning="thinking out loud",
                            finish_reason="length")
        body = client.get(f"/llm/{record.id}").text
        assert "thinking out loud" in body
        assert "cut off by the token ceiling" in body

    def test_a_truncated_reply_is_named_as_such(self, client, db):
        # Whatever downstream code parsed, it parsed from half an answer.
        record = self._call(db, finish_reason="length", response="{\"score\": 8")
        assert "cut off by the token ceiling" in client.get(f"/llm/{record.id}").text

    def test_an_unknown_call_is_a_404(self, client, db):
        assert client.get(f"/llm/{uuid.uuid4()}").status_code == 404

    def test_it_is_in_the_navigation(self, client, db):
        assert 'href="/llm"' in client.get("/jobs").text
