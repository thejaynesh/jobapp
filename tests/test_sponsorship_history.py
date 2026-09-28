"""
Employers' H-1B filings, from DOL's LCA disclosure workbooks to the job card.
The workbooks are built here, in the shape DOL publishes (measured
2026-09-28), and served through mocked `httpx.get`/`httpx.stream`. No network.
"""

import io
import uuid
import zipfile
from datetime import date, datetime, timezone
from xml.sax.saxutils import escape

import httpx
import pytest

from app.models.h1b_filing import H1bFiling
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services import sponsorship_history as sh
from app.services import tunables

COLUMNS = ["CASE_NUMBER", "CASE_STATUS", "RECEIVED_DATE", "DECISION_DATE", "ORIGINAL_CERT_DATE",
           "VISA_CLASS", "JOB_TITLE", "SOC_CODE", "NEW_EMPLOYMENT", "EMPLOYER_NAME"]


def lca(employer, decided: date, status="Certified", visa="H-1B", soc="15-1252.00", new=1):
    return {"CASE_STATUS": status, "DECISION_DATE": (decided - date(1899, 12, 30)).days,
            "VISA_CLASS": visa, "SOC_CODE": soc, "NEW_EMPLOYMENT": new,
            "EMPLOYER_NAME": employer, "JOB_TITLE": "Software Engineer",
            "CASE_NUMBER": f"I-200-{uuid.uuid4().hex[:8]}"}


