#!/usr/bin/env bash
#
# Patch the external Slime checkout with the small hook surface OpenEvo needs for
# bounded async training. Slime is cloned as a git repo, so a normal git patch
# is clearer than the site-packages text replacement used by patch_sglang.sh.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
SLIME_DIR="${1:-${SLIME_DIR:-${PROJECT_ROOT}/slime}}"
PATCH_FILES=(
    "${SCRIPT_DIR}/slime_openevo_async.patch"
)

if [ ! -d "${SLIME_DIR}/.git" ]; then
    echo "ERROR: Slime git checkout not found at ${SLIME_DIR}"
    exit 1
fi

apply_patch_file() {
    local patch_file="$1"
    if git -C "${SLIME_DIR}" apply --check "${patch_file}" 2>/dev/null; then
        echo "Applying OpenEvo Slime compatibility patch: $(basename "${patch_file}")"
        git -C "${SLIME_DIR}" apply "${patch_file}"
    elif git -C "${SLIME_DIR}" apply --reverse --check "${patch_file}" 2>/dev/null; then
        echo "OpenEvo Slime compatibility patch already applied: $(basename "${patch_file}")"
    else
        echo "ERROR: OpenEvo Slime compatibility patch does not apply cleanly to ${SLIME_DIR}: ${patch_file}"
        echo "       Check the Slime version or port the patch to the current checkout."
        exit 1
    fi
}

for patch_file in "${PATCH_FILES[@]}"; do
    apply_patch_file "${patch_file}"
done
