# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0]  -  2026-10-20

### Added

- **Real CI test gate** in both workflows. The Python workflow ran
  `pytest -q || echo "No tests yet"`; the CD pipeline always took an
  import-only fallback and reported success without running the suite.
  Both now execute the tests unconditionally  -  the pipeline job is named
  `pipeline-test` so it is distinguishable from the primary `test` job.
  The suite has 426 tests.
- **Dependency pin guard** (`scripts/pin_guard.py`), wired as a required
  `pin-check` status check.
- **`staging` and `production` deployment environments**, which the
  pipeline referenced but which did not exist.

### Fixed

- **Path traversal in `/local-media`.** A crafted capture [lorem] could read
  arbitrary files from the host, returning HTTP 200 with the file contents.
  The resolver now flattens separators, resolves the candidate path,
  enforces containment beneath the capture root, and requires an existing
  regular file. Five traversal variants are covered by regression tests.
- **SSRF in outbound fetches.** The URL guard now rejects [lorem]-HTTP(S)
  schemes  -  `file://`, `gopher://`, `ftp://`, `data:` and `javascript:`  - 
  as well as relative paths and malformed input.
- **The bind address is configurable** through `GHOST_HOST`, and the
  container-only path defaults were replaced with portable ones.
- **Launcher scripts are location-independent**, so the project runs from
  a fresh clone.
- The log directory is created before the file handler is opened.

### Changed

- The Ruff rule set is pinned to `E4,E7,E9,F` with `ruff==0.17.1`.
- Bandit reports zero findings at the medium and high severity levels.
- **README author credit** added: Conrad CJ Wilson.

### Security

- Secret scanning and push protection enabled at the repository level.

## [0.1.0]

- Initial release: encrypted scraping stack with dashboard, auth and metrics
- Security hardening pass: key rotation, prometheus metrics, CI/CD

[1.0.0]: https://github.com/cjps4linux-creator/ghost-data-suite/releases/tag/v1.0.0
