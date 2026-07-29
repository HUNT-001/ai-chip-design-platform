#!/usr/bin/env bash
# ============================================================================
# AVA / VOE — Tier-A verification toolchain installer  (Ubuntu / WSL2 Ubuntu)
# Unblocks Phase 3: one autonomous engineer verifying real Ibex RTL via
# simulation + formal, driven by the frozen VSA kernel. Open-source, CPU-only.
#
# Windows users: run this INSIDE WSL2 Ubuntu.  If you don't have WSL yet, in
# PowerShell (admin):   wsl --install -d Ubuntu    then reboot and open Ubuntu.
#
# Run:   bash install_toolchain.sh      (review each block; it's intentionally
#        explicit rather than fully automatic so you see what lands where.)
# ============================================================================
set -e
TOOLS="$HOME/tools"; mkdir -p "$TOOLS"

echo "== 0. base packages =="
sudo apt-get update
sudo apt-get install -y build-essential git python3 python3-pip python3-venv \
    device-tree-compiler autoconf automake libtool curl wget gtkwave \
    libboost-all-dev flex bison

echo "== 1. Python verification libs (+ cocotb) =="
python3 -m pip install --upgrade pip
python3 -m pip install cocotb cocotb-bus pytest numpy scipy networkx pandas

echo "== 2. OSS CAD Suite  (Yosys + SymbiYosys/sby + Verilator + Icarus + Z3 + Boolector + Yices, one bundle) =="
# Grab the LATEST linux-x64 tarball from the releases page:
#   https://github.com/YosysHQ/oss-cad-suite-build/releases   (pick newest 'oss-cad-suite-linux-x64-YYYYMMDD.tgz')
# Then:
#   cd "$TOOLS" && tar -xzf ~/Downloads/oss-cad-suite-linux-x64-*.tgz
# Activate it in every shell that runs formal/sim (add to ~/.bashrc):
#   source "$TOOLS/oss-cad-suite/environment"
echo "   >> download the newest oss-cad-suite-linux-x64-*.tgz, extract into $TOOLS,"
echo "   >> then:  echo 'source $TOOLS/oss-cad-suite/environment' >> ~/.bashrc"
echo "   (This provides yosys, sby, verilator, iverilog, z3, boolector, yices, gtkwave."
echo "    Use this Verilator for a known-good match with the formal tools.)"

echo "== 3. RISC-V bare-metal GCC (compile test programs) =="
# Easiest on recent Ubuntu:
sudo apt-get install -y gcc-riscv64-unknown-elf || {
  echo "   apt package unavailable -> use xPack prebuilt instead:"
  echo "   https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases (extract, add bin/ to PATH)"
}

echo "== 4. Spike — RISC-V golden ISS (tandem reference for AGENT_C) =="
if [ ! -d "$TOOLS/riscv-isa-sim" ]; then
  git clone https://github.com/riscv-software-src/riscv-isa-sim.git "$TOOLS/riscv-isa-sim"
fi
cd "$TOOLS/riscv-isa-sim" && mkdir -p build && cd build
../configure --prefix="$TOOLS/spike"
make -j"$(nproc)" && make install
echo "   >> add to PATH:  echo 'export PATH=$TOOLS/spike/bin:\$PATH' >> ~/.bashrc"

echo "== 5. (already have) riscv-dv for stimulus lives in corpus/riscv-dv (pyflow mode) =="

echo ""
echo "== VERIFY (open a fresh shell after adding the PATH/source lines) =="
cat <<'EOF'
  verilator --version
  yosys --version
  sby --help | head -1
  z3 --version
  riscv64-unknown-elf-gcc --version | head -1
  spike --help 2>&1 | head -1
  python3 -c "import cocotb; print('cocotb', cocotb.__version__)"
EOF
echo ""
echo "When all seven print cleanly, Tier A is ready and one real engineer can run."
