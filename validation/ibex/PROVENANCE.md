# Provenance: validation/ibex/

Every `.log` in `fixtures/` is tool output exactly as it was written, untrimmed:

- `*.ibex.log` is Ibex's `trace_core_00000000.log`.
- `*.spike.log` is Spike's stderr.

Nothing here is hand-written except the programs, the scripts and this note. To re-record, use the commands below; never edit a log by hand. `tests/test_ibex_comparator_validation.py` checks every fixture against the sha256 values on this page.

Recorded 2026-11-22 under WSL (Ubuntu), from the repo root, at repo commit `a7fe0b0`. Criteria: #175.

## Tools

| tool | version | path |
|---|---|---|
| Ibex | `lowRISC/ibex` commit `041098fe2380124dea536a0985cb85469fb206c4` (2026-11-20, master), cloned into `work/ibex` (gitignored) | |
| Verilator | `Verilator 5.020 2024-01-01 rev (Debian 5.020-1)` (Ibex requires >= 4.210) | `/usr/bin/verilator` |
| fusesoc / edalize | `2.4.3` / `0.6.8`, in a venv at `~/.cache/ava-ibex-venv` (outside the repo, because `tests/test_atomic_publish.py` scans every `.py` under the tree) | |
| gcc | `riscv64-unknown-elf-gcc (13.2.0-11ubuntu1+12) 13.2.0` | `/usr/bin/riscv64-unknown-elf-gcc` |
| binutils | `GNU assembler (2.42-1ubuntu1+6) 2.42` | |
| Spike | `Spike RISC-V ISA Simulator 1.1.1-dev` (first line of `spike --help`; this build has no `--version`) | `/usr/local/bin/spike` |

## Build and run

```bash
bash validation/ibex/build_ibex.sh     # clone at the pinned commit into work/ibex, fusesoc venv, Verilator build
bash validation/ibex/build_programs.sh # work/ibex_val/<name>.elf
bash validation/ibex/run_ibex.sh       # work/ibex_val/<name>.ibex.log
bash validation/ibex/run_spike.sh      # work/ibex_val/<name>.spike.log
```

**Ibex.** `build_ibex.sh` runs `fusesoc --cores-root=. run --target=sim --setup --build lowrisc:ibex:ibex_simple_system $(util/ibex_config.py small fusesoc_opts)`.

- The build took 7m09s wall time on `/mnt/e`.
- Ibex's pre-build check runs a bare `python3` and imports `edalize` and `packaging`, so the venv has to come first on `PATH`.
- Each program runs as `Vibex_simple_system --term-after-cycles=100000 --meminit=ram,<elf>` in its own directory.
- Every run printed `Terminating simulation by software request.` and exited 0.

**Programs.** Each is built with `riscv64-unknown-elf-gcc -march=rv32im_zicsr -mabi=ilp32 -nostdlib -static -T validation/ibex/programs/link.ld`.

- There are no compressed instructions (`.option norvc`).
- The entry is `0x00100080`.
- Every program halts by writing 1 to `0x00020008`: SimCtrl on Ibex, `tohost` on Spike.

**Spike.** `spike --isa=rv32imc --priv=m -m0x20000:0x1000,0x100000:0x100000 --pc=0x100080 --log-commits <elf>`.

- These are the flags #175 fixed.
- Upstream's own `examples/simple_system/spike-simple-system.sh` uses `--isa=rv32imc --log-commits -l -m0x10000:0x30000,0x100000:0x100000`, with no `--pc` and no `--priv`. The `--pc` here equals the ELF entry. None of these programs touches the timer at `0x30000`.
- Spike exited 0 every time, and its stdout was empty every time.
- With `--pc`, the log has **no** boot-ROM commits; it starts at `0x00100080`.
- Each log is exactly 5000 commit lines. Most of them repeat the terminal `j .`, which Spike keeps retiring until it polls `tohost`.

