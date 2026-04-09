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
├── AGENT_A/      # Schemas and interface specifications
├── AGENT_B/      # AVA package, RTL backends, docs, example CPU, tests
├── AGENT_C/      # ISS execution, Spike parsing, smoke tests, integration tests
├── AGENT_D/      # Commitlog comparison and bug hypothesis generation
├── AGENT_E/      # Compliance runner and RTL adapter
├── AGENT_F/      # Coverage pipeline, cold-path ranking, manifest locking
├── AGENT_G/      # Directed, random, and genetic test generation
├── ava_v2/       # Next-generation AVA-related work
├── ava.py        # Main AVA entry / legacy orchestration file
```
This structure reflects a more concrete verification-oriented system rather than a generic tooling skeleton.
## Architecture Summary

The project is currently organized around multiple specialized subsystems:

## AGENT_A — Schemas and Interfaces

Defines foundational formats and interface specifications, including:

- commitlog.schema.json
- run_manifest.schema.json
- interfaces.md

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
├── ava_coverage_patch.py
├── ava_patched.py
└── project documentation and reports
