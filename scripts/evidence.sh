#!/usr/bin/env bash
# Produce the evidence pack from an already-recorded capability.
#
# The discovery run is NOT part of this script: it is driven by a real LLM and
# recorded once (see evidence/README.md). Everything here is the deterministic
# half, and it is reproducible on any machine with no model installed.
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH=src
export LEDGERHAND_MCB_OPERATOR="${LEDGERHAND_MCB_OPERATOR:-svc.automation}"
export LEDGERHAND_MCB_PASSWORD="${LEDGERHAND_MCB_PASSWORD:-Sandbox!Demo1}"

CAP="member.savings_balance.lookup"
RISKY="member.subaccount.open"
APP="http://127.0.0.1:8848"
run() { echo; echo "=============== $1 ==============="; shift; "$@" || true; }
chaos() { curl -s -X POST "$APP/admin/chaos" -d "$1" >/dev/null; }
reset() { curl -s -X POST "$APP/admin/reset" >/dev/null; }

curl -sf -m 3 "$APP/admin/health" >/dev/null || {
  echo "the target app is not running; start it with: ledgerhand serve-app"; exit 1; }

reset
run "1. replay: success, the member the capability was recorded against" \
  python3 -m ledgerhand.cli replay "$CAP" --arg member_id=12345

reset
run "2. replay: a DIFFERENT member — the capability is parameterised, not a macro" \
  python3 -m ledgerhand.cli replay "$CAP" --arg member_id=23456

reset
run "3. replay: no such member — a BUSINESS OUTCOME, not a failure" \
  python3 -m ledgerhand.cli replay "$CAP" --arg member_id=99999

reset
run "4. replay: restricted record — permission denial, also a business outcome" \
  python3 -m ledgerhand.cli replay "$CAP" --arg member_id=55555

reset
run "5. replay: caller passed a bad argument — rejected before touching the UI" \
  python3 -m ledgerhand.cli replay "$CAP" --arg member_id=not-a-number

reset; chaos '{"interstitial": true}'
run "6. replay: unexpected interstitial — RECOVERED, run completes" \
  python3 -m ledgerhand.cli replay "$CAP" --arg member_id=12345

reset; chaos '{"slow_ms": 1200}'
run "7. replay: transient slowness — absorbed by condition waits" \
  python3 -m ledgerhand.cli replay "$CAP" --arg member_id=12345

reset; chaos '{"server_error": true}'
run "8. replay: HTTP 500 — HARD FAILURE with debuggable detail" \
  python3 -m ledgerhand.cli replay "$CAP" --arg member_id=12345

reset; chaos '{"expire_session": true}'
run "9. replay: session expiry — hard failure ESCALATED to a human, who takes the live session and hands it back" \
  python3 -m ledgerhand.cli replay "$CAP" --arg member_id=12345 --escalate --auto-operator

reset
run "10. cross-tenant: attach tenant B's overlay to the SAME capability" \
  python3 -m ledgerhand.cli overlay overlays/summitcu.yaml

reset
run "11. replay the same capability against tenant B (different labels, routes, version)" \
  python3 -m ledgerhand.cli replay "$CAP" --tenant summitcu --arg member_id=12345

reset
run "12. the capability catalog an AI agent would discover" \
  python3 -m ledgerhand.cli catalog

reset
run "13. invoked by name, as an agent would call it" \
  python3 -m ledgerhand.cli invoke "$CAP" --arg member_id=23456

# ---------------------------------------------------------------------------
# The irreversible capability. Its last click opens a real account, so policy
# classifies it as irreversible and it will not replay unattended until a human
# has approved it. These four runs are the guardrail actually working.
# ---------------------------------------------------------------------------

if [ -f "artifacts/${RISKY}.v1.json" ]; then
  reset
  run "14. the recorded capability that COMMITS something — note the risk column" \
    python3 -m ledgerhand.cli show "$RISKY"

  python3 -m ledgerhand.cli approve "$RISKY" --state draft >/dev/null 2>&1
  reset
  run "15. unattended replay of a DRAFT capability with an irreversible step — REFUSED before touching the UI" \
    python3 -m ledgerhand.cli replay "$RISKY" \
      --arg member_id=23456 --arg account_type=SAVINGS \
      --arg nickname="Holiday fund" --arg initial_deposit=150.00

  reset
  run "16. same capability, ATTENDED — a human is watching, so it proceeds" \
    python3 -m ledgerhand.cli replay "$RISKY" --attended \
      --arg member_id=23456 --arg account_type=SAVINGS \
      --arg nickname="Holiday fund" --arg initial_deposit=150.00

  run "17. a human approves the capability for unattended use" \
    python3 -m ledgerhand.cli approve "$RISKY" --state approved

  reset
  run "18. unattended replay of the APPROVED capability — now permitted" \
    python3 -m ledgerhand.cli replay "$RISKY" \
      --arg member_id=23456 --arg account_type=SAVINGS \
      --arg nickname="Holiday fund" --arg initial_deposit=150.00

  reset
  run "19. a deposit below the capability's own minimum — rejected by the input contract, before the bank ever sees it" \
    python3 -m ledgerhand.cli replay "$RISKY" --attended \
      --arg member_id=23456 --arg account_type=SAVINGS \
      --arg nickname="Holiday fund" --arg initial_deposit=5.00

  reset; chaos '{"force_validation": true}'
  run "20. the host declines the posting — a BUSINESS OUTCOME on a committing capability, not a crash" \
    python3 -m ledgerhand.cli replay "$RISKY" --attended \
      --arg member_id=23456 --arg account_type=SAVINGS \
      --arg nickname="Holiday fund" --arg initial_deposit=150.00

  python3 -m ledgerhand.cli approve "$RISKY" --state draft >/dev/null 2>&1
else
  echo; echo "(skipping 14-19: ${RISKY} has not been recorded yet)"
fi

echo; echo "evidence written under evidence/"
