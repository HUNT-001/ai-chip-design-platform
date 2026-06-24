# ── AVA Verification Platform ─────────────────────────────────────────────────
# Multi-stage build:
#   stage 1 (toolchain): installs Spike ISS + RISC-V GCC from source
#   stage 2 (runtime):   final lean image with Python + EDA tools
#
# Build:
#   docker build -t ava-platform .
#
# Run (interactive):
#   docker run --rm -it -v $(pwd)/runs:/workspace/runs ava-platform bash
#
# Run Agent G baseline test gen:
#   docker run --rm -v $(pwd)/runs:/workspace/runs ava-platform \
#       python -m rv32im_testgen.generate_tests --no-assemble --outdir /workspace/runs
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: EDA toolchain builder ───────────────────────────────────────────
FROM ubuntu:22.04 AS toolchain-builder

ARG RISCV_VERSION=2024.02.02
ARG VERILATOR_VERSION=v5.022
ARG SPIKE_COMMIT=master

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    autoconf automake autotools-dev curl python3 python3-pip libmpc-dev \
    libmpfr-dev libgmp-dev gawk build-essential bison flex texinfo gperf \
    libtool patchutils bc zlib1g-dev libexpat-dev ninja-build git cmake \
    libboost-regex-dev libboost-system-dev pkg-config help2man \
    device-tree-compiler libfdt-dev \
    && rm -rf /var/lib/apt/lists/*

# Build RISC-V GNU toolchain (RV32IMAC bare-metal)
RUN git clone --depth=1 --branch ${RISCV_VERSION} \
    https://github.com/riscv-collab/riscv-gnu-toolchain /tmp/riscv-gnu-toolchain \
    && cd /tmp/riscv-gnu-toolchain \
    && ./configure --prefix=/opt/riscv --with-arch=rv32im --with-abi=ilp32 \
    && make -j$(nproc) \
    && rm -rf /tmp/riscv-gnu-toolchain

# Build Spike ISS
RUN git clone --depth=1 https://github.com/riscv-software-src/riscv-isa-sim /tmp/spike \
    && cd /tmp/spike \
    && mkdir build && cd build \
    && ../configure --prefix=/opt/riscv \
    && make -j$(nproc) \
    && make install \
    && rm -rf /tmp/spike

# Build Verilator
RUN git clone --depth=1 --branch ${VERILATOR_VERSION} \
    https://github.com/verilator/verilator /tmp/verilator \
    && cd /tmp/verilator \
    && autoconf \
    && ./configure --prefix=/opt/verilator \
    && make -j$(nproc) \
    && make install \
    && rm -rf /tmp/verilator


# ── Stage 2: Python runtime ───────────────────────────────────────────────────
FROM ubuntu:22.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-dev python3-pip \
    make gcc g++ libfdt-dev libboost-regex-dev libboost-system-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy EDA toolchain from builder stage
COPY --from=toolchain-builder /opt/riscv      /opt/riscv
COPY --from=toolchain-builder /opt/verilator  /opt/verilator

ENV PATH="/opt/riscv/bin:/opt/verilator/bin:${PATH}"

# Set working directory
WORKDIR /workspace

# Install Python dependencies (core only; ML stack is optional)
COPY requirements.txt requirements-ml.txt ./
RUN python3.11 -m pip install --no-cache-dir --upgrade pip \
    && python3.11 -m pip install --no-cache-dir -r requirements.txt

# Copy project source
COPY . .

# Make AGENT_G importable as rv32im_testgen via the conftest path setup
ENV PYTHONPATH="/workspace:/workspace/AGENT_B:/workspace/AGENT_B/ava:/workspace/AGENT_B/backends:/workspace/AGENT_C:/workspace/AGENT_D:/workspace/AGENT_E:/workspace/AGENT_F:/workspace/AGENT_G"

# Default: run all tests
CMD ["python3.11", "-m", "pytest", "--tb=short", "-v"]
