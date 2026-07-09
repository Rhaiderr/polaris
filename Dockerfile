# Polaris — imagem mínima. Só HTTP para o endpoint LLM e a Gmail API.
# Nada sensível entra na imagem: credentials/token/categorias/state são volumes.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Deps primeiro (cache de camada)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Só o código. config/ e logs/ vêm por bind mount (ver docker-compose.yml).
COPY src/ ./src/

# Roda como usuário não-root.
RUN useradd --create-home --uid 1000 polaris \
    && mkdir -p /app/config /app/logs \
    && chown -R polaris:polaris /app
USER polaris

# Sem CMD padrão perigoso: o entrypoint é o orquestrador; args vêm do compose.
ENTRYPOINT ["python", "-m", "src.orquestrador"]
CMD ["--modo", "incremental", "--dry-run"]
