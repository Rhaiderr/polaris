# Add-on Polaris (Home Assistant)

Triagem de Gmail com LLM local, empacotada como add-on do Home Assistant
Supervisor. Traz um **wizard de onboarding em 4 telas** (via ingress) que leva do
zero à execução agendada, sem terminal.

> O núcleo (classificação, guardrails, multi-conta) é o mesmo do projeto
> [Polaris](https://github.com/Rhaiderr/polaris). O add-on só embala e adiciona o
> front. O trabalho pesado (o modelo) continua **remoto** — o Pi/HA só orquestra.

## Instalação

1. Home Assistant → **Configurações → Add-ons → Loja de add-ons**.
2. Menu **⋮** (canto superior) → **Repositórios** → adicione
   `https://github.com/Rhaiderr/polaris`.
3. Instale **Polaris** na lista, inicie e abra a interface (ingress).

## As 4 telas

1. **Conta Google** — vincula a conta pelo próprio navegador, sem terminal (ver
   "Como funciona o login" abaixo). Um único `credentials.json` (app OAuth tipo
   *Desktop*) autoriza todas as contas; coloque-o em `/config`.
2. **Endpoint do LLM** — URL base (`.../v1`), modelo e API key opcional, com botão
   **testar conexão**. Serve qualquer endpoint compatível com a API da OpenAI.
3. **Categorias** — varre uma amostra da caixa (só remetente/assunto) e o modelo
   propõe categorias novas; você aceita por **checkbox**. Nada é criado sem aceite,
   e categoria nova nunca ganha exclusão automática.
4. **Agendar** — escolhe o horário; a triagem de todas as contas roda 1×/dia
   (scheduler interno do add-on).

## Opções do add-on

| Opção | Descrição |
|-------|-----------|
| `llm_base_url` | Endpoint OpenAI-compatible (termina em `/v1`). |
| `llm_model` | Nome do modelo. |
| `llm_api_key` | Opcional (vazio se o endpoint não exige). |
| `modo_sombra_exclusao` | `true` = candidatas à Lixeira só ganham label, não vão pra Lixeira. |

As opções semeiam o `.env` na primeira execução; depois a Tela 2 passa a ser a dona
da configuração do endpoint.

## Como funciona o login (sem terminal)

O app do Google é do tipo *Desktop*, cujo único redirect permitido é o loopback
(`http://localhost`). Através do ingress o navegador não está no host do HA, então
esse loopback "falha" — mas a URL de retorno carrega o código de autorização. A
Tela 1 aproveita isso em 3 cliques:

1. Você dá um nome à conta e clica em **Autorizar no Google** (abre em nova aba).
2. Aprova com a conta desejada. O navegador mostra uma **página de erro** em
   `localhost` — **é esperado**.
3. Copia a **URL inteira** dessa página e cola no wizard. Pronto: o token é gravado
   e renova sozinho (você não repete).

> Por que não um "código curto no celular" (device flow)? Porque o device flow do
> Google **não permite escopos de Gmail** — só `email`/`profile`/`drive.file`/
> `youtube`. Para o `gmail.modify` sensível, o loopback com colagem é o caminho mais
> simples que as regras do Google permitem.

Alternativa por terminal (power users), equivalente:
`OAUTH_PORT=8765 python -m src.orquestrador --conta <nome> --login`.

## Nota de build

O `Dockerfile` busca o núcleo (`src/`) do repositório em tempo de build
(`ADD .../main.tar.gz`), então a pasta do add-on é um **contexto autossuficiente** —
o Supervisor constrói sem precisar alcançar o `src/` fora dela. Para fixar uma
versão, troque o build-arg `POLARIS_REF` (default `main`).
