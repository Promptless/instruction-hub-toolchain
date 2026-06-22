#!/usr/bin/env bash
set -euo pipefail

mode="${INPUT_MODE:-build}"
hub_root_input="${INPUT_HUB_ROOT:-.}"
release_branch="${INPUT_RELEASE_BRANCH:-release/stable}"
source_branch="${INPUT_SOURCE_BRANCH:-main}"
generated_paths_input="${INPUT_GENERATED_PATHS:-dist .agents/plugins .claude-plugin .cursor-plugin .promptless/releases .promptless/channels}"
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
declare -a release_worktrees=()
declare -a marketplace_pointer_paths=()
declare -a temp_paths=()
original_origin_url=""

cleanup_temp_paths() {
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

pi() {
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

push_origin_ref() {
  local cwd="$1"
  local refspec="$2"
  local status=0
  configure_push_credentials
  git -C "$cwd" push origin "$refspec" || status=$?
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

publish_release_branch() {
  local hub_rel="$1"
  local payload_root="$2"
  local worktree
  worktree="$(mktemp -d)"
  rm -rf "$worktree"
  release_worktrees+=("$worktree")

  git -C "$repo_root" config user.name "$commit_user_name"
  git -C "$repo_root" config user.email "$commit_user_email"

  if remote_release_branch_exists; then
    fetch_release_branch
    git -C "$repo_root" worktree add -B "$release_branch" "$worktree" "origin/$release_branch"
  else
    git -C "$repo_root" worktree add --detach "$worktree" HEAD
    git -C "$worktree" checkout --orphan "$release_branch"
    if [[ -n "$(git -C "$worktree" ls-files)" ]]; then
      git -C "$worktree" rm -rf .
    fi
    if [[ -n "$(git -C "$worktree" ls-files)" ]]; then
      echo "Failed to clear tracked files before creating release branch '$release_branch'." >&2
      exit 1
    fi
  fi

  copy_payload_generated_paths "$payload_root" "$worktree" "$hub_rel"
  if [[ -n "$hub_rel" ]]; then
    git -C "$worktree" add -A "$hub_rel"
  else
    git -C "$worktree" add -A .
  fi

  if ! git -C "$worktree" diff --cached --quiet; then
    git -C "$worktree" commit -m "$commit_message"
    push_origin_ref "$worktree" "HEAD:$release_branch"
  else
    echo "No release branch changes to publish."
  fi

  git -C "$repo_root" worktree remove "$worktree" --force
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
  python - "$platform" "$marketplace_path" "$prepared_path" "$repository_url" "$release_branch" "$hub_rel" "$GITHUB_REPOSITORY" <<'PY'
from pathlib import Path
import json
import sys

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


def cursor_source(path: str) -> dict[str, str]:
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
    path = plugin_local_path(plugin)
    if platform == "cursor":
        plugin["source"] = cursor_source(path)
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

commit_prepared_marketplace_pointers() {
  local pointer_root="$1"
  shift
  local pointer_paths=("$@")

  if [[ "${#pointer_paths[@]}" -eq 0 ]]; then
    echo "No marketplace pointers to publish."
    return
  fi

  for pointer_path in "${pointer_paths[@]}"; do
    local prepared_path="$pointer_root/$pointer_path"
    local destination_path="$repo_root/$pointer_path"
    if [[ -f "$prepared_path" ]]; then
      mkdir -p "$(dirname "$destination_path")"
      cp "$prepared_path" "$destination_path"
    else
      rm -f "$destination_path"
    fi
  done

  git -C "$repo_root" add -A -- "${pointer_paths[@]}"
  if ! git -C "$repo_root" diff --cached --quiet -- "${pointer_paths[@]}"; then
    git -C "$repo_root" commit -m "Update Instruction Hub marketplace pointers"
    push_origin_ref "$repo_root" "HEAD:$source_branch"
  else
    echo "No marketplace pointer changes to publish."
  fi
}

case "$mode" in
  build)
    hub_rel="$(hub_relative_path)"
    pi validate --hub "$hub_root"
    pi build --hub "$hub_root"
    restore_generated_paths_on_default_branch "$hub_rel"
    ;;
  check)
    hub_relative_path >/dev/null
    pi validate --hub "$hub_root"
    pi build --hub "$hub_root" --check
    ;;
  publish)
    require_publish_source_ref
    hub_rel="$(hub_relative_path)"
    payload_root="$(mktemp -d)"
    pointer_root="$(mktemp -d)"
    temp_paths+=("$payload_root" "$pointer_root")
    pi validate --hub "$hub_root"
    pi build --hub "$hub_root"
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
    publish_release_branch "$hub_rel" "$payload_root"
    commit_prepared_marketplace_pointers "$pointer_root" "${marketplace_pointer_paths[@]}"
    ;;
  *)
    echo "Unsupported mode: $mode. Expected build, check, or publish." >&2
    exit 2
    ;;
esac

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'release-branch=%s\n' "$release_branch" >> "$GITHUB_OUTPUT"
fi
