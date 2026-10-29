#!/usr/bin/env bash
# hermes-imagination-director one-line installer
#   curl -fsSL https://raw.githubusercontent.com/AgentSparko/hermes-imagination-director/main/install.sh | bash
# Env options: SKIP_STUDIO=1 (do not install Imagination Studio)  STUDIO_DIR=~/imagination-studio
#              IMAGINATION_MODELS="brain omni" (add "audio" for voice)  BRAIN=lfm3|keep  NO_SERVICES=1
set -euo pipefail
REPO="${IMAGINATION_REPO:-https://github.com/AgentSparko/hermes-imagination-director.git}"
REF="${IMAGINATION_REF:-main}"
PREFIX="${IMAGINATION_HOME:-$HOME/.hermes-imagination-director}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
STUDIO_REPO="${STUDIO_REPO:-https://github.com/wassermanproductions/imagination-studio.git}"
STUDIO_DIR="${STUDIO_DIR:-$HOME/imagination-studio}"
STUDIO_URL="${STUDIO_URL:-http://127.0.0.1:7870}"
BRAIN="${BRAIN:-lfm3}"
export IMAGINATION_HOME="$PREFIX"
VOICE_HOME="$HOME/.hermes-spark-voice"

log(){ printf '\033[1;32m[director]\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[director] %s\033[0m\n' "$*"; }
die(){ printf '\033[1;31m[director] %s\033[0m\n' "$*" >&2; exit 1; }

for bin in git curl python3 ffmpeg; do command -v "$bin" >/dev/null || die "$bin is required (sudo apt install -y git curl python3 python3-venv ffmpeg cmake build-essential libcurl4-openssl-dev)"; done
mkdir -p "$PREFIX/src"

SRC="$PREFIX/src/hermes-imagination-director"
if [[ -d "$SRC/.git" ]]; then git -C "$SRC" fetch -q origin && git -C "$SRC" checkout -q "$REF" && git -C "$SRC" pull -q --ff-only origin "$REF" || true
else git clone -q --branch "$REF" "$REPO" "$SRC"; fi
log "source at $SRC ($(git -C "$SRC" rev-parse --short HEAD))"

# Hermes
if ! command -v hermes >/dev/null 2>&1; then
  warn "Hermes is not installed; installing with the official script"
  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v hermes >/dev/null || die "hermes not on PATH after install; open a new shell and re-run"

# helper venv (pyyaml + hf cli for the scripts)
[[ -x "$PREFIX/venv/bin/python" ]] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip pyyaml "huggingface_hub>=0.25"
export PATH="$PREFIX/venv/bin:$PATH"

# llama.cpp + LFM3 models: reuse hermes-spark-voice's install when present
if [[ -x "$VOICE_HOME/bin/llama-server" && ! -e "$PREFIX/bin/llama-server" ]]; then
  mkdir -p "$PREFIX"; ln -sfn "$VOICE_HOME/bin" "$PREFIX/bin"; ln -sfn "$VOICE_HOME/models" "$PREFIX/models"
  log "reusing llama-server and models from $VOICE_HOME"
fi
bash "$SRC/spark/llama.sh"
bash "$SRC/spark/models.sh"

# Imagination Studio
studio_up(){ curl -fsS -m 3 "$STUDIO_URL/api/state" >/dev/null 2>&1; }
if [[ -z "${SKIP_STUDIO:-}" ]]; then
  if studio_up; then log "Imagination Studio answers at $STUDIO_URL"
  else
    if [[ ! -d "$STUDIO_DIR/.git" ]]; then git clone -q "$STUDIO_REPO" "$STUDIO_DIR"; fi
    log "installing Imagination Studio in $STUDIO_DIR (the studio's own oneshot-setup; ~100 GB of models on a Spark, this takes a while)"
    (cd "$STUDIO_DIR" && bash oneshot-setup.sh ${STUDIO_SETUP_ARGS:-}) || warn "studio setup did not finish; run 'bash $STUDIO_DIR/oneshot-setup.sh' yourself, then 'bash scripts/app.sh'"
    if [[ -f "$STUDIO_DIR/scripts/app.sh" ]] && ! studio_up; then
      (cd "$STUDIO_DIR" && STUDIO_DEFAULT_BRAIN=local STUDIO_LOCAL_ENDPOINT="http://127.0.0.1:${LFM3_BRAIN_PORT:-8183}/v1" STUDIO_LOCAL_MODEL=LFM3-1.7B nohup bash scripts/app.sh >"$PREFIX/studio.log" 2>&1 &)
      for _ in $(seq 1 30); do studio_up && break; sleep 2; done
      studio_up && log "studio started (log: $PREFIX/studio.log)" || warn "studio not answering yet at $STUDIO_URL; see $PREFIX/studio.log"
    fi
  fi
fi

# plugin + config
PLUG="$HERMES_HOME/plugins/imagination-director"
mkdir -p "$HERMES_HOME/plugins"; rm -rf "$PLUG"; cp -r "$SRC/plugin" "$PLUG"
hermes plugins enable imagination-director >/dev/null 2>&1 || true
"$PREFIX/venv/bin/python" "$SRC/scripts/configure-hermes.py" --brain "$BRAIN" --studio-url "$STUDIO_URL"
[[ -f "$PREFIX/env" ]] || sed "s|%h|$HOME|g" "$SRC/spark/env.example" > "$PREFIX/env"
mkdir -p "$PREFIX/films"

# services
if [[ -z "${NO_SERVICES:-}" ]] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  units=()
  for u in lfm3-brain lfm3-omni lfm3-audio; do
    model="${u#lfm3-}"; [[ -e "$PREFIX/models/$model.gguf" ]] || continue
    [[ -f "$HOME/.config/systemd/user/$u.service" ]] || cp "$SRC/spark/units/$u.service" "$HOME/.config/systemd/user/"
    units+=("$u")
  done
  systemctl --user daemon-reload
  systemctl --user enable --now "${units[@]}"
  loginctl enable-linger "$USER" 2>/dev/null || true
  log "services: ${units[*]}"
else
  warn "no systemd user session: start llama-server by hand (see spark/units/*.service for the flags)"
fi

cat <<EOT

$(log "done.")
  hermes director status                        # studio + LFM3 servers + last run
  hermes director run "a fox at dawn, 30 s"     # the whole pipeline, foreground
  In chat: "make a 30 second film about ..."    # the agent uses director_run / director_status
  Studio UI: $STUDIO_URL  (Brains & tools -> local = LFM3-1.7B on :8183; Agent room -> Hermes uses these tools)
EOT
