LAUNCH_OPTIONS = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}


async def safe_inner_text(element, selector: str, default: str = "") -> str:
    try:
        el = await element.query_selector(selector)
        if el:
            return (await el.inner_text()).strip()
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
