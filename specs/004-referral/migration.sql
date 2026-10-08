-- 004-referral: только новые таблицы, существующие не меняются.
-- Сгенерировано SQLAlchemy (диалект SQLite) из моделей src/domain/models/referral.py.
-- Применять к боевой SQLite вручную необязательно: бот сам создаёт недостающие таблицы
-- при старте (Base.metadata.create_all). Перед любым применением сделайте копию data/bot.sqlite3.

CREATE TABLE IF NOT EXISTS referral_links (
    id INTEGER NOT NULL,
    inviter_id BIGINT NOT NULL,
    token VARCHAR NOT NULL,
    created_at DATETIME NOT NULL,
    revoked_at DATETIME,
    used_at DATETIME,
    invitee_id BIGINT,
    PRIMARY KEY (id),
    FOREIGN KEY(inviter_id) REFERENCES users (id),
    UNIQUE (token),
    FOREIGN KEY(invitee_id) REFERENCES users (id)
);
CREATE INDEX IF NOT EXISTS ix_referral_links_inviter_id ON referral_links (inviter_id);

CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER NOT NULL,
    link_id INTEGER NOT NULL,
    inviter_id BIGINT NOT NULL,
    invitee_id BIGINT NOT NULL,
    status VARCHAR NOT NULL,
    close_reason VARCHAR,
    bound_at DATETIME NOT NULL,
    bonus_order_id INTEGER,
    reward_rub INTEGER,
    resolved_at DATETIME,
    rewarded_at DATETIME,
    notified_at DATETIME,
    notify_blocked_at DATETIME,
    PRIMARY KEY (id),
    CONSTRAINT ck_referrals_not_self CHECK (inviter_id <> invitee_id),
    UNIQUE (link_id),
    FOREIGN KEY(link_id) REFERENCES referral_links (id),
    FOREIGN KEY(inviter_id) REFERENCES users (id),
    UNIQUE (invitee_id),
    FOREIGN KEY(invitee_id) REFERENCES users (id),
    FOREIGN KEY(bonus_order_id) REFERENCES orders (id)
);
CREATE INDEX IF NOT EXISTS ix_referrals_inviter_status_rewarded ON referrals (inviter_id, status, rewarded_at);

CREATE TABLE IF NOT EXISTS balance_entries (
    id INTEGER NOT NULL,
    user_id BIGINT NOT NULL,
    kind VARCHAR NOT NULL,
    amount_rub INTEGER NOT NULL,
    ref_id INTEGER,
    note VARCHAR,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_balance_entries_kind_ref UNIQUE (kind, ref_id),
    CONSTRAINT ck_balance_entries_nonzero CHECK (amount_rub <> 0),
    FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE INDEX IF NOT EXISTS ix_balance_entries_user_id ON balance_entries (user_id);

CREATE TABLE IF NOT EXISTS order_payments (
    order_id INTEGER NOT NULL,
    price_rub INTEGER NOT NULL,
    balance_rub INTEGER NOT NULL,
    cash_rub INTEGER NOT NULL,
    bonus_bytes BIGINT NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (order_id),
    CONSTRAINT ck_order_payments_amounts CHECK (balance_rub >= 0 AND cash_rub >= 0 AND balance_rub + cash_rub = price_rub),
    FOREIGN KEY(order_id) REFERENCES orders (id)
);

