# Changelog

All notable changes are listed here. The project follows [Semantic Versioning](https://semver.org):
MAJOR for incompatible changes to data layout or tools, MINOR for new functions, PATCH for fixes.
Each release is a git tag `vX.Y.Z`; the deployed version and commit are recorded in every result.

## [0.10.0] - 2026-11-25

First run on a real clinical dataset (anonymized adaptive MR-Linac case, German structure names) and a
30-question stress test of the agent.

### Added
- `cli/` folder for all command-line tools (`report`, `organize`, `anonymize`, `to_mha`, `structure_names`,
  `structures`)
- MHA export: choose the reference image per plan by modality (`--ref-modality CT|MR`, agent `reference_modality`)
- Structure tools: `structure_set` filter (`Fx30`, `baseline`, RTSTRUCT id) and `plan_dose`; every result line names
  its structure set and plan
- `list_series`: source folder (plan / fraction), folder summary, RT references (RTSTRUCT → image,
  RTPLAN / RTDOSE → plan and structure set, with folder fallback for broken UID references)
- Write tools: `overwrite=true` replaces an earlier result (approved) instead of failing
- Console test mode (`--test`, menu 8) for anonymized or synthetic data while online, logged
- TG-263: about 60 German synonyms and 14 structures, organ-minus-target (`Lunge-GTV` → `Lungs-GTV`), PRV from
  `Myelon+5mm`, primary / nodal target classifiers, German `H_` helper prefix
- Harness guard: answers with measured values or series ids not looked up in the turn are sent back once
- `CHANGELOG.md`, version in the console header

### Changed
- An RTSTRUCT keeps the image series it names; a reference given for all is only a fallback
- Prompts: structure tools work on the existing export; per-fraction results are never added up
- README rewritten with architecture, data protection and workflow diagrams

### Fixed
- **Session space stayed open** when the console was closed while the model was running (bin scripts had lost
  their executable bit; ending the session now cannot be blocked by stopping the model)
- MHA export failed for RTSTRUCTs with Latin-1 ROI names (German ß)
- Anonymizer invented values for empty attributes (fake clinic name and address)
- Model-copied `[tools used …]` lines listed tools that never ran
- Export identifier check reported success when it had nothing to compare (warns and asks now)
- Offline log entries written before the clock was set are marked

## [0.9.0] - 2026-11-24

First version: agent harness, DICOM toolset (scan, organize, anonymize, MHA / RTSTRUCT export, TG-263 names,
structure and dose tools), sandboxed Python with skills, offline console with per-session encrypted space,
export identifier check, web UI, regression tests, deploy script.