def _letters(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def workbook(rows: list[dict]) -> bytes:
    """An .xlsx as DOL writes it: shared strings, except numbers; one inline string."""
    strings: list[str] = []

    def shared(text):
        if text not in strings:
            strings.append(text)
        return strings.index(text)

    def cell(col, row, value, inline=False):
        ref = f"{_letters(col)}{row}"
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"><v>{value}</v></c>'
        if inline:
            return f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
        return f'<c r="{ref}" t="s"><v>{shared(value)}</v></c>'

    lines = ['<row r="1">' + "".join(cell(i + 1, 1, name) for i, name in enumerate(COLUMNS)) + "</row>"]
    for n, row in enumerate(rows, start=2):
        cells = [cell(i + 1, n, row[name], inline=(name == "EMPLOYER_NAME" and n == 2))
                 for i, name in enumerate(COLUMNS) if row.get(name) not in (None, "")]
        lines.append(f'<row r="{n}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    sheet = f'<?xml version="1.0"?><worksheet {ns}><sheetData>{"".join(lines)}</sheetData></worksheet>'
    sst = (f'<?xml version="1.0"?><sst {ns}>'
           + "".join(f"<si><t>{escape(s)}</t></si>" for s in strings) + "</sst>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


PAGE = """<html>
<a href="https://www.dol.gov//media/LCA_Disclosure_Data_FY2026_Q3.xlsx">LCA FY2026 Q3</a>
<a href="/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2025_Q4.xlsx">Q4</a>
<a href="/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2025_Q3.xlsx">Q3</a>
<a href="/sites/dolgov/files/ETA/oflc/pdfs/FY26Q3/LCA_Worksites_FY_2026_Q3.xlsx">worksites</a>
<a href="/sites/dolgov/files/ETA/oflc/pdfs/PERM_Disclosure_Data_FY2026_Q3.xlsx">PERM</a>
</html>"""
URL_26Q3 = "https://www.dol.gov/media/LCA_Disclosure_Data_FY2026_Q3.xlsx"
URL_25Q4 = "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2025_Q4.xlsx"
URL_25Q3 = "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2025_Q3.xlsx"

# The current year to date in one file; past years a quarter per file.
FILES = {
    URL_26Q3: [lca("Google LLC", date(2025, 11, 3)), lca("Google LLC", date(2026, 3, 20)),
               lca("Google LLC", date(2026, 3, 21), soc="13-2011.00"),
               lca("Amazon.com Services LLC", date(2026, 5, 2)),
               lca("Amazon Web Services, Inc.", date(2026, 6, 9)),
               lca("Google LLC", date(2026, 6, 1), status="Withdrawn"),
               lca("Google LLC", date(2026, 6, 2), visa="E-3 Australian")],
    URL_25Q4: [lca("Google LLC", date(2025, 8, 1)), lca("Stripe, Inc.", date(2025, 9, 2)),
               lca("Stripe, Inc.", date(2025, 7, 9)), lca("Stripe, Inc.", date(2025, 8, 11)),
               lca("Stripe, Inc.", date(2025, 9, 12)), lca("Stripe, Inc.", date(2025, 9, 13)),
               lca("Stripe, Inc.", date(2025, 7, 14)),
               # A straggler decided the quarter before: this file does not speak for it.
               lca("Oldco Inc.", date(2025, 6, 30))],
    URL_25Q3: [lca("Oldco Inc.", date(2025, 5, 1))],
}


class Dol:
    def __init__(self, monkeypatch, files=None, page=PAGE):
        self.files = {url: workbook(rows) for url, rows in (files or FILES).items()}
        self.page = page
        self.downloads: list[str] = []
        self.gets: list[str] = []
        monkeypatch.setattr(httpx, "get", self.get)
        monkeypatch.setattr(httpx, "stream", self.stream)

    def get(self, url, **kw):
        self.gets.append(url)
        return httpx.Response(200, text=self.page, request=httpx.Request("GET", url))

    def stream(self, method, url, **kw):
        self.downloads.append(url)
        data = self.files[url]

        class Streamed:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                pass

            def iter_bytes(self):
                yield data[: len(data) // 2]
                yield data[len(data) // 2:]

        return Streamed()


def quarters(db):
    return sorted({q for (q,) in db.query(H1bFiling.quarter).distinct()})


def profile(db, overrides=None, sponsorship=""):
    db.query(Profile).delete()
    data = {tunables.STORE_KEY: overrides or {}}
    if sponsorship:
        data["screening_answers"] = {"sponsorship_required": sponsorship}
    db.add(Profile(data=data))
    db.commit()
    return db.query(Profile).first()


def refresh(db, state=None, **overrides):
    data = {tunables.STORE_KEY: overrides}
    if state:
        data[sh.STATE_KEY] = state
    report = sh.refresh(db, data)
    db.commit()
    return report


@pytest.fixture
def reading(monkeypatch, db):
    """The card's lookups read this test's database."""
    monkeypatch.setattr(sh, "_reader", lambda _=None: sh.read_snapshot(db))
    sh.reset_cache()


# --- Pieces ------------------------------------------------------------------

@pytest.mark.parametrize("filed,key", [
    ("Amazon.com Services LLC", "amazon com services"),
    ("Ernst & Young U.S. LLP", "ernst and young"),
    ("JPMorgan Chase & Co.", "jpmorgan chase"),
    ("BANK OF AMERICA N.A.", "bank of america"),
    ("The Broad Institute, Inc.", "broad institute"),
    ("Acme Holdings Inc. d/b/a Rocket", "acme"),
    ("Amazon Web Services (AWS)", "amazon web services"),
])
def test_employer_keys(filed, key):
    assert sh.employer_key(filed) == key


@pytest.mark.parametrize("day,label", [
    (date(2025, 10, 1), "FY2026 Q1"), (date(2025, 12, 31), "FY2026 Q1"),
    (date(2026, 3, 20), "FY2026 Q2"), (date(2026, 6, 30), "FY2026 Q3"),
    (date(2025, 9, 30), "FY2025 Q4"),
])
def test_federal_fiscal_quarters(day, label):
    assert sh.quarter_label(*sh.fiscal_quarter(day)) == label


def test_the_page_lists_its_lca_workbooks_newest_first():
    assert [(fy, q) for fy, q, _ in sh.disclosure_files(PAGE)] == [(2026, 3), (2025, 4), (2025, 3)]
    assert sh.disclosure_files(PAGE)[0][2] == URL_26Q3


def test_a_workbook_is_read_as_a_stream_of_rows(tmp_path):
    path = tmp_path / "lca.xlsx"
    path.write_bytes(workbook(FILES[URL_25Q4]))
    rows = list(sh.read_rows(str(path)))
    assert len(rows) == 8 and rows[0]["EMPLOYER_NAME"] == "Google LLC"   # the inline one
    assert set(rows[1]) <= set(sh._COLUMNS)                                 # others dropped
    counts, per_quarter = sh.aggregate(rows)
    assert counts[("FY2025 Q4", "stripe")]["certified"] == 6
    assert sh.authoritative(per_quarter) == {"FY2025 Q4"}


def test_only_certified_h1b_applications_count(tmp_path):
    counts, per_quarter = sh.aggregate(FILES[URL_26Q3])
    google = {q: e for (q, k), e in counts.items() if k == "google"}
    assert {q: e["certified"] for q, e in google.items()} == {"FY2026 Q1": 1, "FY2026 Q2": 2}
    assert google["FY2026 Q2"]["computer"] == 1
    # The withdrawn application is an H-1B row of the quarter, uncounted; the E-3 is neither.
    assert per_quarter["FY2026 Q3"] == 3


# --- Keeping it current ----------------------------------------------------------

class TestRefresh:
    def test_the_year_to_date_file_and_the_quarter_before_it(self, db, monkeypatch):
        dol = Dol(monkeypatch)
        report = refresh(db)
        assert sorted(dol.downloads) == sorted([URL_26Q3, URL_25Q4])
        # Q1-Q3 from the one file; Q4 from its own, without its June straggler.
        assert quarters(db) == ["FY2025 Q4", "FY2026 Q1", "FY2026 Q2", "FY2026 Q3"]
        assert report["window"] == ["FY2026 Q3", "FY2026 Q2", "FY2026 Q1", "FY2025 Q4"]
        assert not db.query(H1bFiling).filter_by(employer_key="oldco").count()

    def test_nothing_new_is_one_page_read(self, db, monkeypatch):
        dol = Dol(monkeypatch)
        state = refresh(db)["state"]
        dol.downloads.clear()
        refresh(db, state=state)
        assert dol.downloads == [] and len(dol.gets) == 2

    def test_a_new_quarter_replaces_the_year_to_date(self, db, monkeypatch):
        dol = Dol(monkeypatch)
        state = refresh(db)["state"]
        url_q4 = "https://www.dol.gov/media/LCA_Disclosure_Data_FY2026_Q4.xlsx"
        dol.page = PAGE.replace("https://www.dol.gov//media/LCA_Disclosure_Data_FY2026_Q3.xlsx",
                                url_q4)
        dol.files[url_q4] = workbook(FILES[URL_26Q3] + [lca("Stripe, Inc.", date(2026, 8, d))
                                                        for d in (1, 2, 3)])
        dol.downloads.clear()
        refresh(db, state=state)
        assert dol.downloads == [url_q4]
        # FY2025 Q4 has left the window of four.
        assert quarters(db) == ["FY2026 Q1", "FY2026 Q2", "FY2026 Q3", "FY2026 Q4"]
        google_q2 = db.query(H1bFiling).filter_by(quarter="FY2026 Q2", employer_key="google").one()
        assert google_q2.certified == 2       # replaced, not added to

    def test_a_download_that_fails_is_tried_again_next_time(self, db, monkeypatch):
        dol = Dol(monkeypatch)
        del dol.files[URL_25Q4]
        report = refresh(db)
        assert report["errors"] and URL_25Q4 not in report["state"]["files"]
        assert "FY2026 Q3" in quarters(db)


class TestTheSettingsPageControlsIt:
    def test_off_reads_nothing_and_shows_nothing(self, db, monkeypatch, reading):
        dol = Dol(monkeypatch)
        refresh(db)
        profile(db, {"h1b_history_enabled": False})
        sh.reset_cache()
        assert sh.for_company("Google") is None
        assert refresh(db, h1b_history_enabled=False) == {"skipped": "off"}
        assert len(dol.gets) == 1          # only the first refresh asked

    def test_the_quarters_counted(self, db, monkeypatch, reading):
        Dol(monkeypatch)
        refresh(db)
        profile(db)
        sh.reset_cache()
        assert sh.for_company("Google")["certified"] == 4
        profile(db, {"h1b_history_quarters": 2})
        sh.reset_cache()
        found = sh.for_company("Google")
        assert found["certified"] == 2 and found["period"] == "FY2026 Q2 – FY2026 Q3"

    def test_one_quarter_is_all_a_refresh_keeps(self, db, monkeypatch):
        Dol(monkeypatch)
        refresh(db, h1b_history_quarters=1)
        assert quarters(db) == ["FY2026 Q3"]


# --- Reading it ----------------------------------------------------------------

class TestLookup:
    @pytest.fixture(autouse=True)
    def _loaded(self, db, monkeypatch, reading):
        Dol(monkeypatch)
        refresh(db)
        profile(db)
        sh.reset_cache()

    def test_a_brand_takes_in_the_legal_entities_under_it(self):
        found = sh.for_company("Amazon")
        assert found["certified"] == 2
        assert found["names"] == ["Amazon.com Services LLC", "Amazon Web Services, Inc."] \
            or sorted(found["names"]) == ["Amazon Web Services, Inc.", "Amazon.com Services LLC"]

    def test_the_legal_name_matches_itself(self):
        assert sh.for_company("Stripe, Inc.")["certified"] == 6
        assert sh.for_company("STRIPE")["certified"] == 6

    def test_no_filings_is_zero_not_nothing(self):
        found = sh.for_company("Nobody Consulting")
        assert found["certified"] == 0 and found["names"] == []

    def test_a_short_name_does_not_sweep_in_everything(self):
        assert sh.for_company("Go")["certified"] == 0


class TestTheCard:
    def _job(self, db, company):
        job = Job(source="greenhouse", source_urls=[f"https://x/{uuid.uuid4()}"],
                  title="Backend Engineer", company=company, location="Remote",
                  url=f"https://x/{uuid.uuid4()}", description="d" * 400,
                  status=JobStatus.matched, fetched_at=datetime.now(timezone.utc),
                  dedupe_hash=uuid.uuid4().hex)
        db.add(job)
        db.commit()

    @pytest.fixture(autouse=True)
    def _loaded(self, db, monkeypatch, reading):
        Dol(monkeypatch)
        refresh(db)

    def test_an_employer_that_files_says_how_many(self, client, db):
        profile(db)
        sh.reset_cache()
        self._job(db, "Stripe")
        body = client.get("/jobs").text
        assert "H-1B: 6 filed" in body and "Stripe, Inc." in body

    def test_none_on_file_is_said_only_to_someone_who_needs_sponsorship(self, client, db):
        self._job(db, "Nobody Consulting")
        profile(db)
        sh.reset_cache()
        assert "No H-1B filings found" not in client.get("/jobs").text
        profile(db, sponsorship="Yes — I will require sponsorship (F-1 OPT, seeking H-1B)")
        sh.reset_cache()
        assert "No H-1B filings found" in client.get("/jobs").text

    def test_switched_off_the_card_says_nothing(self, client, db):
        profile(db, {"h1b_history_enabled": False})
        sh.reset_cache()
        self._job(db, "Stripe")
        assert "H-1B:" not in client.get("/jobs").text
