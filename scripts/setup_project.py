"""
Initial project setup script
"""
import sys
import os
from pathlib import Path

def create_init_files():
    """Create __init__.py in all package directories"""
    packages = [
        "api", "api/routes", "api/middleware",
        "core",
        "agents", "agents/rtl_generation", "agents/verification",
        "agents/cosimulation", "agents/ppa_optimization",
        "models",
        "database", "database/schemas",
        "eda_tools", "eda_tools/verilator", "eda_tools/yosys", "eda_tools/openroad",
        "tests", "tests/test_agents", "tests/test_api", "tests/test_eda_tools"
    ]
    
    for package in packages:
        init_file = Path(package) / "__init__.py"
        if not init_file.exists():
            init_file.touch()
            print(f"✓ Created {init_file}")

def create_env_file():
    """Copy .env.example to .env if not exists"""
    env_example = Path(".env.example")
    env_file = Path(".env")
    
    if not env_file.exists() and env_example.exists():
        env_file.write_text(env_example.read_text())
        print("✓ Created .env from .env.example")

def create_gitignore():
    """Create .gitignore file"""
    gitignore_content = """
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
venv/
ENV/
build/
dist/
*.egg-info/

# IDEs
.vscode/
.idea/
*.swp
*.swo

# Environment
.env
.env.local

# Logs
logs/
*.log

# Database
*.db
*.sqlite

# EDA outputs
*.vcd
*.fst
*.gtkw
*.gds
*.lef
*.def

# Models
models/checkpoints/
*.pth
*.onnx

# OS
.DS_Store
Thumbs.db
"""
    
    gitignore_file = Path(".gitignore")
    if not gitignore_file.exists():
        gitignore_file.write_text(gitignore_content)
        print("✓ Created .gitignore")

def main():
    print("🚀 Setting up AI Chip Design Platform...\n")
    
    create_init_files()
    create_env_file()
    create_gitignore()
    
    # Create logs directory
    Path("logs").mkdir(exist_ok=True)
    print("✓ Created logs/ directory")
    
    print("\n✅ Project setup complete!")
    print("\nNext steps:")
    print("1. Edit .env file with your configuration")
    print("2. Run: docker-compose up -d")
    print("3. Run: python scripts/setup_db.py")
    print("4. Start development!")

if __name__ == "__main__":
    main()
