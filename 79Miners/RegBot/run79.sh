#!/bin/sh
# Register hotkey(s) on netuid 79 and auto-launch a miner for each.
# Uses Paul-SuperRegBot/.venv (bittensor); generated miners use sn-79/.venv.
#
# This is the example/template counterpart to run79.sh. It mirrors run79.sh's
# operational flags (dispatcher + RegimeFeeHybridAgent, --reglimit 0.0323,
# --max-count 1, no rsync) but keeps placeholder wallet/telegram so it stays a
# safe-to-copy template. The dispatcher reads run_miner_<uid>.json (re-read every
# poll_seconds) so you can hot-swap the sub-agent/params live without a restart.
#
# Optional Features:
#   - Telegram Notifications: Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
#     See: TELEGRAM_SETUP.md for how to get these values

cd "$(dirname "$0")" || exit 1

VENV_PYTHON="$(pwd)/.venv/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
    echo "Missing $VENV_PYTHON — create .venv and install deps in Paul-SuperRegBot"
    exit 1
fi

# Configuration (customize these — all overridable via env vars)
TELEGRAM_BOT_TOKEN="8535622975:AAF0D8o3r0zzEGOPDegoWViOmyB9HeEx-uE"
TELEGRAM_CHAT_ID="-1003640047338"

# Dispatcher sub-agent to seed at registration, registration price cap, how many
# hotkeys to register, and the axon port band. Override by exporting before
# running, e.g.:
#   DISPATCH_AGENT=RandomTakerAgentBeta REGLIMIT=0.05 MAX_COUNT=3 ./run79.sh
#   PORT_PREFIX=80XX ./run79.sh
DISPATCH_AGENT="${DISPATCH_AGENT:-WarriorAgent}"
# Some agent_ids in dispatch_example.json share the same DISPATCH_AGENT name
# but carry different tuned params (e.g. two GammaPrimeAgent variants) —
# --dispatch-agent (by name) always resolves to the FIRST same-named entry,
# so picking a later duplicate requires --dispatch-index (by agent_id)
# instead. Set DISPATCH_INDEX to select a specific agent_id unambiguously;
# leave it empty to dispatch by name as before.
# NOTE: the dashboard rewrites this exact line (regbot.py _build_script) to
# thread the agent_id through — removing it breaks every dashboard-triggered
# registration with "Template substitution failed".
DISPATCH_INDEX="${DISPATCH_INDEX:-}"
REGLIMIT="${REGLIMIT:-0.1051}"
MAX_COUNT="${MAX_COUNT:-1}"
WALLET="${WALLET:-bittensor001}"  # funded mainnet coldkey

# Axon port band for THIS server. X marks a wildcard digit, so 79XX = 7900-7999
# (100 ports) and 809X = 8090-8099 (10). Comma-separate — no spaces — to spill
# into later bands once the first fills, e.g. PORT_PREFIX="809X,808X".
# Give each server its own band so their miners can never collide:
#   Server1 79XX   Server2 80XX   Server3 89XX   ...
# Allocation never leaves the listed bands. Set PORT_PREFIX="" to fall back to
# creg79.py's legacy --port-base walk (starts at 7900, uncapped band).
# Note the `-` (not `:-`): an explicitly empty PORT_PREFIX="" is honoured as
# "no band", while leaving it unset picks up the 79XX default.
PORT_PREFIX="${PORT_PREFIX-79XX}"

if [ -n "$PORT_PREFIX" ]; then
    PORT_ARGS="--port-prefix $PORT_PREFIX"
else
    PORT_ARGS=""
fi

if [ -n "$DISPATCH_INDEX" ]; then
    DISPATCH_ARG="--dispatch-index $DISPATCH_INDEX"
else
    DISPATCH_ARG="--dispatch-agent $DISPATCH_AGENT"
fi

run_creg79() {
    # Any args given are prepended to the command — used to slot in `sudo`.
    # $PORT_ARGS and $DISPATCH_ARG are intentionally unquoted so they word-split
    # (or vanish when empty); neither ever contains spaces.
    #
    # --reglimit and --feelimit are BOTH required and must match: --reglimit is
    # only an outer one-time gate before the retry loop, while --feelimit is what
    # makes creg79.py re-check the live recycle fee before EVERY retry inside it.
    # Without --feelimit a price spike mid-retry registers anyway — that is how
    # UID 102 cost 0.1302934τ against a 0.1016τ limit. Note creg79.py parses with
    # parse_known_args(), so a dropped or misspelled flag fails silently.
    "$@" "$VENV_PYTHON" "$(pwd)/creg79.py" \
      --netuid 79 \
      --name "$WALLET" \
      --path /root/.bittensor/wallets \
      --launch-miner \
      --miner-repo /root/79/sn-79 \
      --agent-preset dispatcher \
      $DISPATCH_ARG \
      --no-check-whitelist \
      --connect-retries 10 \
      --connect-retry-base 3 \
      --reglimit "$REGLIMIT" \
      --feelimit "$REGLIMIT" \
      --max-count "$MAX_COUNT" \
      $PORT_ARGS \
      --telegram-token "${TELEGRAM_BOT_TOKEN}" \
      --telegram-chat-id "${TELEGRAM_CHAT_ID}"
}

# creg79.py requires root; use venv python (not bare python3 / sudo python3).
if [ "$(id -u)" -eq 0 ]; then
    run_creg79
else
    run_creg79 sudo
fi
