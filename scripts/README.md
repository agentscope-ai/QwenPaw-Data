# Local service entry points

The recommended OSS, no-login workflow remains `python3 scripts/init_local.py`
followed by `python3 scripts/start_local.py`. Windows PowerShell and existing
shell entry points remain supported. They preserve public bind/authentication
guards and do not require Hologres.

For the Auth + Context + Host development stack, run `bash scripts/start.sh`.
`--service cm` starts Auth, Context and the workspace provisioner;
`--service host` starts Host and its unified frontend. Configuration comes from
the root `.env` or `QWENPAW_DATA_ENV_FILE`. Each launcher owns only its children,
rejects occupied ports, and preserves database containers/data on shutdown.
CM-only startup requires the shared Host schema to have been initialized.

Startup repairs missing/mismatched frontend dependencies using the lockfile,
builds embedded Context, and installs Host from `uv.lock` with `--locked`.
Use `QWENPAW_DATA_PYTHON` to supply an already prepared interpreter.
Ports are configurable through `AUTH_PORT`, `CONTEXT_PORT`,
`CONTEXT_FRONTEND_PORT`, `QWENPAW_PORT` and `HOST_FRONTEND_PORT`.
All browser-facing development services bind to loopback by default.

For explicit multi-user account/model initialization, set
`QWENPAW_DATA_AUTH_MODE=multi-user` and run `bash scripts/setup_chat.sh`.
See [Chat setup](chat_setup/README.md). Startup alone does not create accounts,
configure models or run a paid model request. Setup does run a real chat probe.

The old `start_all.sh` and local initialization wrappers are retained as OSS
entry points; their existing behavior is not replaced by the Cloud lifecycle.
Retired profiling algorithms, historical migration utilities and private
walkthrough fixtures are not required by the new startup path.
