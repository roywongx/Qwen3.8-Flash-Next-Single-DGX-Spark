#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# observe-snapshot.sh — append one memory/GPU trend sample to logs/observe/.
#
# Read-only: it starts, stops and restarts nothing, and sends no generation
# request to the model (a large-context probe is what pushed MemAvailable under
# the 6 GiB floor on 2026-09-30, so sampling must stay cheap).
#
# The interesting series is `driver`: the GPU-side hold, computed exactly as
# memwatch.sh does it. It is what the NVIDIA driver can still hand out to the
# graphics stack, so a rising series is the early warning that the next
# kgrctxAllocCtxBuffers allocation will fail — long before MemAvailable reaches
# any floor. memwatch's own LEAK TREND line (TREND_GIB, default 4) watches the
# same number against a post-load baseline; this script just persists it so the
# trend survives a reboot.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_DIR/logs/observe"
SAMPLE="$OUT_DIR/samples.tsv"
mkdir -p "$OUT_DIR"

ts=$(date '+%F %T')

# Same subtraction memwatch.sh uses, from one read of /proc/meminfo.
eval "$(awk '/^(MemTotal|MemFree|MemAvailable|Buffers|Cached|AnonPages|Slab|PageTables|KernelStack):/ {
    gsub(":", "", $1); printf "m_%s=%s\n", $1, $2
}' /proc/meminfo)"

driver=$(( m_MemTotal - m_MemFree - m_Buffers - m_Cached - m_AnonPages - m_Slab - m_PageTables - m_KernelStack ))
gib() { echo "scale=2; $1 / 1048576" | bc; }

container_state=$(docker inspect -f '{{.State.Status}}' qwen38-flash-next 2>/dev/null || echo unknown)
health=$(curl -s -o /dev/null -w '%{http_code}' -m 8 http://127.0.0.1:8000/health 2>/dev/null || echo 000)
nvrm=$(journalctl -k --since '1 hour ago' --no-pager 2>/dev/null | grep -c NV_ERR_NO_MEMORY || echo 0)

# Delivered-notification volume, read-only from netops-ai. This is the metric
# that says whether the 2026-09-30 alert fixes actually helped: SUPERVISOR and
# memwatch were 38 of the 78 notices sent that day, both since throttled.
# Opened mode=ro so a sampling run can never write to (or lock) the netops DB.
notice_counts() {
    python3 - <<'PY' 2>/dev/null || echo "0 0 0"
import os, sqlite3, time
db = "/home/roy/netops-ai/netops.db"
if not os.path.exists(db):
    print("0 0 0"); raise SystemExit
try:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    row = c.execute(
        "SELECT SUM(sent_ok=1),"
        "       SUM(sent_ok=1 AND text LIKE 'SUPERVISOR%'),"
        "       SUM(sent_ok=1 AND text LIKE 'memwatch%')"
        "  FROM notices WHERE ts >= ?", (time.time() - 86400,)).fetchone()
    print(*(int(v or 0) for v in row))
except Exception:
    print("0 0 0")
PY
}
read -r n_all n_supervisor n_memwatch <<<"$(notice_counts)"

if [[ ! -f "$SAMPLE" ]]; then
    printf 'time\tMemTotal\tMemFree\tMemAvailable\tdriver\tcontainer\thealth\tnvrm_1h\tnotice_24h\tsupervisor_24h\tmemwatch_24h\n' > "$SAMPLE"
fi
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$ts" \
    "$(gib "${m_MemTotal:-0}")" \
    "$(gib "${m_MemFree:-0}")" \
    "$(gib "${m_MemAvailable:-0}")" \
    "$(gib "$driver")" \
    "$container_state" \
    "$health" \
    "$nvrm" \
    "${n_all:-0}" \
    "${n_supervisor:-0}" \
    "${n_memwatch:-0}" >> "$SAMPLE"

# Keep 30 days; one row per run.
find "$OUT_DIR" -name 'samples.tsv*' -mtime +30 -delete 2>/dev/null || true

echo "$ts driver=$(gib "$driver")GiB MemAvailable=$(gib "${m_MemAvailable:-0}")GiB health=$health nvrm_1h=$nvrm notices_24h=${n_all:-0}(supervisor=${n_supervisor:-0} memwatch=${n_memwatch:-0})"
