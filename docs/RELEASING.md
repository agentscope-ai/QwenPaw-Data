# Release process

Releases must be produced from a reviewed, clean commit on the default branch.

1. Update `CHANGELOG.md`, package versions, and the compatibility table.
2. Run `scripts/verify.sh` and the frontend lint/build commands.
3. Run `scripts/check_release_versions.py vX.Y.Z` and `python scripts/build_console.py`, then build all Python
   distributions with `uv build --all-packages --out-dir dist`.
4. Run the dependency, license, secret, CodeQL, and SBOM CI gates.
5. Run database and Docker integration jobs on CI.
6. Inspect the generated wheels/sdists and verify the CLI from installed wheels.
7. Tag the commit and attach checksums plus SBOM artifacts to the release.

Publishing a GitHub release triggers `.github/workflows/publish.yml`. Its build
job verifies aligned package/tag versions, builds and checks all five package
wheel/sdist pairs, and uploads each package as a separate artifact. The publish
job then uploads the packages independently, each using PyPI Trusted Publishing
with no stored API token and gated behind its own protected GitHub environment:

| PyPI project | GitHub environment |
| --- | --- |
| `qwenpaw-data-console` | `pypi-console` |
| `qwenpaw-data-cli` | `pypi-cli` |
| `qwenpaw-data-context` | `pypi-context` |
| `qwenpaw-data-host-core` | `pypi-host-core` |
| `qwenpaw-data-skills` | `pypi-skills` |

For each of the five PyPI projects, configure the same owner/repository, the
workflow filename `publish.yml`, and that project's environment name above as a
Trusted Publisher before the first release. Manual workflow dispatch runs the
build checks by default and publishes only when its `publish` input is enabled
together with an existing `release_tag`.

### Console-only releases

A `console-vX.Y.Z` tag selects only `qwenpaw-data-console`. The same workflow
validates that package's version, builds its two prebuilt UIs and wheel/sdist,
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

Console release wheels and sdists must contain both prebuilt UIs. The build
hook rejects missing assets; CI installs the wheels and verifies the packaged
portal without using frontend source directories. Configure the new
`pypi-console` environment and its Trusted Publisher before its first release.

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
