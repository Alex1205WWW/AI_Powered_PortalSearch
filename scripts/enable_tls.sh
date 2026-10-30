#!/usr/bin/env bash
# Turn on HTTPS for the panel once a hostname points at this server.
#
#   ./scripts/enable_tls.sh imoveis.exemplo.pt [email@dela.pt]
#
# Run it ON THE SERVER, from /srv/imoveis. It checks the DNS before changing anything, because opening the
# firewall and restarting Caddy against a name that does not resolve just produces a failed certificate
# request and a panel that is reachable over plain HTTP.
set -euo pipefail

HOST="${1:-}"
ACME_EMAIL="${2:-}"
[ -n "$HOST" ] || { echo "usage: $0 <hostname> [email]" >&2; exit 2; }
cd "$(dirname "$0")/.."

echo "==> 1/5  does $HOST point here?"
MY_IP="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
RESOLVED="$(getent ahostsv4 "$HOST" | awk '{print $1}' | sort -u | head -1 || true)"
echo "    this server : ${MY_IP:-unknown}"
echo "    $HOST -> ${RESOLVED:-nothing}"
if [ -z "$RESOLVED" ]; then
	echo "    The name does not resolve yet. DNS can take a few minutes to an hour." >&2
	echo "    Check again with:  getent ahostsv4 $HOST" >&2
	exit 1
fi
if [ -n "$MY_IP" ] && [ "$RESOLVED" != "$MY_IP" ]; then
	echo "    It resolves somewhere else. Fix the A record before continuing;" >&2
	echo "    a URL-forwarding / redirect record is not an A record and will not work." >&2
	exit 1
fi

case "$HOST" in
*.your-server.de | *.sslip.io | *.nip.io)
	echo "    $HOST cannot get a certificate: its parent domain is not on the Public Suffix List," >&2
	echo "    so Let's Encrypt's per-domain limit is shared with every other user of it." >&2
	exit 1
	;;
esac

echo "==> 2/5  writing PANEL_HOST into .env"
python3 - "$HOST" "$ACME_EMAIL" <<'PY'
import sys, pathlib
host, email = sys.argv[1], sys.argv[2]
path = pathlib.Path(".env")
lines, seen = [], set()
for line in path.read_text(encoding="utf-8").splitlines():
    key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
    if key == "PANEL_HOST":
        line = f"PANEL_HOST={host}"
    elif key == "PANEL_BASE_URL":
        line = f"PANEL_BASE_URL=https://{host}"
    elif key == "ACME_EMAIL":
        line = f"ACME_EMAIL={email}"
    if key:
        seen.add(key)
    lines.append(line)
for key, value in (("PANEL_HOST", host), ("PANEL_BASE_URL", f"https://{host}"), ("ACME_EMAIL", email)):
    if key not in seen:
        lines.append(f"{key}={value}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"    PANEL_HOST={host}")
print(f"    PANEL_BASE_URL=https://{host}")
PY

echo "==> 3/5  opening 80/443 (80 is where the certificate challenge lands)"
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw status | sed -n '1,8p'

echo "==> 4/5  starting Caddy and asking for the certificate"
docker compose up -d caddy
docker compose up -d --force-recreate web >/dev/null  # .env changed; a restart would not re-read it

for i in $(seq 1 30); do
	sleep 4
	if curl -fsS --max-time 10 "https://$HOST/healthz" 2>/dev/null | grep -q '"ok":true'; then
		echo "==> 5/5  HTTPS is live:  https://$HOST"
		curl -sI --max-time 10 "https://$HOST/" | sed -n '1p'
		exit 0
	fi
done

echo "==> 5/5  no certificate yet. The usual causes, in order of likelihood:" >&2
echo "    - port 80 blocked upstream (Hetzner Cloud Firewall in the console, not just ufw)" >&2
echo "    - the A record points somewhere else, or is a redirect rather than an A record" >&2
echo "    - DNS has not propagated yet" >&2
echo "    Read the reason:  docker compose logs caddy --tail 40" >&2
exit 1
