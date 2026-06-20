from urllib.parse import quote_plus

LAUNCH_OPTIONS = {
    "headless": True,
    "args": [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
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
