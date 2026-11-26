#!/bin/sh
# Builds release/Decky-Smooth-Frames.zip, the file to install with Decky ("Install plugin from ZIP").
# Needs everything scripts/build-layer.sh needs, plus Node.js and pnpm.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
cd "$here"

./scripts/build-layer.sh
pnpm install --frozen-lockfile
pnpm run build

out="$here/release"
stage="$(mktemp -d)"
mkdir -p "$stage/Decky-Smooth-Frames/dist" "$out"
cp dist/index.js "$stage/Decky-Smooth-Frames/dist/"
cp -r bin main.py plugin.json package.json LICENSE README.md THIRD_PARTY.md "$stage/Decky-Smooth-Frames/"
rm -f "$out/Decky-Smooth-Frames.zip"
(cd "$stage" && zip -qr "$out/Decky-Smooth-Frames.zip" Decky-Smooth-Frames)
(cd "$out" && sha256sum Decky-Smooth-Frames.zip > Decky-Smooth-Frames.zip.sha256)
rm -rf "$stage"
echo "release: $out/Decky-Smooth-Frames.zip"
