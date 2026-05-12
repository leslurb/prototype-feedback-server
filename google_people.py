"""People API directory autocomplete."""
from __future__ import annotations

from typing import Optional

from googleapiclient.discovery import build

from google_oauth import get_credentials


def _service():
    creds = get_credentials(allow_browser=False)
    return build("people", "v1", credentials=creds, cache_discovery=False)


def search_directory(query: str, page_size: int = 10) -> list[dict]:
    """Search the user's Workspace directory for people matching `query`.

    Returns [{email, name, photo_url, org_title}].
    """
    if not query or len(query.strip()) < 2:
        return []
    svc = _service()
    try:
        resp = (
            svc.people()
            .searchDirectoryPeople(
                query=query,
                readMask="emailAddresses,names,photos,organizations",
                sources=["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"],
                pageSize=page_size,
            )
            .execute()
        )
    except Exception:
        # Fall back to broader contact search if directory isn't available.
        try:
            resp = (
                svc.people()
                .searchContacts(
                    query=query,
                    readMask="emailAddresses,names,photos,organizations",
                    pageSize=page_size,
                )
                .execute()
            )
        except Exception:
            return []

    out = []
    for item in resp.get("people", []) or resp.get("results", []):
        person = item.get("person", item)
        emails = person.get("emailAddresses") or []
        if not emails:
            continue
        names = person.get("names") or []
        photos = person.get("photos") or []
        orgs = person.get("organizations") or []
        out.append(
            {
                "email": emails[0].get("value"),
                "name": (names[0].get("displayName") if names else None),
                "photo_url": (photos[0].get("url") if photos else None),
                "org_title": (orgs[0].get("title") if orgs else None),
            }
        )
    return out
