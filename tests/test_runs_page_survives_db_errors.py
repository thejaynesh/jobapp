"""
One failed panel query must not take the whole Runs page down.

The page loads each subsystem in its own try block so a broken one degrades
to a note. A database error broke that promise: the failed transaction was
left on the session, the next panel's query raised PendingRollbackError, and
the page answered 500 — which is what a Postgres restart during a deploy did.
"""

from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from app.models.profile import Profile


def test_a_panel_whose_query_fails_does_not_break_the_page(client, db):
    db.add(Profile(data={}))
    db.commit()

    def broken(session, *args, **kwargs):
        # Fail the way a dropped connection does: inside the transaction.
        try:
            session.execute(__import__("sqlalchemy").text("SELECT * FROM no_such_table"))
        except Exception as exc:
            raise OperationalError("SELECT", {}, exc) from exc

    with patch("app.services.browser_tasks.queue_stats", side_effect=broken):
        response = client.get("/runs")
    assert response.status_code == 200
    assert "Compare matching models" in response.text
