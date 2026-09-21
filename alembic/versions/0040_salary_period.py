"""Record what the pay is per, and derive an annual figure to compare on.

`jobs.salary_min` and `salary_max` are floats with a currency beside them and
nothing recording *per what*. The extraction prompt asked the model to pick a
convention — "use the annual figure when the posting gives one; if it quotes an
hourly rate, give the hourly number" — so both land in the same column, and
`Job.salary_label` says so out loud in a comment: "Hourly and annual figures
land in the same column."

The display coped. The filter did not. `routers/jobs.py` compares
`coalesce(salary_max, salary_min) >= floor`, so a $100k floor hid a posting
stating $65/hour — about $135k a year, and one of the best-paying things in the
table — while admitting a posting stating €100,000 against a floor the user
meant in dollars. Three incompatible kinds of number compared as if they were
one, in the single place this project has repeatedly said a wrong number is
worse than a missing one.

So:

* `salary_period` — hour | day | week | month | year — what the stated figures
  are per. NULL means the posting did not say, which is not the same as "year";
  see below.
* `salary_annual_min` / `salary_annual_max` — the same band converted once, on
  write, with fixed multipliers. Indexed on the min like the stated column is,
  because this is what the filter now reads.

Two rules the derivation follows, both of them the conservative direction.

**No period means no annual figure.** Guessing "year" is how a $65 hourly rate
became a $65 salary in the first place. A row with stated figures and no period
is simply excluded from a floor, which is the same treatment the existing rule
already gives a row that states no pay at all.

**No currency conversion without a configured rate**, and there is no rate
table here — so a non-USD band gets a period and no annual figure. That keeps
€100,000 out of a dollar floor instead of admitting it, which is the error that
was being made.

Backfill is deliberately partial: existing rows get `salary_period = 'year'`
and an annual figure copied from the stated one **only** where the stated
figure is implausible as anything else. Above 20,000 in any currency, nobody
is quoting an hourly, daily, weekly or monthly rate. Everything at or below
that is left NULL rather than guessed at — it is exactly the population this
migration exists because we cannot read.

Revision ID: 0040
Revises: 0039
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels = None
depends_on = None

# Above this, a stated figure cannot be an hourly, daily, weekly or monthly
# rate in any currency this pipeline sees, so "per year" is a reading rather
# than a guess. Below it, the figure is ambiguous and stays that way.
_UNAMBIGUOUSLY_ANNUAL = 20_000


def upgrade() -> None:
    op.add_column("jobs", sa.Column("salary_period", sa.String(), nullable=True))
    op.add_column("jobs", sa.Column("salary_annual_min", sa.Float(), nullable=True))
    op.add_column("jobs", sa.Column("salary_annual_max", sa.Float(), nullable=True))
    op.create_index("ix_jobs_salary_annual_min", "jobs", ["salary_annual_min"])

    # Only the rows where the reading is forced. See the module docstring.
    #
    # The period is forced above the threshold whatever the currency — nobody
    # quotes 20,000 an hour in any of them — so it is stamped on all of those
    # rows. The annual *figure* is a second question, and copying it across
    # only happens where `services.job_details.annualise` would also do it:
    # USD, or a row that never recorded a currency. A €100,000 posting gets
    # `salary_period = 'year'` and NULL annual columns, so it is excluded from
    # a dollar floor rather than admitted to it, which is the error this
    # migration exists to stop. Backfilling it here and refusing it in the
    # code would leave the two disagreeing about the same row.
    op.execute(
        f"""
        UPDATE jobs
           SET salary_period = 'year'
         WHERE COALESCE(salary_max, salary_min) > {_UNAMBIGUOUSLY_ANNUAL}
        """
    )
    op.execute(
        f"""
        UPDATE jobs
           SET salary_annual_min = salary_min,
               salary_annual_max = salary_max
         WHERE COALESCE(salary_max, salary_min) > {_UNAMBIGUOUSLY_ANNUAL}
           AND (salary_currency IS NULL OR UPPER(salary_currency) IN ('', 'USD'))
        """
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_salary_annual_min", table_name="jobs")
    op.drop_column("jobs", "salary_annual_max")
    op.drop_column("jobs", "salary_annual_min")
    op.drop_column("jobs", "salary_period")
