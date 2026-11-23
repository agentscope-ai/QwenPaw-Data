# Release process

Releases must be produced from a reviewed, clean commit on the default branch.

1. Update `CHANGELOG.md`, package versions, and the compatibility table.
2. Run `scripts/verify.sh` and the frontend lint/build commands.
3. Run `scripts/check_release_versions.py vX.Y.Z` and `python scripts/build_console.py`, then build all Python
   distributions with `uv build --all-packages --out-dir dist`.
4. Run the dependency, license, secret, CodeQL, and SBOM CI gates.
5. Run database and Docker integration jobs on CI.
6. Inspect the generated wheels/sdists and verify the CLI from installed wheels
   in a fresh environment outside the checkout (no editable installs or source
   `PYTHONPATH`). Verify both source-to-wheel and sdist-to-wheel builds.
7. Tag the commit and attach checksums plus SBOM artifacts to the release.

Publishing a GitHub release triggers `.github/workflows/publish.yml`. Its build
job verifies aligned package/tag versions, builds and checks all seven package
wheel/sdist pairs, and uploads each package as a separate artifact. The publish
job then uploads the packages independently, each using PyPI Trusted Publishing
with no stored API token and gated behind its own protected GitHub environment:

| PyPI project | GitHub environment |
| --- | --- |
| `qwenpaw-data-auth` | `pypi-auth` |
| `qwenpaw-data-console` | `pypi-console` |
| `qwenpaw-data-cli` | `pypi-cli` |
| `qwenpaw-data-context` | `pypi-context` |
| `qwenpaw-data-host-core` | `pypi-host-core` |
| `qwenpaw-data-skills` | `pypi-skills` |
| `qwenpaw-data-tracing` | `pypi-tracing` |

For each of the seven PyPI projects, configure the same owner/repository, the
workflow filename `publish.yml`, and that project's environment name above as a
Trusted Publisher before the first release. Manual workflow dispatch runs the
build checks by default and publishes only when its `publish` input is enabled
together with an existing `release_tag`.

Each package carries local `LICENSE` and `NOTICE` distribution copies so isolated
sdist builds never depend on the original monorepo parent directory. Keep them
byte-identical to the root documents when those change; the existing public
package hygiene check enforces this. Both wheel and sdist must retain the notices.

### Console-only releases

A `console-vX.Y.Z` tag selects only `qwenpaw-data-console`. The same workflow
validates that package's version, builds its three prebuilt UIs and wheel/sdist,
and publishes only through `pypi-console`. Other package versions and PyPI
releases remain unchanged. Use this path for the initial Console 0.3.3 release;
the existing coordinated `v0.3.3` tag must not be moved or reused.

Before tagging the reviewed default-branch commit, run:

```bash
python scripts/build_console.py
uv build --package qwenpaw-data-console --out-dir dist-console
uv run python scripts/check_release_versions.py console-v0.3.3 --dist-dir dist-console
uvx --from twine==7.0.0 twine check dist-console/*
```

The release tag determines the publication scope; manual dispatch accepts the
same tag. A Console-only distribution check rejects artifacts for other
packages. Existing `vX.Y.Z` releases retain the coordinated all-package path.

Console release wheels and sdists must contain all three prebuilt UIs. The build
hook rejects missing assets; CI installs the wheels and verifies the packaged
portal without using frontend source directories. Configure the new
`pypi-console` environment and its Trusted Publisher before its first release.

## Updating the internal baseline

Use this checklist for an internal → OSS update before the normal release gates above.
The [adapter decision](adr/0001-internal-baseline-and-oss-adapters.md) defines which
behavior belongs in each edition.

1. **Pin the comparison.** Record the previous and proposed internal revisions and the OSS starting commit in the review. Inventory changes to public behavior, dependencies and stored data, not just the latest commits. Transfer only code cleared for public release; do not mirror private history.
2. **Update the shared implementation first.** Keep upstream fixes in the package that owns the behavior. Reapply only the OSS differences needed at its boundaries. If a new interface changes who may act, who owns a record, or how old data is read, stop and review the change rather than masking it in the adapter.
3. **Verify each boundary in an isolated installation.** Use disposable identities, credentials and databases. Record the observable results:
   - Open the built UI through the local OSS gateway with no account or login. Check that account actions are absent and settings, settlement and cron remain reachable. On a writable local Host, save a fixture model credential and check after restart that it is configured and masked. A managed login deployment is not an OSS release requirement.
   - Confirm a fixture settlement and read it back through DataBridge. Repeat to check deduplication. Keep the shared credential-isolation regression: neither a remote destination nor a denied delegation may receive a local fallback credential.
   - Exercise a fresh home, an old SQLite candidate alone, and both databases together. Discovery must identify the old path without writing it; if both default paths refer to the same file, local Host startup must stop before initializing it.
   - When upgrading from the long-lived personal metadata sync lock, drain old CM writers before starting the new ones. CM creates the claim table on startup; old and new versions do not recognize each other's lock.
4. **Record the release evidence.** In the review, identify the upstream revisions, OSS adaptations, intentional exclusions, checks run and any unverified backends. A fixture or component test is not a live-provider or browser result. Run the normal package and CI gates before tagging.

These targeted checks can be run from the repository root; they supplement the
installed-package and end-to-end checks above:

```sh
bash scripts/tests/run_pytest.sh \
  packages/qwenpaw-data-console/tests/test_console_portal.py \
  packages/qwenpaw-data-host-core/tests/test_settlement_retrieval.py::test_settlement_confirm_uses_only_destination_scoped_credentials \
  packages/qwenpaw-data-context/tests/cm/metadata/test_http_feedback.py \
  packages/qwenpaw-data-cli/tests/test_migration.py::test_discover_reports_old_sqlite_even_with_a_new_store \
  packages/qwenpaw-data-host-core/tests/test_startup_compatibility.py::test_default_database_cannot_alias_legacy_store
npm --prefix packages/qwenpaw-data-console/frontend test
```

## Public-history boundary

An internal development repository may contain private author domains or
historical content that is absent from the current tree. Do not mirror that Git
history into a public repository without an explicit legal/security review.

For a clean initial publication, use:

```bash
scripts/export_public_snapshot.sh dist/QwenPaw-Data-0.1.0.tar.gz
```

The command exports tracked files from `HEAD`, excluding all Git history, and
writes a SHA-256 checksum. Import that archive into a newly initialized public
repository using an approved public author identity. This leaves the internal
history untouched.

Before any history-preserving mirror, audit all refs, not only the current
branch:

```bash
scripts/audit_git_history.py \
  --forbidden-email-domain '<private-domain.example>' \
  --forbidden-term '<private-marker>'
```

The audit intentionally fails if a forbidden author/committer email or a commit
whose patch introduces/removes a forbidden term is found. A failure requires
either a reviewed history rewrite or the clean-snapshot publication flow above.
