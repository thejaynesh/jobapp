from urllib.parse import quote_plus

LAUNCH_OPTIONS = {
    "headless": True,
    "args": [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-setuid-sandbox",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ],
}

CONTEXT_OPTIONS = {
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "viewport": {"width": 1280, "height": 800},
    "locale": "en-US",
}


def encode(text: str) -> str:
    return quote_plus(text)


async def safe_inner_text(element, *selectors: str, default: str = "") -> str:
    for sel in selectors:
        try:
            el = await element.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    return text
        except Exception:
            pass
    return default


async def safe_get_attribute(element, selector: str, attr: str, default: str = "") -> str:
    try:
        el = await element.query_selector(selector)
        if el:
            val = await el.get_attribute(attr)
            return val or default
    except Exception:
        pass
    return default


def is_remote_location(location: str, title: str) -> bool:
    text = (location + " " + title).lower()
    return "remote" in text


# Text that means we were served an interstitial rather than the page we asked
# for. Distinguishing this from "the markup changed" decides the fix entirely:
# one needs different egress, the other needs new selectors.
_CHALLENGE_MARKERS = (
    "just a moment", "captcha", "access denied", "are you a robot",
    "unusual traffic", "verify you are human", "attention required",
    "checking your browser", "enable javascript", "request blocked",
    "403 forbidden", "pardon our interruption",
)


async def describe_page(page, max_chars: int = 300) -> str:
    """
    Describe what a scraper actually received, for when it finds no results.

    "No job cards found with any selector" is true but useless on its own — it
    reads identically whether the site redesigned or handed us a bot challenge.
    """
    title, url, body = "", "", ""
    try:
        url = page.url or ""
    except Exception:
        pass
    try:
        title = (await page.title()) or ""
    except Exception:
        pass
    try:
        body = (await page.inner_text("body"))[:2000]
    except Exception:
        pass

    haystack = f"{title} {body}".lower()
    blocked = any(marker in haystack for marker in _CHALLENGE_MARKERS)
    snippet = " ".join(body.split())[:max_chars]

    description = f"url={url!r} title={title!r} body_chars={len(body)}"
    if blocked:
        description += " — looks like a bot check/block, not a markup change"
    if snippet:
        description += f" | starts: {snippet!r}"
    return description
