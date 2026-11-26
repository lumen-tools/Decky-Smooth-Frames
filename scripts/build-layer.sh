#!/bin/sh
# Builds the bundled afmf-linux layer (v1.3.1 + the Smooth Frames patch: base frame rate limiter, live control) into bin/.
# Needs: git, cmake >= 3.28, ninja, gcc, glslang, Vulkan headers >= 1.3.250.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
git clone --quiet --branch v1.3.1 --depth 1 https://github.com/serialexperimentslainnnn/afmf-linux.git "$work/afmf-linux"
cd "$work/afmf-linux"
git apply "$here/patches/afmf-linux-1.3.1-smooth-frames.patch"
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
mkdir -p "$here/bin"
cp build/layer/libafmf-linux.so "$here/bin/"
cp LICENSE "$here/bin/afmf-linux.LICENSE"
cp shaders/fidelityfx/NOTICE.md "$here/bin/NOTICE.fidelityfx.md"
cp shaders/fidelityfx/LICENSE.txt "$here/bin/LICENSE.fidelityfx.txt"
echo "built: $here/bin/libafmf-linux.so"
