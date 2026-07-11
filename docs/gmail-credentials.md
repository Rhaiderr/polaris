# Creating the Gmail OAuth credentials (manual step, ~10 min)

Polaris needs a Google OAuth credential to talk to your Gmail account with the
`gmail.modify` scope (read, apply labels, archive and send to Trash —
**without** sending email or deleting permanently).

The credential **type** depends on how you run Polaris:

| Usage | OAuth app type | Where the credential goes |
|---|---|---|
| **Home Assistant integration** (recommended) | **Web application** with redirect `https://my.home-assistant.io/redirect/oauth` | Pasted in the HA UI (Application Credentials) — step 5a |
| Standalone CLI / Docker | **Desktop app** | `config/credentials.json` — step 5b |

You do this **once** (steps 1–4 are the same for both usages). Nothing here is
versioned — credentials and tokens are gitignored / stored by HA.

> ⚠️ **Critical step:** publish the app as **"In production"**. If it stays in
> *Testing*, Google **expires the refresh token in 7 days** and the automation
> stops (you would have to log in again every week). Details in step 4.

---

## 1. Create/select a Google Cloud project

1. Go to <https://console.cloud.google.com/>.
2. Top of the page → project selector → **New project** (e.g. `polaris`).

## 2. Enable the Gmail API

1. Menu → **APIs & Services → Library**.
2. Search **Gmail API** → **Enable**.

## 3. OAuth consent screen

1. **APIs & Services → OAuth consent screen**.
2. User type: **External** → **Create**.
3. Fill in the minimum: app name (e.g. `Polaris`), support and contact email
   (yours). Save and continue.
4. **Scopes:** you can leave it empty here (Polaris requests `gmail.modify` at
   login time). Continue.
5. **Test users:** add your own email (`you@gmail.com`).

## 4. Publish the app ("In production") — prevents token expiry

1. Back to **OAuth consent screen**.
2. Under **Publishing status**, click **PUBLISH APP** → confirm to move from
   *Testing* to **In production**.
3. Since the app never went through Google verification, the first login shows
   an **"unverified app"** warning — expected for personal use. You bypass it
   once (**Advanced → Go to Polaris (unsafe)**) and the refresh token stops
   expiring.
   - You do *not* need to submit for verification: that is only for publishing
     the app to third parties. For personal use, "In production" without
     verification is enough.

## 5a. Credential for the Home Assistant integration (Web type)

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Web application**.
3. Under **Authorized redirect URIs**, add exactly:
   `https://my.home-assistant.io/redirect/oauth`
4. **Create** → note the **Client ID** and the **Client Secret**.
5. In Home Assistant: **Settings → Devices & services → Add integration →
   Polaris**. The first time, it asks for the credential — paste the Client ID
   and the Secret. Then just **Sign in with Google** and approve (on the
   "unverified app" screen: **Advanced → Go to Polaris**). For more accounts,
   add the integration again — the credential is reused.

> Requires the [My Home Assistant](https://my.home-assistant.io) service to be
> enabled (default). HA stores and refreshes the token by itself — there is no
> token.json.

## 5b. Credential for the standalone CLI / Docker (Desktop type)

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Desktop app**.
3. Any name → **Create**.
4. **Download the JSON** → save it as **`config/credentials.json`** in the
   Polaris folder.

## 6. First CLI login (creates `token.json`) — standalone usage only

Run **outside the container** (needs to open a browser), in the project folder:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.orquestrador --login              # 'principal' account
# for a named account (or a 2nd account):
#   python -m src.orquestrador --account work --login
```

- It prints a URL (it does not open a browser — good for SSH/headless). Open
  it in your browser; if you are on SSH, tunnel first:
  `ssh -L 8765:localhost:8765 YOUR_HOST`.
- Pick your Gmail account. On the **"unverified app"** screen:
  **Advanced → Go to Polaris (unsafe)** → grant access (it is your own app).
- At the end, Polaris writes **`config/<account>/token.json`** and seeds an
  initial `categorias.yaml` in that folder. Done.

> In Docker, `token.json` is **mounted as a volume** (it never enters the
> image). Create it here and the container uses the same file.

## 7. Verify

```bash
python -m src.orquestrador --modo incremental --dry-run
```

It should connect, **bootstrap** the cursor (the first run processes nothing)
and exit cleanly. To preview the backlog triage in test mode:

```bash
python -m src.orquestrador --modo completo --dry-run --max 30
```

---

### Common problems

| Symptom | Likely cause |
|---|---|
| `No valid OAuth token` / account without login | That account is missing `--login` (the token lives in `config/<account>/token.json`). |
| Login works but stops after ~7 days | The app stayed in *Testing*. Publish **In production** (step 4). |
| `access_denied` at login | Your email is not a test user / the app is not published. |
| `credentials.json not found` | The downloaded JSON was not saved to `config/credentials.json`. |
