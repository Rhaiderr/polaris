"""Constantes da integração Polaris."""

DOMAIN = "polaris"

# Escopo mínimo: ler + modificar (label/archive/trash). NÃO inclui delete nem send.
SCOPE_GMAIL = "https://www.googleapis.com/auth/gmail.modify"

# URLs OAuth do Google (usadas pelo application_credentials).
OAUTH2_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH2_TOKEN = "https://oauth2.googleapis.com/token"

# ---- opções da integração (options flow) ----
CONF_LLM_BASE_URL = "llm_base_url"
CONF_LLM_MODEL = "llm_model"
CONF_LLM_API_KEY = "llm_api_key"
CONF_AGENDAMENTO = "agendamento_ativo"
CONF_HORA = "hora"
CONF_MAX = "max_por_execucao"
CONF_MODO_SOMBRA = "modo_sombra_exclusao"
CONF_DRY_RUN = "dry_run"

DEFAULT_HORA = "07:00:00"
DEFAULT_MAX = 50

# ---- serviços ----
SERVICE_EXECUTAR = "executar"
SERVICE_SUGERIR = "sugerir_categorias"
SERVICE_ACEITAR = "aceitar_categorias"

ATTR_CONTA = "conta"
ATTR_MODO = "modo"
ATTR_MAX = "max"
ATTR_DRY_RUN = "dry_run"
ATTR_REPROCESSAR = "reprocessar"
ATTR_NUMEROS = "numeros"

# ---- sinal/evento pós-execução ----
SIGNAL_EXECUCAO = "polaris_execucao_{}"   # .format(entry_id)
EVENT_EXECUCAO = "polaris_execucao"
