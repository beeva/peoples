# Installing a Slack Token

This guide walks through creating the Slack token that `emails.py` needs and
making it available to the script.

## 1. Create a Slack app

1. Go to **https://api.slack.com/apps** and sign in to your workspace.
2. Click **Create New App** → **From scratch**.
3. Give it a name (e.g. `Email Extractor`), pick your workspace, and click
   **Create App**.

## 2. Add the required scopes

The script reads users and their emails, so it needs two **Bot Token Scopes**:

1. In the left sidebar, open **OAuth & Permissions**.
2. Scroll to **Scopes → Bot Token Scopes** → **Add an OAuth Scope**.
3. Add both:
   - `users:read`
   - `users:read.email`

> `users:read.email` is what allows email access. Without it, every user comes
> back with no email and the CSV will be empty.

## 3. Install the app and copy the token

1. Scroll back up on the **OAuth & Permissions** page.
2. Click **Install to Workspace** → **Allow**.
3. Copy the **Bot User OAuth Token** — it starts with `xoxb-`.

   *(If your workspace requires admin approval, an admin must approve the
   install first.)*

## 4. Give the token to the script

### Option A — environment variable (recommended)

PowerShell:

```powershell
$env:SLACK_TOKEN = "xoxb-your-real-token"
python emails.py > emails.csv
```

To make it persist across sessions:

```powershell
setx SLACK_TOKEN "xoxb-your-real-token"
```

(Then open a **new** terminal — `setx` only affects future sessions.)

macOS / Linux:

```bash
export SLACK_TOKEN=xoxb-your-real-token
python emails.py > emails.csv
```

### Option B — pass it inline

```powershell
python emails.py --token "xoxb-your-real-token" > emails.csv
```

## 5. Verify it works

```powershell
python emails.py
```

- CSV rows (`"Real Name",email`) → success.
- `Slack API error: invalid_auth` → token is wrong or wasn't copied fully.
- `Slack API error: missing_scope` → the scopes from step 2 weren't
  added/installed; re-add them and reinstall.
- Empty output → the app installed but `users:read.email` is missing, or no
  users have emails visible.

## Security notes

- Treat the `xoxb-` token like a password — **do not** paste it into the script
  or commit it. Using `SLACK_TOKEN` keeps it out of the code.
- If a token is ever exposed, revoke/rotate it from the **OAuth & Permissions**
  page.
- The repo's `.gitignore` already exists; if you save a token to a file, make
  sure that file is ignored.
