import time
from typing import Dict, Any, Optional
import httpx
from src.config import settings
from loguru import logger

class MarzbanClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0

    async def _get_token(self) -> str:
        # Check if we have a valid token (with 5 seconds margin)
        if self._access_token and time.time() < self._token_expires_at - 5:
            return self._access_token

        logger.info("Requesting new Marzban API token")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/admin/token",
                data={
                    "username": self.username,
                    "password": self.password,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            response.raise_for_status()
            data = response.json()
            self._access_token = data["access_token"]
            # Assume token is valid for 1 hour if not specified
            expires_in = data.get("expires_in", 3600)
            self._token_expires_at = time.time() + expires_in
            return self._access_token

    async def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        token = await self._get_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        
        async with httpx.AsyncClient() as client:
            url = f"{self.base_url}{endpoint}"
            response = await client.request(method, url, headers=headers, **kwargs)
            response.raise_for_status()
            return response.json()

    async def create_user(self, username: str, data_limit: int) -> Dict[str, Any]:
        """Create a new VPN user in Marzban."""
        payload = {
            "username": username,
            "proxies": {"vless": {}},
            "data_limit": data_limit,
            "expire": None,
            "data_limit_reset_strategy": "no_reset",
            "status": "active"
        }
        logger.info(f"Creating Marzban user: {username}")
        return await self._request("POST", "/api/user", json=payload)

    async def get_user(self, username: str) -> Dict[str, Any]:
        """Get VPN user details from Marzban."""
        logger.info(f"Fetching Marzban user: {username}")
        return await self._request("GET", f"/api/user/{username}")

marzban_client = MarzbanClient(
    base_url=settings.marzban_api_url,
    username=settings.marzban_username,
    password=settings.marzban_password
)
