"""
FIXED: Export RISC-V dataset to JSON
"""
import json
import sys
from pathlib import Path
sys.path.append(".")

from data.processing.rtl_parser import RTLParser

def main():
    print("🔧 Fixing dataset export...\n")
    
    # Re-run parsing
    parser = RTLParser()
    riscv_dir = Path("data/datasets/riscv")
    all_modules = []
    
    print("📂 Scanning RISC-V datasets...")
    for dataset_name in riscv_dir.iterdir():
        if dataset_name.is_dir():
            print(f"Parsing {dataset_name.name}...")
            modules = parser.parse_directory(dataset_name)
            riscv_modules = parser.filter_riscv_modules(modules)
            quality_modules = parser.filter_high_quality_riscv(riscv_modules)
            
            for module in riscv_modules:
                module_dict = module.__dict__.copy()
                all_modules.append(module_dict)
            
            print(f"  ✓ {len(riscv_modules)} modules ({len(quality_modules)} high quality)")
    
    # Create datasets
    Path("data").mkdir(exist_ok=True)
    
    # Full dataset (all 158 modules)
    full_dataset = {
        "metadata": {
            "total_modules": len(all_modules),
            "collected_at": "2026-02-06",
            "cores": len(list(riscv_dir.iterdir()))
        },
        "modules": all_modules
    }
    
    # Training dataset (quality filtered)
    training_modules = []
    for module in all_modules:
        lines = module['lines']
        ports = len(module['ports'])
        if lines > 20 and ports > 3:  # Basic quality filter
            training_modules.append(module)
    
    training_dataset = {
        "metadata": {
            "total_modules": len(training_modules),
            "quality_threshold": "lines>20, ports>3"
        },
        "modules": training_modules
    }
    
    # SAVE FILES
    with open("data/riscv_full_dataset.json", "w", encoding='utf-8') as f:
        json.dump(full_dataset, f, indent=2, ensure_ascii=False)
    
    with open("data/riscv_training_dataset.json", "w", encoding='utf-8') as f:
        json.dump(training_dataset, f, indent=2, ensure_ascii=False)
    
    print(f"\n✅ SAVED DATASETS:")
    print(f"   📁 riscv_full_dataset.json: {len(all_modules)} modules")
    print(f"   🎯 riscv_training_dataset.json: {len(training_modules)} modules")
    
    print("\n📊 QUICK STATS:")
    core_stats = {}
    for module in all_modules[:10]:  # Top 10
        core = Path(module['file_path']).parent.name
        core_stats[core] = core_stats.get(core, 0) + 1
    
    for core, count in core_stats.items():
        print(f"   {core}: {count} modules")

if __name__ == "__main__":
    main()
