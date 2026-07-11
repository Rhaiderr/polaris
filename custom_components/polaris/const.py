"""Constants for the Polaris integration."""

DOMAIN = "polaris"

# Minimal scope: read + modify (label/archive/trash). Does NOT include delete or send.
SCOPE_GMAIL = "https://www.googleapis.com/auth/gmail.modify"

# Google OAuth URLs (used by application_credentials).
OAUTH2_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH2_TOKEN = "https://oauth2.googleapis.com/token"

# ---- integration options (options flow) ----
CONF_LLM_BASE_URL = "llm_base_url"
CONF_LLM_MODEL = "llm_model"
CONF_LLM_API_KEY = "llm_api_key"
CONF_SCHEDULE_ENABLED = "schedule_enabled"
CONF_SCHEDULE_TIME = "schedule_time"
CONF_MAX_PER_RUN = "max_per_run"
CONF_SHADOW_MODE = "shadow_mode"
CONF_USE_GMAIL_LABELS = "use_gmail_labels"
CONF_DRY_RUN = "dry_run"

DEFAULT_SCHEDULE_TIME = "07:00:00"
DEFAULT_MAX_PER_RUN = 50

# ---- services ----
SERVICE_RUN_TRIAGE = "run_triage"
SERVICE_SUGGEST_CATEGORIES = "suggest_categories"
SERVICE_ACCEPT_CATEGORIES = "accept_categories"

ATTR_ACCOUNT = "account"
ATTR_MODE = "mode"
ATTR_MAX = "max"
ATTR_DRY_RUN = "dry_run"
ATTR_REPROCESS = "reprocess"
ATTR_NUMBERS = "numbers"

MODE_INCREMENTAL = "incremental"
MODE_FULL = "full"

# ---- post-run signal/event ----
SIGNAL_RUN_DONE = "polaris_run_done_{}"   # .format(entry_id)
SIGNAL_PROGRESS = "polaris_progress_{}"   # .format(entry_id) — live run progress
EVENT_RUN_COMPLETED = "polaris_run_completed"
