# Polaris

Triagem automática de Gmail com um LLM **local** (ou qualquer endpoint
OpenAI-compatible). O Polaris sincroniza sua caixa de entrada, classifica cada
email em categorias que **você** define, aplica labels, arquiva o que já foi
resolvido e — em modo sombra — marca o lixo promocional como candidato à
Lixeira, para você auditar antes de excluir de verdade.

Projetado para rodar barato: o trabalho pesado (classificação) vai para um
modelo que você já tem em casa, sem custo de API. É um container **run-once**,
agendado por um timer do host.

> ⚠️ **Projeto pessoal, sem garantias.** Ele lê e modifica a sua conta Gmail
> (labels, arquivamento, Lixeira). Rode sempre em `--dry-run` primeiro e
> mantenha o **modo sombra** ligado por semanas antes de confiar na exclusão.
> Nunca faz exclusão permanente — tudo vai para a Lixeira (recuperável ~30 dias).

---

## Como funciona

```mermaid
flowchart LR
    A[Gmail<br/>History API] -->|novas msgs| B[Pré-filtro]
    B --> C[Classificador<br/>LLM local]
    C -->|JSON: categoria,<br/>arquivar, excluir,<br/>confiança| D[Decisão<br/>+ guardrails]
    D -->|label| E[Aplica label]
    D -->|arquivar| F[Remove da Inbox]
    D -->|sombra| G[Label<br/>Lixeira-candidata]
    D -->|excluir| H[Trash]
    D -->|baixa conf.| I[Revisar]
```

O escopo OAuth é **`gmail.modify`** apenas: ler, aplicar labels, arquivar e
mandar para a Lixeira. **Nunca** envia email nem apaga permanentemente.

### Decisão (limiares + guardrails)

A ação vem da classificação do modelo, mas **filtrada por regras
determinísticas** — o modelo nunca decide sozinho excluir algo:

| Ação | Condições |
|------|-----------|
| **Revisar** | confiança `< 0.70`, ou JSON inválido → só recebe label `Revisar`, nada é mexido |
| **Label** | confiança `≥ 0.70` → aplica a label da categoria |
| **Arquivar** | `arquivar=true` + conf `≥ 0.80` + thread de mensagem única + categoria **não** protegida (`arquivar_permitido`) |
| **Sombra / Excluir** | `excluir=true` + conf `≥ 0.95` + categoria **elegível** (`permitir_exclusao`) + **`List-Unsubscribe` presente** + thread única |

- **Exclusão sempre começa em modo sombra**: em vez de mandar para a Lixeira,
  aplica a label `Polaris/Lixeira-candidata`. Você audita e só então desliga o
  modo sombra (`MODO_SOMBRA_EXCLUSAO=false`).
- Só categorias com `permitir_exclusao: true` (ex.: `Promoções`) chegam perto da
  exclusão. Categorias sensíveis (ex.: `Segurança`) podem ter
  `arquivar_permitido: false` para **nunca** sair da inbox automaticamente.
- Idempotência: cada email processado recebe `Polaris/Processado`; execuções
  seguintes o pulam.

---

## Pré-requisitos

- **Python 3.12+** (com `venv`/`pip`) para o 1º login OAuth fora do container.
- **Docker + Docker Compose** para rodar a triagem.
- Um **endpoint LLM OpenAI-compatible** acessível (veja abaixo).
- Uma conta **Google Cloud** para gerar as credenciais OAuth (grátis).

### Funciona com qualquer endpoint OpenAI-compatible

O Polaris nunca menciona um provedor específico — tudo vem de variáveis de
ambiente. Serve LM Studio, Ollama, llama.cpp, vLLM, OpenRouter, OpenAI, etc.:

```bash
# Modelo em OUTRA máquina da LAN:
LLM_BASE_URL=http://192.168.0.50:1234/v1
# Mesma máquina do container (o compose já mapeia host.docker.internal):
LLM_BASE_URL=http://host.docker.internal:1234/v1
# Fora do Docker, modelo no mesmo host:
LLM_BASE_URL=http://localhost:1234/v1
```

Se o endpoint estiver fora do ar, o Polaris **pula a execução** (exit 0) — o
modo incremental recupera na próxima rodada. Nada quebra.

---

## Home Assistant (recomendado): integração nativa via HACS

O Polaris roda como **integração do Home Assistant** — login com Google nativo
(sem terminal, sem colar URL), múltiplas contas, agendamento e serviços:

1. **HACS → Repositórios personalizados** → adicione
   `https://github.com/Rhaiderr/polaris` (categoria *Integration*) → instale
   **Polaris** e reinicie o HA.
2. Crie a credencial OAuth (**app tipo Web**, uma vez):
   [docs/gerar-credenciais-gmail.md](docs/gerar-credenciais-gmail.md).
