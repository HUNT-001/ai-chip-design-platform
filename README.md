# AI Chip Design Platform

A modular open-source platform for **AI-assisted chip design, verification automation, EDA workflow orchestration, and red-team evaluation**.

This project is designed as a foundation for building intelligent hardware-engineering infrastructure that combines:

- **multi-agent orchestration**
- **verification-aware execution**
- **simulation workflow management**
- **coverage-driven analysis**
- **EDA tool integration**
- **dataset-backed experimentation**
- **dashboard and API extensibility**
- **robust red-team testing of agentic verification systems**

---

## Overview

AI Chip Design Platform is an experimental but structured platform for exploring how AI systems can support digital design and verification workflows.

The repository is organized around a central verification and execution core, supported by specialized agents, tooling integrations, datasets, red-team evaluation modules, and API/dashboard layers for future expansion.

The broader goal is to evolve toward a serious open-source infrastructure layer for intelligent chip-design tooling.

---

## Core Focus Areas

This platform currently targets the intersection of:

- AI-assisted verification
- simulation orchestration
- coverage optimization
- agent-based workflow automation
- resilient execution for hardware tasks
- telemetry and verification-state tracking
- validation of LLM/agent behavior in chip-design settings

---

## Repository Structure

```text
ai-chip-design-platform/
├── agents/              # Specialized AI/automation agents
├── api/                 # API layer and service interfaces
├── core/                # Main execution, orchestration, and verification logic
├── dashboard/           # UI / dashboard components
├── data/                # Datasets, benchmarks, processing, and model inputs
├── eda_tools/           # Wrappers/integrations for external EDA tooling
├── models/              # Model assets / future ML model components
├── redteam/             # Red-team evaluation framework for robustness testing
├── scripts/             # Utility and helper scripts
├── tests/               # Project test suite
├── docker-compose.yaml  # Containerized service setup
├── requirements.txt     # Python dependencies
└── README.md            # Project documentation
```
The current structure already reflects a layered platform architecture with separation between orchestration, tooling, datasets, interfaces, and evaluation.

## Key Components
1. Agent Layer

The agents/ directory contains specialized task-oriented agents such as:

- auto triage
- bug triage
- coverage optimization
- simulation control
- test writing
- waveform analysis

These form the basis for an agentic hardware workflow where different modules can own specific verification and debug responsibilities.

2. Core Execution and Verification Layer

The core/ directory is the central engine of the platform and includes components for:

- AI verification agent logic
- engine orchestration
- coverage direction
- multi-agent coordination
- resilient execution
- simulation execution
- telemetry
- verification state management

This is the strongest architectural signal in the repo because it shows the platform is not just a model wrapper, but an execution-oriented system for real verification workflows.

3. Data and Benchmarking Layer

The data/ directory contains:

- benchmarks
- datasets
- collection pipelines
- processing modules
- RISC-V datasets and testbench data

4. EDA Tool Integration

The ```eda_tools```/ directory currently includes Verilator-related validation tooling, indicating the beginning of integration with real hardware-development toolchains.
That suggests the platform is intended not only for orchestration, but also for data-backed experimentation, evaluation, and future model training or fine-tuning workflows.

5. Red-Team Evaluation

The ```redteam```/ module is especially valuable because it distinguishes the project from many ordinary agent repos. It signals that the platform is also concerned with failure analysis, adversarial evaluation, coverage robustness, and trustworthiness of agentic verification behavior.

6. Testing

The ```tests```/ directory already includes red-team tests, which is a good sign that the repo is evolving with validation in mind rather than only demo code.

## Features

Current or emerging platform capabilities include:

- Modular agent architecture for chip-design support workflows
- Multi-agent verification management
- Simulation orchestration and execution
- Coverage-oriented analysis and optimization
- Verification-state tracking and telemetry
- Dataset-backed experimentation using RISC-V-oriented data
- Early EDA integration through tool wrappers
- Red-team evaluation for robustness and failure analysis
- API/dashboard-ready architecture for future productization

## Why This Project Matters

Modern chip-design and verification flows are powerful but fragmented, tool-heavy, and often manually orchestrated. AI Chip Design Platform is motivated by the idea that intelligent systems can improve these workflows by acting as:

- verification assistants
- debugging copilots
- workflow coordinators
- coverage-aware reasoning engines
-  experiment managers
- robustness-tested automation layers

The long-term vision is not merely to call models from scripts, but to build a reusable and extensible platform for AI-native hardware engineering.

## Getting Started
Prerequisites
Python 3.10+
Git
pip
Docker / Docker Compose (optional, for containerized workflows)
Verilator or other external EDA tools as needed for tool integration

## Roadmap

Planned improvements may include:

- Better modular architecture
- Expanded documentation
- More robust test coverage
- Example workflows and demos
- Improved developer setup
- AI-assisted chip design modules
- Verification and automation integrations
- Performance and usability improvements


## Contributing

Contributions are welcome

If you would like to contribute:

1. Fork the repository
2. Create a new branch
3. Make your changes
4. Add or update tests where appropriate
5. Submit a pull request

Please try to keep contributions focused, documented, and aligned with the project direction.

## Author

Tanush Pavan V
GitHub: HUNT-001

## Acknowledgments

This project is inspired by the growing intersection of:

- AI systems
- chip design workflows
- verification engineering
- open-source hardware tooling
- engineering automation

## Contact
For suggestions, collaboration, or feedback, feel free to open an issue or connect through GitHub.
