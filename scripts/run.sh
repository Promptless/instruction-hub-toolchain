#!/usr/bin/env bash
set -euo pipefail

mode="${INPUT_MODE:-build}"
hub_root_input="${INPUT_HUB_ROOT:-.}"
release_branch="${INPUT_RELEASE_BRANCH:-release/stable}"
source_branch="${INPUT_SOURCE_BRANCH:-main}"
generated_paths_input="${INPUT_GENERATED_PATHS:-dist .agents/plugins .claude-plugin .cursor-plugin hub.release.json hub.stable.json}"
update_claude_pointer="${INPUT_UPDATE_CLAUDE_POINTER:-true}"
update_codex_pointer="${INPUT_UPDATE_CODEX_POINTER:-true}"
update_cursor_pointer="${INPUT_UPDATE_CURSOR_POINTER:-true}"
github_token="${INPUT_GITHUB_TOKEN:-}"
commit_user_name="${INPUT_COMMIT_USER_NAME:-github-actions[bot]}"
commit_user_email="${INPUT_COMMIT_USER_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"
commit_message="${INPUT_COMMIT_MESSAGE:-Update Instruction Hub release}"

repo_root="$(git rev-parse --show-toplevel)"
workspace="${GITHUB_WORKSPACE:-$repo_root}"
if [[ "$hub_root_input" = /* ]]; then
  hub_root="$hub_root_input"
else
  hub_root="$workspace/$hub_root_input"
fi
hub_root="$(cd "$hub_root" && pwd)"

declare -a generated_paths=()
declare -a legacy_generated_paths=(".promptless/releases" ".promptless/channels")
declare -a release_worktrees=()
declare -a marketplace_pointer_paths=()
declare -a temp_paths=()
original_origin_url=""

cleanup_temp_paths() {
  set +u
  for worktree_path in "${release_worktrees[@]}"; do
    if [[ -d "$worktree_path" ]]; then
      git -C "$repo_root" worktree remove "$worktree_path" --force >/dev/null 2>&1 || rm -rf "$worktree_path"
    fi
  done
  for temp_path in "${temp_paths[@]}"; do
    rm -rf "$temp_path"
  done
}

trap cleanup_temp_paths EXIT

validate_branch_name() {
  local label="$1"
  local branch="$2"
  if [[ -z "$branch" || "$branch" == -* || "$branch" == *$'\n'* || "$branch" == *$'\r'* ]]; then
    echo "Invalid $label: must be a non-empty git branch name without control characters." >&2
    exit 2
  fi
  if ! git check-ref-format "refs/heads/$branch" >/dev/null 2>&1; then
    echo "Invalid $label '$branch': expected a valid git branch name." >&2
    exit 2
  fi
}

validate_release_branch() {
  validate_branch_name "release-branch" "$1"
}

validate_source_branch() {
  validate_branch_name "source-branch" "$1"
}

validate_distinct_publish_branches() {
  if [[ "$release_branch" == "$source_branch" ]]; then
    echo "Invalid release-branch '$release_branch': release-branch must differ from source-branch." >&2
    exit 2
  fi
}

normalize_bool_input() {
  local label="$1"
  local value="$2"
  local normalized
  normalized="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  case "$normalized" in
    true | false)
      printf '%s\n' "$normalized"
      ;;
    *)
      echo "Invalid $label '$value': expected true or false." >&2
      exit 2
      ;;
  esac
}

reject_generated_path() {
  local path="$1"
  echo "Invalid generated-path '$path': entries must be relative paths inside hub-root without empty, '.', or '..' components." >&2
  exit 2
}

append_generated_path() {
  local path="$1"
  [[ -z "$path" ]] && return
  [[ "$path" = /* ]] && reject_generated_path "$path"
  while [[ "$path" == */ ]]; do
    path="${path%/}"
  done
  [[ -z "$path" || "$path" == "." ]] && reject_generated_path "$1"

  local components=()
  local IFS="/"
  read -r -a components <<< "$path"
  for component in "${components[@]}"; do
    [[ -z "$component" || "$component" == "." || "$component" == ".." ]] && reject_generated_path "$1"
  done

  generated_paths+=("$path")
}

