from typing import Dict, Any, Optional
from loguru import logger
from src.adapters.db.repositories import VPNProfileRepository
from src.adapters.marzban.client import MarzbanClient
from src.domain.models import VPNProfile

class CheckTrafficUseCase:
    def __init__(self, vpn_profile_repo: VPNProfileRepository, marzban_client: MarzbanClient):
        self.vpn_profile_repo = vpn_profile_repo
        self.marzban_client = marzban_client

    async def execute(self, user_id: int) -> Optional[Dict[str, Any]]:
        logger.info(f"Checking traffic for user {user_id}")
        
        # 1. Get active profile from DB
        profile: VPNProfile = await self.vpn_profile_repo.get_by_user_id(user_id)
        if not profile:
            logger.info(f"No active profile found for user {user_id}")
            return None
            
        # 2. Get data from Marzban
        try:
            user_data = await self.marzban_client.get_user(profile.marzban_username)
            
            # 3. Calculate usage
            used_traffic = user_data.get("used_traffic", 0)
            data_limit = user_data.get("data_limit", 0)
            
            used_gb = round(used_traffic / (1024 ** 3), 2)
            limit_gb = round(data_limit / (1024 ** 3), 2) if data_limit else 0
            remaining_gb = round(limit_gb - used_gb, 2) if limit_gb else "Безлимит"
            
            return {
                "used_gb": used_gb,
                "limit_gb": limit_gb,
                "remaining_gb": remaining_gb,
                "sub_url": profile.sub_url
            }
            
        except Exception as e:
            logger.error(f"Error fetching traffic data for user {user_id}: {e}")
            raise
