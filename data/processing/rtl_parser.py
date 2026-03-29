"""
RTL parser and extractor - RISC-V focused
Supports Verilog and SystemVerilog
"""
import re
from pathlib import Path
from typing import List, Dict
from dataclasses import dataclass
import hashlib
import logging

logger = logging.getLogger(__name__)

@dataclass
class RTLModule:
    """Parsed RTL module"""
    name: str
    language: str  # "verilog" or "systemverilog"
    ports: List[Dict[str, str]]  # [{"name": "clk", "direction": "input", "width": "1"}]
    code: str
    file_path: str
    hash: str  # Unique identifier
    lines: int

class RTLParser:
    """Extracts modules from RTL files - RISC-V focused"""
    
    def __init__(self):
        self.verilog_module_re = re.compile(
            r'module\s+(\w+)(?:\s*\((.*?)\))?',
            re.DOTALL | re.MULTILINE
        )
        self.systemverilog_module_re = re.compile(
            r'module\s+(?:automatic\s+)?(\w+)(?:\s*\((.*?)\))?',
            re.DOTALL | re.MULTILINE
        )
    
    def parse_file(self, file_path: Path) -> List[RTLModule]:
        """Parse single RTL file"""
        modules = []
        
        try:
            content = file_path.read_text()
            language = "systemverilog" if "systemverilog" in content.lower() else "verilog"
            
            # Extract modules
            module_re = self.systemverilog_module_re if language == "systemverilog" else self.verilog_module_re
            matches = module_re.findall(content)
            
            for i, (module_name, port_list) in enumerate(matches):
                # Extract full module body (first 500 chars for hash)
                module_start = content.find(f"module {module_name}")
                module_end = content.find("endmodule", module_start)
                if module_end == -1:
                    module_end = len(content)
                
                module_code = content[module_start:module_end + 9]
                
                module_hash = hashlib.md5(module_code.encode()).hexdigest()[:16]
                
                module = RTLModule(
                    name=module_name,
                    language=language,
                    ports=self._parse_ports(port_list),
                    code=module_code.strip(),
                    file_path=str(file_path),
                    hash=module_hash,
                    lines=len([line for line in module_code.splitlines() if line.strip()])
                )
                modules.append(module)
            
        except Exception as e:
            logger.error(f"Failed to parse {file_path}: {e}")
        
        return modules
    
    def _parse_ports(self, port_list: str) -> List[Dict[str, str]]:
        """Parse port declarations (simplified)"""
        ports = []
        if not port_list:
            return ports
        
        # Basic port parsing
        port_re = re.compile(r'\b(\w+)\s*(?:\[(\d+):(\d+)\])?\s*(input|output|inout)?')
        for match in port_re.finditer(port_list):
            ports.append({
                "name": match.group(1),
                "width": f"{match.group(2)}:{match.group(3)}" if match.group(2) else "1",
                "direction": match.group(4) or "unknown"
            })
        
        return ports
    
    def parse_directory(self, directory: Path, extensions: List[str] = None) -> List[RTLModule]:
        """Enhanced RISC-V directory parsing"""
        if extensions is None:
            extensions = [".v", ".sv", ".vh", ".svh"]
    
        modules = []
        rtl_files = []
    
        # Enhanced RISC-V patterns (handle extracted folder structure)
        patterns = [
            "**/*.v", "**/*.sv",  # All Verilog files first
            "rtl/*.v", "rtl/*.sv",
            "src/*.v", "src/*.sv",
            "core/*.v", "core/*.sv",
            "ibex/*.v", "picorv32/*.v", "vexriscv/*.v",
            "src/main/scala/*/*.v",  # VexRiscv generates Verilog
            "verilog/rtl/*.v", "vendor/*.v"
        ]
    
        print(f"🔍 Searching in {directory}")
    
        # Handle common extraction folder patterns
        for subdir in ["ibex-main", "picorv32-master", "VexRiscv-master"]:
            subpath = directory / subdir
            if subpath.exists():
                directory = subpath
                print(f"  📁 Using extracted folder: {subdir}")
                break
    
        for pattern in patterns:
            matches = list(directory.rglob(pattern))
            rtl_files.extend(matches)
    
        rtl_files = list(set([f for f in rtl_files if f.suffix.lower() in extensions]))
        print(f"📄 Found {len(rtl_files)} RTL files")
    
        # Parse files (limit 50 for speed)
        for i, file_path in enumerate(rtl_files[:50]):
            try:
                file_modules = self.parse_file(file_path)
                modules.extend(file_modules)
                if i % 10 == 0:
                    print(f"  Parsed {i+1}/{min(50, len(rtl_files))} files...")
            except Exception as e:
                continue
    
        return modules

    
    def filter_riscv_modules(self, modules: List[RTLModule]) -> List[RTLModule]:
        """Filter for RISC-V relevant modules"""
        riscv_keywords = {
            "rv32", "rv64", "riscv", "ibex", "picorv", "vexriscv", 
            "boom", "plic", "clint", "csr", "pc", "insn", "pcpi"
        }
        
        riscv_modules = []
        for module in modules:
            module_lower = module.name.lower() + module.code.lower()
            if any(keyword in module_lower for keyword in riscv_keywords):
                riscv_modules.append(module)
        
        return riscv_modules
    
    def filter_high_quality_riscv(self, modules: List[RTLModule]) -> List[RTLModule]:
        """Filter for production-quality RISC-V modules"""
        quality_modules = []
    
        riscv_cores = {
            "ibex", "picorv32", "vexriscv", "scr1", "darkriscv",
            "rv32i", "rv_core", "csr", "pcpi", "alu", "decode"
        }
    
        for module in modules:
            # Quality criteria
            lines = module.lines
            ports = len(module.ports)
        
            # Core modules (relaxed criteria)
            is_core_module = any(core in module.name.lower() for core in riscv_cores)
        
            if is_core_module:
                if lines > 20 and ports > 3:  # Basic sanity
                    quality_modules.append(module)
            else:
                # Peripherals need more scrutiny
                if 50 <= lines <= 2000 and 5 <= ports <= 50:
                    quality_modules.append(module)
    
        return quality_modules
