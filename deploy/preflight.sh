#!/usr/bin/env bash
# Preflight for FindClient: what the server can actually reach and which Python it has.
# Run this BEFORE installing anything. Sources are scraped from the server's IP,
# so a blocked host here means a dead section of the bot later.
set -uo pipefail

say() { printf '\n=== %s ===\n' "$1"; }

say "system"
uname -a
if command -v lsb_release >/dev/null 2>&1; then lsb_release -a 2>/dev/null; fi
if [ -r /etc/os-release ]; then . /etc/os-release; echo "os: ${PRETTY_NAME:-unknown}"; fi
echo "cpu: $(nproc 2>/dev/null || echo '?')  mem: $(free -m 2>/dev/null | awk '/Mem:/{print $2" MB"}' || echo '?')"

say "python"
for p in python3 python3.13 python3.12 python3.11 python3.10; do
  command -v "$p" >/dev/null 2>&1 && printf '%-10s %s\n' "$p" "$("$p" -V 2>&1)"
done
command -v python3 >/dev/null 2>&1 && python3 - <<'PY'
import sys
print("venv module:", "ok" if __import__("importlib.util", fromlist=["util"]).find_spec("venv") else "MISSING (apt install python3-venv)")
print("version ok (>=3.10):", sys.version_info >= (3, 10))
PY

say "existing findclient / bot processes"
ps -eo pid,etime,cmd 2>/dev/null | grep -Ei 'bot\.py|findclient' | grep -v grep || echo "none"
systemctl list-units --type=service --all 2>/dev/null | grep -Ei 'findclient|2gis|scraper' || echo "no matching systemd units"
ls -d /opt/findclient /root/findclient /home/*/findclient /srv/findclient 2>/dev/null || echo "no /opt|/root|/home|/srv findclient dir"

say "outbound reachability (5s each, HTTP status / 000 = blocked)"
for url in \
  https://2gis.ru/moscow \
  https://catalog.api.2gis.ru/2.0/region/search \
  https://yandex.ru/maps/ \
  https://search-maps.yandex.ru/v1/ \
  https://kwork.ru/projects \
  https://www.fl.ru/rss/all.xml \
  https://freelance.ru/task \
  https://www.freelancejob.ru/rss.php \
  https://t.me/s/durov \
  https://api.telegram.org
do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -A 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36' "$url" 2>/dev/null)
  printf '%-45s %s\n' "$url" "$code"
done

say "dns"
for h in 2gis.ru catalog.api.2gis.ru yandex.ru kwork.ru t.me api.telegram.org; do
  ip=$(getent hosts "$h" 2>/dev/null | awk 'NR==1{print $1}')
  printf '%-24s %s\n' "$h" "${ip:-FAIL}"
done

say "done"
echo "000 on 2gis/yandex/kwork means the bot will not be able to read that source from here."
