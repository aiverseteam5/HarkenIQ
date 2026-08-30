"""Site enrollment: how a device gets a site, authoritatively.

E1.3 (2026-08-30). Ratified invariant:

    SITE IDENTITY MUST BE AUTHORITATIVE, NOT AGENT-DECLARED.

The Site Manager issues a site-bound, revocable credential. An agent
presents it at registration. The Site Manager resolves it to exactly one
site and persists the binding. There is no field on any message an agent
can set to choose or override its site.

Why not simply let the agent say
-------------------------------
It would be one additive proto field and it would be wrong. Every Site
Manager shares one service token across all of its agents, so a declared
site would be a *claim* any agent could make about any site -- and
correlation, blast radius, error budgets, metering and (since E1.2) both
human and agent authority all resolve from it. E0.2 spent a whole slice
making site identity authoritative rather than inferred; this keeps it
that way on the write path.

What this is NOT
----------------
Not an authorization model. It answers "which site is this device at",
never "what may anybody do". Authority stays at Central Command, above
the Site Manager, exactly as E1.2 landed it. A second resolver here is
explicitly forbidden by the ratified architecture.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("harkeniq.sm.enrollment")

#: Bytes of entropy in an issued credential. 32 bytes -> 64 hex chars.
TOKEN_BYTES = 32

#: Prefix so an operator can recognise one in a config file or a log,
#: and so a leaked string is greppable.
TOKEN_PREFIX = "hqe_"


class EnrollmentError(Exception):
    """The credential does not resolve to a servable site. Fail closed."""

    def __init__(self, reason: str, code: str = "invalid") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


def mint_token() -> str:
    """A fresh credential. Shown to the operator exactly once."""
    return TOKEN_PREFIX + secrets.token_hex(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Only the hash is stored.

    A leaked database therefore yields no usable enrollment credential.
    """
    return hashlib.sha256((token or "").strip().encode()).hexdigest()


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """sqlite returns naive datetimes for tz-aware writes."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_usable(row, *, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if _aware(getattr(row, "revoked_at", None)) is not None:
        return False
    expires = _aware(getattr(row, "expires_at", None))
    return expires is None or expires > now


@dataclass(frozen=True)
class Enrollment:
    """The resolved answer: this device belongs to this site."""

    site_id: str
    site_name: str
    token_id: str
    #: True when the site came from the Site Manager's single configured
    #: site rather than a credential -- the compatibility path for a
    #: deployment that predates E1.3 and serves exactly one site.
    legacy_single_site: bool = False


class EnrollmentService:
    """Issues, revokes and resolves site enrollment credentials."""

    def __init__(self, sessionmaker, config) -> None:
        self.sessionmaker = sessionmaker
        self.config = config

    # -- issuance ------------------------------------------------------

    async def issue(
        self,
        session,
        *,
        site_id: str,
        label: str = "",
        issued_by: str = "",
        expires_at: Optional[datetime] = None,
    ) -> tuple[str, object]:
        """Mint a credential for one site. Returns (secret, row).

        The secret is returned once and never stored; the caller shows it
        to the operator and forgets it.
        """
        from harkeniq_sm.db.models import SiteEnrollmentToken

        secret = mint_token()
        row = SiteEnrollmentToken(
            site_id=site_id,
            token_hash=hash_token(secret),
            label=label,
            issued_by=issued_by,
            expires_at=expires_at,
        )
        session.add(row)
        await session.flush()
        return secret, row

    async def revoke(self, session, row, revoked_by: str = "") -> object:
        row.revoked_at = datetime.now(timezone.utc)
        row.revoked_by = revoked_by
        await session.flush()
        return row

    # -- resolution ----------------------------------------------------

    async def resolve(
        self, session, token: str, *, now: Optional[datetime] = None
    ) -> Enrollment:
        """Which site does this credential name? Fail closed.

        Raises :class:`EnrollmentError` for an absent, unknown, revoked or
        expired credential, and for one naming a site this Site Manager
        no longer serves. A device that cannot prove its site does not
        get one.
        """
        from sqlalchemy import select

        from harkeniq_sm.db.models import Site, SiteEnrollmentToken

        if not (token or "").strip():
            # Compatibility, and ONLY where it is unambiguous: a Site
            # Manager serving exactly one site keeps behaving as it did
            # before E1.3, so an existing deployment upgrades without
            # re-enrolling its fleet. The moment a second site exists the
            # ambiguity is real and a credential becomes mandatory.
            sites = (
                await session.execute(
                    select(Site).where(Site.status == "active")
                )
            ).scalars().all()
            if len(sites) == 1:
                return Enrollment(
                    site_id=sites[0].id,
                    site_name=sites[0].name,
                    token_id="",
                    legacy_single_site=True,
                )
            if not sites:
                # Nothing registered yet: fall back to the configured
                # site so a first boot can still bootstrap itself.
                from harkeniq_sm.db.repos import SiteRepo

                site = await SiteRepo(session).get_or_create(
                    self.config.site_name
                )
                return Enrollment(
                    site_id=site.id,
                    site_name=site.name,
                    token_id="",
                    legacy_single_site=True,
                )
            raise EnrollmentError(
                "this Site Manager serves "
                f"{len(sites)} sites, so an enrollment credential is "
                "required: registration without one would have to guess "
                "which site this device is at",
                code="ambiguous",
            )

        row = (
            await session.execute(
                select(SiteEnrollmentToken).where(
                    SiteEnrollmentToken.token_hash == hash_token(token)
                )
            )
        ).scalar_one_or_none()

        if row is None:
            raise EnrollmentError(
                "unknown enrollment credential", code="unknown"
            )
        if not is_usable(row, now=now):
            raise EnrollmentError(
                "enrollment credential is revoked or expired",
                code="revoked",
            )

        site = await session.get(Site, row.site_id)
        if site is None:
            raise EnrollmentError(
                "the credential names a site this Site Manager no longer has",
                code="missing_site",
            )
        if site.status != "active":
            raise EnrollmentError(
                f"site {site.name!r} is {site.status}", code="inactive_site"
            )

        row.last_used_at = now or datetime.now(timezone.utc)
        row.use_count = (row.use_count or 0) + 1
        return Enrollment(
            site_id=site.id, site_name=site.name, token_id=row.id
        )
