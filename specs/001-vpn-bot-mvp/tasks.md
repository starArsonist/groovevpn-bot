# Tasks: vpn-bot-mvp

**Input**: Design documents from `/specs/001-vpn-bot-mvp/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [ ] T001 Initialize Python project with `uv` in `pyproject.toml`
- [ ] T002 [P] Create configuration files in `docker-compose.yml` and `Dockerfile`
- [ ] T003 [P] Configure `.env` parsing and configuration management in `src/config.py`
- [ ] T004 [P] Setup `loguru` logging in `src/logger.py`
- [ ] T005 Setup SQLite with SQLAlchemy (`async_sessionmaker`, base metadata) in `src/adapters/db/session.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [ ] T006 Create `Base` declarative class in `src/domain/base.py`
- [ ] T007 [P] Create `User` model in `src/domain/models/user.py`
- [ ] T008 [P] Create `Order` model in `src/domain/models/order.py`
- [ ] T009 [P] Create `VPNProfile` model in `src/domain/models/vpn_profile.py`
- [ ] T010 Implement Repository Pattern (`UserRepository`, `OrderRepository`, `VPNProfileRepository`) in `src/adapters/db/repositories.py`
- [ ] T011 Implement `MarzbanClient` (OAuth2 auth with caching) in `src/adapters/marzban/client.py`

**Checkpoint**: Foundation ready - user story implementation can now begin in parallel

---

## Phase 3: User Story 1 - Выбор и покупка пакета (Priority: P1) 🎯 MVP

**Goal**: Клиент видит главное меню, выбирает тариф, получает реквизиты и отправляет фото чека.

**Independent Test**: Запуск бота, прохождение флоу от `/start` до отправки фото и проверка БД на наличие заявки (без админки).

### Implementation for User Story 1

- [ ] T012 [P] [US1] Create `CreateOrderUseCase` in `src/use_cases/order_use_cases.py`
- [ ] T013 [P] [US1] Implement `/start` command handler and main menu in `src/adapters/tg_bot/handlers/start.py`
- [ ] T014 [US1] Implement tariff selection keyboard and handler in `src/adapters/tg_bot/handlers/tariffs.py`
- [ ] T015 [US1] Implement `ConversationHandler` (FSM) to wait for and handle receipt photo in `src/adapters/tg_bot/handlers/payment.py`
- [ ] T016 [US1] Forward receipt photo to admin and create inline buttons "Approve"/"Reject" in `src/adapters/tg_bot/handlers/payment.py`

**Checkpoint**: At this point, User Story 1 should be fully functional and testable independently

---

## Phase 4: User Story 2 & 3 - Подтверждение заявки и Получение доступа (Priority: P1)

**Goal**: Админ подтверждает заявку, бот автоматически создает пользователя в Marzban, сохраняет в БД и отправляет клиенту ссылку.

**Independent Test**: Нажатие кнопки "Подтвердить" в чате админа, успешный запрос к API Marzban, сохранение в `VPNProfile`, получение VLESS ссылки клиентом.

### Implementation for User Story 2 & 3

- [ ] T017 [US2] Create `ApproveOrderUseCase` (orchestrates API, DB update, notification) in `src/use_cases/admin_use_cases.py`
- [ ] T018 [US2] Implement admin callback query handler for "Approve"/"Reject" buttons in `src/adapters/tg_bot/handlers/admin.py`
- [ ] T019 [US3] Implement logic to send `subscription_url` and instructions to client inside `ApproveOrderUseCase` in `src/use_cases/admin_use_cases.py`

**Checkpoint**: At this point, User Stories 1, 2, AND 3 should work independently

---

## Phase 5: User Story 4 - Проверка остатка трафика (Priority: P2)

**Goal**: Клиент запрашивает остаток трафика, бот получает данные из Marzban и показывает остаток.

**Independent Test**: Нажатие кнопки "Моя подписка", успешный запрос к API, корректный расчет остатка трафика.

### Implementation for User Story 4

- [ ] T020 [US4] Create `CheckTrafficUseCase` calling Marzban API in `src/use_cases/traffic_use_cases.py`
- [ ] T021 [US4] Implement "Моя подписка" handler in `src/adapters/tg_bot/handlers/subscription.py`

**Checkpoint**: All user stories should now be independently functional

---

## Phase 6: Polish & Application Assembly

**Purpose**: Improvements that affect multiple user stories and wiring it all together

- [ ] T022 Setup Dependency Injection in `bot_data` for DB sessions and `MarzbanClient` in `src/main.py`
- [ ] T023 Register all handlers to the `Application` and implement startup/shutdown in `src/main.py`
- [ ] T024 Write Alembic migrations setup in `alembic.ini` and `alembic/` (or rely on `Base.metadata.create_all`)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion - BLOCKS all user stories
- **User Stories (Phase 3-5)**: All depend on Foundational phase completion
- **Polish (Final Phase)**: Depends on all user stories being complete

### Parallel Opportunities

- Models creation (T007, T008, T009) can be done in parallel.
- Basic bot handlers (T013) can be worked on in parallel with Use Cases (T012).
