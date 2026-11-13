# Uniqlo price-watch agent

Checks the whole Uniqlo online catalog on a schedule and sends you an alert when:

- a **new product** appears
- a product's **price drops** (sale / markdown)
- a product's **price goes up**
- *(optional)* a product is **no longer listed**

Claude turns the raw changes into a short digest (biggest % drops first, links included).
Without an Anthropic API key the agent still works and sends a plain list.

## How it works

`uniqlo_agent.py` reads Uniqlo's public product API (`/us/api/commerce/v5/en/products`) for
Women, Men, Kids and Baby (about 1,300 unique products, ~15 s), compares the result with
`state/snapshot.json` from the previous run, notifies, then saves the new snapshot.
The first run only records a baseline, so alerts start from the second run.

## Run it every hour for free (GitHub Actions)

1. Push this folder to a new GitHub repository (a private one is fine).
2. In **Settings → Secrets and variables → Actions → Secrets**, add at least one notification channel:

   | Channel | Secrets |
   |---|---|
   | WhatsApp, free ([CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/)) | `WHATSAPP_PHONE` (your mobile), `CALLMEBOT_APIKEY` |
   | Text via carrier email gateway, free (Verizon only, until Mar 2027) | `SMS_EMAIL_TO` = `<number>@vtext.com`, plus `SMTP_HOST`=`smtp.gmail.com`, `SMTP_PORT`=`587`, `SMTP_USER`, `SMTP_PASSWORD` (Gmail app password) |
   | Text message (paid SMS via [Twilio](https://www.twilio.com)) | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` (your Twilio number, e.g. `+15551234567`), `SMS_TO` (your mobile; comma-separate several) |
   | Phone push ([ntfy](https://ntfy.sh), free, no account) | `NTFY_TOPIC`: a long random name, then subscribe to it in the ntfy app |
   | Slack | `SLACK_WEBHOOK_URL` |
   | Discord | `DISCORD_WEBHOOK_URL` |
   | Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
   | Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` |

   Optionally add `ANTHROPIC_API_KEY` for the Claude-written digest.
3. Open **Actions → Uniqlo price watch → Run workflow** once to save the baseline.
   After that it runs every hour and commits the updated snapshot.

## Options (Actions *variables* or environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `WATCH_KEYWORDS` | *(all products)* | Only alert on names containing one of these, e.g. `heattech,ultra light down,jeans` |
| `MIN_PRICE_CHANGE` | `0.01` | Ignore price moves smaller than this |
| `NOTIFY_REMOVED` | `false` | Also alert when products disappear |
| `UNIQLO_REGION` / `UNIQLO_LANG` | `us` / `en` | Store to watch. Other regions use different category ids: set `UNIQLO_PATHS` too |
| `UNIQLO_PATHS` | `22210,22211,22212,22213` | Category ids to scan (US: Women, Men, Kids, Baby) |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Model used for the digest |

## Run it locally

```bash
pip install -r requirements.txt
cp .env.example .env                # then fill in your values; .env is gitignored
python uniqlo_agent.py --dry-run   # prints the alert instead of sending it
```

## Keeping secrets safe

- Tokens and keys live only in **GitHub Actions secrets** (encrypted, hidden in logs) or in a local `.env` file, which `.gitignore` keeps out of git. Nothing secret is in the code.
- The workflow only has `contents: write` permission, which it needs to commit the price snapshot.
- If a token is ever exposed (pasted in chat, committed by mistake), rotate it: for Telegram send `/revoke` to @BotFather, then update the `TELEGRAM_BOT_TOKEN` secret.
- Consider turning on **Settings → Code security → Secret scanning / Push protection** (free for public repos).

Use cron (`17 * * * * cd /path/to/uniqlo-price-agent && python3 uniqlo_agent.py`) to schedule it on your own machine.
