#!/usr/bin/env bash
# Rebuild the patched YouTube from a stock APK pair.
#
#   ./repatch.sh                 pull the current APKs off the TV, patch, build, install
#   ./repatch.sh --no-install    build only, leave the APK in build/
#   ./repatch.sh --keep          keep the decoded tree for inspection
#
# The first run creates your signing key in keystore/. Keep it: only a build signed
# with the same key can upgrade the installed app in place without wiping your sign-in.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$HERE/build"
KS="$HERE/keystore/yt.keystore"
PKG_STOCK="com.google.android.youtube.tv"
PKG_PATCHED="dev.chillbytes.youtubetv"
INSTALL=1; KEEP=0
for a in "$@"; do
  case "$a" in
    --no-install) INSTALL=0 ;;
    --keep) KEEP=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

need() { command -v "$1" >/dev/null || { echo "missing dependency: $1" >&2; exit 1; }; }
need apktool; need jarsigner; need keytool; need adb; need python3
python3 -c "import PIL" 2>/dev/null || { echo "missing dependency: python3 Pillow (pip3 install Pillow)" >&2; exit 1; }

if [ ! -f "$KS" ]; then
  echo "==> creating your signing key in keystore/ (keep it: updates must be signed with it)"
  mkdir -p "$(dirname "$KS")"
  keytool -genkeypair -keystore "$KS" -storepass ytpatch -keypass ytpatch -alias ytpatch \
    -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=ytpatch" >/dev/null
fi

mkdir -p "$BUILD"; rm -rf "$BUILD/decoded" "$BUILD/split"

echo "==> pulling stock APKs from the TV"
PATHS=()
while IFS= read -r line; do [ -n "$line" ] && PATHS+=("$line"); done < <(
  adb shell pm path "$PKG_STOCK" | tr -d '\r' | sed 's/^package://')
[ "${#PATHS[@]}" -ge 2 ] || { echo "expected a base + ABI split, got ${#PATHS[@]}" >&2; exit 1; }
BASE=""; SPLIT=""
for p in "${PATHS[@]}"; do
  case "$p" in
    *split_config.armeabi_v7a*) SPLIT="$p" ;;
    *base.apk) BASE="$p" ;;
  esac
done
[ -n "$BASE" ] && [ -n "$SPLIT" ] || { echo "could not identify base/split among: ${PATHS[*]}" >&2; exit 1; }
adb pull "$BASE"  "$BUILD/base.apk"  >/dev/null
adb pull "$SPLIT" "$BUILD/split.apk" >/dev/null
# NB: no early-exiting filter here - SIGPIPE would trip `set -o pipefail`
VER=$(adb shell dumpsys package "$PKG_STOCK" | tr -d '\r' \
      | sed -n 's/.*versionName=\([^ ]*\).*/\1/p' | sed -n '1p')
echo "    stock version: $VER"

echo "==> decoding"
apktool d -f -o "$BUILD/decoded" "$BUILD/base.apk" >/dev/null
mkdir -p "$BUILD/split" && (cd "$BUILD/split" && unzip -qo "$BUILD/split.apk" 'lib/*')

echo "==> applying patches"
python3 "$HERE/patches/apply.py" "$BUILD/decoded" "$BUILD/split/lib/armeabi-v7a"

OUT="$BUILD/youtube-patched-${VER:-unknown}.apk"
echo "==> building"
rm -f "$OUT"; apktool b -o "$OUT" "$BUILD/decoded" >/dev/null

echo "==> signing"
jarsigner -keystore "$KS" -storepass ytpatch -keypass ytpatch \
  -sigalg SHA256withRSA -digestalg SHA-256 "$OUT" ytpatch >/dev/null
echo "    $OUT"

if [ "$INSTALL" = 1 ]; then
  echo "==> installing"
  adb install -r "$OUT"
  echo "==> done. Launch it with:"
  echo "    adb shell monkey -p $PKG_PATCHED -c android.intent.category.LEANBACK_LAUNCHER 1"
else
  echo "==> built only (not installed)"
fi

[ "$KEEP" = 1 ] || rm -rf "$BUILD/decoded" "$BUILD/split" "$BUILD/base.apk" "$BUILD/split.apk"
