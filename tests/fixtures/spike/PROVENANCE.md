# Provenance: tests/fixtures/spike/

Every `.log` here is Spike's output exactly as it was written, untrimmed. Nothing in this directory is hand-written except the program sources (`csr_trap.S`) and this note. Re-record a fixture with the commands below; never edit a log by hand.

Recorded 2026-11-15 on repo commit `23ca793`, under WSL (Ubuntu), from the repo root.

| tool | version | path |
|---|---|---|
| Spike | `Spike RISC-V ISA Simulator 1.1.1-dev` (first line of `spike --help`; this build has no `--version`) | `/usr/local/bin/spike` |
| gcc | `riscv64-unknown-elf-gcc (13.2.0-11ubuntu1+12) 13.2.0` | `/usr/bin/riscv64-unknown-elf-gcc` |
| binutils | `GNU assembler (2.42-1ubuntu1+6) 2.42` | |

Linker script for both: `AGENT_C/link.ld`, sha256 `d11d005ba0c9234b367542711dbf73eb0801b5c74b1eb23bda13ecdf230fcf35`.

Spike's arguments are in the same order `ava_patched.py` `SpikeISS._simulate_iss` uses: `spike --isa=<isa> --log-commits -l <elf>`. Spike writes the log to stderr, and stdout was empty (0 bytes) for both runs. Spike exited 0 for both.

**Reproducibility.** Rebuilding an ELF does not give the same file hash. gcc assembles through a temporary object, and its random name (`ccXXXXXX.o`) lands in a `FILE` symbol. The loadable image (`objcopy -O binary`) is identical across rebuilds, and so is the Spike log: both were checked byte for byte on a rebuild. The image sha256 is therefore the reproducible check, and the ELF sha256 identifies the committed file.

## rv32im_smoke.log

Source: `AGENT_C/rv32im_smoke.S`, sha256 `96be48b6703a88e83388ab45ff20c2638300d9f310be5fd1bd998e7053b4dd76`.

```bash
riscv64-unknown-elf-gcc -march=rv32im -mabi=ilp32 -nostdlib -static -T AGENT_C/link.ld AGENT_C/rv32im_smoke.S -o tests/fixtures/spike/rv32im_smoke.elf
spike --isa=rv32im --log-commits -l tests/fixtures/spike/rv32im_smoke.elf 2> tests/fixtures/spike/rv32im_smoke.log
```

| file | sha256 | size |
|---|---|---|
| `rv32im_smoke.elf` | `d2af5b6087ed4ee44c8d60336c9d69653e607a85e9bd439ecf71e62dda592a4c` | 9000 bytes |
| loadable image of it | `78998b832541d7af21882b6701a29ba007335fe220543af13e9ec446d5427f59` | 4112 bytes |
| `rv32im_smoke.log` | `09086ab644d560cf52efb5326409465fc5dbd223811ea68274520bf7a9101899` | 5073 lines, 184720 bytes |

ELF entry `0x80000000`. The log has 5000 commit lines (`core   0: 3 0x...`), 73 disassembly lines, and nothing else.

- The first 5 commits are Spike's boot ROM at `0x00001000`-`0x00001010`, ahead of the entry.
- The last 4927 commits repeat `j .` at `0x8000010c`, Spike stepping until it polls `tohost`.
- The log contains no CSR writes.

The same log hash was recorded on 2026-11-14 from an independent build.

## csr_trap.log

Source: `tests/fixtures/spike/csr_trap.S`, sha256 `0b757c9d834fa7b97a0a3b1db4257b221a9df0e6bcb50ac2e16b5f793969feca`. It was written for this fixture, to make Spike print CSR writes, a store, and a trap. `-march` needs `_zicsr` for gcc 13 to accept CSR instructions. Spike runs with the orchestrator's default `--isa=rv32im`.

```bash
riscv64-unknown-elf-gcc -march=rv32im_zicsr -mabi=ilp32 -nostdlib -static -T AGENT_C/link.ld tests/fixtures/spike/csr_trap.S -o tests/fixtures/spike/csr_trap.elf
spike --isa=rv32im --log-commits -l tests/fixtures/spike/csr_trap.elf 2> tests/fixtures/spike/csr_trap.log
```

| file | sha256 | size |
|---|---|---|
| `csr_trap.elf` | `79aa625290cf994f160bcf5e67d438db056485a88c10ae8292cfec0e392fb99b` | 9056 bytes |
| loadable image of it | `17ceaf2a53530b9afb452632da4cb97320eb8fa0635a519a14c60a98e2726f0e` | 4112 bytes |
| `csr_trap.log` | `685b5379b0a2e21b57dbac65f5c80ddad6792bdfe3b796a38a3c2f5d3b584a9c` | 5055 lines, 183029 bytes |

ELF entry `0x80000000`. The log has 5020 commit lines, 33 disassembly lines, and 2 other lines:

- `core   0: exception trap_machine_ecall, epc 0x8000003c`. The trapping `ecall` has a disassembly line but no commit line.
- `core   0: >>>>  trap_handler`, the symbol Spike prints on entering the handler.

CSR writes appear as `c<number>_<name> <value>`, e.g. `c773_mtvec 0x80000070`. `mret` writes two on one line: `c768_mstatus 0x00000080 c784_mstatush 0x00000000`.

Stores appear as `mem <addr> <data>`, and loads as `mem <addr>`. Spike prints no access size.
