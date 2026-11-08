#!/usr/bin/env bash
# Total memory of the desktop app's process tree (Linux), for the #833 budget
# (< 120 MB idle, every process the app owns, webview helpers included).
#
# Walks <pid> and all its descendants (WebKitWebProcess, WebKitNetworkProcess,
# …) and sums three numbers per process:
#   RSS  resident pages, counting every shared library page once per process
#        that maps it — so the sum over-counts (this is what #833 budgets);
#   PSS  shared pages split among their users — the fairer total;
#   USS  pages private to the process — what quitting the app gives back;
#   DIRTY  the private pages that are also dirty: heap, JS, caches — what the
#        kernel cannot simply drop and re-read from a file.
#
# Usage: desktop/scripts/measure-rss.sh <pid>
set -euo pipefail
root="${1:?usage: measure-rss.sh <pid>}"

tree() {
  echo "$1"
  for c in $(cat /proc/"$1"/task/*/children 2>/dev/null); do tree "$c"; done
}

total_rss=0
total_pss=0
total_uss=0
total_dirty=0
printf '%-8s %-20s %10s %10s %10s %10s\n' PID NAME RSS_KB PSS_KB USS_KB DIRTY_KB
for p in $(tree "$root" | sort -un); do
  [ -r /proc/"$p"/status ] || continue
  name=$(awk '/^Name:/{print $2}' /proc/"$p"/status)
  rss=$(awk '/^VmRSS:/{print $2}' /proc/"$p"/status)
  pss=$(awk '/^Pss:/{print $2}' /proc/"$p"/smaps_rollup 2>/dev/null || echo 0)
  uss=$(awk '/^Private_(Clean|Dirty):/{s+=$2} END{print s+0}' /proc/"$p"/smaps_rollup 2>/dev/null || echo 0)
  dirty=$(awk '/^Private_Dirty:/{print $2}' /proc/"$p"/smaps_rollup 2>/dev/null || echo 0)
  rss=${rss:-0}; pss=${pss:-0}; uss=${uss:-0}; dirty=${dirty:-0}
  printf '%-8s %-20s %10s %10s %10s %10s\n' "$p" "$name" "$rss" "$pss" "$uss" "$dirty"
  total_rss=$((total_rss + rss))
  total_pss=$((total_pss + pss))
  total_uss=$((total_uss + uss))
  total_dirty=$((total_dirty + dirty))
done
printf '%-29s %10s %10s %10s %10s\n' TOTAL_KB "$total_rss" "$total_pss" "$total_uss" "$total_dirty"
awk -v r="$total_rss" -v p="$total_pss" -v u="$total_uss" -v d="$total_dirty" \
  'BEGIN { printf "TOTAL: RSS %.1f MB, PSS %.1f MB, USS %.1f MB, private dirty %.1f MB\n", r / 1024, p / 1024, u / 1024, d / 1024 }'
