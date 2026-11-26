#!/bin/sh
# Builds the bundled afmf-linux layer (v1.3.1 + the Smooth Frames patch: base frame rate limiter, live control) into bin/.
# Needs: git, cmake >= 3.28, ninja, gcc, glslang, Vulkan headers >= 1.3.250.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
git clone --quiet --branch v1.3.1 --depth 1 https://github.com/serialexperimentslainnnn/afmf-linux.git "$work/afmf-linux"
# Pinned Vulkan headers, the version the layer manifest declares. Older distribution headers
# (Ubuntu 24.04 ships 1.3.275) lack VK_KHR_present_id2: the layer then compiles without carrying
# DXVK's present ids, presents every frame inline and generated frames lose their pacing.
git clone --quiet --branch v1.4.341 --depth 1 https://github.com/KhronosGroup/Vulkan-Headers.git "$work/vulkan-headers"
cd "$work/afmf-linux"
git apply "$here/patches/afmf-linux-1.3.1-smooth-frames.patch"
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DVulkan_INCLUDE_DIR="$work/vulkan-headers/include"
cmake --build build
mkdir -p "$here/bin"
cp build/layer/libafmf-linux.so "$here/bin/"
cp LICENSE "$here/bin/afmf-linux.LICENSE"
cp shaders/fidelityfx/NOTICE.md "$here/bin/NOTICE.fidelityfx.md"
cp shaders/fidelityfx/LICENSE.txt "$here/bin/LICENSE.fidelityfx.txt"
# guard: the present_id2 case (sType 1000479001 = 0x3ba21919) must be compiled in
objdump -d build/layer/libafmf-linux.so | grep -q 0x3ba21919 || { echo "layer built without VK_KHR_present_id2" >&2; exit 1; }
echo "built: $here/bin/libafmf-linux.so"
