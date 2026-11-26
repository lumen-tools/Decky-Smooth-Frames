#!/bin/sh
# Builds release/Decky-AFMF.zip, the file to install with Decky ("Install plugin from ZIP").
# Needs everything scripts/build-layer.sh needs, plus Node.js and pnpm.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
cd "$here"

./scripts/build-layer.sh
pnpm install --frozen-lockfile
pnpm run build

out="$here/release"
stage="$(mktemp -d)"
mkdir -p "$stage/Decky-AFMF/dist" "$out"
cp dist/index.js "$stage/Decky-AFMF/dist/"
cp -r bin main.py plugin.json package.json LICENSE README.md THIRD_PARTY.md "$stage/Decky-AFMF/"
rm -f "$out/Decky-AFMF.zip"
(cd "$stage" && zip -qr "$out/Decky-AFMF.zip" Decky-AFMF)
(cd "$out" && sha256sum Decky-AFMF.zip > Decky-AFMF.zip.sha256)
rm -rf "$stage"
echo "release: $out/Decky-AFMF.zip"
