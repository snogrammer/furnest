# Discord setup

How to create the Discord application furnest posts through, and how to wire it to
your deployment. The design is in [ARCHITECTURE.md](ARCHITECTURE.md) (the **Discord**
paragraphs). Examples use `furnest.example.com`; use your own `BASE_URL` instead.

## How it fits together

furnest doesn't run a gateway bot. It uses two one-way channels:

- **furnest → Discord.** The worker posts new matches (together, as one list message
  per data pull), the daily digest and source alerts, and edits a list when a property
  on it changes. It calls Discord's REST API
  with the bot token.
- **Discord → furnest.** Picks from a list's menu, button clicks and the Deny form arrive at
  `POST /discord/interactions` on the web process. Every request is checked against the
  application's Ed25519 public key.

So the bot needs no gateway intents and no slash commands. It only needs to be able to
post in one channel.

## What you need

- furnest deployed behind TLS, with `BASE_URL` set to its public address
  (`https://furnest.example.com`). Discord only calls HTTPS URLs with a valid certificate.
- `/discord/interactions` reachable from the internet. You can restrict the rest of the
  dashboard; see [`deploy/Caddyfile.example`](../deploy/Caddyfile.example).
- A Discord server where you can add a bot and manage the target channel.

## 1. Create the application

1. Open the [Developer Portal](https://discord.com/developers/applications) and choose
   **New Application**. The name is what the bot shows in the channel.
2. On **General Information**, copy:

| Portal field | Setting |
| --- | --- |
| Application ID | `DISCORD_APPLICATION_ID` |
| Public Key | `DISCORD_PUBLIC_KEY` (64 hex characters; furnest refuses to start with anything else) |

furnest doesn't read `DISCORD_APPLICATION_ID` yet. Keep it with the others: you need it
for the invite URL below.

Leave **Interactions Endpoint URL** empty for now. Discord tests the URL when you save
it, so furnest must be running with the public key first (step 4).

## 2. Create the bot

On the **Bot** page:

- **Reset Token** and copy it into `DISCORD_BOT_TOKEN`. Discord shows it only once. It
  gives full control of the bot, so keep it out of the repo and logs like any password.
  If it leaks, reset it here, update the setting and restart the worker.
- **Privileged Gateway Intents:** leave all of them off. furnest never connects to the
  gateway.
- **Requires OAuth2 Code Grant:** off.
- **Public Bot:** turn it off so nobody else can add your bot to their server. The portal
  only lets you do that once **Installation → Install Link** is set to **None**.

## 3. Invite the bot to your server

The bot needs the `bot` scope and three permissions:

| Permission | Value | Why |
| --- | --- | --- |
| View Channel | 1024 | See the channel it posts in. |
| Send Messages | 2048 | Post matches, the digest and alerts. |
| Embed Links | 16384 | The messages are embeds. |

Together that is `permissions=19456`. Open this URL (with your application id), pick
your server and authorize:

```text
https://discord.com/oauth2/authorize?client_id=<DISCORD_APPLICATION_ID>&scope=bot&permissions=19456
```

The `applications.commands` scope isn't needed: furnest has no slash commands, and
clicks on the bot's own buttons reach the interactions endpoint without it. Editing the
bot's own messages needs no extra permission.

If the channel is private, the server-level permissions aren't enough. Open the
channel's **Edit Channel → Permissions**, add the bot (or its role) and allow the three
permissions above.

## 4. Find the channel id

In Discord, turn on **User Settings → Advanced → Developer Mode**. Then right-click the
channel and choose **Copy Channel ID**. That is `DISCORD_CHANNEL_ID`.

## 5. Configure furnest

Add the values to `.env` (or `stack.env`, or the Portainer stack):

```env
DISCORD_APPLICATION_ID=000000000000000000
DISCORD_PUBLIC_KEY=0000000000000000000000000000000000000000000000000000000000000000
DISCORD_BOT_TOKEN=your-bot-token
DISCORD_CHANNEL_ID=000000000000000000
```

Each process reads what it needs:

| Process | Needs | Without it |
| --- | --- | --- |
| `web` | `DISCORD_PUBLIC_KEY`, plus `DISCORD_CHANNEL_ID` to refuse clicks from other channels | `POST /discord/interactions` answers 404, as if it didn't exist. |
| `worker` | `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` | Match posts, message updates, the digest and alerts are skipped (logged with `reason="not configured"`). |

The optional settings are documented in [`.env.example`](../.env.example):

| Setting | Default | Meaning |
| --- | --- | --- |
| `DISCORD_REQUEST_TIMEOUT_SECONDS` | 10 | Per-request timeout for the REST API. |
| `DISCORD_MAX_RETRIES` | 3 | Retries for 5xx and network errors. Rate limits are waited out separately. |
| `DISCORD_INTERACTION_MAX_AGE_SECONDS` | 300 | How far an interaction's signed timestamp may be from the server clock before it is rejected. |
| `DISCORD_NOTIFY_BATCH_MINUTES` | `30` | New matches are collected this long, from the first one, then posted together as one list message. `0` posts at the worker's next turn. |
| `DISCORD_DIGEST_TIME` | unset | Daily digest at this local time (`TIMEZONE`), as `HH:MM`. Unset means no digest. |
| `DISCORD_DIGEST_TOP` | 5 | How many new suggestions the digest lists. |
| `DISCORD_ALLOWED_USER_IDS` | empty | Comma-separated Discord user ids allowed to press Approve, Deny, Inquire and Snooze. Empty means anyone who can see the channel. Anyone else gets an ephemeral "not allowed" reply, and nothing changes. Copy a user id the same way as the channel id. |
| `DISCORD_ALERT_AFTER_FAILURES` | 3 | Alert when a source fails this many runs in a row, and again when it recovers. 0 turns alerts off. |

Redeploy `web` and `worker` before the next step.

## 6. Set the Interactions Endpoint URL

Back on **General Information**, set **Interactions Endpoint URL** to

```text
https://furnest.example.com/discord/interactions
```

and save. Discord checks the URL before it accepts it: it sends a signed `PING`, which
furnest answers with `PONG`, and a request with a deliberately bad signature, which
furnest must reject with 401. If either check fails, the portal won't save the URL (see
[Troubleshooting](#troubleshooting)).

Discord keeps sending checks like these later. If the endpoint stops passing them, for
example because the web process lost `DISCORD_PUBLIC_KEY` and answers 404, Discord
removes the URL and tells you by email and system DM. Fix the cause and save the URL
again.

## 7. Check that it works

1. The portal saved the URL. That proves the web side: reachability, TLS and the key.
2. Wait for a property to be suggested (the list goes out `DISCORD_NOTIFY_BATCH_MINUTES`
   after the first one; set it to `0` while testing), or set `DISCORD_DIGEST_TIME` a
   couple of minutes ahead and restart the worker. The worker logs
   `discord_batch_posted` or `digest_posted`, and the message appears in the channel. That proves the bot token,
   the channel id and the bot's permissions.
3. Pick a listing from the list's menu: its card appears, visible only to you. Click
   **Snooze** on it. The card redraws at once, the web logs `discord_interaction` with
   `action="snooze"`, then the worker logs `discord_list_synced` and the list shows 💤
   in front of the listing. That proves the whole round trip.

## Troubleshooting

### The portal won't save the Interactions Endpoint URL

Discord shows "The specified interactions endpoint URL could not be verified." Check
the web log for the request first: no log line means it never reached furnest.

| Cause | Sign | Fix |
| --- | --- | --- |
| Not reachable | No request in the web log. | The URL must resolve publicly and serve a valid certificate. Check DNS, the proxy and firewalls. Try `curl -i -X POST https://furnest.example.com/discord/interactions` from outside your network: a 401 means it is reachable. |
| Blocked by the proxy or a CDN | 403 or a challenge page instead of furnest's 401. | Exclude `/discord/interactions` from IP allow-lists (the `@dashboard` rule in the Caddyfile example already does), WAF rules and bot protection. Discord can't solve challenges. |
| Public key not set on `web` | The curl above answers 404. | Set `DISCORD_PUBLIC_KEY` for the web process and redeploy. |
| Wrong public key | Web logs `discord_interaction_rejected` with `reason="bad or stale signature"` for Discord's valid PING too. | Copy the **Public Key** of this application again. It is not the bot token or the client secret, and each application has its own. |
| Server clock off | Same log line, and the key is right. | Signed timestamps more than `DISCORD_INTERACTION_MAX_AGE_SECONDS` (300s) from the server clock are rejected. Sync the host clock (NTP). |
| Body changed in transit | Same log line, and key and clock are right. | The signature covers the exact request body. Make sure nothing between Discord and furnest rewrites or re-encodes it. |

### "This interaction failed" when clicking a button

Discord shows this when the endpoint didn't answer within 3 seconds or didn't answer
with a 2xx.

- Look for `discord_interaction_rejected` (a signature problem: see above), a 400
  response, or an error in the web log around the click.
- No log line at all means the click didn't reach furnest. Was the endpoint URL removed
  (step 6), or is the proxy down?
- A click on an action that no longer applies, such as Approve on an already denied
  property, isn't a failure: only you see the reply, which says why nothing changed.

### Nothing is posted to the channel

The worker logs each failed attempt as `job_failed` with the job type (`notify`,
`discord.digest`, `discord.source_alert`), and the error carries Discord's status and
error code. The job queue retries it.

| Worker log | Cause | Fix |
| --- | --- | --- |
| `notify_skipped`, `digest_skipped` or `source_alert_skipped` with `reason="not configured"` | `DISCORD_BOT_TOKEN` or `DISCORD_CHANNEL_ID` isn't set for the worker. | Set both and restart the worker. |
| `HTTP 401 (0: 401: Unauthorized)` | The bot token is wrong, or was reset. | Copy a fresh token (step 2). |
| `HTTP 403 (50001: Missing Access)` | The bot isn't in the server, or can't see the channel. | Invite it (step 3); for a private channel, add it to the channel's permissions. |
| `HTTP 403 (50013: Missing Permissions)` | The bot can see the channel but can't send messages or embeds there. | Allow Send Messages and Embed Links for the bot in that channel. |
| `HTTP 404 (10003: Unknown Channel)` | `DISCORD_CHANNEL_ID` is wrong, or the channel was deleted. | Copy the channel id again (step 4). |

`notify_skipped` with any other reason is normal: nothing was waiting to be posted, or
every waiting property was decided on, snoozed or merged by the time the job ran.
`notify_deferred` means the job is waiting out `DISCORD_NOTIFY_BATCH_MINUTES` so the
whole data pull goes out in one message.

### Buttons work but the message never changes

The click is handled by `web`, but the message is edited by `worker`.

- The worker must be running with the bot token and channel id. Look for
  `discord_list_synced`, `discord_message_synced` or `discord_sync_skipped` after a click.
- Edits fail with the same 401 and 403 errors as posts (see above).
- `discord_sync_skipped` with `reason="message gone"` means the message was deleted in
  Discord. That is expected; the property keeps its state.

### furnest won't start

`DISCORD_PUBLIC_KEY must be the 64-character hex key from the developer portal`: the
value isn't the application's public key. A common mix-up is pasting the bot token or
the client secret.
