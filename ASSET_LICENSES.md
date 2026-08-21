# Repository Asset Licensing and Provenance

Unless a file contains an adjacent notice stating otherwise, the following
repository-maintained asset groups were created for QwenPaw-Data by the
QwenPaw-Data Authors and are distributed as part of the Work under the Apache
License 2.0:

| Asset group | Paths | Intended use |
| --- | --- | --- |
| Project identity and diagrams | `assets/*.png` | README identity, architecture, and walkthrough illustrations |
| Frontend identity | `packages/qwenpaw-data-context/frontend/public/*.png` | DataBridge favicon and wordmark |
| Technical report | `docs/Technical_Report.pdf` | Project architecture and design documentation |
| Public examples | `examples/demo_kg_doc.docx`, `examples/demo_semantic_config.xlsx` | Synthetic documentation and semantic-configuration examples |
| Data-analysis Skill Pack | `packages/qwenpaw-data-skills/skills/**`, `skills/qwenpaw-data-cli/**` | Skill specifications, references, scripts, prompts, and HTML templates |

The examples are intended to contain synthetic data only. A release must not
include internal exports, customer data, credentials, employee identifiers,
or third-party assets without a corresponding entry in this inventory and an
approved redistribution basis.

Dependency-provided assets are not relicensed by this document. Their package
names, versions, integrity hashes, and declared licenses are recorded by
`uv.lock`, the frontend `package-lock.json`, and the generated SBOM/license
inventory in CI.

Names, logos, and trademarks are not granted trademark rights by the Apache
License; see Section 6 of the License and `NOTICE`.
