#!/usr/bin/env bash
set -euo pipefail

segment="${1:?segment number is required}"
mkdir -p state-out

if [[ "$segment" == "1" ]]; then
  encrypted_db="bot.db.enc"
  date -u -d '+24 hours' +%s > deadline.txt
else
  encrypted_db="state-in/bot.db.enc"
  cp state-in/deadline.txt deadline.txt
fi

openssl enc -d -aes-256-cbc -pbkdf2 \
  -in "$encrypted_db" -out bot.db -pass env:STATE_KEY

deadline="$(cat deadline.txt)"
now="$(date -u +%s)"
remaining=$((deadline - now))

if (( remaining > 0 )); then
  # Leave ample time below GitHub's six-hour job cap for setup and state upload.
  max_segment=20500
  (( remaining < max_segment )) && max_segment="$remaining"
  echo "Starting bot segment $segment for up to $max_segment seconds."

  set +e
  timeout --signal=INT --kill-after=30s "${max_segment}s" python -u bot.py
  status=$?
  set -e

  # GNU timeout returns 124 when the planned segment duration is reached.
  if [[ "$status" != "0" && "$status" != "124" ]]; then
    echo "Bot stopped unexpectedly with status $status."
    exit "$status"
  fi
else
  echo "The 24-hour deadline has already passed."
fi

openssl enc -aes-256-cbc -salt -pbkdf2 \
  -in bot.db -out state-out/bot.db.enc -pass env:STATE_KEY
cp deadline.txt state-out/deadline.txt