while IFS= read -r line; do
  read -r -a path_tokens <<< "$line"
  for path in "${path_tokens[@]}"; do
    append_generated_path "$path"
  done
done <<< "$generated_paths_input"

validate_release_branch "$release_branch"
validate_source_branch "$source_branch"
if [[ "$mode" == "publish" ]]; then
  validate_distinct_publish_branches
fi
update_claude_pointer="$(normalize_bool_input "update-claude-pointer" "$update_claude_pointer")"
update_codex_pointer="$(normalize_bool_input "update-codex-pointer" "$update_codex_pointer")"
update_cursor_pointer="$(normalize_bool_input "update-cursor-pointer" "$update_cursor_pointer")"

pig() {
  uv run --project "$GITHUB_ACTION_PATH" promptless-instruction-hub "$@"
}

hub_relative_path() {
  python - "$repo_root" "$hub_root" <<'PY'
from pathlib import Path
import sys

repo_root = Path(sys.argv[1]).resolve()
hub_root = Path(sys.argv[2]).resolve()
try:
    rel = hub_root.relative_to(repo_root)
except ValueError as exc:
    raise SystemExit(f"hub-root must be inside the git checkout: {hub_root}") from exc
print("" if str(rel) == "." else rel.as_posix())
PY
}

require_publish_source_ref() {
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    if [[ "${GITHUB_REF_TYPE:-}" != "branch" ]]; then
      echo "Publish mode must run from branch ref '$source_branch'; got ref type '${GITHUB_REF_TYPE:-unset}'." >&2
      exit 2
    fi
    if [[ "${GITHUB_REF_NAME:-}" != "$source_branch" ]]; then
      echo "Publish mode must run from source branch '$source_branch'; got '${GITHUB_REF_NAME:-unset}'." >&2
      exit 2
    fi
    return
  fi

  local current_branch
  current_branch="$(git -C "$repo_root" branch --show-current)"
  if [[ -n "$current_branch" && "$current_branch" != "$source_branch" ]]; then
    echo "Publish mode must run from source branch '$source_branch'; got '$current_branch'." >&2
    exit 2
  fi
}

github_repository_url() {
  local token="${1:-}"
  python - "$token" "${GITHUB_SERVER_URL:-https://github.com}" "$GITHUB_REPOSITORY" <<'PY'
from urllib.parse import quote, urlsplit, urlunsplit
import sys

token = sys.argv[1]
server_url = sys.argv[2].rstrip("/")
repository = sys.argv[3]

parts = urlsplit(server_url)
if not parts.scheme or not parts.netloc:
    raise SystemExit(f"GITHUB_SERVER_URL must include scheme and host: {server_url}")

netloc = parts.netloc
if token:
    netloc = f"x-access-token:{quote(token, safe='')}@{netloc}"

base_path = parts.path.rstrip("/")
repo_path = f"{base_path}/{repository}.git" if base_path else f"/{repository}.git"
print(urlunsplit((parts.scheme, netloc, repo_path, "", "")))
PY
}

configure_push_credentials() {
  if [[ -z "$github_token" || -z "${GITHUB_REPOSITORY:-}" ]]; then
    return
  fi
  if [[ -z "$original_origin_url" ]]; then
    original_origin_url="$(git -C "$repo_root" remote get-url origin)"
  fi
  git -C "$repo_root" remote set-url origin "$(github_repository_url "$github_token")"
}

restore_push_credentials() {
  if [[ -z "$original_origin_url" ]]; then
    return
  fi
  git -C "$repo_root" remote set-url origin "$original_origin_url"
  original_origin_url=""
}

push_origin_refs() {
  local cwd="$1"
  shift
  local status=0
  configure_push_credentials
  git -C "$cwd" push --atomic \
    "--force-with-lease=refs/heads/$source_branch:$source_base" \
    "--force-with-lease=refs/heads/$release_branch:$release_base" \
    origin "$@" || status=$?
  restore_push_credentials
  return "$status"
}

