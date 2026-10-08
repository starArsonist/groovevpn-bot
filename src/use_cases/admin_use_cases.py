import asyncio
import html
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import httpx
from telegram import InlineKeyboardMarkup
from telegram.ext import ExtBot
from loguru import logger
from src.adapters.db.order_settlement import BALANCE_PHOTO, CancelOutcome, CompletionResult, OrderSettlement
from src.adapters.db.repositories import OrderRepository, VPNProfileRepository, UserRepository
from src.adapters.db.trial_repository import TrialRepository
from src.adapters.marzban.client import MarzbanClient
from src.adapters.marzban.inbounds import build_inbounds_payload
from src.adapters.tg_bot.connect import CONNECT_HINT, ConnectKeyboard, send_with_fallback
from src.adapters.tg_bot.support import support_button
from src.domain.clock import utc_now
from src.domain.models import Order, User, VPNProfile
from src.domain.referral_rules import ReferralConfig
from src.domain.tariffs import get_tariff
from src.domain.trial_rules import build_trial_conversion_plan
from src.use_cases.referral_use_cases import RewardNotificationUseCase
from src.domain.traffic_carryover import (
    RENEWAL_WINDOW_DAYS,
    calculate_renewal_plan,
    purchase_time_now,
)

BYTES_PER_GB = 1024 ** 3


