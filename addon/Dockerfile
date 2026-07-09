# Add-on Polaris. Base do HA (traz s6-overlay + bashio).
ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8

WORKDIR /app

# O núcleo (src/) é o mesmo código do repo, buscado do GitHub em tempo de build
# — assim a pasta do add-on é um contexto autossuficiente (o Supervisor constrói
# com o diretório do add-on como contexto, sem alcançar ../src).
ARG POLARIS_REF=main
ADD https://github.com/Rhaiderr/polaris/archive/refs/heads/${POLARIS_REF}.tar.gz /tmp/polaris.tar.gz

# Deps de build só para compilar wheels que faltam em musl (removidas ao fim).
RUN apk add --no-cache --virtual .build gcc musl-dev libffi-dev openssl-dev cargo \
 && tar -xzf /tmp/polaris.tar.gz -C /tmp \
 && cp -r /tmp/polaris-${POLARIS_REF}/src /app/src \
 && cp /tmp/polaris-${POLARIS_REF}/requirements.txt /app/requirements.core.txt \
 && rm -rf /tmp/polaris*

# Deps do núcleo + do wizard (Flask/gunicorn).
COPY requirements.txt /app/requirements.addon.txt
RUN pip install --no-cache-dir -r /app/requirements.core.txt -r /app/requirements.addon.txt \
 && apk del .build

# O wizard (front + servidor) é local ao add-on.
COPY web/ /app/web/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# src/ é importável e chamável como `python -m src.orquestrador`.
ENV PYTHONPATH=/app

CMD ["/run.sh"]