remote_release_branch_exists() {
  local status=0
  configure_push_credentials
  git -C "$repo_root" ls-remote --exit-code --heads origin "$release_branch" >/dev/null 2>&1 || status=$?
  restore_push_credentials

  case "$status" in
    0)
      return 0
      ;;
    2)
      return 1
      ;;
    *)
      echo "Failed to inspect release branch '$release_branch' on origin; check checkout credentials or github-token." >&2
      exit 1
      ;;
  esac
}

fetch_release_branch() {
  local status=0
  configure_push_credentials
  git -C "$repo_root" fetch origin "+refs/heads/$release_branch:refs/remotes/origin/$release_branch" || status=$?
  restore_push_credentials

  if [[ "$status" -ne 0 ]]; then
    echo "Failed to fetch existing release branch '$release_branch' from origin." >&2
    exit 1
  fi
}

snapshot_publish_source() {
  if ! git -C "$repo_root" diff --quiet || ! git -C "$repo_root" diff --cached --quiet; then
    echo "Publish requires committed source changes and a clean index." >&2
    exit 1
  fi
  if [[ -n "$(git -C "$hub_root" ls-files --others --exclude-standard -- hub.yaml plugins assets hub.repo-context.json)" ]]; then
    echo "Publish requires all hub source files to be committed." >&2
    exit 1
  fi
  local status=0
  configure_push_credentials
  git -C "$repo_root" fetch origin "+refs/heads/$source_branch:refs/remotes/origin/$source_branch" || status=$?
  restore_push_credentials
  [[ "$status" -eq 0 ]] || exit "$status"
  source_base="$(git -C "$repo_root" rev-parse "origin/$source_branch")"
  if ! git -C "$repo_root" merge-base --is-ancestor "$source_base" HEAD; then
    echo "Source branch advanced; rerun publication from the latest $source_branch." >&2
    exit 1
  fi
}

copy_previous_release_branch() {
  local destination_root="$1"

  if remote_release_branch_exists; then
    fetch_release_branch
    release_base="$(git -C "$repo_root" rev-parse "origin/$release_branch")"
    git -C "$repo_root" archive "$release_base" | tar -x -C "$destination_root"
    return 0
  fi
  return 1
}

resolve_publish_version() {
  local previous_release_root="$1"
  local hub_rel="$2"
  local previous_release_exists="$3"

  local args=(
    publish-version
    --hub "$hub_root"
    --hub-relative-path "$hub_rel"
  )
  if [[ "$previous_release_exists" == "true" ]]; then
    args+=(--previous-release-root "$previous_release_root")
  fi

  pig "${args[@]}"
}

copy_generated_paths() {
  local destination_root="$1"
  local hub_rel="$2"

  for generated_path in "${generated_paths[@]}"; do
    local source_path="$hub_root/$generated_path"
    local destination_path
    if [[ -n "$hub_rel" ]]; then
      destination_path="$destination_root/$hub_rel/$generated_path"
    else
      destination_path="$destination_root/$generated_path"
    fi

    rm -rf "$destination_path"
    if [[ -e "$source_path" ]]; then
      mkdir -p "$(dirname "$destination_path")"
      cp -R "$source_path" "$destination_path"
    fi
  done
}

copy_payload_generated_paths() {
  local source_root="$1"
  local destination_root="$2"
  local hub_rel="$3"

  for generated_path in "${generated_paths[@]}"; do
    local source_path
    local destination_path
    if [[ -n "$hub_rel" ]]; then
      source_path="$source_root/$hub_rel/$generated_path"
      destination_path="$destination_root/$hub_rel/$generated_path"
    else
      source_path="$source_root/$generated_path"
      destination_path="$destination_root/$generated_path"
    fi

    rm -rf "$destination_path"
    if [[ -e "$source_path" ]]; then
      mkdir -p "$(dirname "$destination_path")"
      cp -R "$source_path" "$destination_path"
    fi
  done
}

