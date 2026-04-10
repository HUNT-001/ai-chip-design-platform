# Contributing to AI Chip Design Platform

Thank you for your interest in contributing to **AI Chip Design Platform**.

This project is an evolving open-source framework focused on multi-agent RISC-V verification, coverage-aware validation, compliance workflows, commitlog analysis, and automated test generation.

We welcome contributions that improve the codebase, documentation, testing, architecture, and usability of the project.

---

## Ways to Contribute

You can contribute in several ways, including:

- fixing bugs
- improving documentation
- adding tests
- improving module-level README files
- refining interfaces and schemas
- improving verification workflows
- adding examples and demos
- suggesting architectural improvements
- reporting security or correctness concerns responsibly

---

## Before You Start

Please:

1. Read the top-level `README.md`
2. Check existing issues and pull requests
3. Open an issue first for large changes, architecture changes, or new subsystems
4. Keep contributions focused and well-scoped

---

## Development Workflow

1. Fork the repository
2. Create a new branch from `main`
3. Make your changes
4. Test your changes where applicable
5. Update documentation if needed
6. Submit a pull request

Example:

```bash
git checkout -b feature/improve-coverage-pipeline
```
## Branch Naming Suggestions

# Use clear branch names such as:

- feature/...
- fix/...
- docs/...
- test/...
- refactor/...

# Examples:

- feature/add-iss-parser-checks
- fix/commitlog-comparison-bug
- docs/update-agent-readmes
- Coding Expectations

# Please try to keep contributions:

- modular
- readable
- well-commented where necessary
- consistent with the existing project structure
- focused on one main purpose per pull request

Avoid mixing unrelated changes into a single PR.

## Documentation Expectations

If your contribution changes behavior, workflows, interfaces, manifests, or setup steps, please update the relevant documentation, such as:

- top-level README.md
- module-level README.md
- interface/schema documentation
- example usage sections
- Testing Expectations

Where relevant, contributors should:

1. add or update tests
2. avoid breaking existing tests
3. verify that the modified workflow still behaves as expected

If a change cannot easily be tested yet, explain that clearly in the pull request.

## Commit Message Guidelines

Use clear commit messages.

Examples:

- Add README for coverage module
- Fix manifest parsing in ISS workflow
- Improve commitlog comparison diagnostics
- Pull Request Guidelines

## A good pull request should include:

1. a clear title
2. a short explanation of what changed
3. why the change is needed
4. any testing performed
5. any follow-up work still needed

Small, focused pull requests are preferred over very large ones.

## Areas Especially Welcome

Contributions are especially welcome in areas such as:

- documentation improvement
- module cleanup and semantic renaming
- test and CI improvements
- architecture clarity
- better examples and quickstart flows
- robustness improvements in verification workflows