# qwenpaw-data-skills

Reusable data analysis skills for QwenPawData.

This is a pure asset package. Bundled skill definitions live directly in `skills/` and are loaded by QwenPawData host packages from either the source checkout or the installed wheel metadata.

When promoting a change to the OSS built-in skills (including their scripts and
resources), bump `skills/.qwenpaw-data-builtin-version` in the same commit.
Host uses this pack revision, not its own package version, to refresh each
agent's installed built-ins. Existing OSS installs without a version stamp
refresh once; unmarked custom skills are not replaced or removed.
