#!/usr/bin/env bash
# Total memory of the desktop app's process tree (Linux), for the #833 budget
# (< 120 MB idle, every process the app owns, webview helpers included).
#
# Walks <pid> and all its descendants (WebKitWebProcess, WebKitNetworkProcess,
# …) and sums VmRSS and PSS. RSS counts each shared library page once per
# process that maps it, so the RSS sum over-counts; PSS splits shared pages
# among their users and is the fairer "what does this app cost" number.
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
printf '%-8s %-28s %10s %10s\n' PID NAME RSS_KB PSS_KB
for p in $(tree "$root" | sort -un); do
  [ -r /proc/"$p"/status ] || continue
  name=$(awk '/^Name:/{print $2}' /proc/"$p"/status)
  rss=$(awk '/^VmRSS:/{print $2}' /proc/"$p"/status)
  pss=$(awk '/^Pss:/{print $2}' /proc/"$p"/smaps_rollup 2>/dev/null || echo 0)
  rss=${rss:-0}; pss=${pss:-0}
  printf '%-8s %-28s %10s %10s\n' "$p" "$name" "$rss" "$pss"
  total_rss=$((total_rss + rss))
  total_pss=$((total_pss + pss))
done
printf '%-37s %10s %10s\n' TOTAL_KB "$total_rss" "$total_pss"
awk -v r="$total_rss" -v p="$total_pss" 'BEGIN { printf "TOTAL: RSS %.1f MB, PSS %.1f MB\n", r / 1024, p / 1024 }'
