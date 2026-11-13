#!/usr/bin/env bash
# When sourced by the service launcher, expose configuration functions only.
# curl + jq own initialization; chat_setup/verify.py only validates its results.

cs_log() { printf '    [%s] %s\n' "${cs_phase}" "$*" >&2; }
cs_die() { cs_log "$*"; exit 1; }
cs_json() { jq -cn "$@"; }
cs_value() { jq -er "$@" <<<"${cs_body}"; }
cs_uri() { jq -rn --arg value "$1" '$value | @uri'; }
cs_data() { jq -c 'if type == "object" and has("data") then .data else . end' <<<"${cs_body}"; }

cs_request() {
  local session="$1" method="$2" url="$3" payload="$4"
  shift 4
  local cookie="${cs_tmp}/session-${session}.cookies"
  touch "${cookie}"
  cs_status=$(printf '%s' "${payload}" | curl --silent --show-error \
    --connect-timeout 10 --max-time "${QWENPAW_DATA_SETUP_HTTP_TIMEOUT:-1800}" \
    --cookie "${cookie}" --cookie-jar "${cookie}" \
    --request "${method}" --output "${cs_tmp}/response" --write-out '%{http_code}' \
    "$@" "${url}") || cs_die "${method} ${url}: transport failed"
  cs_body=$(cat "${cs_tmp}/response")
}

cs_http_error() { cs_die "${cs_method} ${cs_url}: HTTP ${cs_status}: ${cs_body:0:600}"; }

cs_api() {
  local session="$1" method="$2" url="$3" payload="${4:-}"
  local args=(-H 'Accept: application/json')
  if [[ -n "${payload}" ]]; then args=(-H 'Content-Type: application/json' --data-binary @-); fi
  cs_request "${session}" "${method}" "${url}" "${payload}" "${args[@]}"
  if [[ "${cs_status}" == 401 && "${method}" == GET && "${session}" != platform ]]; then
    cs_request "${session}" POST "${cs_auth}/api/auth/refresh" ''
    cs_request "${session}" "${method}" "${url}" "${payload}" "${args[@]}"
  fi
  cs_method="${method}" cs_url="${url}"
  [[ "${cs_status}" == 2?? ]] || cs_http_error
}

cs_session() {
  cs_sid=$(jq -r --arg u "$1" '.[$u] // empty' <<<"${cs_sessions}")
  if [[ -z "${cs_sid}" ]]; then
    cs_sid=$(jq 'length' <<<"${cs_sessions}")
    cs_sessions=$(jq -c --arg u "$1" --arg sid "${cs_sid}" '.[$u]=$sid' <<<"${cs_sessions}")
  fi
}

cs_prompt_secret() {
  cs_secret=''
  if ! { exec 3</dev/tty; } 2>/dev/null; then return 1; fi
  printf '%s' "$1" >&2
  IFS= read -rs cs_secret <&3 || cs_secret=''
  exec 3<&-
  printf '\n' >&2
  [[ -n "${cs_secret}" ]]
}

cs_credential() {
  cs_password=''
  cs_password_file="${cs_state_dir}/credentials/$1-$(cs_uri "$2").password"
  if [[ -f "${cs_password_file}" ]]; then
    cs_password=$(<"${cs_password_file}")
  fi
}

cs_new_password() {
  cs_credential "$1" "$2"
  if [[ -n "${cs_password}" ]]; then return; fi
  local candidate
  mkdir -p "${cs_state_dir}/credentials"
  # Persist before creating the account so an interrupted run can still log in.
  candidate=$(mktemp "${cs_state_dir}/credentials/.password.XXXXXX")
  "${cs_python}" -c 'import secrets; print("Aa1!" + secrets.token_urlsafe(24))' >"${candidate}"
  if ! ln "${candidate}" "${cs_password_file}" 2>/dev/null && [[ ! -f "${cs_password_file}" ]]; then
    cs_die "Cannot save password to ${cs_password_file}"
  fi
  rm -f "${candidate}"
  cs_password=$(<"${cs_password_file}")
  cs_log "$2: generated password saved to ${cs_password_file}"
}

cs_login() {
  local username="$1" password="$2" kind="${3:-account}" attempt payload session url
  cs_status=401 cs_body='{}'
  if [[ -z "${password}" ]]; then return 1; fi
  if [[ "${kind}" == platform ]]; then
    session=platform url="${cs_auth}/api/platform/login"
  else
    cs_session "${username}"
    session="${cs_sid}" url="${cs_auth}/api/auth/login"
  fi
  payload=$(CS_PASSWORD="${password}" cs_json --arg u "${username}" '{username:$u,password:env.CS_PASSWORD}')
  for ((attempt=0; attempt<12; attempt++)); do
    cs_request "${session}" POST "${url}" "${payload}" -H 'Content-Type: application/json' --data-binary @-
    if [[ "${cs_status}" == 2?? ]]; then return 0; fi
    if [[ "${QWENPAW_DATA_DB_URL:-sqlite:}" != sqlite* || ! "${cs_status}" =~ ^(500|503)$ || ${attempt} == 11 ]]; then return 1; fi
    cs_log "${username}: Auth busy; retry $((attempt+1))/11"
    sleep "$((attempt < 3 ? 1 << attempt : 5))"
  done
}