cleanup_legacy_generated_paths() {
  local destination_root="$1"
  local hub_rel="$2"

  for legacy_generated_path in "${legacy_generated_paths[@]}"; do
    local destination_path
    if [[ -n "$hub_rel" ]]; then
      destination_path="$destination_root/$hub_rel/$legacy_generated_path"
    else
      destination_path="$destination_root/$legacy_generated_path"
    fi
    rm -rf "$destination_path"
  done
}

restore_generated_paths_on_default_branch() {
  local hub_rel="$1"

  for generated_path in "${generated_paths[@]}"; do
    local repo_path
    if [[ -n "$hub_rel" ]]; then
      repo_path="$hub_rel/$generated_path"
    else
      repo_path="$generated_path"
    fi

    if git -C "$repo_root" ls-files --error-unmatch "$repo_path" >/dev/null 2>&1; then
      git -C "$repo_root" restore --staged --worktree -- "$repo_path"
      git -C "$repo_root" clean -fdx -- "$repo_path"
    else
      rm -rf "$repo_root/$repo_path"
    fi
  done
}

prepare_release_commit() {
  local hub_rel="$1"
  local payload_root="$2"
  local worktree
  worktree="$(mktemp -d)"
  rm -rf "$worktree"
  release_worktrees+=("$worktree")

  git -C "$repo_root" config user.name "$commit_user_name"
  git -C "$repo_root" config user.email "$commit_user_email"

  if [[ -n "$release_base" ]]; then
    git -C "$repo_root" worktree add --detach "$worktree" "$release_base"
  else
    git -C "$repo_root" worktree add --detach "$worktree" HEAD
    git -C "$worktree" checkout --orphan "hub-release-$$"
    if [[ -n "$(git -C "$worktree" ls-files)" ]]; then
      git -C "$worktree" rm -rf .
    fi
    if [[ -n "$(git -C "$worktree" ls-files)" ]]; then
      echo "Failed to clear tracked files before creating release branch '$release_branch'." >&2
      exit 1
    fi
  fi

  cleanup_legacy_generated_paths "$worktree" "$hub_rel"
  copy_payload_generated_paths "$payload_root" "$worktree" "$hub_rel"
  if [[ -n "$hub_rel" ]]; then
    git -C "$worktree" add -A "$hub_rel"
  else
    git -C "$worktree" add -A .
  fi

  if ! git -C "$worktree" diff --cached --quiet; then
    git -C "$worktree" commit -m "$commit_message"
  else
    echo "No release branch changes to publish."
  fi

  release_commit="$(git -C "$worktree" rev-parse HEAD)"
  git -C "$repo_root" worktree remove "$worktree" --force
  if [[ -z "$release_base" ]]; then
    git -C "$repo_root" branch -D "hub-release-$$"
  fi
}

prepare_marketplace_pointer() {
  local platform="$1"
  local payload_root="$2"
  local pointer_root="$3"
  local hub_rel="$4"
  local label
  local marketplace_relative_path
  local marketplace_path
  local destination_relative_path
  local destination_path
  local prepared_path
  local repository_url

  case "$platform" in
    claude)
      label="Claude"
      marketplace_relative_path=".claude-plugin/marketplace.json"
      ;;
    codex)
      label="Codex"
      marketplace_relative_path=".agents/plugins/marketplace.json"
      ;;
    cursor)
      label="Cursor"
      marketplace_relative_path=".cursor-plugin/marketplace.json"
      ;;
    *)
      echo "Unsupported marketplace pointer platform: $platform" >&2
      exit 2
      ;;
  esac

  marketplace_path="$payload_root/$marketplace_relative_path"
  destination_relative_path="$marketplace_relative_path"
  if [[ -n "$hub_rel" ]]; then
    marketplace_path="$payload_root/$hub_rel/$marketplace_relative_path"
    destination_relative_path="$hub_rel/$marketplace_relative_path"
  fi
  destination_path="$repo_root/$destination_relative_path"
  prepared_path="$pointer_root/$destination_relative_path"

  if [[ ! -f "$marketplace_path" ]]; then
    local destination_tracked=false
    local tracking_status=0
    if git -C "$repo_root" ls-files --error-unmatch "$destination_relative_path" >/dev/null 2>&1; then
      destination_tracked=true
    else
      tracking_status=$?
      if [[ "$tracking_status" -ne 1 ]]; then
        echo "Failed to inspect tracked marketplace pointer path: $destination_relative_path" >&2
        exit 1
      fi
    fi
    if [[ -e "$destination_path" || "$destination_tracked" == "true" ]]; then
      echo "No $label marketplace was generated; removing stale source-branch $label pointer."
      marketplace_pointer_paths+=("$destination_relative_path")
    else
      echo "No $label marketplace was generated; skipping source-branch $label pointer."
    fi
    return
  fi

  if [[ -z "${GITHUB_REPOSITORY:-}" ]]; then
    echo "GITHUB_REPOSITORY is required to write marketplace pointers." >&2
    exit 2
  fi

  repository_url="$(github_repository_url)"
  mkdir -p "$(dirname "$prepared_path")"
  uv run --project "$GITHUB_ACTION_PATH" python - "$platform" "$marketplace_path" "$prepared_path" "$repository_url" "$release_branch" "$hub_rel" "$GITHUB_REPOSITORY" <<'PY'
