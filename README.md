# Channel Publisher Bot

A Python Telegram bot for publishing one message to every connected channel (or to a saved channel template), without the original forward attribution.

## Features

- **Auto-connect channels:** add the bot as an administrator in a channel. The bot detects the `my_chat_member` update, saves the channel, and posts `✅ Bot added successfully! This channel is now connected.`
- **Copy, don't forward:** posts are sent with Telegram's `copyMessage` method. This removes the forward header/source attribution without editing the original text, caption, entities, media, or formatting.
- **Inline keyboard pass-through:** the original `reply_markup` JSON is passed straight into `copyMessage`. The bot does not recreate button labels or styles, so available button color/style fields are preserved as received.
- **Channel templates:** create named channel groups. While a template is being created, channels newly connected by that admin are automatically included. You can also select channels already connected to the bot.
- **Default publishing target:** choose all connected channels or one saved template. Every new message sent to the bot is published to that target.
- **Auto-delete:** persistent scheduled deletion for posts made by this bot. Jobs survive a restart because they are stored in SQLite.
- **Unlimited channel records:** there is no application-level 100-channel cap. Actual send speed is governed by Telegram's API limits and flood-wait responses; the bot retries `RetryAfter` errors.
- **Private admin panel:** only `OWNER_IDS` and optional `ADMIN_IDS` can use the panel or publish posts.

## Telegram limitation: deletion is at most 48 hours

Telegram's Bot API `deleteMessage` method only deletes messages sent less than 48 hours ago. The timer therefore offers **Off, 30 minutes, 1 hour, 2 hours, 6 hours, 12 hours, 24 hours, 36 hours, and a 48-hour safe-cutoff option**. The 48-hour option is scheduled a little before the hard boundary to allow for polling delay. The bot must be running around the scheduled deletion time; if it stays offline until the message is older than 48 hours, Telegram will reject the deletion. **3–6 day deletion cannot be implemented reliably with the normal Bot API**, so those options are intentionally not shown.

The bot needs to be an administrator in each channel with **Post Messages** permission. Telegram's Bot API permits the bot to delete its own outgoing channel posts when it has `can_post_messages` permission. If the bot cannot post the welcome message, check its channel permissions.

## Requirements

- Python 3.10+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your numeric Telegram user ID for `OWNER_IDS`
- A persistent filesystem/disk for `data/bot.db` in production (auto-delete jobs and channel/template settings are stored there)

## Run locally

1. Download/unzip this repository and open a terminal in the project folder.
2. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate       # Windows: .venv\\Scripts\\activate
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in:

   ```env
   BOT_TOKEN=your_bot_token
   OWNER_IDS=your_numeric_telegram_user_id
   ADMIN_IDS=
   DATABASE_PATH=data/bot.db
   ```

4. Start the bot:

   ```bash
   python main.py
   ```

5. Open the bot in Telegram and send `/start` or `/admin`.
6. Add the bot as an administrator in each channel where it should publish. Grant **Post Messages**.
7. Send or forward a text/media post to the bot in its private chat. It will copy that message to the currently selected target.

## Admin panel guide

### Channel connection

Add the bot to a channel as an administrator. The `my_chat_member` update is used to register the channel. No manual channel ID is needed. The bot posts a confirmation in the channel; if Telegram rejects that confirmation, the channel is still saved and the owner receives a warning.

### Channel templates

1. Open **Set channel templates → Create a template**.
2. Send a template name.
3. Add the bot as an administrator in any new channels you want included. They are automatically attached to the template while capture is active.
4. Or tap **Select already-connected channels** and tap channels to toggle them into/out of the template.
5. Tap **Finish adding channels**.
6. Open **Default target** and choose **All connected channels** or one template.

### Auto-delete

Open **Auto delete** and choose the interval. Only posts successfully copied by the bot are scheduled. The timer is global and affects future posts; changing it does not alter existing scheduled jobs. `Off` stops scheduling new deletions but does not cancel jobs already waiting.

## Deploy on Render

This repository includes `render.yaml` and a Dockerfile for a **Background Worker**:

1. Push this folder to a GitHub repository.
2. In Render, create a new Blueprint from that repository.
3. Set `BOT_TOKEN` and `OWNER_IDS` in the service environment.
4. Keep the persistent disk mounted at `/app/data` and `DATABASE_PATH=/app/data/bot.db`.
5. Deploy the worker. Do not run a second worker with the same bot token at the same time; Telegram polling should have one active process.

A persistent disk is important. Without it, a redeploy/restart may lose the channel list, templates, and queued auto-deletions.

## Environment variables

| Variable | Required | Default | Meaning |
|---|---:|---|---|
| `BOT_TOKEN` | Yes | — | BotFather token |
| `OWNER_IDS` | Yes | — | Comma-separated owner Telegram user IDs |
| `ADMIN_IDS` | No | empty | Additional trusted admin IDs |
| `DATABASE_PATH` | No | `data/bot.db` | SQLite database location |
| `POLL_TIMEOUT` | No | `50` | Long-poll timeout in seconds (5–60) |
| `SEND_DELAY_SECONDS` | No | `0.10` | Delay between channel sends (0–5) |
| `DELETE_CHECK_INTERVAL` | No | `10` | Auto-delete worker interval (5–300) |
| `LOG_LEVEL` | No | `INFO` | Python log level |

## Important behavior and limitations

- The bot copies the message it receives in its private chat. The original forward header is absent in the destination.
- Telegram's Bot API does not allow copying every possible message type. Paid media, giveaway-related messages, invoices, widgets, and some quiz polls are not copyable. Protected messages may not be forwardable to the bot in the first place. Such failures are listed in the publishing summary.
- Inline keyboards are passed through exactly as received. URL buttons remain URL buttons. Telegram may omit a keyboard when it does not allow that keyboard to be forwarded to the bot; the publisher can only preserve `reply_markup` actually present in the incoming update. Callback-data buttons may still be displayed, but their original callback behavior belongs to the bot that created them; this publisher bot cannot reproduce another bot's private callback logic.
- Every incoming album item is copied as its own message so the bot can preserve each message's own inline keyboard. Telegram album grouping may therefore not be retained.
- A channel is registered when Telegram reports that this bot became an administrator there. If you add the bot before starting a template capture, create the template and select those existing channels from the picker.
- The bot does not bypass protected-content settings or Telegram's permissions. It only posts to channels where its administrators have added it and granted posting rights.
- The application itself does not cap the number of connected channels. Telegram can still apply flood limits; the bot sends sequentially and handles `retry_after` responses.

## Tests

Run the included helper tests:

```bash
python -m unittest discover -s tests -v
```

The tests cover the deletion limit, timer deadline calculation, and raw inline-keyboard pass-through. They do not replace an end-to-end test with a real Telegram bot token and a test channel.
