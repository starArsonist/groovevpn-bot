import asyncio
from contextlib import suppress
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from loguru import logger

from src.config import settings
from src.logger import setup_logger
from src.adapters.db.session import engine, AsyncSessionLocal
from src.domain.base import Base

from src.adapters.db.repositories import UserRepository, OrderRepository, VPNProfileRepository
from src.adapters.db.trial_repository import TrialRepository
from src.adapters.marzban.client import marzban_client
from src.adapters.scheduler import run_periodically
from src.domain.trial_rules import TrialConfig
from src.use_cases.order_use_cases import CreateOrderUseCase
from src.use_cases.admin_use_cases import AdminUseCases
from src.use_cases.traffic_use_cases import CheckTrafficUseCase
from src.use_cases.trial_use_cases import ActivateTrialUseCase, TrialOfferUseCase
from src.use_cases.trial_monitor import TrialMonitorUseCase

from src.adapters.tg_bot.handlers.start import start_handler
from src.adapters.tg_bot.handlers.tariffs import tariffs_handler
from src.adapters.tg_bot.handlers.payment import payment_conv_handler
from src.adapters.tg_bot.handlers.admin import admin_decision_handler
from src.adapters.tg_bot.handlers.subscription import my_subscription_handler
from src.adapters.tg_bot.handlers.trial import trial_start_handler, TRIAL_CALLBACK
from src.adapters.tg_bot.notifier import TelegramTrialNotifier
from src.adapters.tg_bot.connect import build_connect_keyboard

TRIAL_CHECK_INTERVAL_SECONDS = 300

async def init_db():
    logger.info("Initializing database schema...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def main():
    setup_logger()
    
    # Init DB
    await init_db()

    # Create Telegram Application
    application = Application.builder().token(settings.telegram_bot_token).build()

    # DI Setup (storing shared resources in bot_data)
    application.bot_data["config"] = settings
    
    # Normally for async sessions in handlers you'd use a middleware,
    # but for simplicity we inject instances.
    # Note: In a production scale with high concurrency, you'd want to manage sessions per request.
    # We will instantiate Repositories and UseCases using a single shared session instance
    # or create a session inside the handler. Here we provide the async_sessionmaker to the bot_data
    # so handlers can instantiate Repos as needed, but for MVP we can pre-instantiate UseCases
    # and provide a sessionmaker to the UseCase or just use a session per handler.
    # The simplest approach is to store the sessionmaker in bot_data and let a middleware create a session.
    
    # For MVP, we will pre-instantiate repositories with a single session for the bot lifecycle,
    # or better, let's just make UseCases instantiate their own session or receive it.
    # Actually, we can wrap the UseCases to spawn sessions, but to keep it simple, we will provide
    # the repositories that will use a shared session. (Not ideal for concurrency).
    # Best MVP way:
    session = AsyncSessionLocal()
    user_repo = UserRepository(session)
    order_repo = OrderRepository(session)
    vpn_repo = VPNProfileRepository(session)
    
    # Триал: репозиторий работает через фабрику сессий (короткие транзакции),
    # потому что фоновая проверка идёт параллельно хендлерам.
    trial_repo = TrialRepository(AsyncSessionLocal)
    trial_config = TrialConfig(
        enabled=settings.trial_enabled,
        data_gb=settings.trial_data_gb,
        days=settings.trial_days,
        daily_cap=settings.trial_daily_cap,
    )
    activate_trial_uc = ActivateTrialUseCase(trial_repo, marzban_client, trial_config)
    trial_offer_uc = TrialOfferUseCase(trial_repo, trial_config)
    trial_monitor = TrialMonitorUseCase(
        trial_repo, marzban_client, TelegramTrialNotifier(application.bot)
    )

    # Кнопки подключения в один тап (страница-прослойка); при пустой/невалидной
    # конфигурации выключены, бот работает как раньше
    connect_keyboard = build_connect_keyboard(settings.connect_page_url, settings.connect_apps)

    create_order_uc = CreateOrderUseCase(order_repo, user_repo, vpn_repo)
    admin_uc = AdminUseCases(
        order_repo, user_repo, vpn_repo, marzban_client, application.bot,
        trial_repo=trial_repo, connect_keyboard=connect_keyboard,
    )
    traffic_uc = CheckTrafficUseCase(vpn_repo, marzban_client)
    
    application.bot_data["create_order_uc"] = create_order_uc
    application.bot_data["admin_uc"] = admin_uc
    application.bot_data["traffic_uc"] = traffic_uc
    application.bot_data["trial_repo"] = trial_repo
    application.bot_data["connect_keyboard"] = connect_keyboard
    application.bot_data["activate_trial_uc"] = activate_trial_uc
    application.bot_data["trial_offer_uc"] = trial_offer_uc

    # Register Handlers
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CallbackQueryHandler(start_handler, pattern="^start$"))
    application.add_handler(CallbackQueryHandler(tariffs_handler, pattern="^buy_vpn$"))
    application.add_handler(CallbackQueryHandler(my_subscription_handler, pattern="^my_subscription$"))
    application.add_handler(CallbackQueryHandler(trial_start_handler, pattern=f"^{TRIAL_CALLBACK}$"))
    
    # Admin handler
    application.add_handler(CallbackQueryHandler(admin_decision_handler, pattern="^(approve|reject)_"))
    
    # FSM for payment
    application.add_handler(payment_conv_handler)
    
    logger.info("Starting Telegram Bot...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    # Фоновая пакетная проверка триалов (состояние и флаги уведомлений - в БД)
    monitor_task = asyncio.create_task(
        run_periodically("trial-monitor", TRIAL_CHECK_INTERVAL_SECONDS, trial_monitor.run_once)
    )

    # Keep the bot running
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        monitor_task.cancel()
        with suppress(asyncio.CancelledError):
            await monitor_task

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