cs_existing_login() {
  local username="$1" password="$2" kind="${3:-account}" label="$1"
  if [[ "${kind}" == platform ]]; then label+=" (platform administrator)"; fi
  if cs_login "${username}" "${password}" "${kind}"; then return; fi
  if [[ "${cs_status}" == 401 ]] &&
    cs_prompt_secret "Existing password for ${label}: "; then
    if cs_login "${username}" "${cs_secret}" "${kind}"; then return; fi
  fi
  cs_die "${label}: cannot log in (HTTP ${cs_status}); rerun in a terminal with the existing password"
}

cs_platform() {
  if [[ "${cs_platform_ready}" == 1 ]]; then return; fi
  local payload username
  username=$(jq -er '.platform_admin' <<<"${cs_accounts}")
  cs_api platform GET "${cs_auth}/api/auth/status"
  if jq -e '.platform_bootstrap_required == true' <<<"${cs_body}" >/dev/null; then
    [[ -n "${QWENPAW_DATA_PLATFORM_BOOTSTRAP_TOKEN:-}" ]] || cs_die 'QWENPAW_DATA_PLATFORM_BOOTSTRAP_TOKEN is required for initial bootstrap'
    cs_new_password platform "${username}"
    payload=$(CS_PASSWORD="${cs_password}" cs_json --arg u "${username}" '{username:$u,password:env.CS_PASSWORD}')
    printf 'X-QwenPaw-Data-Bootstrap-Token: %s\n' "${QWENPAW_DATA_PLATFORM_BOOTSTRAP_TOKEN}" >"${cs_tmp}/bootstrap-header"
    cs_request platform POST "${cs_auth}/api/platform/bootstrap" "${payload}" -H @"${cs_tmp}/bootstrap-header" -H 'Content-Type: application/json' --data-binary @-
    [[ "${cs_status}" == 2?? ]] || cs_die 'Platform bootstrap failed; check account and bootstrap token'
  else
    cs_credential platform "${username}"
    cs_existing_login "${username}" "${cs_password}" platform
  fi
  cs_platform_ready=1
}

cs_wait_resource() {
  local session="$1" url="$2" list="$3" id="$4" label="$5" started=$SECONDS previous='' status
  while true; do
    cs_api "${session}" GET "${url}"
    cs_row=$(jq -c --arg list "${list}" --arg id "${id}" '.[$list][] | select(.id == $id)' <<<"${cs_body}")
    [[ -n "${cs_row}" ]] || cs_die "${label}: resource is no longer visible"
    status=$(jq -r '[.status,.instance_status,.onboarding_state] | join("/")' <<<"${cs_row}")
    if [[ "${status}" != "${previous}" ]]; then cs_log "${label}: ${status}"; previous="${status}"; fi
    if jq -e '[.status,.instance_status,.onboarding_state] | index("failed") != null' <<<"${cs_row}" >/dev/null; then
      cs_die "${label}: provisioning failed: $(jq -r '.onboarding_error // "check provisioner logs"' <<<"${cs_row}")"
    fi
    if jq -e '.status == "active" and .instance_status == "ready"' <<<"${cs_row}" >/dev/null; then return; fi
    (( SECONDS - started < 300 )) || cs_die "${label}: provisioning timed out after 300s"
    sleep 2
  done
}

cs_path() {
  case "$1" in
    '~/'*) printf '%s/%s\n' "${HOME}" "${1#\~/}" ;;
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "${cs_env_dir}" "$1" ;;
  esac
}

cs_setup_paths() {
  cs_root="$1"
  local env_file="${QWENPAW_DATA_ENV_FILE:-${cs_root}/.env}"
  cs_env_dir="$(dirname "${env_file}")"
  cs_config_file="${env_file}.chat-setup.json"
  cs_state_dir="${env_file}.chat-setup"
}

