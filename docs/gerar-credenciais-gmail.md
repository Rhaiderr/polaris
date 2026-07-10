# Gerar as credenciais OAuth do Gmail (passo manual, ~10 min)

> 🇺🇸 [English version](gmail-credentials.md)

O Polaris precisa de uma credencial OAuth do Google para falar com a sua conta
Gmail no escopo `gmail.modify` (ler, aplicar labels, arquivar e mandar para a
Lixeira — **sem** enviar nem apagar permanentemente).

O **tipo** da credencial depende de como você usa o Polaris:

| Uso | Tipo de app OAuth | Onde a credencial entra |
|---|---|---|
| **Integração do Home Assistant** (recomendado) | **Aplicativo da Web** com redirect `https://my.home-assistant.io/redirect/oauth` | Colada na UI do HA (Credenciais de Aplicação) — passo 5a |
| CLI / Docker standalone | **App para computador (Desktop)** | `config/credentials.json` — passo 5b |

Você faz isto **uma vez** (os passos 1–4 são iguais para os dois usos). Nada
aqui é versionado — credenciais e tokens são gitignored / ficam no HA.

> ⚠️ **Passo crítico:** publique o app como **"In production"**. Se deixar em
> *Testing*, o Google **expira o refresh token em 7 dias** e a automação para
> (exigiria refazer login toda semana). Detalhe no passo 4.

---

## 1. Criar/selecionar um projeto no Google Cloud

1. Acesse <https://console.cloud.google.com/>.
2. Topo da página → seletor de projeto → **Novo projeto** (ex.: `polaris`).

## 2. Ativar a Gmail API

1. Menu → **APIs e serviços → Biblioteca**.
2. Busque **Gmail API** → **Ativar**.

## 3. Tela de consentimento OAuth

1. **APIs e serviços → Tela de permissão OAuth**.
2. Tipo de usuário: **Externo** → **Criar**.
3. Preencha o mínimo: nome do app (ex.: `Polaris`), e-mail de suporte e de
   contato (o seu). Salve e avance.
4. **Escopos:** pode deixar vazio aqui (o Polaris pede `gmail.modify` na hora do
   login). Avance.
5. **Usuários de teste:** adicione o seu próprio e-mail (`você@gmail.com`).

## 4. Publicar o app ("In production") — evita expirar o token

1. Volte para **Tela de permissão OAuth**.
2. Em **Status de publicação**, clique **PUBLICAR APLICATIVO** → confirme para
   mudar de *Testing* para **In production**.
3. Como o app não passou por verificação do Google, no 1º login vai aparecer um
   aviso **"app não verificado"** — isso é esperado para uso próprio. Você
   contorna uma vez (passo 6) e o refresh token deixa de expirar.
   - *Não* é preciso enviar para verificação: ela só serve para publicar o app
     para terceiros. Para uso pessoal, "In production" sem verificação basta.

## 5a. Credencial para a integração do Home Assistant (tipo Web)

1. **APIs e serviços → Credenciais → Criar credenciais → ID do cliente OAuth**.
2. Tipo de aplicativo: **Aplicativo da Web**.
3. Em **URIs de redirecionamento autorizados**, adicione exatamente:
   `https://my.home-assistant.io/redirect/oauth`
4. **Criar** → anote o **Client ID** e o **Client Secret**.
5. No Home Assistant: **Configurações → Dispositivos e serviços → Adicionar
   integração → Polaris**. Na primeira vez, ele pede a credencial — cole o
   Client ID e o Secret. Depois é só **Entrar com Google** e aprovar (na tela
   "app não verificado": **Avançado → Acessar Polaris**). Para outras contas,
   adicione a integração de novo — a credencial é reaproveitada.

> Requer o serviço [My Home Assistant](https://my.home-assistant.io) habilitado
> (padrão). O HA guarda e renova o token sozinho — não existe token.json.

## 5b. Credencial para a CLI / Docker standalone (tipo Desktop)

1. **APIs e serviços → Credenciais → Criar credenciais → ID do cliente OAuth**.
2. Tipo de aplicativo: **App para computador (Desktop app)**.
3. Nome qualquer → **Criar**.
4. **Fazer o download do JSON** → salve como **`config/credentials.json`** na
   pasta do Polaris.

## 6. Primeiro login da CLI (gera o `token.json`) — só para o uso standalone

Rode **fora do container** (precisa abrir o navegador), na pasta do projeto:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.orquestrador --login              # conta 'principal'
# para uma conta com nome próprio (ou uma 2ª conta):
#   python -m src.orquestrador --conta trabalho --login
```

- Imprime uma URL (não abre navegador — bom para SSH/headless). Abra-a no
  navegador; se estiver acessando por SSH, faça antes um túnel:
  `ssh -L 8765:localhost:8765 SEU_HOST`.
- Escolha a sua conta Gmail. Na tela **"app não verificado"**:
  **Avançado → Acessar Polaris (não seguro)** → conceda o acesso (é o seu app).
- Ao final, o Polaris grava **`config/<conta>/token.json`** e semeia um
  `categorias.yaml` inicial naquela pasta. Pronto.

> No Docker o `token.json` é **montado como volume** (nunca entra na imagem).
> Gere-o aqui e o container usa o mesmo arquivo.

## 7. Conferir

```bash
python -m src.orquestrador --modo incremental --dry-run
```

Deve conectar, fazer o **bootstrap** do cursor (1ª vez não processa nada) e sair
sem erro. Para ver a triagem do backlog em modo de teste:

```bash
python -m src.orquestrador --modo completo --dry-run --max 30
```

---

### Problemas comuns

| Sintoma | Causa provável |
|---|---|
| `Sem token OAuth válido` / `Conta sem login` | Faltou rodar `--login` daquela conta (o token fica em `config/<conta>/token.json`). |
| Login OK mas para de funcionar em ~7 dias | App ficou em *Testing*. Publique **In production** (passo 4). |
| `access_denied` no login | Seu e-mail não está como usuário de teste / app não publicado. |
| `credentials.json não encontrado` | O JSON baixado não foi salvo em `config/credentials.json`. |
