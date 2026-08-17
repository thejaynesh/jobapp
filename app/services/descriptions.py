"""
One text shape for every stored description.

Sources hand us the same posting in wildly different states: Greenhouse sends
HTML that it escaped before putting into JSON, an aggregator that re-serializes
Greenhouse's payload escapes it a second time (`&amp;lt;p&amp;gt;`), RSS feeds
send raw markup, and a blocked scrape stores the Cloudflare interstitial as if
it were the job. Everything downstream then reads that soup: the skill filter
greps it for keyword counts, the matcher prompt quotes it to the model, and the
document generator writes bullets from it.

So there is exactly one door into `Job.description`, and this is it. Call
`clean()` on the way in and the rest of the pipeline can assume plain text.

Two decisions worth stating, because both are the kind that look wrong later:

*Unescape twice, never three times.* Two passes take off both layers seen in
the wild. A third would start eating literal ampersands out of prose — "R&D"
arrives as `R&amp;D`, and one pass is what makes it "R&D" again.

*A block page is not a short description, it is no description.* Storing
"Verify you are human" as the job text is worse than storing nothing: the
matcher scores it, the filter counts its skills, and the generator quotes it.
Returning "" lets `no_description` mean what it says — and lets the enrichment
pass find the job and go get the real text.
"""

import html
import re
from html.parser import HTMLParser

# Tags that mean the text around them is markup, not prose. Matching a known
# list rather than "anything in angle brackets" keeps plain-text descriptions
# intact: "scale from 10<b of headcount" should not lose the rest of its line
# to an imaginary <b> tag.
_HTML_TAGS = (
    "p|div|span|br|hr|ul|ol|li|dl|dt|dd|table|thead|tbody|tfoot|tr|td|th"
    "|h[1-6]|a|b|i|u|em|strong|small|sub|sup|font|blockquote|pre|code"
    "|section|article|header|footer|nav|aside|main|figure|figcaption"
    "|script|style|img|iframe|form|input|button|label|center"
)
_LOOKS_LIKE_HTML = re.compile(rf"</?({_HTML_TAGS})\b[^<>]*>", re.IGNORECASE)

# Phrases that belong to a challenge page rather than to a job. Matched against
# lowercased text.
_BLOCK_PAGE_MARKERS = (
    "verify you are human",
    "enable javascript and cookies",
    "checking your browser before accessing",
    "please turn javascript on",
    "javascript is disabled in this browser",
    "unusual traffic from your computer",
    "are you a robot",
    "cloudflare",
    "security check",
    "ddos protection by",
    "access denied",
    "attention required",
    "captcha",
    "403 forbidden",
    "request blocked",
)

# Above this length, markers stop being evidence. Challenge pages are a few
# hundred characters of apology; a real posting that says "Cloudflare" is an
# infrastructure job, and throwing it away would be the worse error. Sized so
# the longest interstitials seen (Cloudflare's, with its ray ID and footer)
# still fall inside it.
_BLOCK_PAGE_MAX_LEN = 2000

# Tags after which text starts on a new line.
_BREAKING_TAGS = frozenset({
    "p", "div", "br", "hr", "ul", "ol", "dl", "dt", "dd", "table", "tr",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "section",
    "article", "header", "footer", "nav", "aside", "main", "figure", "form",
})

# Tags whose contents are machinery, and must not survive as text.
_OPAQUE_TAGS = frozenset({"script", "style", "head", "title", "noscript", "svg"})


class _TextExtractor(HTMLParser):
    """
    HTML in, readable text out, keeping the structure that carries meaning.

    List items become "- " lines because a requirements list read as one
    run-on paragraph loses the boundaries that make it a list — which is
    precisely what both the skill counter and the model are reading it for.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._opaque_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _OPAQUE_TAGS:
            self._opaque_depth += 1
        elif tag == "li":
            self._parts.append("\n- ")
        elif tag in _BREAKING_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _OPAQUE_TAGS:
            self._opaque_depth = max(0, self._opaque_depth - 1)
        elif tag in _BREAKING_TAGS:
            self._parts.append("\n")
        # `</li>` deliberately emits nothing: the next `<li>` already opens a
        # line, and closing one too would put a blank line between every
        # bullet — pure padding in a text whose whole job is to be read.

    def handle_data(self, data: str) -> None:
        if not self._opaque_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _strip_tags(text: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        # Malformed markup should cost us the formatting, not the posting.
        return re.sub(r"<[^>]+>", " ", text)
    return parser.text()


def _collapse(text: str) -> str:
    """Trim each line, drop repeated blanks, and lose empty bullets."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Entities unescape into characters that read as text but compare as
    # something else; a non-breaking space is the one that bites, because
    # `"a b".split()` and `"a\xa0b".split()` disagree about how many words
    # there are — and the skill counter splits on whitespace.
    text = text.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")

    lines: list[str] = []
    for raw_line in text.split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
        if line in ("-", "*", "•"):
            continue  # a bullet whose item was an image or an empty tag
        if not line:
            if not lines or lines[-1] == "":
                continue  # no leading blank, never two in a row
            lines.append("")
        else:
            lines.append(line)

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def looks_like_block_page(text: str | None) -> bool:
    """
    True when this text is a bot wall rather than a job posting.

    Exposed separately so adapters can drop the posting entirely instead of
    storing a job with no description — a RemoteOK row that only ever had a
    challenge page in it was never a real listing to begin with.
    """
    if not text:
        return False
    if len(text) > _BLOCK_PAGE_MAX_LEN:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _BLOCK_PAGE_MARKERS)


def clean(text: str | None) -> str:
    """
    Canonical plain text for a job description, or "" when there isn't one.

    Idempotent: cleaning already-clean text returns it unchanged, which is what
    lets every write path call it without anybody tracking who called it first.
    """
    if not text:
        return ""

    unescaped = html.unescape(html.unescape(str(text)))
    if _LOOKS_LIKE_HTML.search(unescaped):
        unescaped = _strip_tags(unescaped)
    collapsed = _collapse(unescaped)

    if looks_like_block_page(collapsed):
        return ""
    return collapsed
