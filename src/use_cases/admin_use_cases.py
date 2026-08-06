import re
import httpx
from telegram.ext import ExtBot
from loguru import logger
from src.adapters.db.repositories import OrderRepository, VPNProfileRepository, UserRepository
from src.adapters.marzban.client import MarzbanClient
from src.domain.models import Order, User, VPNProfile

class AdminUseCases:
    def __init__(self, order_repo: OrderRepository, user_repo: UserRepository, vpn_profile_repo: VPNProfileRepository, marzban_client: MarzbanClient, bot: ExtBot):
        self.order_repo = order_repo
        self.user_repo = user_repo
        self.vpn_profile_repo = vpn_profile_repo
        self.marzban_client = marzban_client
        self.bot = bot

    async def approve_order(self, order_id: int) -> bool:
        logger.info(f"Approving order {order_id}")
        order: Order = await self.order_repo.get_by_id(order_id)
        if not order or order.status != "pending":
            logger.warning(f"Order {order_id} not found or not pending")
            return False

        user: User = await self.user_repo.get_by_id(order.user_id)
        data_limit_bytes = order.tariff_gb * 1024 * 1024 * 1024
        
        try:
            if order.order_type == "topup":
                # TOP-UP FLOW
                profile: VPNProfile = await self.vpn_profile_repo.get_by_user_id(user.id)
                if not profile:
                    logger.error(f"Cannot topup: no VPN profile found for user {user.id}")
                    return False
                
                # Get current user limit
                try:
                    marzban_user = await self.marzban_client.get_user(profile.marzban_username)
                    current_limit = marzban_user.get("data_limit", 0) or 0
                    
                    # Add new limit
                    new_limit = current_limit + data_limit_bytes
                    await self.marzban_client.update_user(profile.marzban_username, new_limit)
                    
                    # Update order status
                    await self.order_repo.update(order.id, status="completed")
                    
                    # Notify user
                    success_msg = (
                        "✅ Ваша оплата подтверждена!\n\n"
                        f"Ваш тариф продлен на {order.tariff_gb} ГБ.\n"
                        "Приятного пользования!"
                    )
                    await self.bot.send_message(chat_id=user.id, text=success_msg, parse_mode="Markdown")
                    
                    return True
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        logger.warning(f"Marzban user {profile.marzban_username} not found. Falling back to new purchase flow.")
                        await self.vpn_profile_repo.update(profile.id, status="disabled")
                        order.order_type = "new"  # Fallback to new logic below
                    else:
                        raise e
                        
            if order.order_type == "new":
                # NEW PURCHASE FLOW
                marzban_username = f"user_{order.user_id}_{order.id}"

                # Fetch inbounds
                inbounds_response = await self.marzban_client.get_inbounds()
                inbounds_dict = {}
                for protocol, items in inbounds_response.items():
                    if isinstance(items, list):
                        tags = [item["tag"] for item in items if isinstance(item, dict) and "tag" in item]
                        if tags:
                            inbounds_dict[protocol] = tags
                
                # Create user in Marzban
                marzban_user = await self.marzban_client.create_user(
                    username=marzban_username,
                    data_limit=data_limit_bytes,
                    inbounds=inbounds_dict
                )
                
                sub_url = marzban_user.get("subscription_url", "")
                
                # Save VPNProfile in DB
                await self.vpn_profile_repo.create(
                    user_id=order.user_id,
                    marzban_username=marzban_username,
                    sub_url=sub_url,
                    status="active"
                )
                
                # Update order status
                await self.order_repo.update(order.id, status="completed")
                
                # Notify user
                success_msg = (
                    "✅ Ваша оплата подтверждена!\n\n"
                    "Ваша ссылка (ключ) для подключения:\n"
                    f"`{sub_url}`\n\n"
                    "Как добавить в приложение Happ:\n"
                    "1. Скопируйте ссылку подписки\n"
                    "2. Откройте приложение Happ\n"
                    "3. Нажмите на '+'\n"
                    "4. Вставить из буфера обмена"
                )
                await self.bot.send_message(chat_id=user.id, text=success_msg, parse_mode="Markdown")
                
                return True
                
        except Exception as e:
            logger.error(f"Error approving order {order_id}: {e}")
            return False

    async def reject_order(self, order_id: int) -> bool:
        logger.info(f"Rejecting order {order_id}")
        order = await self.order_repo.get_by_id(order_id)
        if not order or order.status != "pending":
            return False
            
        await self.order_repo.update(order.id, status="rejected")
        
        # Notify user
        reject_msg = (
            "❌ Ваша заявка на оплату была отклонена администратором.\n"
            "Если произошла ошибка, пожалуйста, попробуйте еще раз или свяжитесь с поддержкой."
        )
        try:
            await self.bot.send_message(chat_id=order.user_id, text=reject_msg)
        except Exception as e:
            logger.error(f"Failed to send reject message to {order.user_id}: {e}")
            
        return True
