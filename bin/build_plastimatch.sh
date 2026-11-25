#!/bin/bash
# Build ITK 5.3 + plastimatch on Jetson (aarch64). Log: ~/build_plastimatch.log
set -e
export PATH=/usr/local/cuda/bin:$PATH
J=4
echo "== [1/4] apt dependencies $(date)"
sudo -n apt-get install -y -q libdcmtk-dev libfftw3-dev libblas-dev liblapack-dev libsqlite3-dev \
     zlib1g-dev libpng-dev libtiff-dev libjpeg-dev git cmake build-essential >/dev/null
mkdir -p ~/src && cd ~/src

echo "== [2/4] ITK 5.3.0 $(date)"
[ -d ITK ] || git clone --depth 1 --branch v5.3.0 https://github.com/InsightSoftwareConsortium/ITK.git
mkdir -p ITK-build && cd ITK-build
cmake ../ITK -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF \
      -DBUILD_SHARED_LIBS=ON -DITK_BUILD_DEFAULT_MODULES=ON -DModule_ITKReview=ON \
      -DITK_USE_SYSTEM_DCMTK=OFF -DCMAKE_INSTALL_PREFIX=$HOME/opt/itk >/dev/null
cmake --build . -j$J 2>&1 | { grep -E "error:|Error " || true; } ; test ${PIPESTATUS[0]} -eq 0
cmake --install . >/dev/null
cd ~/src

echo "== [3/4] plastimatch $(date)"
[ -d plastimatch ] || git clone --depth 1 https://gitlab.com/plastimatch/plastimatch.git
mkdir -p plastimatch-build && cd plastimatch-build
cmake ../plastimatch -DCMAKE_BUILD_TYPE=Release -DITK_DIR=$HOME/opt/itk/lib/cmake/ITK-5.3 \
      -DPLM_CONFIG_ENABLE_CUDA=OFF -DPLM_CONFIG_ENABLE_OPENCL=OFF -DPLM_CONFIG_ENABLE_MATLAB=OFF \
      -DBUILD_TESTING=OFF -DCMAKE_INSTALL_PREFIX=$HOME/opt/plastimatch 2>&1 | grep -iE "error|not found|warning: ITK" | head -20 || true
cmake --build . -j$J 2>&1 | { grep -E "error:|Error " || true; } ; test ${PIPESTATUS[0]} -eq 0
cmake --install . >/dev/null

echo "== [4/4] check $(date)"
export LD_LIBRARY_PATH=$HOME/opt/itk/lib:$HOME/opt/plastimatch/lib:$LD_LIBRARY_PATH
$HOME/opt/plastimatch/bin/plastimatch --version
echo "== PLASTIMATCH BUILD DONE"
