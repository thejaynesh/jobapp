"""
A ranking learned from what you applied to and what you dismissed.

The match score is one model's reading of one posting against the profile. It
knows nothing of what you then did: that you apply to remote roles scored 72
and skip on-site ones scored 85, that one company's postings always get
dismissed, that "platform" in a title means yes and "full stack" means no. Your
decisions say all of that, and there are enough of them after a few weeks to
learn from.

The model is small on purpose: logistic regression over the match score, the
similarity, the keyword share, pay, remoteness, the gap between required and
actual years, freshness, and the words of the title, company and source. It
trains in well under a second on a few hundred decisions with no library. It
is scored on a held-out fifth of your decisions against the match score alone,
and the jobs list offers "For you" only once it has beaten it. A ranking you
cannot check is one you cannot trust.

Kept in the profile (`ranking_model`) and retrained on the schedule the
settings page sets.
"""

import math
import random
import re
from datetime import datetime, timezone

STORE_KEY = "ranking_model"
MIN_YES = 10
MIN_NO = 10
HOLDOUT = 0.2
ITERATIONS = 300
LEARNING_RATE = 0.5
L2 = 0.01
# Title words kept after training, by weight. Enough to carry what matters;
# few enough that the profile does not grow with every retrain.
MAX_WORDS = 300

_WORD = re.compile(r"[a-z][a-z0-9+#.]*")
_TITLE_STOP = frozenset("and or of the a an to in for with at ii iii iv i".split())


def features(job, my_years: float = 0.0, now: datetime | None = None) -> dict:
    """What the model reads about one job; any object with Job's columns."""
    now = now or datetime.now(timezone.utc)
    score = job.llm_score_deep if getattr(job, "llm_score_deep", None) is not None else job.llm_score
    salary = getattr(job, "salary_annual_max", None) or getattr(job, "salary_annual_min", None)
    posted = getattr(job, "posted_at", None) or getattr(job, "fetched_at", None)
    age_days = (now - posted).days if posted else 30
    required = getattr(job, "required_years", None)
    found = {
        "score": (score if score is not None else 50) / 100,
        "unscored": 1.0 if score is None else 0.0,
        "similarity": (getattr(job, "similarity", None) or 0) / 100,
        "keyword": getattr(job, "keyword_score", None) or 0.0,
        "remote": 1.0 if getattr(job, "is_remote", False) else 0.0,
        "salary_known": 1.0 if salary else 0.0,
        "salary": min(float(salary or 0), 300000.0) / 300000.0,
        "years_over": max(0.0, float(required or 0) - my_years) / 5.0,
        "age": min(max(age_days, 0), 60) / 60.0,
    }
    for word in set(_WORD.findall((job.title or "").lower())) - _TITLE_STOP:
        found["t:" + word] = 1.0
    if getattr(job, "company", None):
        found["c:" + job.company.strip().lower()] = 1.0
    if getattr(job, "source", None):
        found["s:" + job.source] = 1.0
    if getattr(job, "experience_level", None):
        found["l:" + job.experience_level] = 1.0
    return found


def _sigmoid(x: float) -> float:
    if x < -30:
        return 0.0
    if x > 30:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def _predict(weights: dict, bias: float, row: dict) -> float:
    return _sigmoid(bias + sum(weights.get(k, 0.0) * v for k, v in row.items()))


def train(rows: list[dict], labels: list[int]) -> tuple[dict, float]:
    """Class-balanced logistic regression by batch gradient descent."""
    n_pos = sum(labels) or 1
    n_neg = (len(labels) - sum(labels)) or 1
    sample_weight = [len(labels) / (2 * n_pos) if y else len(labels) / (2 * n_neg) for y in labels]
    weights: dict = {}
    bias = 0.0
    n = len(rows)
    for _ in range(ITERATIONS):
        grad: dict = {}
        grad_bias = 0.0
        for row, y, w in zip(rows, labels, sample_weight):
            error = (_predict(weights, bias, row) - y) * w
            grad_bias += error
            for k, v in row.items():
                grad[k] = grad.get(k, 0.0) + error * v
        bias -= LEARNING_RATE * grad_bias / n
        for k, g in grad.items():
            current = weights.get(k, 0.0)
            weights[k] = current - LEARNING_RATE * (g / n + L2 * current)
    return weights, bias


