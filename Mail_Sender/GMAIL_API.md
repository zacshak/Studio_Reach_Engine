# Mail backends: Gmail API and app password

Each mailbox sends and reads through one of two backends:

| Backend | How | Used by default for |
|---|---|---|
| `gmail_api` | Service account `sre-mailer@sre-mail.iam.gserviceaccount.com` impersonates the mailbox (domain-wide delegation, scope `https://mail.google.com/`). | Addresses on the Workspace domains in `gmail_api_domains` |
| `app_password` | The existing SMTP/IMAP path: an app password from `SENDER_ACCOUNTS`, or XOAUTH2 for Microsoft 365. | Everything else (zacgamedev.com, xrevise.cc, @gmail.com) |

## Config: `mail_backends.json`

- `gmail_api_domains`: the Workspace domains the service account can impersonate. When a new domain joins the Workspace, append it here.
- `mailboxes`: settings for each mailbox. `{}` uses the default. `{"backend": "app_password"}` switches a Workspace inbox back to SMTP/IMAP, with no code change. It needs that inbox's app password in `SENDER_ACCOUNTS`. `gmail_api` is refused for addresses outside the Workspace domains.
- `failover`: `false` by default.
  - **Off:** a failing backend is logged with the mailbox and backend (for example `meshak@thaswin.com [gmail_api]: unauthorized_client ...`), and nothing falls back.
  - **On:** a mailbox that fails sign-in retries once on its other backend for that run, if that backend is configured. Each switch is logged as `FAILOVER`.

A Workspace inbox can also send campaigns only when it has a row in `sender_accounts` with a daily cap, the same as any other sending account.

## Key

The JSON key comes from the file named by `GOOGLE_SA_KEY_FILE`. It is never logged or printed, and key files are in `.gitignore`.

In GitHub Actions, the `GOOGLE_SA_KEY` repository secret holds the key's JSON content. The workflows write it to `$RUNNER_TEMP` (mode 600) and set `GOOGLE_SA_KEY_FILE`. Without the secret, the `gmail_api` mailboxes report `gmail_api: GOOGLE_SA_KEY_FILE not set`, and the app-password mailboxes are unaffected.

## What the Gmail API backend does

- **Send:** `users.messages.send`, with the same MIME message as SMTP (base64url `raw`). The From address is the impersonated mailbox. Replies pass `threadId` and set `In-Reply-To` and `References`.
- **Retries:** 429 and rate-limit 403 responses are retried with backoff.
  - After a 5xx or a network error, the mailbox is searched for the Message-ID before retrying, so a mail that went out is not sent twice.
  - If there is still no answer, the row stays `Sending` and is reconciled in the next run, the same as SMTP.
- **Read:** `users.messages.list` and `users.messages.get` (`fetch_unread`, the bounce scan), plus `users.threads.get` for the reply scan. `mark_read` removes the `UNREAD` label.
- **`unauthorized_client`:** delegation has not taken effect yet (it can take up to 24 hours), or the scopes in Google Admin are wrong. The error says so.

## Checks

Run the **Gmail API check** workflow (`gmail-check.yml`) in one of three modes:

- `check`: `users.getProfile` for every Workspace inbox. Prints `emailAddress` and `messagesTotal`.
- `thread_test`: sends FROM → TO (default `meshak@thaswin.com` → `meshakzac@thaswin.com`), marks the copy as read, replies TO → FROM in the thread, and confirms the reply joined the original thread.
- `mailbox`: signs in to one mailbox on its configured backend. It sends nothing and writes nothing.

Locally:

```bash
GOOGLE_SA_KEY_FILE=/path/key.json python Mail_Sender/gmail_api.py --check
```

```bash
python Mail_Sender/mailer.py --check-mailbox meshak@zacgamedev.com
```
