"""Public listing formats observed when a page has no JobPosting JSON-LD."""

import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


def _text(parts) -> str:
    return " ".join(" ".join(parts).split())


def _job(source, slug, title, url, location="", job_id=None, company=None):
    from app.services.sources.base import parse_experience_level

    return {
        "source": source, "source_job_id": job_id,
        "title": title, "company": company or slug, "url": url,
        "location": location, "is_remote": "remote" in f"{title} {location}".lower(),
        # A listing card is not a description. Let enrichment fetch the detail.
        "description": "", "posted_at": None,
        "experience_level": parse_experience_level(title, ""),
    }


class _ListingReader(HTMLParser):
    def __init__(self, source, slug, url):
        super().__init__(convert_charrefs=True)
        self.source, self.slug, self.url = source, slug, url
        self.jobs = {}
        self.card_tag = "tr" if source == "jobvite" else "li"
        self.depth = 0
        self.link = ""
        self.title = []
        self.location = []
        self.in_title = False
        self.in_location = False
        self.after_link = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.source == "ycombinator":
            raw = attrs.get("data-page")
            if raw:
                self._yc(raw)
            return
        if tag == self.card_tag:
            if self.depth == 0:
                self.link, self.title, self.location = "", [], []
                self.in_title = self.in_location = self.after_link = False
            self.depth += 1
        if not self.depth:
            return
        if tag == "td":
            self.in_location = "jv-job-list-location" in (attrs.get("class") or "").split()
        if tag == "a":
            candidate = urljoin(self.url, attrs.get("href") or "")
            parsed = urlsplit(candidate)
            pattern = (
                rf"/{re.escape(self.slug)}/job/[^/]+/?$"
                if self.source == "jobvite" else r"/jobs/\d+-[^/]+/?$"
            )
            if (parsed.scheme in ("http", "https")
                    and parsed.hostname == urlsplit(self.url).hostname
                    and re.fullmatch(pattern, parsed.path)):
                self.link = candidate
                self.in_title = True

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        elif self.depth and (self.in_location or (
                self.source == "teamtailor" and self.after_link)):
            self.location.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.in_title:
            self.in_title = False
            self.after_link = True
        if tag == "td":
            self.in_location = False
        if tag != self.card_tag or not self.depth:
            return
        self.depth -= 1
        if self.depth or not self.link:
            return
        title = _text(self.title)
        if not title:
            return
        location = _text(self.location)
        if self.source == "teamtailor":
            # Cards separate department, location, and optional work mode with
            # middle dots. Keep the location and mode, not the department.
            parts = [part.strip() for part in location.split("·") if part.strip()]
            mode = parts.pop() if parts and re.search(r"remote|hybrid|on.?site", parts[-1], re.I) else ""
            location = "; ".join(part for part in [parts[-1] if parts else "", mode] if part)
        job_id = urlsplit(self.link).path.rstrip("/").split("/")[-1]
        if self.source == "teamtailor":
            job_id = job_id.split("-", 1)[0]
        self.jobs[self.link] = _job(
            self.source, self.slug, title, self.link, location,
            job_id=f"{self.slug}:{job_id}",
        )

    def _yc(self, raw):
        try:
            page = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(page, dict) or page.get("component") != "WaasJobListingsPage":
            return
        props = page.get("props")
        rows = props.get("jobPostings") if isinstance(props, dict) else None
        if not isinstance(rows, list):
            return
        for item in rows:
            if not isinstance(item, dict):
                continue
            title, company, path = (item.get(key) for key in ("title", "companyName", "url"))
            if not all(isinstance(value, str) and value.strip() for value in (title, company, path)):
                continue
            url = urljoin(self.url, path)
            parsed = urlsplit(url)
            if (parsed.scheme not in ("http", "https")
                    or parsed.hostname != urlsplit(self.url).hostname
                    or not re.fullmatch(r"/companies/[^/]+/jobs/[^/]+/?", parsed.path)):
                continue
            location = item.get("location")
            self.jobs[url] = _job(
                self.source, self.slug, title.strip(), url,
                location=location if isinstance(location, str) else "",
                job_id=str(item["id"]) if item.get("id") is not None else None,
                company=company.strip(),
            )


class _CardReader(HTMLParser):
    """Read Built In and iCIMS cards using explicit field markers."""

    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, source, slug, url):
        super().__init__(convert_charrefs=True)
        self.source, self.slug, self.url = source, slug, url
        self.stack = []
        self.fields = {}
        self.link = ""
        self.jobs = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        marker = attrs.get("data-id")
        if not self.stack:
            is_card = (marker == "job-card" if self.source == "builtin"
                       else "iCIMS_JobCardItem" in classes)
            if not is_card:
                return
            self.fields, self.link = {}, ""
        field = self.stack[-1][1] if self.stack else ""
        if "sr-only" in classes or tag in {"script", "style"}:
            field = "ignore"
        elif self.source == "builtin":
            if marker == "company-title":
                field = "company"
            elif marker == "job-card-title":
                field = "title"
            elif tag == "i" and len(self.stack) >= 2:
                # Icon wrapper and text are siblings inside the field row.
                if "fa-location-dot" in classes:
                    self.stack[-2][1] = field = "location"
                elif "fa-house-building" in classes:
                    self.stack[-2][1] = field = "mode"
        else:
            if tag == "div" and {"header", "left"}.issubset(classes):
                field = "location"
            elif tag == "h3":
                field = "title"
        if tag == "a":
            candidate = urljoin(self.url, attrs.get("href") or "")
            parsed = urlsplit(candidate)
            pattern = (r"/job/[^/]+/\d+/?" if self.source == "builtin"
                       else r"/jobs/\d+/[^/]+/job/?")
            if (parsed.scheme in {"http", "https"}
                    and parsed.hostname == urlsplit(self.url).hostname
                    and re.fullmatch(pattern, parsed.path)):
                self.link = parsed._replace(query="", fragment="").geturl()
        if tag not in self._VOID:
            self.stack.append([tag, field])

    def handle_data(self, data):
        if self.stack and self.stack[-1][1] not in {"", "ignore"}:
            self.fields.setdefault(self.stack[-1][1], []).append(data)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                if not self.stack:
                    self._finish()
                break

    def _finish(self):
        title = _text(self.fields.get("title", []))
        company = _text(self.fields.get("company", []))
        if not title or not self.link or (self.source == "builtin" and not company):
            return
        location = "; ".join(filter(None, (
            _text(self.fields.get("location", [])), _text(self.fields.get("mode", [])),
        )))
        path = urlsplit(self.link).path.strip("/").split("/")
        job_id = path[-1] if self.source == "builtin" else f"{self.slug}:{path[1]}"
        self.jobs[self.link] = _job(self.source, self.slug, title, self.link,
                                    location, job_id, company)


def extract_listing_jobs(html: str, url: str, source: str, slug: str) -> list[dict]:
    """Read only recognized listing formats and same-host posting links."""
    if source not in {"jobvite", "teamtailor", "ycombinator", "builtin", "icims"}:
        return []
    reader_type = _CardReader if source in {"builtin", "icims"} else _ListingReader
    reader = reader_type(source, slug, url)
    reader.feed(html)
    return list(reader.jobs.values())
