# Release process

Releases must be produced from a reviewed, clean commit on the default branch.

1. Update `CHANGELOG.md`, package versions, and the compatibility table.
2. Run `scripts/verify.sh` and the frontend lint/build commands.
3. Run `scripts/check_release_versions.py vX.Y.Z`, then build all Python
   distributions with `uv build --all-packages --out-dir dist`.
4. Run the dependency, license, secret, CodeQL, and SBOM CI gates.
5. Run database and Docker integration jobs on CI.
6. Inspect the generated wheels/sdists and verify the CLI from installed wheels.
7. Tag the commit and attach checksums plus SBOM artifacts to the release.

Publishing a GitHub release triggers `.github/workflows/publish.yml`. Its build
job verifies aligned package/tag versions, builds and checks all four package
wheel/sdist pairs, and uploads each package as a separate artifact. The publish
job then uploads the packages independently, each using PyPI Trusted Publishing
with no stored API token and gated behind its own protected GitHub environment:

| PyPI project | GitHub environment |
| --- | --- |
| `qwenpaw-data-cli` | `pypi-cli` |
| `qwenpaw-data-context` | `pypi-context` |
| `qwenpaw-data-host-core` | `pypi-host-core` |
| `qwenpaw-data-skills` | `pypi-skills` |

For each of the four PyPI projects, configure the same owner/repository, the
workflow filename `publish.yml`, and that project's environment name above as a
Trusted Publisher before the first release. Manual workflow dispatch runs the
build checks by default and publishes only when its `publish` input is enabled
together with an existing `release_tag`.

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