def auc(scores: list[float], labels: list[int]) -> float | None:
    """How often a yes is ranked above a no."""
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return None
    wins = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def _prune(weights: dict) -> dict:
    fixed = {k: v for k, v in weights.items() if ":" not in k}
    words = sorted(((k, v) for k, v in weights.items() if ":" in k),
                   key=lambda kv: abs(kv[1]), reverse=True)[:MAX_WORDS]
    return {**fixed, **{k: round(v, 5) for k, v in words}}


def fit(db, profile_data: dict, now: datetime | None = None, seed: int = 7) -> dict:
    """Train on every decision and say whether it beats the match score."""
    from app.services.experience import total_years
    from app.services.match_report import decisions

    now = now or datetime.now(timezone.utc)
    my_years = total_years((profile_data or {}).get("experience") or [])
    rows = decisions(db)
    labelled = [(features(r["job"], my_years, now), 1 if r["verdict"] == "yes" else 0,
                 r["score"]) for r in rows]
    n_yes = sum(1 for _, y, _ in labelled if y)
    n_no = len(labelled) - n_yes
    result = {"trained_at": now.isoformat(), "yes": n_yes, "no": n_no, "usable": False}
    if n_yes < MIN_YES or n_no < MIN_NO:
        return {**result, "reason": f"needs {MIN_YES} of each; has {n_yes} yes and {n_no} no"}

    order = list(range(len(labelled)))
    random.Random(seed).shuffle(order)
    cut = max(1, int(len(order) * HOLDOUT))
    test, train_idx = order[:cut], order[cut:]
    weights, bias = train([labelled[i][0] for i in train_idx], [labelled[i][1] for i in train_idx])
    test_labels = [labelled[i][1] for i in test]
    learned = auc([_predict(weights, bias, labelled[i][0]) for i in test], test_labels)
    baseline = auc([labelled[i][2] if labelled[i][2] is not None else 0 for i in test], test_labels)

    # The model the list uses is trained on everything; the holdout only
    # decides whether it is shown.
    weights, bias = train([r for r, _, _ in labelled], [y for _, y, _ in labelled])
    # A tie counts: on a held-out fifth of a few dozen decisions both often
    # order everything right, and the learned one then reads more than the
    # score does. Worse than the score, or no better than a coin, does not.
    usable = learned is not None and learned > 0.5 and (baseline is None or learned >= baseline)
    return {**result, "weights": _prune(weights), "bias": round(bias, 5),
            "auc": learned, "score_auc": baseline, "my_years": my_years, "usable": usable,
            "reason": "" if usable else "ordered your held-out decisions worse than the match "
                                        "score alone"}


def save(db, model: dict) -> None:
    import copy

    from app.models.profile import Profile

    profile = db.query(Profile).first()
    if profile is None:
        return
    data = copy.deepcopy(profile.data or {})
    data[STORE_KEY] = model
    profile.data = data
    db.commit()


def model_for(profile_data: dict | None) -> dict | None:
    """The stored model, when it has earned a place on the jobs list."""
    model = (profile_data or {}).get(STORE_KEY)
    return model if model and model.get("usable") and model.get("weights") else None


def rank(model: dict, jobs, now: datetime | None = None) -> list[tuple[float, object]]:
    """Each job with its probability of being a yes, best first."""
    now = now or datetime.now(timezone.utc)
    years = model.get("my_years") or 0.0
    scored = [(_predict(model["weights"], model["bias"], features(job, years, now)), job)
              for job in jobs]
    return sorted(scored, key=lambda pair: pair[0], reverse=True)
