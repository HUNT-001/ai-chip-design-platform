// backends/sim/elf_loader.h — Minimal ELF32 loader (no libelf needed).
// Loads PT_LOAD segments into a flat RAM byte array.
#pragma once
#include <cstdint>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

struct Elf32_Ehdr {
    uint8_t  e_ident[16];
    uint16_t e_type, e_machine;
    uint32_t e_version, e_entry, e_phoff, e_shoff, e_flags;
    uint16_t e_ehsize, e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx;
};
struct Elf32_Phdr {
    uint32_t p_type, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_flags, p_align;
};
static constexpr uint32_t PT_LOAD = 1;

struct LoadedElf { uint32_t entry, load_base, load_top; };

inline LoadedElf elf_load(const std::string& path,
                           uint8_t* mem, size_t mem_size, uint32_t mem_base)
{
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("Cannot open ELF: " + path);
    f.seekg(0, std::ios::end); size_t fsize = f.tellg(); f.seekg(0);
    std::vector<uint8_t> buf(fsize);
    f.read(reinterpret_cast<char*>(buf.data()), fsize);

    auto* eh = reinterpret_cast<Elf32_Ehdr*>(buf.data());
    if (fsize < sizeof(Elf32_Ehdr) ||
        eh->e_ident[0] != 0x7F || eh->e_ident[1] != 'E' ||
        eh->e_ident[2] != 'L'  || eh->e_ident[3] != 'F')
        throw std::runtime_error("Not an ELF file: " + path);
    if (eh->e_ident[4] != 1) throw std::runtime_error("Not ELF32");
    if (eh->e_ident[5] != 1) throw std::runtime_error("Not little-endian ELF");

    uint32_t load_base = UINT32_MAX, load_top = 0;
    for (int i = 0; i < eh->e_phnum; i++) {
        size_t phoff = eh->e_phoff + (size_t)i * eh->e_phentsize;
        auto* ph = reinterpret_cast<Elf32_Phdr*>(buf.data() + phoff);
        if (ph->p_type != PT_LOAD || ph->p_memsz == 0) continue;
        uint32_t paddr = ph->p_paddr;
        load_base = std::min(load_base, paddr);
        load_top  = std::max(load_top, paddr + ph->p_memsz);
        if ((uint64_t)paddr + ph->p_memsz > (uint64_t)mem_base + mem_size)
            throw std::runtime_error("ELF segment exceeds RAM");
        size_t dst = paddr - mem_base;
        std::memset(mem + dst, 0, ph->p_memsz);
        if (ph->p_filesz)
            std::memcpy(mem + dst, buf.data() + ph->p_offset, ph->p_filesz);
    }
    if (load_base == UINT32_MAX) load_base = mem_base;
    return { eh->e_entry, load_base, load_top };
}
