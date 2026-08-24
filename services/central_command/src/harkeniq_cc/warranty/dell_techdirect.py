"""Dell TechDirect warranty adapter (R4-2 P15).

Documented API shape (TechDirect "Warranty & Entitlements", key issued
via TechDirect enrollment):
  - OAuth2 client-credentials token:
      POST https://apigtwb2c.us.dell.com/auth/oauth/v2/token
      grant_type=client_credentials + client_id/client_secret
      -> {"access_token": ..., "expires_in": 3600, ...}
  - Entitlements (up to 100 service tags per call):
      GET https://apigtwb2c.us.dell.com/PROD/sbil/eapi/v5/asset-entitlements
          ?servicetags=TAG1,TAG2
      Authorization: Bearer <token>
      -> [{"serviceTag": ..., "entitlements": [
            {"serviceLevelDescription": ..., "startDate": ...,
             "endDate": ...}, ...]}, ...]

The record's end date is the LATEST entitlement end date (a device
often carries several entitlements: base warranty + ProSupport etc.).
Failures return partial results and never raise into the refresh loop.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from harkeniq_cc.warranty.base import WarrantyProvider, WarrantyRecord

logger = logging.getLogger("harkeniq.cc.warranty.dell")

DEFAULT_TOKEN_URL = "https://apigtwb2c.us.dell.com/auth/oauth/v2/token"
DEFAULT_API_URL = (
    "https://apigtwb2c.us.dell.com/PROD/sbil/eapi/v5/asset-entitlements"
)
BATCH_SIZE = 100  # documented per-call service-tag limit


class DellTechDirectProvider(WarrantyProvider):
    name = "dell_techdirect"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_url: str = DEFAULT_TOKEN_URL,
        api_url: str = DEFAULT_API_URL,
        timeout: float = 30.0,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.api_url = api_url
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0

    async def _get_token(self, client: httpx.AsyncClient) -> Optional[str]:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        try:
            resp = await client.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Dell TechDirect token request failed: %s", e)
            return None
        self._token = data.get("access_token")
        self._token_expiry = time.time() + float(data.get("expires_in", 3600))
        return self._token

    async def fetch(self, service_tags: list[str]) -> list[WarrantyRecord]:
        tags = [t for t in service_tags if t]
        if not tags:
            return []
        records: list[WarrantyRecord] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            token = await self._get_token(client)
            if token is None:
                return []
            for start in range(0, len(tags), BATCH_SIZE):
                batch = tags[start:start + BATCH_SIZE]
                try:
                    resp = await client.get(
                        self.api_url,
                        params={"servicetags": ",".join(batch)},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    resp.raise_for_status()
                    assets = resp.json()
                except Exception as e:
                    logger.warning(
                        "Dell TechDirect entitlement fetch failed "
                        "(batch of %d): %s", len(batch), e,
                    )
                    continue
                if not isinstance(assets, list):
                    continue
                for asset in assets:
                    record = self._parse_asset(asset)
                    if record:
                        records.append(record)
        return records

    @staticmethod
    def _parse_asset(asset: dict) -> Optional[WarrantyRecord]:
        tag = str(asset.get("serviceTag", "") or "")
        if not tag:
            return None
        entitlements = asset.get("entitlements") or []
        best: dict = {}
        for ent in entitlements:
            if not isinstance(ent, dict):
                continue
            if str(ent.get("endDate", "") or "") > str(best.get("endDate", "") or ""):
                best = ent
        return WarrantyRecord(
            service_tag=tag,
            vendor="dell",
            service_level=str(best.get("serviceLevelDescription", "") or ""),
            start_date=str(best.get("startDate", "") or "")[:10],
            end_date=str(best.get("endDate", "") or "")[:10],
            source="dell_techdirect",
        )
