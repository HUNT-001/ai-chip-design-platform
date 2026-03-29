"""
RISC-V focused data collection pipeline
"""
import sys
from pathlib import Path
sys.path.append(".")

from data.collection.dataset_downloader import RISCVDatasetDownloader
from data.processing.rtl_parser import RTLParser

def main():
    print("🚀 RISC-V Data Collection Pipeline\n")
    
    # 1. Download RISC-V datasets
    print("📥 Downloading RISC-V datasets...")
    downloader = RISCVDatasetDownloader()
    riscv_datasets = downloader.download_all()
    
    # 2. Parse RISC-V modules
    print("\n📖 Extracting RISC-V modules...")
    parser = RTLParser()
    all_riscv_modules = []
    
    for name, dataset_path in riscv_datasets.items():
        print(f"Parsing {name}...")
        modules = parser.parse_directory(dataset_path)
        riscv_modules = parser.filter_riscv_modules(modules)
        all_riscv_modules.extend(riscv_modules)
        print(f"  ✓ {len(riscv_modules)} RISC-V modules")
    
    # 3. Save RISC-V dataset
    riscv_output = Path("data/riscv_dataset.json")
    print(f"\n💾 Saving {len(all_riscv_modules)} RISC-V modules...")
    
    # TODO: Export to JSON
    
    print("\n🎉 RISC-V data collection complete!")
    print(f"📊 {len(all_riscv_modules)} RISC-V modules ready")
    print("\nNext: RISC-V validation + synthetic generation")

if __name__ == "__main__":
    main()