from pathlib import Path
from urllib.parse import urlsplit
import json
import sys

from promptless_instruction_hub.render.external import validate_external_marketplace_source

platform = sys.argv[1]
source_path = Path(sys.argv[2])
destination_path = Path(sys.argv[3])
repository_url = sys.argv[4]
release_branch = sys.argv[5]
hub_rel = sys.argv[6].strip("/")
github_repository = sys.argv[7]


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def normalize_local_path(local_path: str) -> str:
    path = local_path.strip().removeprefix("./").rstrip("/")
    parts = path.split("/")
    if not path or path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        fail(f"Invalid {platform} marketplace source path: {local_path}")
    return f"{hub_rel}/{path}" if hub_rel else path


def plugin_local_path(plugin: dict[str, object]) -> str:
    source = plugin.get("source")
    if platform in {"claude", "cursor"}:
        if not isinstance(source, str):
            fail(f"Expected {platform} marketplace source to be a string.")
        return normalize_local_path(source)
    if not isinstance(source, dict) or source.get("source") != "local":
        fail("Expected Codex marketplace source to be a local source object.")
    path = source.get("path")
    if not isinstance(path, str):
        fail("Expected Codex marketplace source path to be a string.")
    return normalize_local_path(path)


def github_cursor_source(path: str) -> dict[str, str]:
    owner, separator, repo = github_repository.partition("/")
    if separator != "/" or not owner or not repo:
        fail(f"GITHUB_REPOSITORY must be owner/repo, got: {github_repository}")
    return {
        "type": "github",
        "owner": owner,
        "repo": repo,
        "path": path,
        "ref": release_branch,
    }

marketplace = json.loads(source_path.read_text())
plugins = marketplace.get("plugins")
if not isinstance(plugins, list):
    fail("Expected marketplace plugins to be a list.")
for plugin in plugins:
    if not isinstance(plugin, dict):
        fail("Expected marketplace plugins to be objects.")
    source = plugin.get("source")
    if platform in {"claude", "codex"} and isinstance(source, dict) and source.get("source") != "local":
        validate_external_marketplace_source(source)
        continue
    path = plugin_local_path(plugin)
    if platform == "cursor" and urlsplit(repository_url).hostname == "github.com":
        plugin["source"] = github_cursor_source(path)
    else:
        plugin["source"] = {
            "source": "git-subdir",
            "url": repository_url,
            "path": path,
            "ref": release_branch,
        }
    plugin.pop("version", None)

destination_path.write_text(json.dumps(marketplace, indent=2, sort_keys=True) + "\n")
PY
  marketplace_pointer_paths+=("$destination_relative_path")
}

