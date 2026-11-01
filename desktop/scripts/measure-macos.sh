#!/usr/bin/env bash
# Total memory of the desktop app on macOS, for the #833 budget (< 120 MB
# idle, every process the app owns, WebKit's helpers included). The Linux
# counterpart is measure-rss.sh.
#
# WKWebView runs the page in XPC services — com.apple.WebKit.WebContent,
# .GPU and .Networking — that launchd starts, so their parent is launchd (1),
# not the app, and `ps -o ppid` cannot tell ours from Safari's or Mail's.
# macOS does record which app is *responsible* for each one (that is how
# Activity Monitor and the privacy prompts attribute them); this script asks
# for it, and counts only the helpers the app is responsible for.
#
# Two numbers per process:
#   RSS        resident pages (what `ps` shows; counts shared frameworks once
#              per process, so the sum over-counts)
#   FOOTPRINT  physical footprint: private dirty + compressed + IOKit memory,
#              the number Activity Monitor's Memory column shows
#
# Usage: desktop/scripts/measure-macos.sh [app-pid]
#   (default: the running kube-coder-desktop)
#
# Needs python3 (part of the Xcode command-line tools the build needs). Without
# it the script falls back to `ps` RSS and helpers started after the app,
# which is only right when no other app using WebKit started since.
set -euo pipefail

# pgrep -f: macOS keeps only 16 characters of a process name, and
# "kube-coder-desktop" is 18, so `pgrep -x` would never match it.
app="${1:-$(pgrep -f '/kube-coder-desktop( |$)' | head -n1 || true)}"
if [ -z "$app" ] || ! kill -0 "$app" 2>/dev/null; then
  echo "kube-coder-desktop is not running (pass its pid)" >&2
  exit 1
fi

if command -v python3 >/dev/null 2>&1; then
  python3 -I - "$app" <<'PY'
import ctypes, ctypes.util, subprocess, sys

app = int(sys.argv[1])
libc = ctypes.CDLL(ctypes.util.find_library("c") or "/usr/lib/libSystem.B.dylib")

# <sys/resource.h>: struct rusage_info_v0, flavor RUSAGE_INFO_V0 = 0.
class RusageInfoV0(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [
        (n, ctypes.c_uint64) for n in (
            "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups",
            "ri_pageins", "ri_wired_size", "ri_resident_size", "ri_phys_footprint",
            "ri_proc_start_abstime", "ri_proc_exit_abstime")]

libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(RusageInfoV0)]
try:
    responsible_for = libc.responsibility_get_pid_responsible_for_pid
    responsible_for.argtypes = [ctypes.c_int]
    responsible_for.restype = ctypes.c_int
except AttributeError:
    responsible_for = None

def usage(pid):
    info = RusageInfoV0()
    if libc.proc_pid_rusage(pid, 0, ctypes.byref(info)) != 0:
        return None
    return info.ri_resident_size, info.ri_phys_footprint

procs = []
for line in subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True).stdout.splitlines():
    pid, _, comm = line.strip().partition(" ")
    procs.append((int(pid), comm.strip()))

rows = [(app, "kube-coder-desktop")]
others = []
for pid, comm in procs:
    name = comm.rsplit("/", 1)[-1]
    if not name.startswith("com.apple.WebKit."):
        continue
    owner = responsible_for(pid) if responsible_for else -1
    (rows if owner == app else others).append((pid, name))

if responsible_for is None:
    print("warning: cannot ask macOS which app a WebKit helper belongs to; counting none", file=sys.stderr)

mb = lambda b: b / (1024 * 1024)
print(f"{'PID':<8} {'PROCESS':<34} {'RSS_MB':>9} {'FOOTPRINT_MB':>13}")
tot_rss = tot_fp = 0
for pid, name in rows:
    u = usage(pid)
    if u is None:
        print(f"{pid:<8} {name:<34} {'?':>9} {'?':>13}")
        continue
    rss, fp = u
    tot_rss += rss
    tot_fp += fp
    print(f"{pid:<8} {name:<34} {mb(rss):9.1f} {mb(fp):13.1f}")
print(f"TOTAL: RSS {mb(tot_rss):.1f} MB, physical footprint {mb(tot_fp):.1f} MB "
      f"({len(rows)} processes: the app + {len(rows) - 1} WebKit helpers)")
if others:
    print(f"(not counted: {len(others)} WebKit helpers belonging to other apps)")
PY
else
  echo "python3 not found: RSS only, and helpers matched by start time" >&2
  start=$(ps -o lstart= -p "$app")
  since=$(date -j -f "%a %b %d %T %Y" "$start" +%s)
  total=0
  printf '%-8s %-34s %9s\n' PID PROCESS RSS_MB
  while read -r pid rss comm; do
    name=${comm##*/}
    if [ "$pid" != "$app" ]; then
      case "$name" in com.apple.WebKit.*) ;; *) continue ;; esac
      s=$(date -j -f "%a %b %d %T %Y" "$(ps -o lstart= -p "$pid")" +%s)
      [ "$s" -ge "$since" ] || continue
    fi
    printf '%-8s %-34s %9.1f\n' "$pid" "$name" "$(echo "$rss / 1024" | bc -l)"
    total=$((total + rss))
  done < <(ps -axo pid=,rss=,comm=)
  printf 'TOTAL: RSS %.1f MB\n' "$(echo "$total / 1024" | bc -l)"
fi

# Apple's own per-process breakdown, when the tool is there (macOS 11+).
if command -v footprint >/dev/null 2>&1; then
  echo
  echo "footprint (the app process):"
  footprint "$app" 2>/dev/null | grep -iE 'footprint|^ *TOTAL' | head -n 5 || true
fi
