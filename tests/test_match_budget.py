"""
The matching cycle's call budget, and why it has to outlive one batch.

`MAX_PAID_MATCH_CALLS_PER_CYCLE` and `DEEP_MATCH_MAX_PER_CYCLE` were counted in
a dict created inside `match_all_new_jobs`. That was right when a cycle was one
pass; once matching was cut into chained batches of 25, a batch could not spend
150 paid calls before the dict was thrown away and remade at zero — so both
ceilings were unreachable by arithmetic, and a backlog with the primary provider
down would have made batch after batch of paid calls, each convinced it was the
first.
"""

from unittest.mock import patch

from app.services import match_budget


class _FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.expires: dict[str, int] = {}

    def hgetall(self, key):
        return {k.encode(): str(v).encode() for k, v in self.hashes.get(key, {}).items()}

    def hset(self, key, mapping=None):
        self.hashes.setdefault(key, {}).update(mapping or {})

    def expire(self, key, ttl):
        self.expires[key] = ttl

    def delete(self, key):
        self.hashes.pop(key, None)


class _DeadRedis:
    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise ConnectionError("redis is not there")
        return boom


class TestTheBudgetSurvivesTheBatchThatSpentIt:
    def test_what_one_batch_spends_the_next_one_starts_from(self):
        fake = _FakeRedis()
        with patch.object(match_budget, "_client", return_value=fake):
            match_budget.save({"paid_calls": 40, "deep_calls": 12})
            assert match_budget.load() == {"paid_calls": 40, "deep_calls": 12}

    def test_the_counters_carry_an_expiry_so_a_killed_chain_frees_them(self):
        # Without it, a worker killed mid-chain would hold every later cycle
        # down against a spend nobody is still making.
        fake = _FakeRedis()
        with patch.object(match_budget, "_client", return_value=fake):
            match_budget.save({"paid_calls": 1, "deep_calls": 0})
        assert fake.expires[match_budget.KEY] == match_budget.TTL_SECONDS

    def test_clearing_starts_the_next_cycle_fresh(self):
        fake = _FakeRedis()
        with patch.object(match_budget, "_client", return_value=fake):
            match_budget.save({"paid_calls": 150, "deep_calls": 100})
            match_budget.clear()
            assert match_budget.load() == {"paid_calls": 0, "deep_calls": 0}

    def test_a_junk_value_reads_as_nothing_spent_rather_than_raising(self):
        fake = _FakeRedis()
        fake.hashes[match_budget.KEY] = {"paid_calls": "seven", "deep_calls": 3}
        with patch.object(match_budget, "_client", return_value=fake):
            assert match_budget.load() == {"paid_calls": 0, "deep_calls": 3}


class TestAnUnreachableRedisDoesNotStopMatching:
    """
    The same call the lock makes: refusing to score jobs because the accounting
    store is down is worse than the overspend the accounting was guarding
    against.
    """

    def test_the_budget_reads_as_empty(self):
        with patch.object(match_budget, "_client", return_value=_DeadRedis()):
            assert match_budget.load() == {"paid_calls": 0, "deep_calls": 0}

    def test_saving_and_clearing_swallow_the_failure(self):
        with patch.object(match_budget, "_client", return_value=_DeadRedis()):
            match_budget.save({"paid_calls": 3, "deep_calls": 1})
            match_budget.clear()


class TestTheCycleIsTheChainNotTheBatch:
    def test_a_pass_seeds_itself_from_what_earlier_batches_spent(self, db):
        from app.models.profile import Profile
        from app.services.matcher import match_all_new_jobs

        db.add(Profile(data={"target_roles": ["Backend Engineer"]}))
        db.commit()

        with patch.object(match_budget, "load",
                          return_value={"paid_calls": 149, "deep_calls": 0}) as load, \
             patch.object(match_budget, "save") as save:
            match_all_new_jobs(db, limit=1)

        load.assert_called_once()
        # Written back even with nothing to score, so a batch that spent its
        # last call does not hand the next one a stale number.
        assert save.call_args[0][0]["paid_calls"] == 149

    def test_a_caller_with_its_own_dict_is_left_alone(self, db):
        from app.models.profile import Profile
        from app.services.matcher import match_all_new_jobs

        db.add(Profile(data={"target_roles": ["Backend Engineer"]}))
        db.commit()

        with patch.object(match_budget, "load") as load, \
             patch.object(match_budget, "save") as save:
            match_all_new_jobs(db, limit=1, budget={"paid_calls": 0, "deep_calls": 0})

        load.assert_not_called()
        save.assert_not_called()

    def test_the_budget_is_reset_when_the_chain_stops(self):
        from app.tasks.match import _chain_if_more

        with patch.object(match_budget, "clear") as clear:
            _chain_if_more({"remaining": 0, "matched": 5, "filtered_out": 2})
        clear.assert_called_once()

    def test_the_budget_is_reset_when_a_batch_made_no_progress(self):
        # Every job came back rate-limited, so the chain stops and the schedule
        # retries — which is a new cycle, and gets a new ceiling.
        from app.tasks.match import _chain_if_more

        with patch.object(match_budget, "clear") as clear:
            _chain_if_more({"remaining": 40, "matched": 0, "filtered_out": 0})
        clear.assert_called_once()

    def test_the_budget_survives_a_batch_that_chains(self):
        from app.tasks import match as match_task

        with patch.object(match_budget, "clear") as clear, \
             patch.object(match_task.match_jobs, "delay"):
            match_task._chain_if_more({"remaining": 40, "matched": 5, "filtered_out": 1})
        clear.assert_not_called()