class AdminUseCases:
    def __init__(self, order_repo: OrderRepository, user_repo: UserRepository, vpn_profile_repo: VPNProfileRepository, marzban_client: MarzbanClient, bot: ExtBot, trial_repo: TrialRepository | None = None, connect_keyboard: ConnectKeyboard | None = None, payments: OrderSettlement | None = None, referral_config: ReferralConfig | None = None, reward_notifier: RewardNotificationUseCase | None = None, clock: Callable[[], datetime] = utc_now):
        self.order_repo = order_repo
        self.user_repo = user_repo
        self.vpn_profile_repo = vpn_profile_repo
        self.marzban_client = marzban_client
        self.bot = bot
        self.trial_repo = trial_repo
        self.connect_keyboard = connect_keyboard
        # Денежные транзакции заказа (баланс, бонус приглашённого, награда). Без них
        # (payments=None) approve/reject работают как раньше.
        self.payments = payments
        self.referral_config = referral_config or ReferralConfig(enabled=False)
        self.reward_notifier = reward_notifier
        self._clock = clock
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
        order: Order = await self.order_repo.get_fresh(order_id)
        if not order or order.status != "pending":
            logger.warning(f"Order {order_id} not found or not pending")
            return False

        if self.payments is not None and not await self._payment_is_valid(order):
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
                    # План продления (перенос остатка) к новому пользователю не относится
                    order = await self.order_repo.update(
                        order.id,
                        order_type="new",
                        plan_computed=False,
                        planned_data_limit=None,
                        planned_expire_at=None,
                        carried_over_bytes=None,
                        reset_applied=False,
                    )

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
            # get_user нужен и для триала: это проба существования пользователя в
            # Marzban (при 404 сработает fallback на создание нового пользователя).
            marzban_user = await self.marzban_client.get_user(profile.marzban_username)
            if await self._is_unconverted_trial_user(user.id):
                # Триал - не платный пакет: остаток не переносится, пользователь
                # обновляется как при обычной покупке (reset, лимит = пакет, +30 дней).
                plan = build_trial_conversion_plan(
                    purchased_bytes=purchased_bytes,
                    purchase_time=purchase_time_now(),
                )
            else:
                plan = calculate_renewal_plan(
                    current_status=marzban_user.get("status", "active"),
                    current_data_limit=marzban_user.get("data_limit"),
                    current_used_traffic=marzban_user.get("used_traffic", 0),
                    purchased_bytes=purchased_bytes,
                    purchase_time=purchase_time_now(),
                )
            bonus = await self._claim_invitee_bonus(order, limited=plan.new_data_limit is not None)
            order = await self.order_repo.update(
                order.id,
                plan_computed=True,
                planned_data_limit=None if plan.new_data_limit is None else plan.new_data_limit + bonus,
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

        await self._mark_trial_converted(user.id)
        completion = await self._complete_order(order)
        if completion is None:
            return False

        await self.bot.send_message(
            chat_id=user.id,
            text=self._topup_success_message(order, await self._bonus_bytes(order.id)),
            parse_mode="HTML",
        )
        await self._notify_reward(completion)
        return True

    async def _apply_new_purchase(self, order: Order, user: User, purchased_bytes: int) -> bool:
        marzban_username = f"user_{order.user_id}_{order.id}"

        existing_profile = await self.vpn_profile_repo.get_by_marzban_username(marzban_username)
        if existing_profile is not None and existing_profile.user_id != order.user_id:
            logger.error(f"Order {order.id}: Marzban name {marzban_username} belongs to a profile of another user")
            return False

        # План (лимит и срок) считается один раз и сохраняется до первого вызова
        # Marzban, как и в продлении: повтор после сбоя берёт его из БД.
        if not order.plan_computed:
            bonus = await self._claim_invitee_bonus(order, limited=True)
            order = await self.order_repo.update(
                order.id,
                plan_computed=True,
                planned_data_limit=purchased_bytes + bonus,
                planned_expire_at=int((purchase_time_now() + timedelta(days=RENEWAL_WINDOW_DAYS)).timestamp()),
                carried_over_bytes=0,
            )

        inbounds_dict = build_inbounds_payload(await self.marzban_client.get_inbounds())

        try:
            marzban_user = await self.marzban_client.create_user(
                username=marzban_username,
                data_limit=order.planned_data_limit,
                expire=order.planned_expire_at,
                inbounds=inbounds_dict
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 409:
                raise
            marzban_user = await self._adopt_existing_marzban_user(order, marzban_username)

        sub_url = marzban_user.get("subscription_url", "")

        if existing_profile is None:
            await self.vpn_profile_repo.create(
                user_id=order.user_id,
                marzban_username=marzban_username,
                sub_url=sub_url,
                status="active"
            )
        else:
            await self.vpn_profile_repo.update(existing_profile.id, sub_url=sub_url, status="active")

        # Update order status
        await self._mark_trial_converted(user.id)
        completion = await self._complete_order(order)
        if completion is None:
            return False

        # Notify user
        await self._notify_first_issue(
            user.id, sub_url, self._format_expire(order.planned_expire_at), await self._bonus_bytes(order.id)
        )
        await self._notify_reward(completion)

        return True

    async def _adopt_existing_marzban_user(self, order: Order, marzban_username: str) -> dict:
        """409 на создании: прошлая попытка уже создала пользователя. Переиспользуем его,
        только если имя соответствует нашей схеме для этого telegram_id, и применяем сохранённый план."""
        if not self._is_own_marzban_username(marzban_username, order.user_id):
            raise RuntimeError(f"Marzban user {marzban_username} does not belong to user {order.user_id}")
        logger.warning(f"Marzban user {marzban_username} already exists (retry of order {order.id}); applying saved plan")
        marzban_user = await self.marzban_client.get_user(marzban_username)
        await self.marzban_client.update_user(
            marzban_username,
            data_limit=order.planned_data_limit,
            expire=order.planned_expire_at,
            status="active",
        )
        return marzban_user

    @staticmethod
    def _is_own_marzban_username(marzban_username: str, user_id: int) -> bool:
        match = re.fullmatch(r"user_(\d+)_(\d+)", marzban_username)
        return match is not None and int(match.group(1)) == user_id

    async def _notify_first_issue(self, user_id: int, sub_url: str, expire_str: str, bonus_bytes: int = 0) -> None:
        """Сообщение с первой выдачей ссылки. Кнопки подключения - только здесь (не при продлении)."""

        def build_text(with_buttons: bool) -> str:
            instructions = (
                CONNECT_HINT
                if with_buttons
                else (
                    "Как добавить в приложение Happ:\n"
                    "1. Скопируйте ссылку подписки\n"
                    "2. Откройте приложение Happ\n"
                    "3. Нажмите на «+»\n"
                    "4. Вставьте из буфера обмена"
                )
            )
            return (
                "✅ <b>Оплата подтверждена!</b>\n\n"
                "Ваша ссылка (ключ) для подключения:\n"
                f"<code>{html.escape(sub_url)}</code>\n\n"
                f"Действует до: <b>{expire_str}</b>\n"
                f"{self._bonus_line(bonus_bytes)}\n"
                f"{instructions}"
            )

        async def deliver(text: str, markup: InlineKeyboardMarkup | None) -> None:
            if markup is None:
                await self.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
            else:
                await self.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=markup)

        connect_rows = self.connect_keyboard.app_rows(sub_url, "first_purchase") if self.connect_keyboard else []
        plain = (build_text(with_buttons=False), None)
        if not connect_rows:
            await deliver(*plain)
            return

        with_buttons = (
            build_text(with_buttons=True),
            InlineKeyboardMarkup([*connect_rows, [support_button()]]),
        )
        await send_with_fallback(deliver, with_buttons, plain)

    def _topup_success_message(self, order: Order, bonus_bytes: int = 0) -> str:
        expire_str = self._format_expire(order.planned_expire_at)
        if order.planned_data_limit is not None:
            carried_gb = round((order.carried_over_bytes or 0) / BYTES_PER_GB, 2)
            limit_gb = round(order.planned_data_limit / BYTES_PER_GB, 2)
            carried_line = f"Перенесено с предыдущего пакета: <b>{carried_gb} ГБ</b>\n" if carried_gb > 0 else ""
            traffic_line = f"{carried_line}Новый лимит: <b>{limit_gb} ГБ</b>\n"
        else:
            traffic_line = "Лимит трафика: <b>безлимит</b>\n"

        return (
            "✅ <b>Оплата подтверждена!</b>\n\n"
            f"Ваш тариф продлён на {order.tariff_gb} ГБ.\n"
            f"{traffic_line}"
            f"{self._bonus_line(bonus_bytes)}"
            f"Действует до: <b>{expire_str}</b>\n"
            "Приятного пользования!"
        )

    @staticmethod
    def _bonus_line(bonus_bytes: int) -> str:
        if bonus_bytes <= 0:
            return ""
        bonus_gb = bonus_bytes / BYTES_PER_GB
        value = int(bonus_gb) if float(bonus_gb).is_integer() else round(bonus_gb, 2)
        return f"Бонус по приглашению: +{value} ГБ\n"

    async def _payment_is_valid(self, order: Order) -> bool:
        """Заказ без реальной цены или оплаты не подтверждается: цена известна и положительна,
        удержание баланса есть в журнале, нулевая денежная часть допустима только при полной оплате балансом."""
        tariff = get_tariff(order.tariff_gb)
        if tariff is None or tariff.price_rub <= 0:
            logger.error(f"Order {order.id} refused: no price for tariff {order.tariff_gb}GB")
            return False
        payment = await self.payments.ensure_payment(order.id, tariff.price_rub, self._clock())
        if payment.price_rub <= 0:
            logger.error(f"Order {order.id} refused: non-positive price")
            return False
        if payment.balance_rub > 0 and not await self.payments.has_hold(order.id, payment.balance_rub):
            logger.error(f"Order {order.id} refused: balance hold is missing in the ledger")
            return False
        paid_by_balance = order.photo_file_id == BALANCE_PHOTO
        if payment.cash_rub == 0 and not (paid_by_balance and payment.balance_rub == payment.price_rub):
            logger.error(f"Order {order.id} refused: nothing was paid")
            return False
        if paid_by_balance and payment.cash_rub != 0:
            logger.error(f"Order {order.id} refused: marked as paid by balance but cash is due")
            return False
        return True

    async def _claim_invitee_bonus(self, order: Order, limited: bool) -> int:
        """Бонус приглашённого для плана заказа (0 - не положен). Заявка атомарна и идемпотентна."""
        if self.payments is None:
            return 0
        bonus = self.referral_config.invitee_bonus_bytes if limited else 0
        return await self.payments.claim_invitee_bonus(
            order.id, order.user_id, bonus, self._clock(), self.referral_config.enabled
        )

    async def _bonus_bytes(self, order_id: int) -> int:
        if self.payments is None:
            return 0
        payment = await self.payments.get_payment(order_id)
        return payment.bonus_bytes if payment is not None else 0

    async def _complete_order(self, order: Order) -> CompletionResult | None:
        """pending -> completed. С подключёнными платежами - одной транзакцией с наградой; None, если заказ уже не pending."""
        if self.payments is None:
            await self.order_repo.update(order.id, status="completed")
            return CompletionResult(completed=True)
        result = await self.payments.complete(order.id, self._clock(), self.referral_config)
        await self.order_repo.get_fresh(order.id)  # синхронизирует кэш общей сессии
        if not result.completed:
            logger.warning(f"Order {order.id} was not pending at completion; nothing changed")
            return None
        return result

    async def _notify_reward(self, completion: CompletionResult) -> None:
        if completion.reward is not None and self.reward_notifier is not None:
            await self.reward_notifier.notify(completion.reward.referral_id)

    async def _is_unconverted_trial_user(self, user_id: int) -> bool:
        return self.trial_repo is not None and await self.trial_repo.has_unconverted(user_id)

    async def _mark_trial_converted(self, user_id: int) -> None:
        if self.trial_repo is None:
            return
        if await self.trial_repo.mark_converted(user_id, utc_now()):
            logger.info(f"Trial of user {user_id} converted to a paid package")

    @staticmethod
    def _format_expire(expire_at: int) -> str:
        return datetime.fromtimestamp(expire_at, tz=timezone.utc).strftime("%d.%m.%Y")

    async def cancel_order(self, order_id: int, user_id: int) -> CancelOutcome:
        """Пользователь отменяет свой неоплаченный заказ; резерв баланса возвращается.
        Переход pending -> cancelled условен и идёт под тем же lock, что подтверждение и отклонение."""
        if self.payments is None:
            return CancelOutcome.NOT_PENDING
        async with self._lock_for(order_id):
            logger.info(f"User {user_id} cancels order {order_id}")
            outcome = await self.payments.cancel(order_id, user_id, self._clock())
            await self.order_repo.get_fresh(order_id)
            return outcome

    async def reject_order(self, order_id: int) -> bool:
        async with self._lock_for(order_id):
            return await self._reject_order_locked(order_id)

    async def _reject_order_locked(self, order_id: int) -> bool:
        logger.info(f"Rejecting order {order_id}")
        order = await self.order_repo.get_fresh(order_id)
        if not order or order.status != "pending":
            return False

        if self.payments is not None:
            rejected = await self.payments.reject(order.id, self._clock())
            await self.order_repo.get_fresh(order.id)
        else:
            rejected = await self.order_repo.transition_status(order.id, "pending", "rejected")
        if not rejected:
            return False

        # Notify user
        reject_msg = (
            "❌ <b>Заявка отклонена администратором.</b>\n"
            "Если произошла ошибка, пожалуйста, попробуйте ещё раз или свяжитесь с поддержкой."
        )
        try:
            await self.bot.send_message(
                chat_id=order.user_id,
                text=reject_msg,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[support_button()]]),
            )
        except Exception as e:
            logger.error(f"Failed to send reject message to {order.user_id}: {e}")

        return True
