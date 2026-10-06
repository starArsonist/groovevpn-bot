import time
from typing import Dict, Any, Optional
import httpx
from src.config import settings
from loguru import logger

USERS_BATCH_SIZE = 50


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

    async def create_user(
        self,
        username: str,
        data_limit: int,
        expire: Optional[int] = None,
        inbounds: Optional[Dict[str, list[str]]] = None,
    ) -> Dict[str, Any]:
        """Create a new VPN user in Marzban."""
        payload = {
            "username": username,
            "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
            "data_limit": data_limit,
            "expire": expire,
            "data_limit_reset_strategy": "no_reset",
            "status": "active"
        }
        if inbounds:
            payload["inbounds"] = inbounds

        logger.info(f"Creating Marzban user: {username}")
        return await self._request("POST", "/api/user", json=payload)

    async def get_user(self, username: str) -> Dict[str, Any]:
        """Get VPN user details from Marzban."""
        logger.info(f"Fetching Marzban user: {username}")
        return await self._request("GET", f"/api/user/{username}")

    async def update_user(
        self,
        username: str,
        data_limit: Optional[int] = None,
        expire: Optional[int] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update existing VPN user in Marzban (top-up / renewal)."""
        payload: Dict[str, Any] = {}
        if data_limit is not None:
            payload["data_limit"] = data_limit
        if expire is not None:
            payload["expire"] = expire
        if status is not None:
            payload["status"] = status
        logger.info(f"Updating Marzban user {username} with: {payload}")
        return await self._request("PUT", f"/api/user/{username}", json=payload)

    async def get_users(self, usernames: list[str]) -> list[Dict[str, Any]]:
        """Batch-запрос пользователей по списку имён (GET /api/users, порциями)."""
        users: list[Dict[str, Any]] = []
        for start in range(0, len(usernames), USERS_BATCH_SIZE):
            chunk = usernames[start:start + USERS_BATCH_SIZE]
            logger.info(f"Fetching {len(chunk)} Marzban users in one batch")
            data = await self._request(
                "GET", "/api/users", params={"username": chunk, "limit": len(chunk)}
            )
            users.extend(data.get("users", []))
        return users

    async def reset_user_data_usage(self, username: str) -> Dict[str, Any]:
        """Reset a user's used_traffic counter to zero."""
        logger.info(f"Resetting data usage for Marzban user: {username}")
        return await self._request("POST", f"/api/user/{username}/reset")

    async def get_inbounds(self) -> Dict[str, Any]:
        """Get all available inbounds from Marzban."""
        logger.info("Fetching inbounds from Marzban")
        return await self._request("GET", "/api/inbounds")

marzban_client = MarzbanClient(
    base_url=settings.marzban_api_url,
    username=settings.marzban_username,
    password=settings.marzban_password
)
