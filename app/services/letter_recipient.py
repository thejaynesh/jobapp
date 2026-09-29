"""
Who a cover letter is addressed to.

Every letter opened "Dear Hiring Manager", to "Hiring Manager, <company>",
while the application page often knew exactly who that was: the outreach
panel finds the hiring manager named in the posting, the recruiters Hunter
knows, the people added by hand. A letter to a name is read as written to
someone. One to a title reads as a form letter.

The choice, from the application's contacts that are not archived:

* a hiring manager before a recruiter. Nobody else: an engineer or an
  executive is who you message, not who a letter is for;
* within a role, someone the posting itself names, then someone the user
  added, then anyone found by search;
* only a person's name, first and last. "Recruiting Team" is not a name, and a
  lone first name is too familiar for a letter's first line.

The salutation is "Dear <full name>". No honorific is ever guessed from a name.
"""

ROLE_ORDER = {"hiring_manager": 0, "recruiter": 1}
SOURCE_ORDER = {"description": 0, "manual": 1}
FALLBACK = "Hiring Manager"

_NOT_A_PERSON = ("team", "recruiting", "talent", "careers", "jobs", "hiring", "hr",
                 "people", "admin", "info")


def person_name(contact) -> str | None:
    """The contact's full name, when it is one."""
    name = " ".join((contact.name or "").split())
    if not name:
        name = " ".join(x for x in (contact.first_name, contact.last_name) if x).strip()
    words = name.split()
    if len(words) < 2 or any(w.lower().strip(".,") in _NOT_A_PERSON for w in words):
        return None
    return name


def candidates(contacts) -> list:
    """The contacts a letter could be addressed to, best first."""
    usable = [c for c in contacts or [] if not c.archived
              and c.role in ROLE_ORDER and person_name(c)]
    return sorted(usable, key=lambda c: (ROLE_ORDER[c.role], SOURCE_ORDER.get(c.source, 2)))


def recipient(contact) -> dict | None:
    if contact is None or not person_name(contact):
        return None
    return {"name": person_name(contact), "title": (contact.title or "").strip(),
            "contact_id": str(contact.id)}


def for_application(application) -> dict | None:
    """The recipient generation addresses a new letter to, or None for the fallback."""
    try:
        contacts = list(application.contacts or [])
    except TypeError:
        return None
    best = candidates(contacts)
    return recipient(best[0]) if best else None


def salutation(chosen: dict | None) -> str:
    return (chosen or {}).get("name") or FALLBACK
