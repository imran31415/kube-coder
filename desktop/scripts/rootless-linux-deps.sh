#!/usr/bin/env bash
# Install Tauri's Linux build/run dependencies WITHOUT root, for a kube-coder
# workspace pod where sudo is unavailable (the pod runs with
# allowPrivilegeEscalation: false, so "no new privileges" blocks sudo).
#
# apt-get resolves the packages against its own lists in a user directory,
# downloads the .debs and unpacks them into a prefix; nothing is installed
# system-wide. The prefix is on the persistent volume, so it survives a pod
# restart. Prints the environment to source for `cargo` / `tauri build` and
# for running the app.
#
# Usage: desktop/scripts/rootless-linux-deps.sh [prefix]   (default /home/dev/.local/tauri-root)
set -euo pipefail

PREFIX="${1:-/home/dev/.local/tauri-root}"
APT_DIR="${PREFIX}-apt"
PKGS=(libwebkit2gtk-4.1-dev libsoup-3.0-dev librsvg2-dev libayatana-appindicator3-dev
      libxdo-dev libssl-dev libdbus-1-dev patchelf file)

mkdir -p "$APT_DIR/lists/partial" "$APT_DIR/cache/archives/partial" "$APT_DIR/debs" "$PREFIX"
APT=(apt-get -o "Dir::State::Lists=$APT_DIR/lists" -o "Dir::Cache=$APT_DIR/cache"
     -o Debug::NoLocking=1 -o "APT::Sandbox::User=$(id -un)")

"${APT[@]}" update >/dev/null
mapfile -t need < <("${APT[@]}" -s install "${PKGS[@]}" 2>/dev/null | awk '/^Inst /{print $2}')
echo "downloading ${#need[@]} packages"
(cd "$APT_DIR/debs" && "${APT[@]}" download "${need[@]}" >/dev/null)
for d in "$APT_DIR"/debs/*.deb; do dpkg-deb -x "$d" "$PREFIX"; done

# The .pc files say prefix=/usr; point them at the unpacked tree.
for f in "$PREFIX"/usr/lib/x86_64-linux-gnu/pkgconfig/*.pc "$PREFIX"/usr/share/pkgconfig/*.pc; do
  [ -f "$f" ] || continue
  sed -i -e "s#^prefix=/usr\$#prefix=$PREFIX/usr#" \
         -e "s#\(^\|[=: ]\|-I\|-L\)/usr/#\1$PREFIX/usr/#g" "$f"
done

# A -dev package's libfoo.so is a relative link to libfoo.so.N, which lives in
# the runtime package — often already installed system-wide, so not unpacked
# here. Point those links at the system copy.
find "$PREFIX" -type l -name '*.so*' | while read -r l; do
  [ -e "$l" ] && continue
  t=$(basename "$(readlink "$l")")
  for d in /usr/lib/x86_64-linux-gnu /lib/x86_64-linux-gnu; do
    if [ -e "$d/$t" ]; then ln -sf "$d/$t" "$l"; break; fi
  done
done

LIB="$PREFIX/usr/lib/x86_64-linux-gnu"

# WebKitGTK release builds ignore WEBKIT_EXEC_PATH and spawn their helper
# processes from a path compiled into libwebkit2gtk. Rewrite that path in the
# unpacked (private) copy to one of the SAME length that links to the helpers.
WK_OLD=/usr/lib/x86_64-linux-gnu/webkit2gtk-4.1
WK_NEW=$(python3 -I - "$(dirname "$PREFIX")" "$WK_OLD" <<'PY'
import sys
root, old = sys.argv[1], sys.argv[2]
tail = '/webkit2gtk-4.1'
pad = len(old) - len(root) - len('/wk') - len(tail)
if pad < 0:
    sys.exit('prefix too long for the WebKit path patch')
print(root + '/wk' + 'x' * pad + tail)
PY
)
mkdir -p "$(dirname "$WK_NEW")"
ln -sfn "$LIB/webkit2gtk-4.1" "$WK_NEW"
python3 -I - "$(readlink -f "$LIB/libwebkit2gtk-4.1.so.0")" "$WK_OLD" "$WK_NEW" <<'PY'
import sys
path, old, new = sys.argv[1], sys.argv[2].encode(), sys.argv[3].encode()
data = open(path, 'rb').read()
open(path, 'wb').write(data.replace(old, new))
PY

cat <<EOF
# --- source this to build ---
export PKG_CONFIG_PATH=$LIB/pkgconfig:$PREFIX/usr/share/pkgconfig
export LIBRARY_PATH=$LIB
export C_INCLUDE_PATH=$PREFIX/usr/include
export RUSTFLAGS="-C link-arg=-Wl,-rpath-link,$LIB -C link-arg=-Wl,-rpath-link,$PREFIX/lib/x86_64-linux-gnu"
# --- and this to run the built app ---
export LD_LIBRARY_PATH=$LIB:$PREFIX/lib/x86_64-linux-gnu
export GIO_MODULE_DIR=$LIB/gio/modules
export XDG_DATA_DIRS=$PREFIX/usr/share:\${XDG_DATA_DIRS:-/usr/share}
# Mesa's EGL vendor file is in the prefix, not /usr/share/glvnd.
export __EGL_VENDOR_LIBRARY_DIRS=$PREFIX/usr/share/glvnd/egl_vendor.d
# A virtual display (Xvfb) has no GPU: render without DMA-BUF.
export WEBKIT_DISABLE_DMABUF_RENDERER=1
EOF
