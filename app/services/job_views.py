"""
Saved views of the jobs list.

The list takes twenty-odd filters, and the useful combinations are few and
used daily: remote, scored 75 or more, not applied to yet, sponsorship not
refused; or starred and still open. Rebuilding one by hand every morning is
friction the page puts on its most frequent use.

A view is a name and the query string of the list's own parameters, so it is
exactly the URL it stands for, and it is shown with how many jobs it holds
now. One view can be the default: `/jobs` with no parameters opens on it, and
"All jobs" (`?view=none`) is always one click away.

Kept in the profile (`job_views`), like the excluded companies and the other
lists the user keeps.
"""

import copy
import uuid
from urllib.parse import parse_qsl, urlencode

STORE_KEY = "job_views"
MAX_VIEWS = 12


def views(profile_data: dict | None) -> list[dict]:
    return [v for v in (profile_data or {}).get(STORE_KEY) or []
            if isinstance(v, dict) and v.get("id") and v.get("name")]


def default(saved: list[dict]) -> dict | None:
    return next((v for v in saved if v.get("default")), None)


def query_string(params: dict) -> str:
    """The set parameters as a stable query string; page and view are not part of a view."""
    from app.routers.jobs import FILTER_PARAMS

    return urlencode([(key, params[key]) for key in FILTER_PARAMS
                      if params.get(key) not in (None, "")])


def parse(query: str) -> dict:
    return dict(parse_qsl(query or "", keep_blank_values=False))


def with_counts(saved: list[dict], current_query: str, count) -> list[dict]:
    """Each view with how many jobs it holds now, and whether it is the one showing."""
    shown = []
    for view in saved:
        try:
            n = count(parse(view["query"]))
        except Exception:
            n = None
        shown.append({**view, "count": n, "active": view["query"] == current_query})
    return shown


def _write(db, change) -> dict:
    from app.services.profile_service import get_or_create_profile

    profile = get_or_create_profile(db)
    data = copy.deepcopy(profile.data or {})
    data[STORE_KEY] = change(views(data))
    profile.data = data
    db.commit()
    return data


def save(db, name: str, query: str) -> dict:
    """Save the list as it is now under `name`; a view of that name is replaced."""
    name = " ".join((name or "").split())[:60]
    if not name:
        raise ValueError("A view needs a name")
    view = {"id": uuid.uuid4().hex[:10], "name": name, "query": query or "", "default": False}

    def change(saved):
        kept = [v for v in saved if v["name"].lower() != name.lower()]
        replaced = next((v for v in saved if v["name"].lower() == name.lower()), None)
        if replaced:
            view["default"] = replaced.get("default", False)
        if len(kept) >= MAX_VIEWS:
            raise ValueError(f"At most {MAX_VIEWS} views; delete one first")
        return kept + [view]

    _write(db, change)
    return view


def set_default(db, view_id: str) -> None:
    """Make this the view /jobs opens on, or, if it already is, stop."""
    def change(saved):
        target = next((v for v in saved if v["id"] == view_id), None)
        if target is None:
            raise KeyError(view_id)
        turn_on = not target.get("default")
        return [{**v, "default": turn_on and v["id"] == view_id} for v in saved]

    _write(db, change)


def remove(db, view_id: str) -> None:
    _write(db, lambda saved: [v for v in saved if v["id"] != view_id])
