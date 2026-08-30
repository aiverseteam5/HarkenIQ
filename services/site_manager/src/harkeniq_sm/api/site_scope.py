"""Which site is an operator's request about? E1.3.

A Site Manager that serves one site can answer without being asked, and
did so for every read before E1.3. A Site Manager serving several cannot,
and must not guess: a read that quietly widened to "everything this
process holds" is precisely the class of leak E0.2 spent a slice closing
on the Central Command path.

So the rule is one sentence: **name the site, or be unambiguous.**

This is not authorization. It answers "which site is this request about",
never "who may ask" -- that stays the service identity the Site Manager
already authenticates, and human and agent authority stay above at
Central Command (E1.2), where E1.3 introduces nothing.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Query, Request

from harkeniq_sm.db.repos import SiteRepo


class SiteScope:
    """The resolved site for one request."""

    def __init__(self, site) -> None:
        self.site = site
        self.id = site.id
        self.name = site.name


async def resolve_site(
    request: Request, site: Optional[str] = Query(None, description="site name"),
) -> SiteScope:
    """FastAPI dependency: the site this request is about.

    * named explicitly -> that site, or 404
    * not named, one active site -> that site (pre-E1.3 behaviour, kept
      so an existing single-site deployment upgrades untouched)
    * not named, several -> **400**, naming them. Never all of them.
    """
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        repo = SiteRepo(session)
        if site:
            row = await repo.get_by_name(site)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"this Site Manager does not serve a site named {site!r}",
                )
            return SiteScope(row)

        rows = [s for s in await repo.list_all() if s.status == "active"]
        if len(rows) == 1:
            return SiteScope(rows[0])
        if not rows:
            return SiteScope(await repo.get_or_create(state.config.site_name))
        raise HTTPException(
            status_code=400,
            detail=(
                "this Site Manager serves "
                f"{len(rows)} sites ({', '.join(s.name for s in rows)}); name "
                "one with ?site=<name>. Answering for all of them would be "
                "the cross-site read this boundary exists to prevent"
            ),
        )
