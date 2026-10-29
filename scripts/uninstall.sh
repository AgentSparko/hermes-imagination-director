#!/usr/bin/env bash
set -euo pipefail
PREFIX="${IMAGINATION_HOME:-$HOME/.hermes-imagination-director}"
rm -rf "$HOME/.hermes/plugins/imagination-director"
if [[ -L "$PREFIX/bin" ]]; then rm -f "$PREFIX/bin" "$PREFIX/models"; fi   # shared with hermes-spark-voice: leave those
rm -rf "${PREFIX:?}/venv" "${PREFIX:?}/src"
[[ "${KEEP_FILMS:-1}" == 1 ]] || rm -rf "${PREFIX:?}/films"
echo "plugin removed; LFM3 units (lfm3-brain/omni/audio) and Imagination Studio left in place. 'systemctl --user disable --now lfm3-brain lfm3-omni' to stop them."
