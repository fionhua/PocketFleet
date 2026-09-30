# Real Fleet Mode

`/fleet` coordinates two real, synchronous coding-agent executors:

1. Lead: inspect the repository and produce a testable acceptance contract.
2. Builder: edit the workspace and run relevant tests.
3. Lead: inspect the resulting workspace, rerun verification, and return
   `VERDICT: PASS` or `VERDICT: FAIL`.

PocketFleet never substitutes simulation output for an unavailable executor.
`/sim` is an explicit preview mode and must state that no agent, file change, or
test execution occurred.

## Telegram identity

Use one dedicated PocketFleet bot for inbound commands and outbound status.
Do not reuse a personal agent bot token: Telegram permits only one `getUpdates`
consumer per bot, so reuse will make the agent and PocketFleet steal messages
from each other.

Agent names in fleet output identify the executor that produced the content.
PocketFleet does not send generated text through an agent's personal bot token.
An independent Codex CLI turn is labeled `OpenAI Codex worker`; it is never presented
as a named teammate or as the user's current interactive Codex conversation.

## Antigravity CLI

PocketFleet uses the official `agy --print` interface and waits for its process to
return the actual model response. Configure `POCKETFLEET_ANTIGRAVITY_CLI` in `.env`
only when `agy` is not already on `PATH`.

The product does not inject tasks into a user's interactive Antigravity IDE track.
`agentapi send-message` is only a delivery acknowledgement and is never accepted as
completed work.
