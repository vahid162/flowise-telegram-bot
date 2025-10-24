-- /tokens/001_init.sql
-- ISO week anchored to Monday 00:00:00 UTC; all grants are unique per (tenant,user,week_start)
BEGIN;

CREATE TABLE IF NOT EXISTS wallets (
  tenant_id     BIGINT      NOT NULL,
  user_id       BIGINT      NOT NULL,
  balance       INTEGER     NOT NULL DEFAULT 0,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS weekly_grants (
  tenant_id        BIGINT      NOT NULL,
  user_id         BIGINT      NOT NULL,
  week_start_date DATE        NOT NULL, -- Monday (UTC) of ISO-week
  granted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  carried_over    BOOLEAN     NOT NULL DEFAULT FALSE,
  CONSTRAINT weekly_grants_unique UNIQUE (tenant_id, user_id, week_start_date)
);

CREATE TABLE IF NOT EXISTS ledger (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT      NOT NULL,
  user_id    BIGINT      NOT NULL,
  type       TEXT        NOT NULL CHECK (type IN ('grant','spend_ad','admin_adjust','refund')),
  amount     INTEGER     NOT NULL, -- positive for grant/refund, negative for spend
  ref_id     TEXT,
  note       TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ledger_tid_uid_idx ON ledger (tenant_id, user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS group_settings (
  tenant_id BIGINT PRIMARY KEY,
  price_stars INTEGER,
  price_rial  INTEGER,
  allow_transfer BOOLEAN NOT NULL DEFAULT FALSE,
  require_manual_approval BOOLEAN NOT NULL DEFAULT FALSE,
  -- Anti-abuse defaults per group:
  anti_abuse_min_membership_days INTEGER NOT NULL DEFAULT 7,
  anti_abuse_cooldown_hours      INTEGER NOT NULL DEFAULT 24,
  anti_abuse_max_same_domain_per_week INTEGER NOT NULL DEFAULT 2,
  pin_capacity INTEGER NOT NULL DEFAULT 3,
  max_carry INTEGER NOT NULL DEFAULT 1, -- سقف انباشت هدیه در MVP
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS abuse_counters (
  tenant_id   BIGINT      NOT NULL,
  user_id     BIGINT      NOT NULL,
  week_start  DATE        NOT NULL,    -- ISO-week Monday
  key_type    TEXT        NOT NULL,    -- e.g., 'domain'
  key_value   TEXT        NOT NULL,    -- normalized (lower + punycode + eTLD+1)
  count       INTEGER     NOT NULL DEFAULT 0,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, user_id, week_start, key_type, key_value)
);

-- triggers for updated_at on wallets & group_settings
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_proc WHERE proname = 'set_timestamp'
  ) THEN
    CREATE OR REPLACE FUNCTION set_timestamp()
    RETURNS TRIGGER AS $f$
    BEGIN
      NEW.updated_at = NOW();
      RETURN NEW;
    END;
    $f$ LANGUAGE plpgsql;
  END IF;
END$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.triggers WHERE trigger_name='wallets_set_timestamp') THEN
    CREATE TRIGGER wallets_set_timestamp
    BEFORE UPDATE ON wallets
    FOR EACH ROW EXECUTE FUNCTION set_timestamp();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.triggers WHERE trigger_name='group_settings_set_timestamp') THEN
    CREATE TRIGGER group_settings_set_timestamp
    BEFORE UPDATE ON group_settings
    FOR EACH ROW EXECUTE FUNCTION set_timestamp();
  END IF;
END$$;

COMMIT;
