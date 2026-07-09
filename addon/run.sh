#!/usr/bin/with-contenv bashio
# Entrypoint do add-on: aponta os caminhos da CLI para o volume persistente
# /data, semeia o .env a partir das opções (só na 1ª vez) e sobe o wizard.
set -e

mkdir -p /data/config /data/logs
ln -sfn /data/config /app/config
ln -sfn /data/logs /app/logs
ln -sfn /data/.env /app/.env

# Semeia o .env com as opções do add-on apenas se ainda não existir.
# Depois disso, a Tela 2 do wizard passa a ser a dona do arquivo.
if ! bashio::fs.file_exists /data/.env; then
  bashio::log.info "Semeando /data/.env a partir das opções do add-on"
  {
    echo "LLM_BASE_URL=$(bashio::config 'llm_base_url')"
    echo "LLM_MODEL=$(bashio::config 'llm_model')"
    echo "LLM_API_KEY=$(bashio::config 'llm_api_key')"
    echo "MODO_SOMBRA_EXCLUSAO=$(bashio::config 'modo_sombra_exclusao')"
  } > /data/.env
fi

cd /app
bashio::log.info "Polaris wizard em http://0.0.0.0:8099 (via ingress)"
exec gunicorn --bind 0.0.0.0:8099 --workers 1 --timeout 120 web.app:app