prepare_source_commit() {
  local pointer_root="$1"
  local hub_rel="$2"
  local worktree
  worktree="$(mktemp -d)"
  rm -rf "$worktree"
  release_worktrees+=("$worktree")
  git -C "$repo_root" worktree add --detach "$worktree" HEAD
  local config_path="${hub_rel:+$hub_rel/}hub.yaml"
  pig set-version --hub "$worktree/${hub_rel:-.}" --version "$publish_version"

  for pointer_path in "${marketplace_pointer_paths[@]}"; do
    local prepared_path="$pointer_root/$pointer_path"
    local destination_path="$worktree/$pointer_path"
    if [[ -f "$prepared_path" ]]; then
      mkdir -p "$(dirname "$destination_path")"
      cp "$prepared_path" "$destination_path"
    else
      rm -f "$destination_path"
    fi
  done

  git -C "$worktree" add -A -- "$config_path" "${marketplace_pointer_paths[@]}"
  # Git skips lease checks for unchanged refs. Every release update needs a
  # source commit, including explicit versions whose hub.yaml is already current.
  if [[ "$release_commit" != "$release_base" ]] || ! git -C "$worktree" diff --cached --quiet; then
    git -C "$worktree" commit --allow-empty -m "Record Instruction Hub release $publish_version"
  else
    echo "No source version or marketplace pointer changes to publish."
  fi
  source_commit="$(git -C "$worktree" rev-parse HEAD)"
  git -C "$repo_root" worktree remove "$worktree" --force
}

case "$mode" in
  build)
    hub_rel="$(hub_relative_path)"
    pig validate --hub "$hub_root"
    pig verify-external --hub "$hub_root"
    pig build --hub "$hub_root"
    restore_generated_paths_on_default_branch "$hub_rel"
    ;;
  check)
    hub_relative_path >/dev/null
    pig validate --hub "$hub_root"
    pig verify-external --hub "$hub_root"
    pig build --hub "$hub_root" --check
    ;;
  publish)
    require_publish_source_ref
    hub_rel="$(hub_relative_path)"
    snapshot_publish_source
    release_base=""
    previous_release_root="$(mktemp -d)"
    payload_root="$(mktemp -d)"
    pointer_root="$(mktemp -d)"
    temp_paths+=("$previous_release_root" "$payload_root" "$pointer_root")
    pig validate --hub "$hub_root"
    if copy_previous_release_branch "$previous_release_root"; then
      previous_release_exists=true
      pig verify-external --hub "$hub_root" --previous-release-root "$previous_release_root" --hub-relative-path "$hub_rel"
    else
      previous_release_exists=false
      pig verify-external --hub "$hub_root"
    fi
    publish_version="$(resolve_publish_version "$previous_release_root" "$hub_rel" "$previous_release_exists")"
    pig build --hub "$hub_root" --version "$publish_version"
    copy_generated_paths "$payload_root" "$hub_rel"
    restore_generated_paths_on_default_branch "$hub_rel"
    marketplace_pointer_paths=()
    if [[ "$update_claude_pointer" == "true" ]]; then
      prepare_marketplace_pointer "claude" "$payload_root" "$pointer_root" "$hub_rel"
    fi
    if [[ "$update_codex_pointer" == "true" ]]; then
      prepare_marketplace_pointer "codex" "$payload_root" "$pointer_root" "$hub_rel"
    fi
    if [[ "$update_cursor_pointer" == "true" ]]; then
      prepare_marketplace_pointer "cursor" "$payload_root" "$pointer_root" "$hub_rel"
    fi
    prepare_release_commit "$hub_rel" "$payload_root"
    prepare_source_commit "$pointer_root" "$hub_rel"
    # Also guard the release snapshot when only source metadata needs repair.
    if [[ "$source_commit" != "$source_base" && "$release_commit" == "$release_base" ]]; then
      release_commit="$(git -C "$repo_root" commit-tree "$release_commit^{tree}" -p "$release_commit" \
        -m "Record Instruction Hub source version $publish_version")"
    fi
    push_origin_refs "$repo_root" "$source_commit:refs/heads/$source_branch" "$release_commit:refs/heads/$release_branch"
    git -C "$repo_root" merge --ff-only "$source_commit"
    ;;
  *)
    echo "Unsupported mode: $mode. Expected build, check, or publish." >&2
    exit 2
    ;;
esac

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'release-branch=%s\n' "$release_branch" >> "$GITHUB_OUTPUT"
fi