3. **Configurações → Dispositivos e serviços → Adicionar integração → Polaris**
   → cole a credencial (1ª vez) → **Entrar com Google** → pronto. Outra conta?
   Adicione a integração de novo.
4. Nas **opções** da integração: endpoint do modelo, horário diário, modo sombra.
5. Categorias em `/config/polaris/<email>/categorias.yaml` (criado com um
   exemplo). Serviços: `polaris.executar` (triagem; modo `completo` p/ backlog),
   `polaris.sugerir_categorias` / `polaris.aceitar_categorias` (sugestões por IA),
   sensor de última execução e evento `polaris_execucao` para automações.

O restante deste README cobre o uso **standalone** (CLI/Docker/systemd), que
continua 100% suportado.

---

## Quickstart (~10 min)

```bash
# 1. Clonar e instalar
git clone https://github.com/Rhaiderr/polaris.git && cd polaris
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# 2. Configurar o endpoint do modelo
cp .env.example .env
$EDITOR .env            # ajuste LLM_BASE_URL e LLM_MODEL

# 3. Credenciais OAuth (uma vez) — veja docs/gerar-credenciais-gmail.md
#    Baixe o credentials.json (OAuth Desktop) para config/credentials.json

# 4. Adicionar sua conta (login + categorias iniciais em UM comando)
python -m src.orquestrador --login              # cria a conta 'principal'
$EDITOR config/principal/categorias.yaml        # ajuste com as SUAS labels do Gmail

# 5. Ver a triagem SEM aplicar nada
python -m src.orquestrador --dry-run --modo completo --max 30
```

Confira as linhas `[DRY]` — cada uma mostra a categoria, a confiança e a ação
que o Polaris *tomaria*. Nada é aplicado em `--dry-run`.

O `--login` faz tudo de uma vez: cria `config/principal/`, gera o token, semeia
um `categorias.yaml` inicial e mostra o próximo passo.

> **Login OAuth em máquina headless / via SSH:** o `--login` sobe um servidor
> local na porta `OAUTH_PORT` (default 8765) e imprime a URL sem abrir
> navegador. Faça um túnel — `ssh -L 8765:localhost:8765 seu-host` — e abra a
> URL no navegador da sua máquina. Detalhes em
> [`docs/gerar-credenciais-gmail.md`](docs/gerar-credenciais-gmail.md).

---

## Uso

### CLI

```bash
python -m src.orquestrador [opções]
  --conta NOME                    qual conta processar (config/NOME/).
                                  Omitido: TODAS as contas configuradas
  --modo {incremental,completo}   incremental (padrão) usa a History API;
                                  completo varre o backlog inteiro
  --dry-run                       não aplica nada; só mostra o que faria
  --reprocessar                   reprocessa mensagens já marcadas Processado
  --max N                         limita quantas mensagens processar
  --login                         adiciona/reautentica uma conta e sai
  --sugerir-categorias            sugere categorias novas a partir da caixa e sai
  --aceitar NUMS                  aceita sugestões salvas ('1,3' ou 'todas')
```

Na 1ª execução incremental, o Polaris só **fixa o cursor** (bootstrap) e não
processa nada. Use `--modo completo` para o backlog existente.

### Sugestão de categorias (IA)

Não sabe por onde começar as categorias? Deixe o modelo olhar a sua caixa e
propor — você só marca o que quiser:

```bash
python -m src.orquestrador --conta principal --sugerir-categorias --max 200
```

Ele amostra os emails mais recentes (**só remetente/assunto**, sem baixar o
conteúdo), propõe categorias novas — sem repetir as que você já tem — e mostra
a lista numerada para você aceitar (`1,3`, `todos` ou Enter para nenhuma). As
aceitas entram no `categorias.yaml` com `permitir_exclusao: false` (exclusão é
sempre decisão sua, explícita) e um backup `.bak` é criado antes.

Sem terminal interativo (automação/front-end), as sugestões são salvas em
`logs/<conta>/sugestoes.json`; o aceite vem depois:

```bash
python -m src.orquestrador --conta principal --aceitar '1,2'
```

### Múltiplas contas

Cada conta é um perfil independente em `config/<conta>/` (token, categorias e
estado próprios); o `credentials.json` é **compartilhado** (um app OAuth
autoriza várias contas Google). Adicionar outra conta é **um comando**:

```bash
python -m src.orquestrador --conta trabalho --login   # abre o login e semeia as categorias
$EDITOR config/trabalho/categorias.yaml
```

Rode uma conta com `--conta trabalho`, ou **todas de uma vez** omitindo `--conta`
(o padrão — perfeito para o timer processar cada conta em sequência).

### Docker (recomendado para o dia a dia)

