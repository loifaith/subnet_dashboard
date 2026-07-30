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

# Dispatcher sub-agent to seed at registration, registration price cap, and how
# many hotkeys to register. Override by exporting before running, e.g.:
#   DISPATCH_AGENT=RandomTakerAgentBeta REGLIMIT=0.05 MAX_COUNT=3 ./run79.example.sh
DISPATCH_AGENT="${DISPATCH_AGENT:-WarriorAgent}"
# Some agent_ids in dispatch_example.json share the same DISPATCH_AGENT name
# but carry different tuned params (e.g. two GammaPrimeAgent variants) —
# --dispatch-agent (by name) always resolves to the FIRST same-named entry,
# so picking a later duplicate requires --dispatch-index (by agent_id)
# instead. Set DISPATCH_INDEX to select a specific agent_id unambiguously;
# leave it empty to dispatch by name as before.
DISPATCH_INDEX="${DISPATCH_INDEX:-}"
REGLIMIT="${REGLIMIT:-0.1051}"
MAX_COUNT="${MAX_COUNT:-3}"
WALLET="${WALLET:-bittensor001}"  # funded mainnet coldkey

if [ -n "$DISPATCH_INDEX" ]; then
    DISPATCH_ARG="--dispatch-index $DISPATCH_INDEX"
else
    DISPATCH_ARG="--dispatch-agent $DISPATCH_AGENT"
fi

run_creg79() {
    "$VENV_PYTHON" "$(pwd)/creg79.py" \
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
      --telegram-token "${TELEGRAM_BOT_TOKEN}" \
      --telegram-chat-id "${TELEGRAM_CHAT_ID}"
}

# creg79.py requires root; use venv python (not bare python3 / sudo python3).
if [ "$(id -u)" -eq 0 ]; then
    run_creg79
else
    sudo "$VENV_PYTHON" "$(pwd)/creg79.py" \
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
      --telegram-token "${TELEGRAM_BOT_TOKEN}" \
      --telegram-chat-id "${TELEGRAM_CHAT_ID}"
fi
