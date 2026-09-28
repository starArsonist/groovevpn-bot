import asyncio
import html
from datetime import datetime, timedelta, timezone
import httpx
from telegram.ext import ExtBot
from loguru import logger
from src.adapters.db.repositories import OrderRepository, VPNProfileRepository, UserRepository
from src.adapters.marzban.client import MarzbanClient
from src.domain.models import Order, User, VPNProfile
from src.domain.traffic_carryover import (
    RENEWAL_WINDOW_DAYS,
    calculate_renewal_plan,
    purchase_time_now,
)

BYTES_PER_GB = 1024 ** 3


class AdminUseCases:
    def __init__(self, order_repo: OrderRepository, user_repo: UserRepository, vpn_profile_repo: VPNProfileRepository, marzban_client: MarzbanClient, bot: ExtBot):
        self.order_repo = order_repo
        self.user_repo = user_repo
        self.vpn_profile_repo = vpn_profile_repo
        self.marzban_client = marzban_client
        self.bot = bot
        # Бот работает в единственном процессе на общей сессии (см. main.py), поэтому
        # in-process лока по order_id достаточно, чтобы закрыть гонку при двойном
        # клике администратора/повторной доставке одного и того же callback-а.
        self._order_locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, order_id: int) -> asyncio.Lock:
        lock = self._order_locks.get(order_id)
        if lock is None:
            lock = asyncio.Lock()
            self._order_locks[order_id] = lock
        return lock

    async def approve_order(self, order_id: int) -> bool:
        async with self._lock_for(order_id):
            return await self._approve_order_locked(order_id)

    async def _approve_order_locked(self, order_id: int) -> bool:
        logger.info(f"Approving order {order_id}")
        order: Order = await self.order_repo.get_by_id(order_id)
        if not order or order.status != "pending":
            logger.warning(f"Order {order_id} not found or not pending")
            return False

        user: User = await self.user_repo.get_by_id(order.user_id)
        purchased_bytes = order.tariff_gb * BYTES_PER_GB

        try:
            if order.order_type == "topup":
                profile: VPNProfile = await self.vpn_profile_repo.get_by_user_id(user.id)
                if not profile:
                    logger.error(f"Cannot topup: no VPN profile found for user {user.id}")
                    return False

                try:
                    return await self._apply_topup(order, user, profile, purchased_bytes)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code != 404:
                        raise
                    logger.warning(f"Marzban user {profile.marzban_username} not found. Falling back to new purchase flow.")
                    await self.vpn_profile_repo.update(profile.id, status="disabled")
                    order = await self.order_repo.update(order.id, order_type="new")

            return await self._apply_new_purchase(order, user, purchased_bytes)

        except Exception as e:
            logger.error(f"Error approving order {order_id}: {e}")
            return False

    async def _apply_topup(self, order: Order, user: User, profile: VPNProfile, purchased_bytes: int) -> bool:
        # 1. План (сколько перенести, новый лимит, новая дата окончания) считается
        # один раз и сохраняется до первого мутирующего вызова панели. При повторной
        # обработке (retry после сбоя, повторный вебхук) он берётся из БД, а не
        # пересчитывается заново по уже обнулённому used_traffic.
        if not order.plan_computed:
            marzban_user = await self.marzban_client.get_user(profile.marzban_username)
            plan = calculate_renewal_plan(
                current_status=marzban_user.get("status", "active"),
                current_data_limit=marzban_user.get("data_limit"),
                current_used_traffic=marzban_user.get("used_traffic", 0),
                purchased_bytes=purchased_bytes,
                purchase_time=purchase_time_now(),
            )
            order = await self.order_repo.update(
                order.id,
                plan_computed=True,
                planned_data_limit=plan.new_data_limit,
                planned_expire_at=plan.new_expire_at,
                carried_over_bytes=plan.carried_over_bytes,
            )

        # 2. Сброс used_traffic — только для клиентов с ограниченным лимитом
        # (планом предусмотрен перенос/новый лимит). Идемпотентен: если он уже
        # прошёл на предыдущей попытке, повторно не вызывается.
        if not order.reset_applied and order.planned_data_limit is not None:
            await self.marzban_client.reset_user_data_usage(profile.marzban_username)
            order = await self.order_repo.update(order.id, reset_applied=True)

        # 3. Применяем итоговые лимит/срок действия и возвращаем статус active.
        # PUT идемпотентен сам по себе — безопасно повторить при retry.
        await self.marzban_client.update_user(
            profile.marzban_username,
            data_limit=order.planned_data_limit,
            expire=order.planned_expire_at,
            status="active",
        )

        order = await self.order_repo.update(order.id, status="completed")

        await self.bot.send_message(
            chat_id=user.id,
            text=self._topup_success_message(order),
            parse_mode="HTML",
        )
        return True

    async def _apply_new_purchase(self, order: Order, user: User, purchased_bytes: int) -> bool:
        marzban_username = f"user_{order.user_id}_{order.id}"

        # Fetch inbounds
        inbounds_response = await self.marzban_client.get_inbounds()
        inbounds_dict = {}
        for protocol, items in inbounds_response.items():
            if isinstance(items, list):
                tags = [item["tag"] for item in items if isinstance(item, dict) and "tag" in item]
                if tags:
                    inbounds_dict[protocol] = tags

        expire_at = int((purchase_time_now() + timedelta(days=RENEWAL_WINDOW_DAYS)).timestamp())

        # Create user in Marzban
        marzban_user = await self.marzban_client.create_user(
            username=marzban_username,
            data_limit=purchased_bytes,
            expire=expire_at,
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
        expire_str = self._format_expire(expire_at)
        success_msg = (
            "✅ <b>Оплата подтверждена!</b>\n\n"
            "🔗 Ваша ссылка (ключ) для подключения:\n"
            f"<code>{html.escape(sub_url)}</code>\n\n"
            f"Действует до: <b>{expire_str}</b>\n\n"
            "📲 Как добавить в приложение Happ:\n"
            "1️⃣ Скопируйте ссылку подписки\n"
            "2️⃣ Откройте приложение Happ\n"
            "3️⃣ Нажмите на «+»\n"
            "4️⃣ Вставьте из буфера обмена"
        )
        await self.bot.send_message(chat_id=user.id, text=success_msg, parse_mode="HTML")

        return True

    def _topup_success_message(self, order: Order) -> str:
        expire_str = self._format_expire(order.planned_expire_at)
        if order.planned_data_limit is not None:
            carried_gb = round((order.carried_over_bytes or 0) / BYTES_PER_GB, 2)
            limit_gb = round(order.planned_data_limit / BYTES_PER_GB, 2)
            traffic_line = (
                f"Перенесено с предыдущего пакета: <b>{carried_gb} ГБ</b>\n"
                f"Новый лимит: <b>{limit_gb} ГБ</b>\n"
            )
        else:
            traffic_line = "Лимит трафика: <b>безлимит</b>\n"

        return (
            "✅ <b>Оплата подтверждена!</b>\n\n"
            f"Ваш тариф продлён на {order.tariff_gb} ГБ.\n"
            f"{traffic_line}"
            f"Действует до: <b>{expire_str}</b>\n"
            "Приятного пользования! 🚀"
        )

    @staticmethod
    def _format_expire(expire_at: int) -> str:
        return datetime.fromtimestamp(expire_at, tz=timezone.utc).strftime("%d.%m.%Y")

    async def reject_order(self, order_id: int) -> bool:
        logger.info(f"Rejecting order {order_id}")
        order = await self.order_repo.get_by_id(order_id)
        if not order or order.status != "pending":
            return False

        await self.order_repo.update(order.id, status="rejected")

        # Notify user
        reject_msg = (
            "❌ <b>Заявка отклонена администратором.</b>\n"
            "Если произошла ошибка, пожалуйста, попробуйте ещё раз или свяжитесь с поддержкой."
        )
        try:
            await self.bot.send_message(chat_id=order.user_id, text=reject_msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to send reject message to {order.user_id}: {e}")

        return True
