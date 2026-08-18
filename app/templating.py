"""
One place that builds a template environment, so every page agrees on time.

Everything in this database is stored as timezone-aware UTC, which is the only
sane thing to store. Every page then rendered it with a bare `strftime`, which
prints whatever the datetime carries — so a fetch at half past four in the
afternoon appeared as `Aug 18 23:53`, and the only way to read the Runs page
was to do arithmetic in your head. Worse, `datetime.now()` in a template filter
and a UTC timestamp in the row would have disagreed silently.

So: one filter, one setting, one conversion. `DISPLAY_TIMEZONE` names the zone
(`America/Los_Angeles` by default, which is Pacific and handles the PST/PDT
switch on its own — a fixed -8 would be an hour wrong for two thirds of the
year).

Storage is untouched. This is a rendering concern and lives entirely in the
rendering layer; nothing that compares, sorts or expires a timestamp goes near
it.
"""

from fastapi.templating import Jinja2Templates

from app.services.timefmt import label, local_time, on, when


def install(templates: Jinja2Templates) -> Jinja2Templates:
    """Add the shared filters to a template environment."""
    templates.env.filters["when"] = when
    templates.env.filters["local"] = local_time
    # For dates a source stated rather than instants. See `timefmt.on`.
    templates.env.filters["on"] = on
    # Callable rather than a value: the abbreviation changes with daylight
    # saving, and a string captured at import would say PST all summer.
    templates.env.globals["tz_label"] = label
    return templates


def build(directory: str = "app/templates") -> Jinja2Templates:
    """
    A template environment with this app's filters already on it.

    A factory rather than a single shared instance because several routers add
    globals of their own (the profile page registers its region options), and
    one environment shared across them would leak those everywhere.
    """
    return install(Jinja2Templates(directory=directory))
