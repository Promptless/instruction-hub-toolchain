#!/usr/bin/env bash
set -euo pipefail

mode="${INPUT_MODE:-build}"
hub_root_input="${INPUT_HUB_ROOT:-.}"
release_branch="${INPUT_RELEASE_BRANCH:-release/stable}"
generated_paths_input="${INPUT_GENERATED_PATHS:-dist .agents/plugins .claude-plugin .cursor-plugin .promptless/releases .promptless/channels}"
update_claude_pointer="${INPUT_UPDATE_CLAUDE_POINTER:-true}"
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
declare -a temp_paths=()

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

validate_release_branch() {
  local branch="$1"
  if [[ -z "$branch" || "$branch" == -* || "$branch" == *$'\n'* || "$branch" == *$'\r'* ]]; then
    echo "Invalid release-branch: must be a non-empty git branch name without control characters." >&2
    exit 2
  fi
  if ! git check-ref-format "refs/heads/$branch" >/dev/null 2>&1; then
    echo "Invalid release-branch '$branch': expected a valid git branch name." >&2
    exit 2
  fi
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
    else
      rm -rf "$repo_root/$repo_path"
    fi
  done
}

publish_release_branch() {
  local hub_rel="$1"
  local worktree
  worktree="$(mktemp -d)"
  rm -rf "$worktree"
  release_worktrees+=("$worktree")

  git -C "$repo_root" config user.name "$commit_user_name"
  git -C "$repo_root" config user.email "$commit_user_email"

  if git -C "$repo_root" ls-remote --exit-code --heads origin "$release_branch" >/dev/null 2>&1; then
    git -C "$repo_root" fetch origin "+refs/heads/$release_branch:refs/remotes/origin/$release_branch"
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

  copy_generated_paths "$worktree" "$hub_rel"
  if [[ -n "$hub_rel" ]]; then
    git -C "$worktree" add -A "$hub_rel"
  else
    git -C "$worktree" add -A .
  fi

  if ! git -C "$worktree" diff --cached --quiet; then
    git -C "$worktree" commit -m "$commit_message"
    git -C "$worktree" push origin "HEAD:$release_branch"
  else
    echo "No release branch changes to publish."
  fi

  git -C "$repo_root" worktree remove "$worktree" --force
}

write_claude_pointer() {
  local payload_root="$1"
  local hub_rel="$2"
  local marketplace_path
  local destination_path
  local plugin_path

  if [[ -n "$hub_rel" ]]; then
    marketplace_path="$payload_root/$hub_rel/.claude-plugin/marketplace.json"
    destination_path="$repo_root/$hub_rel/.claude-plugin/marketplace.json"
    plugin_path="$hub_rel/dist/claude"
  else
    marketplace_path="$payload_root/.claude-plugin/marketplace.json"
    destination_path="$repo_root/.claude-plugin/marketplace.json"
    plugin_path="dist/claude"
  fi

  if [[ ! -f "$marketplace_path" ]]; then
    echo "No Claude marketplace was generated; skipping default-branch Claude pointer."
    return 1
  fi

  if [[ -z "${GITHUB_REPOSITORY:-}" ]]; then
    echo "GITHUB_REPOSITORY is required to write the Claude pointer." >&2
    exit 2
  fi

  mkdir -p "$(dirname "$destination_path")"
  python - "$marketplace_path" "$destination_path" "${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY}.git" "$release_branch" "$plugin_path" <<'PY'
from pathlib import Path
import json
import sys

source_path = Path(sys.argv[1])
destination_path = Path(sys.argv[2])
repository_url = sys.argv[3]
release_branch = sys.argv[4]
plugin_path = sys.argv[5]

marketplace = json.loads(source_path.read_text())
for plugin in marketplace.get("plugins", []):
    plugin["source"] = {
        "source": "git-subdir",
        "url": repository_url,
        "path": plugin_path,
        "ref": release_branch,
    }
    plugin.pop("version", None)

destination_path.write_text(json.dumps(marketplace, indent=2, sort_keys=True) + "\n")
PY
}

commit_claude_pointer() {
  local hub_rel="$1"
  local branch_name="${GITHUB_REF_NAME:-$(git -C "$repo_root" branch --show-current)}"
  local pointer_path
  if [[ -n "$hub_rel" ]]; then
    pointer_path="$hub_rel/.claude-plugin/marketplace.json"
  else
    pointer_path=".claude-plugin/marketplace.json"
  fi

  git -C "$repo_root" add "$pointer_path"
  if ! git -C "$repo_root" diff --cached --quiet -- "$pointer_path"; then
    git -C "$repo_root" commit -m "Update Claude Instruction Hub pointer"
    git -C "$repo_root" push origin "HEAD:$branch_name"
  else
    echo "No Claude pointer changes to publish."
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
    pi validate --hub "$hub_root"
    pi build --hub "$hub_root" --check
    ;;
  publish)
    hub_rel="$(hub_relative_path)"
    payload_root="$(mktemp -d)"
    temp_paths+=("$payload_root")
    pi validate --hub "$hub_root"
    pi build --hub "$hub_root"
    copy_generated_paths "$payload_root" "$hub_rel"
    publish_release_branch "$hub_rel"
    restore_generated_paths_on_default_branch "$hub_rel"
    if [[ "$update_claude_pointer" == "true" ]]; then
      if write_claude_pointer "$payload_root" "$hub_rel"; then
        commit_claude_pointer "$hub_rel"
      fi
    fi
    ;;
  *)
    echo "Unsupported mode: $mode. Expected build, check, or publish." >&2
    exit 2
    ;;
esac

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'release-branch=%s\n' "$release_branch" >> "$GITHUB_OUTPUT"
fi