cs_inputs() {
  cs_phase=configuration
  local accounts workbook datasource
  cs_config='{}'
  if [[ -f "${cs_config_file}" ]]; then
    cs_config=$(jq -ce -s 'select(length == 1) | .[0] | select(type == "object")' "${cs_config_file}") ||
      cs_die "Expected a JSON object in ${cs_config_file}"
  fi
  if jq -e 'has("accounts")' <<<"${cs_config}" >/dev/null; then
    accounts=$(jq -c '.accounts' <<<"${cs_config}")
  elif [[ "${QWENPAW_DATA_ENV:-development}" == development ]]; then
    accounts=$(jq -ce '.accounts' "${cs_root}/scripts/chat_setup/fixtures/config.example.json")
  else
    cs_die "accounts is required outside development; configure ${cs_config_file}"
  fi
  cs_accounts=$(jq -ce '
    select((.tenants | type == "array" and length > 0) and
           (.users | type == "array" and length > 0)) |
    . as $accounts | select(all(.users[]; . as $user |
      any($accounts.tenants[]; .key == $user.tenant and
        ($user.workspace == null or any([.default_workspace] + (.extra_workspaces // []) | .[]; .key == $user.workspace)))))
  ' <<<"${accounts}") || cs_die 'Accounts must contain tenants and users with valid tenant/Workspace targets'
  workbook=$(jq -r '.workbook // ""' <<<"${cs_config}")
  datasource=$(jq -ce '.datasource // {} | select(type == "object")' <<<"${cs_config}") ||
    cs_die 'datasource must be a JSON object or null'
  cs_workbook='' cs_source=null
  # A preset with a blank password is inactive until the user supplies it.
  if jq -e '(.host // "") != "" and (.password // "") == ""' <<<"${datasource}" >/dev/null; then
    datasource='{}' workbook=''
  fi
  if jq -e '(.host // "") != ""' <<<"${datasource}" >/dev/null; then
    cs_source=$(jq -ce 'select([.database,.user,.password] | all(type == "string" and length > 0)) |
      {datasource_type:"postgresql",datasource_name:(if (.name // "") == "" then .database else .name end),
       config:{host,port:(if (.port // "") == "" then 5432 else (.port | tonumber) end),database,user,password}}' <<<"${datasource}") ||
      cs_die 'Datasource database, user, password and a valid port are required when host is set'
  fi
  if [[ -n "${workbook}" ]]; then
    [[ "${cs_source}" != null ]] || cs_die 'workbook requires a configured datasource'
    cs_workbook=$(cs_path "${workbook}")
    [[ -f "${cs_workbook}" ]] || cs_die "Workbook does not exist: ${cs_workbook}"
  fi
  cs_manifest=$(jq -cn --argjson accounts "${cs_accounts}" --arg workbook "${cs_workbook}" '{accounts:$accounts,workspaces:{},user_ids:{},datasources:{},workbook:($workbook != "")}')
}

cs_tenant_login() {
  local tenant="$1" tenant_id="$2" admin="$3" row payload password
  cs_credential account "${admin}"
  password="${cs_password}"
  if cs_login "${admin}" "${password}"; then return; fi
  [[ "${cs_status}" == 401 || "${cs_status}" == 403 ]] || cs_die "${admin}: login failed (HTTP ${cs_status})"
  # An existing tenant admin can reuse their tenant without platform credentials.
  if [[ "${cs_status}" == 401 ]]; then
    cs_api platform GET "${cs_auth}/api/auth/status"
    if jq -e '.has_users == true' <<<"${cs_body}" >/dev/null &&
      cs_prompt_secret "Existing password for ${admin} (Enter if creating a tenant): "; then
      password="${cs_secret}"
      if cs_login "${admin}" "${password}"; then return; fi
      [[ "${cs_status}" == 403 ]] || cs_die "${admin}: cannot log in with the entered password (HTTP ${cs_status})"
    fi
  fi
  cs_platform
  cs_api platform GET "${cs_auth}/api/platform/tenants"
  row=$(jq -c --arg id "${tenant_id}" '.tenants[] | select(.id == $id)' <<<"${cs_body}")
  if [[ -z "${row}" ]]; then
    cs_new_password account "${admin}"
    password="${cs_password}"
    payload=$(CS_PASSWORD="${password}" jq -c '{id,name,workspace_id:.default_workspace.id,workspace_name:.default_workspace.name,admin_username:.admin,admin_password:env.CS_PASSWORD}' <<<"${tenant}")
    cs_api platform POST "${cs_auth}/api/platform/tenants" "${payload}"
    cs_api platform GET "${cs_auth}/api/platform/tenants"
    row=$(jq -c --arg id "${tenant_id}" '.tenants[] | select(.id == $id)' <<<"${cs_body}")
  fi
  [[ -n "${row}" ]] && jq -e '.instance_status != null' <<<"${row}" >/dev/null || cs_die "${tenant_id}: no Workspace is available; an administrator must restore a usable Workspace before rerunning"
  if jq -e '.instance_status == "pending" or .instance_status == "failed"' <<<"${row}" >/dev/null; then
    cs_api platform POST "${cs_auth}/api/platform/tenants/$(cs_uri "${tenant_id}")/provision"
  fi
  cs_wait_resource platform "${cs_auth}/api/platform/tenants" tenants "${tenant_id}" "${tenant_id}"
  cs_existing_login "${admin}" "${password}"
}

cs_onboard() {
  cs_phase=onboard
  local tenant tenant_id admin spec key id name row payload username entry users tenant_key
  while IFS= read -r tenant; do
    tenant_id=$(jq -r '.id' <<<"${tenant}") admin=$(jq -r '.admin' <<<"${tenant}") tenant_key=$(jq -r '.key' <<<"${tenant}")
    cs_tenant_login "${tenant}" "${tenant_id}" "${admin}"
    cs_api "${cs_sid}" GET "${cs_auth}/api/auth/me"
    jq -e --arg id "${tenant_id}" '(. * (.user // {})) | .tenant_id == $id and .role == "tenant_admin"' <<<"${cs_body}" >/dev/null || cs_die "${admin}: unexpected tenant or role; existing account left unchanged"
    while IFS= read -r spec; do
      key=$(jq -r '.key' <<<"${spec}") name=$(jq -r '.name' <<<"${spec}")
      cs_api "${cs_sid}" GET "${cs_auth}/api/workspaces"
      row=$(jq -c --argjson spec "${spec}" '([.workspaces[] | select(.id == $spec.id)][0] // [.workspaces[] | select(.name == $spec.name)][0]) // empty' <<<"${cs_body}")
      if [[ -z "${row}" ]]; then
        cs_api "${cs_sid}" POST "${cs_auth}/api/workspaces" "$(jq -c '{name}' <<<"${spec}")"
        row=$(cs_data | jq -ce '.workspace')
      fi
      id=$(jq -er '.id' <<<"${row}")
      if ! jq -e '.status == "active" and .instance_status == "ready"' <<<"${row}" >/dev/null; then
        if jq -e '.instance_status == "pending" or .instance_status == "failed"' <<<"${row}" >/dev/null; then
          cs_api "${cs_sid}" POST "${cs_auth}/api/workspaces/$(cs_uri "${id}")/provision"
        fi
        cs_wait_resource "${cs_sid}" "${cs_auth}/api/workspaces" workspaces "${id}" "${name}"
      fi
      cs_manifest=$(jq -c --arg key "${key}" --arg name "${name}" --arg id "${id}" --arg tenant_key "${tenant_key}" --arg tenant_id "${tenant_id}" \
        '.workspaces[$key]={key:$key,name:$name,workspace_id:$id,tenant_key:$tenant_key,tenant_id:$tenant_id}' <<<"${cs_manifest}")
    done < <(jq -c '[.default_workspace]+(.extra_workspaces // []) | .[]' <<<"${tenant}")
    # Refresh claims after workspace activation before inviting or configuring users.
    cs_api "${cs_sid}" POST "${cs_auth}/api/auth/refresh"
    cs_api "${cs_sid}" GET "${cs_auth}/api/users"
    users="${cs_body}"
    while IFS= read -r entry; do
      username=$(jq -r '.username' <<<"${entry}")
      if ! jq -e --arg u "${username}" '.users | any(.username == $u)' <<<"${users}" >/dev/null; then
        cs_new_password account "${username}"
        payload=$(CS_PASSWORD="${cs_password}" jq -c --argjson m "${cs_manifest}" '{username,password:env.CS_PASSWORD,role} + (if .workspace then {workspace_id:$m.workspaces[.workspace].workspace_id} else {} end)' <<<"${entry}")
        cs_session "${admin}"
        cs_invite "${cs_sid}" "${payload}"
      fi
      if [[ "${username}" == "${admin}" ]]; then
        cs_session "${username}"
      else
        cs_credential account "${username}"
        cs_existing_login "${username}" "${cs_password}"
      fi
      cs_api "${cs_sid}" GET "${cs_auth}/api/auth/me"
      jq -e --arg id "${tenant_id}" '(. * (.user // {})) | .tenant_id == $id and (.must_change_password != true)' <<<"${cs_body}" >/dev/null || cs_die "${username}: unexpected tenant or password change required"
      id=$(cs_value '(. * (.user // {})).user_id')
      cs_manifest=$(jq -c --arg u "${username}" --arg id "${id}" '.user_ids[$u]=$id' <<<"${cs_manifest}")
      cs_log "${username}: ready"
    done < <(jq -c --arg tenant "${tenant_key}" '.users[] | select(.tenant == $tenant)' <<<"${cs_accounts}")
  done < <(jq -c '.tenants[]' <<<"${cs_accounts}")
}

cs_invite() {
  local session="$1" payload="$2" attempt
  for ((attempt=0; attempt<12; attempt++)); do
    cs_request "${session}" POST "${cs_auth}/api/users" "${payload}" -H 'Content-Type: application/json' --data-binary @-
    if [[ "${cs_status}" == 2?? ]]; then return; fi
    if [[ "${QWENPAW_DATA_DB_URL:-sqlite:}" != sqlite* || "${cs_status}" != 409 || ${attempt} == 11 ]] || ! jq -e '.detail == "Unable to invite user"' <<<"${cs_body}" >/dev/null; then
      cs_die "User invitation failed (HTTP ${cs_status}): ${cs_body:0:600}"
    fi
    sleep "$((attempt < 3 ? 1 << attempt : 5))"
  done
}

cs_targets() {
  jq -c '. as $m | .accounts.users[] as $u | $m.workspaces[] | select(.tenant_key == $u.tenant and ($u.workspace == null or $u.workspace == .key)) | . + {username:$u.username}' <<<"${cs_manifest}"
}

cs_scope() {
  local username="$1" workspace="$2"
  cs_session "${username}"
  cs_api "${cs_sid}" POST "${cs_auth}/api/auth/switch-workspace" "$(cs_json --arg id "${workspace}" '{workspace_id:$id}')"
}

cs_admin_scope() {
  local workspace="$1" username id
  username=$(jq -r --arg k "${workspace}" '. as $m | .accounts.tenants[] | select(.key == $m.workspaces[$k].tenant_key) | .admin' <<<"${cs_manifest}")
  id=$(jq -r --arg k "${workspace}" '.workspaces[$k].workspace_id' <<<"${cs_manifest}")
  cs_scope "${username}" "${id}"
}

# Resolve inputs only for a missing model.
cs_model() {
  local channel="$1" document="${2:-}" providers row provider payload endpoint
  config=$(jq -cn --arg channel "${channel}" --slurpfile setup <(printf '%s' "${cs_config}") '
    def envval($key; $fallback): (env[$key] // "") | if . == "" then $fallback else . end;
    def merge_model($defaults; $settings):
      ($settings // {} | with_entries(select(.key != "api_key" or (.value // "") != ""))) as $s |
      ($defaults + $s) |
      if ($s | has("base_url") and (has("api_key") | not)) and
         (.base_url | rtrimstr("/")) != ($defaults.base_url | rtrimstr("/"))
      then .api_key="" else . end;
    $setup[0].models as $models |
    merge_model({base_url:envval("OPENAI_BASE_URL";"https://dashscope.aliyuncs.com/compatible-mode/v1"),
      model:envval("LLM_MODEL";"qwen3.7-max"),api_key:envval("OPENAI_API_KEY";"")};$models.llm) as $llm |
    (if $channel == "host" then
      merge_model({base_url:envval("QWENPAW_DATA_MODEL_BASE_URL";$llm.base_url),model:envval("QWENPAW_DATA_MODEL_NAME";$llm.model),
       api_key:envval("QWENPAW_DATA_MODEL_API_KEY";""),provider_id:envval("QWENPAW_DATA_MODEL_PROVIDER";"")};$models.host)
    elif $channel == "embedding" then
      merge_model({base_url:envval("EMBED_OPENAI_BASE_URL";$llm.base_url),model:envval("EMBED_MODEL";"text-embedding-v3"),
       api_key:envval("EMBED_OPENAI_API_KEY";""),dim:envval("EMBED_DIM";1024)};$models.embedding)
    else $llm end) |
    if [.model,.base_url] | all(type == "string" and length > 0) then . else error("Model and endpoint are required") end |
    .api_key = (if (.api_key // "") == "" and (.base_url | rtrimstr("/")) == ($llm.base_url | rtrimstr("/")) then $llm.api_key else .api_key end) |
    .api_key //= "" |
    if has("dim") then .dim |= tonumber elif .context_size != null then .context_size |= tonumber else . end') ||
    cs_die "${channel}: incomplete model settings in ${cs_config_file} or env"
  endpoint=$(jq -r '.base_url | rtrimstr("/")' <<<"${config}")
  config=$(jq -c --arg url "${endpoint}" --slurpfile keys <(printf '%s' "${cs_model_keys}") '
    if .api_key == "" then .api_key=($keys[0][$url] // "") else . end' <<<"${config}")

  if [[ "${channel}" == embedding ]]; then
    # Reuse an adopted vector space; never implicitly migrate it.
    jq -e --slurpfile config <(printf '%s' "${config}") '.embedding_space | (.adopted | not) or
      (.model == $config[0].model and (.base_url | rtrimstr("/")) == ($config[0].base_url | rtrimstr("/")) and .dim == $config[0].dim)' <<<"${document}" >/dev/null || cs_die "${key}: embedding space differs; migrate it explicitly"
  fi
  cs_api "${cs_sid}" GET "${cs_host}/api/preferences/providers"
  providers=$(cs_data)
  provider=$(jq -r --slurpfile configs <(printf '%s' "${config}") \
    --slurpfile keys <(printf '%s' "${cs_provider_keys}") --arg sid "${cs_sid}" '
    $configs[0] as $c |
    def matches:
      .configured and (.base_url | rtrimstr("/")) == ($c.base_url | rtrimstr("/")) and
      ($c.api_key == "" or $keys[0][$sid][.id] == null or $keys[0][$sid][.id] == $c.api_key);
    [.providers[] | select(.id == "openai" or .id == "dashscope")] as $slots |
    (if ($c.base_url | test("^https?://dashscope\\.aliyuncs\\.com([/:]|$)")) then "dashscope" else "openai" end) as $preferred |
    ($c.provider_id | select(. != null and . != "")) //
    ([$slots[] | select(matches)][0].id) //
    ([$slots[] | select(.id == $preferred and (.configured | not))][0].id) //
    ([$slots[] | select(.configured | not)][0].id) // empty' <<<"${providers}")
  [[ -n "${provider}" ]] || cs_die "${channel}: no available Host provider slot; existing credentials left unchanged"
  config=$(jq -c --arg p "${provider}" '.provider_id=$p' <<<"${config}")
  row=$(jq -c --arg p "${provider}" '.providers[] | select(.id == $p)' <<<"${providers}")
  [[ -n "${row}" ]] || cs_die "Host does not support Provider ${provider}"
  if jq -e '.configured' <<<"${row}" >/dev/null; then
    [[ "$(jq -r '.base_url | rtrimstr("/")' <<<"${row}")" == "$(jq -r '.base_url | rtrimstr("/")' <<<"${config}")" ]] || cs_die "Provider ${provider} endpoint differs; existing credentials left unchanged"
  fi
  if ! jq -e '.configured and (.api_key_masked // "") != ""' <<<"${row}" >/dev/null; then
    if ! jq -e '.api_key != ""' <<<"${config}" >/dev/null; then
      cs_prompt_secret "API key for ${endpoint}: " || cs_die "${channel}: API key required for Provider ${provider}; run in a terminal or configure models in ${cs_config_file}"
      cs_model_keys=$(CS_API_KEY="${cs_secret}" jq -c --arg url "${endpoint}" '.[$url]=env.CS_API_KEY' <<<"${cs_model_keys}")
      config=$(CS_API_KEY="${cs_secret}" jq -c '.api_key=env.CS_API_KEY' <<<"${config}")
    fi
    payload=$(jq -c '{api_key,base_url}' <<<"${config}")
    cs_api "${cs_sid}" PUT "${cs_host}/api/preferences/providers/${provider}" "${payload}"
    cs_provider_keys=$(jq -c --arg sid "${cs_sid}" --arg p "${provider}" \
      --slurpfile configs <(printf '%s' "${config}") '.[$sid][$p]=$configs[0].api_key' <<<"${cs_provider_keys}")
  fi
}

cs_save_model() {
  local path="$1" payload="$2" attempt max=0
  [[ "${QWENPAW_DATA_DB_URL:-sqlite:}" != sqlite* ]] || max=6
  for ((attempt=0; attempt<=max; attempt++)); do
    cs_request "${cs_sid}" PUT "${cs_cm}/api/v1/config/${path}" "${payload}" -H 'Content-Type: application/json' --data-binary @-
    if [[ "${cs_status}" == 2?? ]]; then return; fi
    [[ "${cs_status}" == 5?? && ${attempt} -lt ${max} ]] || cs_die "Save ${path} failed (HTTP ${cs_status}): ${cs_body:0:600}"
    sleep 2
  done
}

cs_configure_models() {
  cs_phase=model_config
  local target username key id active config provider model row size document channel path current payload
  while IFS= read -r target; do
    username=$(jq -r '.username' <<<"${target}") key=$(jq -r '.key' <<<"${target}") id=$(jq -r '.workspace_id' <<<"${target}")
    cs_scope "${username}" "${id}"
    cs_api "${cs_sid}" GET "${cs_host}/api/preferences/active-models"
    active=$(cs_data)
    if ! jq -e '.active_models | [.default_provider_id,.default_model_id] | all(. != null and . != "")' <<<"${active}" >/dev/null; then
      cs_model host
      provider=$(jq -r '.provider_id' <<<"${config}") model=$(jq -r '.model' <<<"${config}")
      cs_api "${cs_sid}" GET "${cs_host}/api/preferences/providers/${provider}/models"
      row=$(cs_data | jq -c --arg model "${model}" '.models[] | select(.id == $model)')
      if [[ -z "${row}" ]] || jq -e '.source == "extra" and (.context_size == null or .context_size == 0)' <<<"${row}" >/dev/null; then
        size=$(jq -r '.context_size // empty' <<<"${config}")
        if [[ -z "${size}" ]]; then
          cs_api "${cs_sid}" GET "${cs_host}/api/preferences/providers"
          size=$(cs_data | jq -r --arg model "${model}" '[.providers[].models[]? | select(.id == $model and .context_size) | .context_size][0] // empty')
        fi
        [[ -n "${size}" ]] || cs_die "${model}: set models.host.context_size in ${cs_config_file}"
        cs_api "${cs_sid}" PUT "${cs_host}/api/preferences/providers/${provider}/models/$(cs_uri "${model}")" "$(cs_json --arg model "${model}" --argjson size "${size}" '{source:"extra",name:$model,context_size:$size}')"
      fi
      cs_api "${cs_sid}" PUT "${cs_host}/api/preferences/active-models" "$(cs_json --arg p "${provider}" --arg m "${model}" '{default_provider_id:$p,default_model_id:$m,light_provider_id:$p,light_model_id:$m}')"
    fi
    cs_api "${cs_sid}" GET "${cs_cm}/api/v1/config"
    document="${cs_body}"
    for channel in llm embedding; do
      path=llm; [[ "${channel}" != embedding ]] || path=embed
      current=$(jq -c --arg c "${channel}" '.[$c]' <<<"${document}")
      if ! jq -e '.model != null and .model != ""' <<<"${current}" >/dev/null; then
        cs_model "${channel}" "${document}"
        payload=$(jq -c 'del(.api_key,.context_size) ' <<<"${config}")
        cs_save_model "${path}" "${payload}"
      else
        jq -e --arg c "${channel}" '(.errors // {}) | has($c) | not' <<<"${document}" >/dev/null || cs_die "${username}: existing ${channel} configuration is invalid; repair it before rerunning"
        if [[ "${channel}" == embedding ]] && ! jq -e '.embedding_space.adopted' <<<"${document}" >/dev/null; then
          cs_save_model embed "$(jq -c '{provider_id,model,base_url,dim}' <<<"${current}")"
        fi
      fi
    done
    cs_log "${username}@${key}: model configuration complete"
  done < <(cs_targets)
}

cs_wait_weave() {
  local task="$1" started=$SECONDS status
  while true; do
    cs_api "${cs_sid}" GET "${cs_cm}/api/semantic-config/weave-task/$(cs_uri "${task}")"
    status=$(cs_value '.status')
    case "${status}" in
      SUCCESS) return ;;
      DEGRADED|FAILED|KILLED) cs_die "Weave ${task} ended as ${status}: $(jq -r '.error_msg // "no detail"' <<<"${cs_body}")" ;;
    esac
    (( SECONDS - started < ${QWENPAW_DATA_SETUP_WEAVE_TIMEOUT:-1800} )) || cs_die "Weave ${task} timed out; check provisioner/CM logs"
    sleep 3
  done
}

cs_configure_data() {
  local workspace key name id row task target active imported page total
  cs_phase=datasource
  if [[ "${cs_source}" == null ]]; then cs_log 'Datasource setup skipped; configure data in the UI'; return; fi
  name=$(jq -r '.datasource_name' <<<"${cs_source}")
  while IFS= read -r workspace; do
    cs_phase=datasource
    key=$(jq -r '.key' <<<"${workspace}")
    cs_admin_scope "${key}"
    cs_api "${cs_sid}" GET "${cs_cm}/api/semantic-config/datasource?datasource_name=$(cs_uri "${name}")&size=50"
    id=$(jq -r --arg name "${name}" '(.items // .records // .data // []) | if type == "object" then .items // [] else . end | [.[] | select(.datasource_name == $name) | .datasource_id][0] // empty' <<<"${cs_body}")
    if [[ -z "${id}" ]]; then
      cs_api "${cs_sid}" POST "${cs_cm}/api/semantic-config/datasource" "${cs_source}"
      id=$(cs_data | jq -er '.datasource_id')
      if [[ -n "${cs_workbook}" ]]; then
        cs_api "${cs_sid}" PUT "${cs_cm}/api/semantic-config/datasources/$(cs_uri "${id}")/explore-scope" '{"enabled":0}'
      fi
    fi
    cs_manifest=$(jq -c --arg k "${key}" --arg id "${id}" '.datasources[$k]=$id' <<<"${cs_manifest}")
    cs_api "${cs_sid}" POST "${cs_cm}/api/semantic-config/datasource/$(cs_uri "${id}")/test-connection"
    jq -e '.success == true' <<<"${cs_body}" >/dev/null || cs_die "Datasource connection test failed: $(jq -r '.message // "no detail"' <<<"${cs_body}")"
    if [[ -z "${cs_workbook}" ]]; then continue; fi
    cs_phase=semantic_config
    cs_api "${cs_sid}" GET "${cs_cm}/api/semantic-config/dataset-meta?datasource_id=$(cs_uri "${id}")&size=1"
    task='' imported=0
    if ! jq -e '.total > 0' <<<"${cs_body}" >/dev/null; then
      # Quote the filename for curl form syntax (spaces, commas and semicolons).
      local upload="${cs_workbook//\\/\\\\}"
      upload="${upload//\"/\\\"}"
      cs_request "${cs_sid}" POST "${cs_cm}/api/semantic-config/import/excel" '' \
        --form "file=@\"${upload}\";type=application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" --form-string "datasource_id=${id}"
      [[ "${cs_status}" == 2?? ]] && jq -e '.success == true' <<<"${cs_body}" >/dev/null || cs_die "Workbook import failed: ${cs_body:0:600}"
      task=$(jq -r '.weave_task_id // empty' <<<"${cs_body}")
      imported=1
    fi
    cs_phase=weave
    if [[ -z "${task}" && "${imported}" == 0 ]]; then
      # Tasks are newest first. Match the ID because the name filter is fuzzy.
      page=1 row=null
      while true; do
        cs_api "${cs_sid}" GET "${cs_cm}/api/semantic-config/weave-task?datasource_name=$(cs_uri "${name}")&page=${page}&size=50"
        row=$(jq -c --arg id "${id}" '[.records[] | select(.datasource_id == $id)][0] // null' <<<"${cs_body}")
        total=$(cs_value '.total')
        if [[ "${row}" != null ]] || (( page * 50 >= total )); then break; fi
        page=$((page+1))
      done
      case "$(jq -r '.status // empty' <<<"${row}")" in
        SUCCESS) cs_log "${key}: reuse completed weave"; continue ;;
        FAILED|KILLED|DEGRADED|'') ;; # Retry a failed or missing weave.
        *) task=$(jq -er '.task_id' <<<"${row}") ;;
      esac
    fi
    if [[ -z "${task}" ]]; then
      cs_api "${cs_sid}" POST "${cs_cm}/api/semantic-config/weave-task/submit" "$(cs_json --arg id "${id}" --arg k "${key}" '{task_name:("chat-setup-"+$k),datasource_id:$id,weave_mode:"FULL"}')"
      task=$(cs_value '.task_id')
    fi
    cs_wait_weave "${task}"
  done < <(jq -c '.workspaces[]' <<<"${cs_manifest}")
  if [[ -z "${cs_workbook}" ]]; then return; fi
  cs_phase=personal_mg
  while IFS= read -r target; do
    key=$(jq -r '.key' <<<"${target}")
    cs_scope "$(jq -r '.username' <<<"${target}")" "$(jq -r '.workspace_id' <<<"${target}")"
    cs_api "${cs_sid}" GET "${cs_cm}/api/v1/mg/userspace/sync-status"
    if ! cs_data | jq -e '.needs_sync == false' >/dev/null; then
      cs_api "${cs_sid}" POST "${cs_cm}/api/v1/mg/userspace/sync"
      cs_data | jq -e '(.conflict_count // 0) == 0' >/dev/null || cs_die "${key}: unresolved MG conflicts; resolve them in DataBridge"
    fi
    cs_api "${cs_sid}" GET "${cs_cm}/api/datasources/active"
    active=$(cs_data | jq -r '.datasource_id // empty')
    if [[ -z "${active}" ]]; then
      id=$(jq -r --arg k "${key}" '.datasources[$k]' <<<"${cs_manifest}")
      cs_api "${cs_sid}" PUT "${cs_cm}/api/datasources/active" "$(cs_json --arg id "${id}" '{datasource_id:$id}')"
    fi
  done < <(cs_targets)
}

cs_cleanup() {
  local rc=$?
  trap - EXIT
  # Bash 3.2 can report zero to EXIT after a nounset expansion error.
  if [[ "${cs_completed:-0}" != 1 && "${rc}" == 0 ]]; then rc=1; fi
  if [[ -n "${cs_tmp:-}" ]]; then rm -rf "${cs_tmp}"; fi
  if (( rc != 0 )); then
    printf 'Chat setup failed at %s (exit %s). Existing configuration was not rolled back; fix the error and rerun.\n' "${cs_phase}" "${rc}" >&2
  fi
  exit "${rc}"
}

chat_setup_configure() (
  set -euo pipefail
  umask 077
  cs_phase=configuration cs_completed=0
  for cs_dependency in curl jq; do
    command -v "${cs_dependency}" >/dev/null 2>&1 || cs_die "${cs_dependency} is required for chat setup"
  done
  cs_setup_paths "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  cs_python="$1"
  cs_tmp=$(mktemp -d "${TMPDIR:-/tmp}/qwenpaw-data-chat-setup.XXXXXX")
  trap cs_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  cs_auth="${QWENPAW_DATA_AUTH_URL:-http://localhost:${AUTH_PORT:-8088}}"; cs_auth="${cs_auth%/}"
  cs_cm="${QWENPAW_DATA_CM_BASE_URL:-http://localhost:${CONTEXT_PORT:-8765}}"; cs_cm="${cs_cm%/}"
  cs_host="${QWENPAW_DATA_HOST_BASE_URL:-http://localhost:${QWENPAW_PORT:-8089}}"; cs_host="${cs_host%/}"
  cs_provider_keys='{}' cs_model_keys='{}'
  cs_sessions='{}' cs_platform_ready=0 cs_body='' cs_status=''
  mkdir -p "${cs_state_dir}"
  printf 'export DP_CHAT_READY=0\n' >"${cs_state_dir}/state.env"
  # This common validator checks the launcher's environment binding, not setup data.
  "${cs_python}" "${cs_root}/scripts/common/service_manifest.py" check --auth-url "${cs_auth}" --context-url "${cs_cm}" --host-url "${cs_host}"
  cs_inputs
  cs_api anonymous GET "${cs_auth}/api/auth/status"
  if ! jq -e '.enabled == true' <<<"${cs_body}" >/dev/null; then
    cs_die 'Account setup requires QWENPAW_DATA_AUTH_MODE=multi-user. For local no-login use scripts/init_local.py and scripts/start_local.py.'
  fi
  cs_onboard
  cs_configure_models
  cs_configure_data
  cs_manifest=$(jq -c --argjson sessions "${cs_sessions}" --arg dir "${cs_tmp}" \
    '.cookies=($sessions | with_entries(.value=($dir+"/session-"+.value+".cookies")))' <<<"${cs_manifest}")
  cs_phase=verify
  printf '%s\n' "${cs_manifest}" | "${cs_python}" "${cs_root}/scripts/chat_setup/verify.py"
  if [[ -d "${cs_state_dir}/credentials" ]]; then
    printf 'Passwords generated by setup are in %s/credentials/\n' "${cs_state_dir}"
  fi
  cs_completed=1
)

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [[ $# -ne 0 ]]; then
    echo "This script accepts no arguments. See ${repo_root}/scripts/chat_setup/fixtures/config.example.json for optional settings." >&2
    exit 2
  fi
  for dependency in curl jq; do
    command -v "${dependency}" >/dev/null 2>&1 || { echo "${dependency} is required for chat setup." >&2; exit 2; }
  done
  # shellcheck source=scripts/common/env.sh
  source "${repo_root}/scripts/common/env.sh"
  ensure_qwenpaw_data_env_file
  load_qwenpaw_data_env
  if [[ "${QWENPAW_DATA_AUTH_MODE:-local}" != multi-user ]]; then
    echo 'Account setup requires QWENPAW_DATA_AUTH_MODE=multi-user. Local no-login remains available via scripts/init_local.py and scripts/start_local.py.' >&2
    exit 2
  fi
  # Reject invalid inputs before installing dependencies or starting services.
  (cs_setup_paths "${repo_root}"; cs_inputs)
  echo '聊天环境准备：确保服务启动 → 等待全部就绪 → 创建或复用配置 → 验证聊天。'
  export QWENPAW_DATA_SETUP_ON_START=1
  exec bash "${repo_root}/scripts/start.sh"
fi
