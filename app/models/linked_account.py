"""
A board whose API we can call directly, and the credential that lets us.

Most sources need no state: an ATS board is a public URL, and an aggregator's
key sits in the environment. Tsenta is the first that needs something a person
had to be signed in to obtain — its API takes a Firebase ID token, which lasts
an hour, and the only durable half is the refresh token its web SDK keeps in
the browser.

So the extension hands that over once, and the server mints ID tokens from it
for as long as it stays valid. What is stored here is exactly what the user's
own browser already holds for the same site, and it buys the one thing the
extension path cannot: sweeps that happen whether or not a laptop is open.

A table rather than a key on the profile blob, for one specific reason. The
refresh token **rotates** — Google may return a new one on every mint — so this
is written by a Celery task on a schedule, while the web process writes the
profile blob for settings, board caches and expanded queries. Two writers on
one JSONB document is the lost-update bug this project has already fixed once,
and a credential is a bad place to rediscover it.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LinkedAccount(Base):
    __tablename__ = "linked_accounts"

    # One row per site, so re-linking is an upsert and there is never a
    # question of which of two credentials is current.
    site: Mapped[str] = mapped_column(String, primary_key=True)

    # Firebase's public web API key, read from the page rather than configured:
    # it is per-project and it is already in their JavaScript bundle.
    api_key: Mapped[str] = mapped_column(String, nullable=False)

    # The long-lived half. Text rather than String because these are long and
    # their length is not ours to bound.
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)

    # When the extension last handed one over. Every visit to the board
    # re-links, so a stale value here is itself the diagnosis: the browser has
    # not been on that site since, and a credential that stops working will not
    # fix itself until it is.
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # The last successful mint, and the last failure. Both, because "never
    # worked" and "worked until Tuesday" are different problems and the panel
    # should not have to guess which one it is looking at.
    last_minted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
