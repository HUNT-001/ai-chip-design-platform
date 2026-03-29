"""
Master data collection pipeline
"""
import sys
from pathlib import Path
sys.path.append(".")

from data.collection.dataset_downloader import DatasetDownloader
from data.processing.rtl_parser import RTLParser

def main():
    print("🔥 AI Chip Design Data Collection Pipeline\n")
    
    # Step 1: Download public datasets
    print("📥 Step 1: Downloading public datasets...")
    downloader = DatasetDownloader()
    datasets = downloader.get_all_datasets()
    
    # Step 2: Parse RTL modules
    print("\n📖 Step 2: Parsing RTL modules...")
    all_modules = []
    
    parser = RTLParser()
    for name, dataset_path in datasets.items():
        print(f"  Parsing {name}...")
        modules = parser.parse_directory(dataset_path)
        all_modules.extend(modules)
        print(f"    ✓ {len(modules)} modules extracted")
    
    # Save parsed modules
    output_path = Path("data/processed_rtl_modules.json")
    # TODO: Save modules to JSON
    
    print(f"\n🎉 Data collection complete!")
    print(f"📊 Total modules extracted: {len(all_modules)}")
    print(f"📁 Raw datasets: {len(datasets)}")
    print(f"\nNext steps:")
    print("1. Review data/processing/rtl_parser.py for quality")
    print("2. Run synthetic data generation")
    print("3. Build validation pipeline")
    
    # Print stats
    langs = {}
    avg_ports = {}
    for module in all_modules:
        langs[module.language] = langs.get(module.language, 0) + 1
        avg_ports[module.name.split('_')[0]] = avg_ports.get(module.name.split('_')[0], 0) + len(module.ports)
    
    print("\n📈 Dataset stats:")
    for lang, count in langs.items():
        print(f"  {lang}: {count} modules")

if __name__ == "__main__":
    main()
