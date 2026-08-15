#!/usr/bin/env bash
# Probe a running stack and print a health summary.
#
# Usage:  make verify        (or)  bash scripts/verify_stack.sh
#
# Exits non-zero if any *required* component is unhealthy. Optional components
# that are switched off (the LLM) and extensions that this environment does not
# require are reported but do not fail the check.

set -uo pipefail

API="${API_URL:-http://localhost:8000}"
WEB="${WEB_URL:-http://localhost:3000}"

bold=$'\033[1m'; dim=$'\033[2m'; red=$'\033[31m'; green=$'\033[32m'
yellow=$'\033[33m'; gray=$'\033[90m'; reset=$'\033[0m'

fail=0

printf '%s\n' "${bold}twquant — stack verification${reset}"
printf '%s\n' "${dim}api: ${API}   web: ${WEB}${reset}"
echo

# ------------------------------------------------------------------ api
code=$(curl -s -o /tmp/_health.json -w '%{http_code}' --max-time 10 "${API}/api/v1/health/full" || echo 000)
if [[ "$code" == "000" ]]; then
  printf '  %s✗%s API unreachable at %s\n' "$red" "$reset" "$API"
  echo
  echo "  Start it with: make up   (or) make api"
  exit 1
fi

python3 - "$code" <<'PY'
import json, sys

code = sys.argv[1]
report = json.load(open("/tmp/_health.json"))["data"]

COLOR = {
    "healthy":  "\033[32m",
    "degraded": "\033[33m",
    "unhealthy":"\033[31m",
    "disabled": "\033[90m",
    "unknown":  "\033[90m",
}
RESET = "\033[0m"
NAMES = {
    "api": "API", "postgres": "DATABASE", "timescaledb": "TIMESCALEDB",
    "pgvector": "PGVECTOR", "redis": "REDIS", "celery": "CELERY", "llm": "LLM",
}
ORDER = ["api", "postgres", "timescaledb", "pgvector", "redis", "celery", "llm"]
REQUIRED = {"api", "postgres", "redis", "celery"}

print(f"  SYSTEM HEALTH   http {code}   overall "
      f"{COLOR[report['status']]}{report['status'].upper()}{RESET}")
print(f"  \033[2m{report['app']} v{report['version']} · {report['environment']}\033[0m")
print()

components = sorted(
    report["components"],
    key=lambda c: ORDER.index(c["name"]) if c["name"] in ORDER else 99,
)

failed = []
for c in components:
    name = NAMES.get(c["name"], c["name"].upper())
    status = c["status"]
    latency = f"{c['latency_ms']:.1f} ms" if c.get("latency_ms") is not None else "—"
    version = (c.get("version") or "")[:28]
    dot = f"{COLOR[status]}●{RESET}"
    print(f"  {name:<14} {dot} {COLOR[status]}{status.upper():<10}{RESET} "
          f"\033[90m{latency:>9}  {version}{RESET}")
    if c.get("error"):
        print(f"                 \033[31m{c['error'][:90]}{RESET}")
    if c["name"] in REQUIRED and status == "unhealthy":
        failed.append(c["name"])

print()
sys.exit(1 if failed else 0)
PY
[[ $? -ne 0 ]] && fail=1

# ------------------------------------------------------------------ docs
for path in /docs /api/v1/openapi.json /api/v1/meta/contracts /api/v1/meta/capabilities; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${API}${path}" || echo 000)
  if [[ "$code" == "200" ]]; then
    printf '  %s✓%s %-28s %s\n' "$green" "$reset" "$path" "$code"
  else
    printf '  %s✗%s %-28s %s\n' "$red" "$reset" "$path" "$code"
    fail=1
  fi
done

# ------------------------------------------------------------------ web
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "${WEB}/login" || echo 000)
if [[ "$code" == "200" ]]; then
  printf '  %s✓%s %-28s %s\n' "$green" "$reset" "web /login" "$code"
else
  printf '  %s%s%s %-28s %s %s\n' "$yellow" "!" "$reset" "web /login" "$code" \
    "${gray}(frontend not running — not fatal)${reset}"
fi

echo
if [[ $fail -eq 0 ]]; then
  printf '  %s✓ stack verified%s\n' "$green" "$reset"
else
  printf '  %s✗ one or more required components are unhealthy%s\n' "$red" "$reset"
fi
exit $fail
