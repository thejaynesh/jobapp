import json
from unittest.mock import patch


def _profile():
    return {
        "target_roles": ["Software Engineer", "Backend Engineer"],
        "skills": {"languages": ["Java", "Python"], "frameworks": ["Spring Boot"]},
    }


class TestExpandSearchQueries:
    def test_generates_and_caches_queries(self):
        from app.services.query_expansion import expand_search_queries
        raw = json.dumps(["Java Developer", "Software Developer"])
        with patch("app.services.query_expansion.chat_completion", return_value=raw):
            queries, cache = expand_search_queries(_profile(), "k", "u", "m")
        assert queries[:2] == ["Software Engineer", "Backend Engineer"]
        assert "Java Developer" in queries
        assert cache is not None
        assert cache["queries"] == queries
        assert cache["basis"]

    def test_cache_hit_skips_llm(self):
        from app.services.query_expansion import expand_search_queries, _basis_hash
        profile = _profile()
        skills_flat = ["Java", "Python", "Spring Boot"]
        profile["search_query_cache"] = {
            "basis": _basis_hash(profile["target_roles"], skills_flat),
            "queries": ["Software Engineer", "Backend Engineer", "Java Developer"],
        }
        with patch("app.services.query_expansion.chat_completion") as mock_cc:
            queries, cache = expand_search_queries(profile, "k", "u", "m")
        mock_cc.assert_not_called()
        assert cache is None
        assert queries == ["Software Engineer", "Backend Engineer", "Java Developer"]

    def test_stale_cache_regenerates(self):
        from app.services.query_expansion import expand_search_queries
        profile = _profile()
        profile["search_query_cache"] = {"basis": "old-hash", "queries": ["Old Query"]}
        raw = json.dumps(["Java Developer"])
        with patch("app.services.query_expansion.chat_completion", return_value=raw):
            queries, cache = expand_search_queries(profile, "k", "u", "m")
        assert "Old Query" not in queries
        assert cache is not None

    def test_llm_failure_falls_back_to_roles_without_caching(self):
        from app.services.query_expansion import expand_search_queries
        with patch("app.services.query_expansion.chat_completion", side_effect=Exception("down")):
            queries, cache = expand_search_queries(_profile(), "k", "u", "m")
        assert queries == ["Software Engineer", "Backend Engineer"]
        assert cache is None

    def test_dedupes_case_insensitively_and_caps(self):
        from app.services.query_expansion import expand_search_queries, MAX_QUERIES
        raw = json.dumps([
            "software engineer",  # dup of a role
            "Java Developer", "Java Developer",  # dup of itself
            "Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8", "Q9",
        ])
        with patch("app.services.query_expansion.chat_completion", return_value=raw):
            queries, _ = expand_search_queries(_profile(), "k", "u", "m")
        assert len(queries) <= MAX_QUERIES
        assert len([q for q in queries if q.lower() == "software engineer"]) == 1
        assert len([q for q in queries if q == "Java Developer"]) == 1

    def test_empty_roles_returns_empty(self):
        from app.services.query_expansion import expand_search_queries
        queries, cache = expand_search_queries({"target_roles": []}, "k", "u", "m")
        assert queries == []
        assert cache is None
