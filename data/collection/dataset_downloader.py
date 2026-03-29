"""
RISC-V specific dataset downloader - FIXED URLs
"""
import requests
import zipfile
from pathlib import Path
from typing import Dict

class RISCVDatasetDownloader:
    """Downloads RISC-V specific datasets with verified URLs"""
    
    # ✅ Working URLs only
    RISCV_DATASETS = {
        "ibex": {
            "url": "https://github.com/lowRISC/ibex/archive/refs/heads/main.zip",
            "description": "LowRISC Ibex (RV32IMCB)"
        },
        "picorv32": {
            "url": "https://github.com/YosysHQ/picorv32/archive/refs/heads/master.zip",
            "description": "PicoRV32 (RV32I/M)"
        },
        "vexriscv": {
            "url": "https://github.com/SpinalHDL/VexRiscv/archive/refs/heads/master.zip",
            "description": "VexRiscv (RV32/64IM)"
        },
        "scr1": {
            "url": "https://github.com/syntacore/scr1/archive/refs/heads/master.zip",
            "description": "SCR1 RISC-V MCU core"
        },
        "darkriscv": {
            "url": "https://github.com/darklife/darkriscv/archive/refs/heads/master.zip",
            "description": "DarkRISC-V CPU core"
        },
        "rv32i_core": {
            "url": "https://github.com/AngeloJacobo/RISC-V/archive/refs/heads/master.zip",
            "description": "RV32I core with Zicsr"
        },
        "riscv_tests": {
            "url": "https://github.com/riscv-software-src/riscv-tests/archive/refs/heads/master.zip",
            "description": "RISC-V compliance tests"
        }
    }
    
    def __init__(self, data_dir: Path = Path("data/datasets/riscv")):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
    
    def download_all(self) -> Dict[str, Path]:
        """Download all RISC-V datasets"""
        datasets = {}
        for name, info in self.RISCV_DATASETS.items():
            try:
                datasets[name] = self._download_single(name, info["url"])
                print(f"✅ {name} ready")
            except Exception as e:
                print(f"❌ Failed {name}: {e}")
                continue
        return datasets
    
    def _download_single(self, name: str, url: str) -> Path:
        """Download single dataset"""
        dataset_dir = self.data_dir / name
        dataset_dir.mkdir(exist_ok=True)
        
        if not any(dataset_dir.iterdir()):
            print(f"📥 Downloading {name}...")
            
            zip_path = dataset_dir / f"{name}.zip"
            response = requests.get(url, stream=True, timeout=30)
            response.raise_for_status()
            
            with open(zip_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(dataset_dir)
            
            zip_path.unlink()
        
        return dataset_dir


if __name__ == "__main__":
    downloader = RISCVDatasetDownloader()
    datasets = downloader.download_all()
    print(f"\n🎉 Downloaded {len(datasets)} RISC-V datasets!")