```bash
docker compose build
docker compose run --rm polaris --modo incremental --dry-run   # teste
docker compose run --rm polaris --modo incremental             # pra valer
```

O `docker-compose.yml` monta `config/` e `logs/` como volumes (nada sensível
entra na imagem) e carrega o `.env` via `env_file`.

### Agendamento (systemd timer no host)

O Polaris é run-once; o agendamento fica no host, não no container. Copie os
exemplos, **ajuste os caminhos e o horário**, e habilite:

```bash
cp systemd/polaris.service.example ~/.config/systemd/user/polaris.service
cp systemd/polaris.timer.example   ~/.config/systemd/user/polaris.timer
$EDITOR ~/.config/systemd/user/polaris.*   # caminhos + OnCalendar
systemctl --user daemon-reload
systemctl --user enable --now polaris.timer
```

Se o seu modelo **não** fica sempre ligado, o `.service` tem hooks opcionais
(`ExecStartPre`/`ExecStopPost`) para acordar/desligar o modelo em volta da
execução — aponte para o seu próprio script (o Polaris não embute isso).

---

## Auditoria e reversão

- **Log de decisões:** cada execução real acrescenta uma linha JSON em
  `logs/decisoes.jsonl` (remetente, assunto, categoria, confiança, ação,
  motivo). É a fonte de verdade para ajustar categorias/limiares com evidência.
  Retenção configurável (`LOG_RETENCAO_DIAS`, default 90).
- **Reverter arquivamento:** os emails continuam no Gmail, só saíram da inbox —
  busque pela label da categoria.
- **Reverter exclusão:** tudo vai para a **Lixeira** (recuperável ~30 dias),
  nunca apagado. No modo sombra, sequer isso: é só a label
  `Polaris/Lixeira-candidata`, que você remove quando quiser.

---

## Configuração (`.env`)

| Variável | Default | Descrição |
|----------|---------|-----------|
| `LLM_BASE_URL` | — | Endpoint OpenAI-compatible (com `/v1`). **Obrigatório.** |
| `LLM_MODEL` | — | Nome do modelo como o endpoint o expõe. **Obrigatório.** |
| `LLM_API_KEY` | vazio | Chave (vazio para endpoints locais). |
| `LLM_TEMPERATURE` | `0.0` | Temperatura da classificação. |
| `LLM_MAX_TOKENS` | `400` | Teto de tokens da resposta. |
| `LLM_TIMEOUT` | `120` | Timeout (s) por chamada. |
| `MODO_SOMBRA_EXCLUSAO` | `true` | Exclusão vira apenas label `Lixeira-candidata`. Mantenha `true`. |
| `EXCLUSAO_PERMANENTE` | `false` | Reservado; o código ignora — exclusão é sempre Lixeira. |
| `LOG_RETENCAO_DIAS` | `90` | Retenção do `decisoes.jsonl`. |
| `OAUTH_PORT` | `8765` | Porta do servidor de login OAuth. |

As **categorias** ficam em `config/<conta>/categorias.yaml` (gitignored — nomes
de labels são dado pessoal). Cada categoria tem `nome`, `descricao` (orienta o
modelo) e as flags `permitir_exclusao` / `arquivar_permitido`. Trocar
categorias **não** exige mexer em Python.

---

## Troubleshooting

| Sintoma | Causa provável |
|---------|----------------|
| `Endpoint LLM indisponível. Pulando.` | O modelo/endpoint não respondeu. O Polaris pula (exit 0); rode de novo com o modelo no ar. |
| Container não alcança o modelo em `localhost` | Dentro do container, `localhost` é o próprio container. Use `host.docker.internal` (modelo no host) ou o IP da LAN. |
| `Sem token OAuth válido` / `Conta sem login` | Faltou o `--login` daquela conta (o token fica em `config/<conta>/token.json`). |
| Login OK mas para em ~7 dias | App OAuth ficou em *Testing*. Publique **"In production"** (o refresh token deixa de expirar). Veja o tutorial. |
| `credentials.json não encontrado` | O JSON baixado não foi salvo em `config/credentials.json`. |

---

## Segurança e privacidade

- Escopo mínimo `gmail.modify`; nunca `send` nem `delete` permanente.
- Nada sensível é versionado: o `.gitignore` ignora **tudo** em `config/`
  (o `credentials.json` compartilhado e as pastas por-conta `config/<conta>/`
  com `token.json`, `categorias.yaml` e `state.json`), além do `.env` e de
  `logs/`. O repositório traz apenas os `.example` genéricos.
- O corpo do email é tratado como **entrada não confiável**: o classificador
  delimita o conteúdo e instrui o modelo a ignorar comandos vindos de dentro
  dele (defesa contra prompt injection), com guardrails determinísticos como
  rede de segurança.

---

## Licença

[MIT](LICENSE) © 2026 Leonardo Arouck.