| program | Ibex cycles | Ibex instructions retired (core's own counter) | trace records |
|---|---|---|---|
| arith_branch | 87 | 58 | 58 |
| muldiv | 270 | 25 | 25 |
| loadstore | 55 | 29 | 29 |
| csr | 25 | 16 | 16 |

Each Ibex trace ends at the halt store. SimCtrl calls `$finish` on it, so the `j .` after it never retires. That is #178.

**Reproducibility.** I re-recorded everything the same day from a second, independent build of the simulator, with the venv deleted and recreated, which took 4m31s. Every `.ibex.log` and `.spike.log` came out byte-identical to the fixtures, and so did every loadable image.

Rebuilding an ELF does not give the same file hash, because gcc's temporary object name lands in a `FILE` symbol. The loadable image (`objcopy -O binary`) is the reproducible check; the ELF sha256 identifies the committed file.

## Sources

| file | sha256 |
|---|---|
| programs/arith_branch.S | `2a2cb8bd83b2b1da887c971e4c20dedc6ef86c547f5ed590af2952da86fa681a` |
| programs/muldiv.S | `5c81378d0fe7519bccfe47d2721369ac55c51878baf87eadc2de1c43d5133540` |
| programs/loadstore.S | `5c2688b4df620fd7182b8b56b578bc36fdd6a3e240dd6f8a64c6ecc58df13748` |
| programs/csr.S | `53517de1bbbab44c55623e8044eea0fcd5bc997a9bb787188849268475366be4` |
| programs/common.inc | `110a55e61492dde248fb6982f7d7233f72ec4344e0400a35ff0c5c6885d6d700` |
| programs/link.ld | `670ca18d72b29f879dfefb5c51b19031650e0fab6637107d5321063669cf8518` |

## Fixtures

| file | sha256 | size |
|---|---|---|
| `arith_branch.elf` | `45ed2c9450bf05ba01c550ab1e2e8c565fb6bc0d32eab663b69da275893fe18e` | 5112 bytes |
| `muldiv.elf` | `e36b02b91f1589b59ca49c299cdb53ee2b37d14e53646094f8b34c7656a7a9b4` | 4952 bytes |
| `loadstore.elf` | `5b149677091f60e364425087ad1a5b9173c30c95ba0e56d99833695689421c80` | 8912 bytes |
| `csr.elf` | `e7657ab29f8244aa79fcd807f26c26e3b76492353c41c44217e56e8faf5af25f` | 4920 bytes |
| `arith_branch.ibex.log` | `eb1bcbedf58b597d858614c95e0a201c36760e69fd111ba6c41245571483726e` | 59 lines, 5589 bytes |
| `muldiv.ibex.log` | `882a024864fe7bf99bbaa9a078b4183d716bd0e9fd2da11974e59c6db5190024` | 26 lines, 2583 bytes |
| `loadstore.ibex.log` | `cdf40b0b10a3502b1245e497c913efb229ce4c96701b63021340d186c0dc3933` | 30 lines, 3159 bytes |
| `csr.ibex.log` | `0b658fc38f689e0ab217695ce49fdbc30d6bc8f1c5a2acbc617084f964e30fce` | 17 lines, 1517 bytes |
| `arith_branch.spike.log` | `ab4a7a850b7615f9fb24db534df7752472eec0fe085adf40ceeb08aad32c8917` | 5000 lines, 180686 bytes |
| `muldiv.spike.log` | `5488181f7696dd8d714aec6ce94d16c822ee73f1f88a68448d22e96d8aae87c2` | 5000 lines, 180386 bytes |
| `loadstore.spike.log` | `b75385f742fa8a77861dde2afe29ce87dde15b8c6d68077dc44444a3c1b5a728` | 5000 lines, 180633 bytes |
| `csr.spike.log` | `b9c76094c0985bccf52ffd2ba5f1ff659563e17222471b25c70616cccf266566` | 5000 lines, 180386 bytes |

The `.ibex.log` line counts include the tracer's header line.

| loadable image | sha256 | size |
|---|---|---|
| image of `arith_branch.elf` | `bb5e168a6bae9e80feae97cb1029b9db7dfbb77448c5e4a92b66b618e26078a9` | 332 bytes |
| image of `muldiv.elf` | `b60bc6a340aded13665661b3479271e5e29ccae42d8c11ed9a0004c0c5031867` | 232 bytes |
| image of `loadstore.elf` | `0ee55aca7bc12fce0480ec631d8a2f91922be8ccb2a772e95ed198cda3d54139` | 65552 bytes (`.text` and `.data` at `0x110000`, zero-filled between) |
| image of `csr.elf` | `bde4039095668eb0205fe1cdd999c8f76067a381b077fc6800a8e92443e77a1e` | 196 bytes |

## Trace-format notes found on the real logs

- **`auipc` capability form.** `auipc` is printed in the CHERIoT capability form, `x8=0x00110080+0x000000000`, even with CHERIoT off. `decode_cheriot_auipcc_insn` sets `CD` unconditionally. The converter reads the integer half and refuses a non-zero capability half.
- **Store data.** The tracer prints store data by `rvfi_mem_wmask`, and Ibex sets that mask by access type, not shifted by the byte offset: `sb` prints `0x??????NN` and `sh` prints `0x????NNNN`. `PA:` is the unaligned byte address. The converter takes the size from funct3 and keeps only those bytes.
