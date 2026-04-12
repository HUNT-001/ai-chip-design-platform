# AI Chip Design Platform

A modular **multi-agent RISC-V verification and test-generation framework** for AI-assisted hardware validation workflows.

This project explores how agentic systems can support verification tasks such as RTL execution, ISS comparison, commitlog analysis, compliance testing, coverage-driven prioritization, and automated test generation.

---

## Overview

**AI Chip Design Platform** is an open-source experimental framework for building intelligent verification workflows around digital hardware systems, with a current focus on **RISC-V-oriented verification automation**.

The repository is organized into specialized agent modules that address different parts of the verification stack, including:

- interface and schema definitions
- RTL backend orchestration
- ISS execution and trace parsing
- commitlog comparison and bug hypothesis generation
- compliance execution
- coverage analysis and cold-path ranking
- directed, random, and genetic test generation

The long-term goal is to evolve this repository into a serious open-source platform for **agentic hardware verification and validation automation**.

---

## Repository Structure

```text
ai-chip-design-platform/
├── Schemas/      # Schemas and interface specifications
├── Backend/      # AVA package, RTL backends, docs, example CPU, tests
├── ISS_Spike_Checker/      # ISS execution, Spike parsing, smoke tests, integration tests
├── Comparator/      # Commitlog comparison and bug hypothesis generation
├── Rtl_runner/      # Compliance runner and RTL adapter
├── Coverage/      # Coverage pipeline, cold-path ranking, manifest locking
├── Test_generator/      # Directed, random, and genetic test generation
├── ava_v2/       # Next-generation AVA-related work
├── ava.py        # Main AVA entry / legacy orchestration file
├── ava_coverage_patch.py
├── ava_patched.py
├── .Github
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md
├── README.md
├── SECURITY.md
├── LICENSE
└── project documentation and reports
```
This structure reflects a more concrete verification-oriented system rather than a generic tooling skeleton.
## Architecture Summary

The project is currently organized around multiple specialized subsystems:

## AGENT_A — Schemas and Interfaces

Defines foundational formats and interface specifications, including:

- ```commitlog.schema.json```
- ```run_manifest.schema.json```
- ```interfaces.md```

This layer acts as the structural contract for the rest of the framework.

## AGENT_B — AVA + RTL Backend Layer

Contains the AVA package, backend execution logic, documentation, example RTL, and test assets. It includes:

- configuration and model definitions
- RTL backend support
- example CPU RTL
- interface docs
- linker/test files

This module forms the main execution-facing backend layer for RTL-oriented workflows.

## AGENT_C — ISS and Trace Analysis

Focused on ISS-backed validation and parser-assisted analysis. Includes:

- ISS execution flow
- Spike parser
- smoke assembly
- integration tests
- manifest/schema support

This layer strengthens the framework’s ability to compare expected and observed behavior at the instruction level.

## AGENT_D — Commitlog Comparison and Bug Hypothesis Generation

Includes:

- commitlog comparison
- comparator tests
- bug hypothesis logic

This module pushes the project toward diagnosis and debugging assistance, not just execution orchestration.

## AGENT_E — Compliance and RTL Adaptation

Supports:

- compliance execution
- RTL adaptation
- compliance test infrastructure

This helps position the framework closer to standards-oriented validation workflows.

## AGENT_F — Coverage Intelligence

Contains components for:

- coverage patching
- coverage database handling
- coverage pipeline execution
- manifest locking
- cold-path ranking

This suggests a coverage-aware verification workflow where under-exercised regions can drive prioritization or new stimulus generation.

## AGENT_G — Test Generation

Implements several stimulus-generation approaches, including:

- assembly building
- directed test generation
- random generation
- genetic engine support
- manifest-backed generation

This gives the project a strong automated-test-generation dimension.

## Current Capabilities

Based on the present repository structure, the framework supports or is being actively developed toward:

- Multi-agent verification workflows
- Schema-defined manifests and interfaces
- RTL backend execution
- ISS-backed validation flows
- Spike trace parsing
- Commitlog comparison
- Bug hypothesis generation
- Compliance testing
- Coverage-driven prioritization
- Directed test generation
- Randomized stimulus generation
- Genetic test generation

## Why This Project Matters

Verification workflows in hardware engineering are often fragmented, manually orchestrated, and difficult to scale. This project explores a different direction: using modular agents and structured execution layers to coordinate multiple verification activities in a more automated and extensible way.

Instead of treating verification as a collection of disconnected scripts, this project moves toward a unified framework spanning:

- execution
- comparison
- diagnosis
- compliance
- coverage
- test generation

That makes it much closer to a reusable verification framework than a one-off prototype.

## Getting Started

Prerequisites

Make sure you have the following installed:

- Python 3.10+
- Git
- pip

RISC-V toolchain components as needed
- Verilator or other RTL simulation tools where applicable
- Spike or another ISS tool if required by your local flow
## Clone the Repository
```
git clone https://github.com/HUNT-001/ai-chip-design-platform.git
cd ai-chip-design-platform
```
## Create a Virtual Environment

Windows
```bash
python -m venv venv
venv\Scripts\activate
```
Linux / macOS
```bash
python -m venv venv
source venv/bin/activate
```
Install Dependencies
```bash
pip install -r requirements.txt
```

## Suggested Workflow Areas

Depending on the subsystem you want to work with, this repository currently appears suited for workflows such as:

- running RTL-backed validation
- launching ISS-backed execution
- parsing and comparing traces or commit logs
- running compliance checks
- analyzing coverage gaps
- generating new tests using directed, random, or genetic approaches

As the project matures, this section should be expanded with exact commands and example pipelines for each agent module.

## Development Status

This project is currently in an active experimental stage.

That means:

- architecture may continue to evolve
- module names and boundaries may still change
- top-level usability and entry points may be refined
- documentation and run flows will improve over time

The current structure already provides a strong technical foundation, but it is still being actively shaped.

## Roadmap

Planned improvements include:

- semantic renaming of ```AGENT_*``` folders into clearer subsystem names
- unified top-level orchestration or CLI entry point
- exact setup and execution examples
- CI integration for unit and integration testing
- architecture diagrams
- benchmark examples and reference outputs
- stronger contributor-facing documentation

## Contributing

Contributions are welcome.

Recommended next project files for open-source maturity:

- ```CONTRIBUTING.md```
- ```CODE_OF_CONDUCT.md```
- ```SECURITY.md```
- issue templates
- pull request template

These will make the project easier for others to understand and contribute to.

## License

This project is licensed under the terms of the Apache License 2.0

## Author

Tanush Pavan V
GitHub: HUNT-001

## Vision

AI Chip Design Platform aims to grow into a serious open-source foundation for:

agentic verification + RISC-V validation + coverage-aware automation + intelligent test generation
