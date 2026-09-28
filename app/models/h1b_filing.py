from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class H1bFiling(Base):
    """
    One employer's certified H-1B labor condition applications in one federal
    fiscal quarter, counted from the Department of Labor's public disclosure
    files (`services.sponsorship_history`).

    Counts, not cases: a quarter is ~200,000 applications, and the question
    asked of them is only "has this employer sponsored lately, and for what".
    """

    __tablename__ = "h1b_filings"

    # "FY2026 Q2": the fiscal quarter of the decision date.
    quarter: Mapped[str] = mapped_column(String(12), primary_key=True)
    # `sponsorship_history.employer_key` of the name as filed.
    employer_key: Mapped[str] = mapped_column(String, primary_key=True)
    # The name as filed most often under that key.
    employer_name: Mapped[str] = mapped_column(String, nullable=False)
    certified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Computer and mathematical occupations (SOC 15-).
    computer: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Workers the applications are for who are new to the employer's H-1B roll
    # (NEW_EMPLOYMENT), as against extensions and transfers.
    new_employment: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
