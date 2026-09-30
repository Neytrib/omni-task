# Three-minute demo

This is a **planned 3:00 walkthrough**, not a claim that it has been executed. Use disposable, non-sensitive content. Demonstrate the running application in Telegram Desktop and **Arc**. Keep credentials, login links, terminal environment output, and unrelated private tasks out of recordings/screenshares.

## Before starting the timer

1. Complete [README setup](../README.md). From the project root, run `docker compose ps` and `curl --fail http://127.0.0.1:8080/api/health/ready`. Have Telegram's private bot chat, Arc, and a terminal ready. The local loopback link works on the Docker computer; a phone cannot reach that computer through its own `127.0.0.1`. This script does not require a public tunnel.
2. Confirm the provider choice privately. **Real recognition** uses the owner's configured OpenAI key, `TRANSCRIPTION_PROVIDER=openai`, and `ALLOW_PAID_TRANSCRIPTION=true`; sending the recording can be billable and must be the owner's explicitly approved choice. The existing enabled setting is not approval for the assistant to initiate another paid test. **No-charge pipeline demo** uses explicitly chosen `TRANSCRIPTION_PROVIDER=fake` and `ALLOW_PAID_TRANSCRIPTION=false`; it downloads/validates/converts the recording but returns labeled sample text. Say that aloud and never present it as recognized speech. Do not silently change another person's running configuration. After an intentional configuration change, recreate API/worker with `docker compose up -d --wait api worker`. Disabled/unconfigured mode should fail clearly and create no task.
3. Prepare one text message, including its final line, to make full-content inspection obvious:

   ```text
   Demo: prepare the internship walkthrough
   Explain why a retry cannot create a second task.
   Final line: keep the complete original message.
   ```

4. Prepare a 5–8 second voice sentence: “Demo voice: explain the database, queue, and notification flow.” Avoid sensitive information. Have a second Arc tab at the same local origin available after login to show synchronization; do not reuse a consumed login token.
5. Paste, but do not yet run, this guard in the terminal. It pauses both worker consumers and restores them when you press Enter, or interrupt the guard with Ctrl-C. It leaves the bot and API running.

   ```sh
   bash -c '
   set -e
   trap "docker compose unpause worker >/dev/null" EXIT
   trap "exit 130" INT
   trap "exit 143" TERM
   docker compose pause worker
   read -r -p "Send voice and /help, then press Enter to resume: " _
   '
   ```

   An EXIT trap cannot survive a forcibly killed terminal or machine shutdown. Always perform the cleanup check below, including after an interrupted demo.

## Timed walkthrough — exactly 3:00 allocated

| Time | Action | Observable result and narration |
| --- | --- | --- |
| 0:00–0:20 | Send `/profile`, click **Open Dashboard**, open in Arc, and bring up the second same-origin tab. | The token disappears from the visible URL; the private board loads and reaches **Live**. “Telegram is the entry point; the link becomes an expiring server session.” Do not expose the original link in a recording. |
| 0:20–0:45 | Send the prepared multiline text. Keep the board visible without refreshing. | One **Pending** card appears, with the original title/content preview and source. Telegram confirms creation with status buttons. “One message is one task, and the short title is derived without rewriting the content.” |
| 0:45–1:15 | Run the pause guard. Send the short voice recording, then `/help`. After observing the quick acknowledgement and command reply, press Enter to resume the worker. | The receipt appears while the worker is paused, and `/help` still responds. “Processing is queued independently of commands.” The terminal resumes the worker. This is a controlled temporary worker outage, not a failed transcription. In fake mode say: “This run exercises delivery with labeled sample text; it is not speech recognition.” |
| 1:15–1:40 | On the text confirmation, choose **In Progress**. In Arc, use the status selector to choose **Completed**. Send `/list` and open that task again in Telegram. | The bot action moves the card in both tabs. The dashboard action appears in the freshly opened Telegram details. “Existing Telegram messages do not auto-refresh; opening/listing reads current state.” The selector provides keyboard/mobile access without needing a drag. |
| 1:40–2:00 | Open the text card's details. Show all three lines, including the final line. | Complete content and timestamps are available, displayed as safe text. “Only previews and titles are shortened; the accepted original content is preserved.” Close details. |
| 2:00–2:25 | Inspect the voice acknowledgement and board. Open the voice task if ready. | A successful transcription edits the acknowledgement and creates **one Pending task** with the complete accepted transcript. Say “transcription completed,” not “the task is Completed.” Fake mode must visibly disclose sample content. If processing is still pending, show that honestly; do not invent success or resend the recording to fill the time. |
| 2:25–2:50 | Open the disposable text task, choose Delete, then Cancel. Reopen Delete and confirm. | Cancel retains the task; confirmation removes it from both boards. If its details were open in the other tab, they close gracefully. This demonstrates a real deletion rather than just hiding a card. |
| 2:50–3:00 | Show the live board and stop the timer. | “PostgreSQL is authoritative. Redis carries jobs and live hints; reconnect reloads the board. Codex assisted substantially with implementation and tests; the evidence and remaining limits are documented.” |

Provider and Telegram latency are external. The time slots total three minutes, but they do not promise that recognition will finish within that window. If it does not, end at 3:00 with the honest pending/failure state and verify the eventual result afterward. A failed transcription must produce a clear failure without a fake Pending task.

## Cleanup and post-demo checks

Run even if the walkthrough was interrupted:

```sh
docker compose unpause worker
docker compose ps worker
curl --fail http://127.0.0.1:8080/api/health/ready
```

If already unpaused, check the status rather than treating that message as a failed application. Confirm the worker is running/healthy; do not leave it paused. Verify any pending recording eventually produces exactly one task or an explicit failure. Delete only your disposable demo tasks through the normal confirmation flow. Do not reset volumes or remove existing user tasks. Close only tabs you opened for the demo; preserve Arc's existing tabs/profile.

For no-charge repeatable fault checks, run the existing integration tests instead of sending more provider requests:

```sh
python3 scripts/test_backend.py -q tests/test_delivery_recovery_integration.py tests/test_voice_worker_integration.py
```

These use real Redis/separate workers with fake Telegram/provider transports. They establish recovery behavior, not recognition quality. Repeated jobs cannot create duplicate tasks, but ambiguous external success before a durable checkpoint can repeat a provider call or fallback notification; do not claim exactly-once billing or delivery.

The current evidence ledger is [REQUIREMENTS.md](REQUIREMENTS.md) and [TASKS.md](../TASKS.md). S9 recorded 382 backend and 125 frontend tests passing; those are automated results, not results of this demo. Still outstanding are physical-phone/phone-layout, light-theme, 200%-zoom, and whole-card pointer-drag visual checks in Arc. A three-minute demonstration does not replace them or prove a public deployment.
