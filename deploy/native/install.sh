#!/usr/bin/env bash
# MeshHome — installer, upgrader and uninstaller for Debian-based systems
# (Raspberry Pi OS / Debian 12 "bookworm", Debian 13 "trixie"; 64-bit ARM or x86-64).
#
#   Install (latest release):
#     curl -fsSLo install.sh https://github.com/roach0816/MeshHome/releases/latest/download/install.sh
#     sudo bash install.sh
#
#   Options:
#     --version X.Y.Z    install/upgrade to a specific release (default: latest)
#     --from-file PATH   install from a local meshhome-X.Y.Z.tar.gz (offline / testing)
#     --port N           HTTP port for a fresh install (default 8080)
#     --yes              answer "yes" to every prompt (unattended)
#     --plain            plain line-by-line output instead of the full-screen dashboard
#     --upgrade          upgrade an existing install (also chosen automatically when one exists)
#     --from-request     web-UI upgrade: read the requested version from the state directory and
#                        report progress there (run by meshhome-update.service, never by hand)
#     --uninstall [--purge]   remove the app; --purge also deletes the database, config and data
#     --https            set up (or redo) HTTPS on an existing install: nginx + Let's Encrypt via
#                        DNS validation at one of 25 DNS providers (also offered when installing)
#     --https-disable    remove the HTTPS front end and serve plain HTTP again
#     --security-updates turn on Debian's automatic security updates (unattended-upgrades)
#     --radio-hat        set up the RAK6421 radio HAT on a Raspberry Pi 4/5 (ZephCore); with
#                        --remove [--purge] remove it again
#     --radio-hat-sync   after an upgrade, update the radio HAT software (run by the installer)
#     --apply-config     apply network/HTTPS settings requested from the web UI (run by
#                        meshhome-config.service, never by hand)
#     --sync-units       install helper systemd units shipped with the running release (run as
#                        root on every app start)
#     --acme-renew       renew the HTTPS certificate if it is due (run by a timer)
#     --migrate          move an install made before the rename to MeshHome (0.9 and earlier) to
#                        the new names: /opt/meshhome, the meshhome service, user and database
#
# Everything the script changes is listed on screen and confirmed first. Answering "n" to any
# confirmation cancels the installation.
set -Eeuo pipefail
umask 022

# ---- constants -----------------------------------------------------------------------------
APP_NAME="MeshHome"
REPO="${MESHHOME_REPO:-${MESHCORE_HOME_REPO:-roach0816/MeshHome}}"
# Names. Installs made before the rename to MeshHome (0.9 and earlier) keep the old ones until
# `sudo $CLI_NAME migrate` moves them (see migrate_layout). New installs use the new ones.
NEW_PREFIX=/opt/meshhome NEW_CONF_DIR=/etc/meshhome NEW_STATE_DIR=/var/lib/meshhome
NEW_SERVICE=meshhome NEW_USER=meshhome
OLD_PREFIX=/opt/meshcore-home OLD_CONF_DIR=/etc/meshcore-home OLD_STATE_DIR=/var/lib/meshcore-home
OLD_SERVICE=meshcore-home OLD_USER=meshcore
use_layout() {  # use_layout new|legacy — point every name at one layout
  if [[ $1 == legacy ]]; then
    LAYOUT=legacy SERVICE=$OLD_SERVICE APP_USER=$OLD_USER DB_NAME=$OLD_USER
    PREFIX=$OLD_PREFIX CONF_DIR=$OLD_CONF_DIR STATE_DIR=$OLD_STATE_DIR
    HAT_USER=meshcore-radio
  else
    LAYOUT=new SERVICE=$NEW_SERVICE APP_USER=$NEW_USER DB_NAME=$NEW_USER
    PREFIX=$NEW_PREFIX CONF_DIR=$NEW_CONF_DIR STATE_DIR=$NEW_STATE_DIR
    HAT_USER=meshhome-radio
  fi
  ENV_FILE=$CONF_DIR/$SERVICE.env
  BACKUP_DIR=$STATE_DIR/backups
  LOG_FILE=/var/log/$SERVICE-install.log
  CLI_NAME=$SERVICE
  CLI_LINK=/usr/local/bin/$SERVICE
  NGINX_SITE=/etc/nginx/sites-available/$SERVICE
  NGINX_ENABLED=/etc/nginx/sites-enabled/$SERVICE
  CF_CREDENTIALS=/etc/letsencrypt/$SERVICE-cloudflare.ini
  ADMIN_LOCK=/run/$SERVICE-admin.lock
  ACME_DIR=$CONF_DIR/acme ACME_CREDENTIALS=$CONF_DIR/acme/credentials.env ACME_DATA=$CONF_DIR/acme/data
  LEGO_DIR=/opt/$SERVICE-acme
  HAT_DIR=/opt/$SERVICE-radio HAT_DATA=/var/lib/$SERVICE-radio HAT_SERVICE=$SERVICE-radio
  HAT_UDEV_RULE=/etc/udev/rules.d/90-$SERVICE-radio.rules
  HAT_UNIT=/etc/systemd/system/$SERVICE-radio.service HAT_STATUS=$STATE_DIR/radio-hat.json
  HELPER_UNITS=("$SERVICE-update.service" "$SERVICE-update.path" "$SERVICE-config.service" "$SERVICE-config.path")
  ACME_UNITS=("$SERVICE-acme-renew.service" "$SERVICE-acme-renew.timer")
  STATUS_FILE=$STATE_DIR/update-status.json
  CONFIG_REQUEST=$STATE_DIR/config-request.json CONFIG_STATUS=$STATE_DIR/config-status.json
  NETWORK_SNAPSHOT=$STATE_DIR/network.json CONF_BACKUP=$CONF_DIR/.previous
  SYSTEM_EXPORT=$STATE_DIR/system-export.tar.gz SYSTEM_RESTORE=$STATE_DIR/system-restore.tar.gz
}
if [[ -d $OLD_PREFIX && ! -d $NEW_PREFIX ]]; then use_layout legacy; else use_layout new; fi
UNIT_DIR=/etc/systemd/system
DEFAULT_PORT=8080
KEEP_RELEASES=2
KEEP_BACKUPS=5
REQUIRED_PACKAGES=(python3 python3-venv postgresql postgresql-client curl ca-certificates tar)
# Optional HTTPS: nginx in front of the app with a Let's Encrypt certificate obtained by lego
# through a DNS-01 challenge at the owner's DNS provider (works for private/LAN-only hosts;
# nothing is exposed publicly). See "HTTPS certificates with lego" below.
HTTPS_PACKAGES=(nginx openssl)

# Test/mirror overrides (MESHHOME_*, or MESHCORE_HOME_* as before the rename; also read from
# $ENV_FILE on upgrades, which is root-owned).
API_URL="${MESHHOME_API:-${MESHCORE_HOME_API:-https://api.github.com}}"
DOWNLOAD_BASE="${MESHHOME_DOWNLOAD_BASE:-${MESHCORE_HOME_DOWNLOAD_BASE:-}}"

# ---- arguments -----------------------------------------------------------------------------
WANT_VERSION="" FROM_FILE="" PORT="" PLAIN=0 HAT_REMOVE=0 ASSUME_YES=0 MODE="" FROM_REQUEST=0 PURGE=0 REPAIR=0 CONFIG_MODE=0
HTTPS_HOST="${MESHHOME_HTTPS_HOST:-${MESHCORE_HOME_HTTPS_HOST:-}}" HTTPS_EMAIL="${MESHHOME_HTTPS_EMAIL:-${MESHCORE_HOME_HTTPS_EMAIL:-}}"
while (($#)); do
  case "$1" in
    --version) WANT_VERSION="${2:?--version needs a value}"; shift ;;
    --from-file) FROM_FILE="${2:?--from-file needs a path}"; shift ;;
    --port) PORT="${2:?--port needs a value}"; shift ;;
    --yes|-y) ASSUME_YES=1 ;;
    --plain) PLAIN=1 ;;
    --upgrade) MODE=upgrade ;;
    --from-request) MODE=upgrade; FROM_REQUEST=1; ASSUME_YES=1 ;;
    --uninstall) MODE=uninstall ;;
    --https) MODE=https ;;
    --https-disable) MODE="https-disable" ;;
    --https-host) HTTPS_HOST="${2:?--https-host needs a value}"; shift ;;
    --security-updates) MODE=security-updates ;;
    --radio-hat) MODE=radio-hat ;;
    --remove) HAT_REMOVE=1 ;;
    --radio-hat-sync) MODE=radio-hat-sync; CONFIG_MODE=1; ASSUME_YES=1 ;;
    --apply-config) MODE=apply-config; CONFIG_MODE=1; ASSUME_YES=1 ;;
    --sync-units) MODE=sync-units; CONFIG_MODE=1; ASSUME_YES=1 ;;
    --acme-renew) MODE="acme-renew"; CONFIG_MODE=1; ASSUME_YES=1 ;;
    --migrate) MODE=migrate ;;
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,/^[^#]/p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed -e '$d' -e 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

# ---- terminal UI ---------------------------------------------------------------------------
# An interactive terminal gets a full-screen dashboard (on the alternate screen, like `less` or
# `top`): the checklist of steps, what is happening now, and one overall progress bar, all redrawn
# in place. Explanations and questions appear in a panel on the same screen. When it ends, a short
# summary is printed to the normal terminal so it stays in the scrollback.
# Everything else (web-UI runs, pipes, --plain, small or "dumb" terminals) gets plain line output.
if [[ -t 1 && $FROM_REQUEST -eq 0 && $CONFIG_MODE -eq 0 ]]; then
  B=$'\e[1m' D=$'\e[2m' R=$'\e[31m' G=$'\e[32m' Y=$'\e[33m' C=$'\e[36m' N=$'\e[0m'
else
  B="" D="" R="" G="" Y="" C="" N=""
fi
TTY_IN=/dev/tty
TARGET_VERSION=""
STEP=0 STEPS=0 STEP_TITLES=() STEP_STATE=() STEP_NOTE=() STEP_WEIGHTS=()
RECENT=() WARNINGS=() PANEL=() REPLY_TEXT=""
ACTIVITY="" ACT_PCT=-1 ACT_SINCE=0 BUSY=0 SPIN_I=0 ALL_DONE=0
UI_TITLE="Installer" PROMPT_Q="" PROMPT_ERR="" PROMPT_ROW=1 PROMPT_COL=1
ROWS=24 COLS=80 TUI=0 TUI_ON=0 TUI_RESIZED=0 TUI_LAST=0 STTY_SAVED=""

# Character counts below assume a UTF-8 locale; minimal systems often have none configured,
# but glibc always provides C.UTF-8. Only character handling changes (messages stay as they are).
if [[ $(locale charmap 2>/dev/null || true) != UTF-8 && $'\n'"$(locale -a 2>/dev/null || true)"$'\n' == *$'\n'C.[uU][tT][fF]8$'\n'* ]]; then
  if [[ -n ${LC_ALL:-} ]]; then export LC_ALL=C.UTF-8; else export LC_CTYPE=C.UTF-8; fi
fi

SPIN=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
repeat() { local i out=""; for ((i = 0; i < $2; i++)); do out+="$1"; done; printf '%s' "$out"; }
repv() {  # repv VAR CHAR N — like repeat, into VAR without a subshell (the dashboard redraws often)
  local -n _rep=$1
  printf -v _rep '%*s' "$3" ''
  _rep=${_rep// /$2}
}

tui_size() {
  local r c
  if read -r r c < <(stty size <"$TTY_IN" 2>/dev/null) && [[ $r =~ ^[0-9]+$ && $c =~ ^[0-9]+$ ]] && ((r > 0 && c > 0)); then
    ROWS=$r COLS=$c
  fi
}

if [[ -t 1 && $FROM_REQUEST -eq 0 && $CONFIG_MODE -eq 0 && $PLAIN -eq 0 && ${TERM:-dumb} != dumb ]] &&
  { : <"$TTY_IN"; } 2>/dev/null; then
  tui_size
  ((ROWS >= 20 && COLS >= 64)) && TUI=1
fi

log() { strip_ansi "$*"; printf '%s %s\n' "$(date '+%F %T')" "$STRIPPED" >>"$LOG_FILE"; }
say() { printf '%s\n' "$*"; log "$*"; }
remember() {  # remember LINE — shown under the current step on the dashboard (last three kept)
  RECENT+=("$1")
  ((${#RECENT[@]} <= 3)) || RECENT=("${RECENT[@]: -3}")
  tui_draw
}
ok() { if ((TUI_ON)); then log "  ok: $*"; remember "${G}✓${N} $*"; else say "  ${G}✓${N} $*"; fi; }
info() { if ((TUI_ON)); then log "  $*"; remember "${C}•${N} $*"; else say "  ${C}•${N} $*"; fi; }
warn() {
  if ((TUI_ON)); then log "  warning: $*"; WARNINGS+=("$*"); remember "${Y}!${N} $*"; else say "  ${Y}!${N} $*"; fi
}
note() {  # note TEXT — explanation shown with the next question (plain output: printed now)
  log "  $*"
  if ((TUI_ON)); then PANEL+=("$*"); else printf '  %s\n' "$*"; fi
}
note_row() {  # note_row TEXT — like note, but a table row: kept on one line (cut to fit), never wrapped
  log "  $*"
  if ((TUI_ON)); then PANEL+=($'\x01'"$*"); else printf '  %s\n' "$*"; fi
}
activity() {  # activity TEXT [PERCENT] — what is happening now (shown above the progress bar)
  ACTIVITY=$1 ACT_PCT=${2:--1} ACT_SINCE=$SECONDS
  tui_draw
}
step_note() { ((STEP > 0)) || return 0; STEP_NOTE[STEP - 1]=$1; tui_draw; }  # short result beside the current step
step_skip() {  # step_skip [NOTE] — mark the current step as skipped
  ((STEP > 0)) || return 0
  STEP_STATE[STEP - 1]=skipped
  [[ -z ${1:-} ]] || STEP_NOTE[STEP - 1]=$1
  tui_draw
}

# ---- drawing ---------------------------------------------------------------------------------
VL=0 FIT="" STRIPPED=""
# Only our own colour codes appear on screen: ESC [ one or two digits m.
strip_ansi() { STRIPPED=${1//$'\e['[0-9]m/}; STRIPPED=${STRIPPED//$'\e['[0-9][0-9]m/}; }
vis_len() { strip_ansi "$1"; VL=${#STRIPPED}; }
fit() {  # fit TEXT WIDTH — sets FIT to TEXT cut to WIDTH visible columns
  vis_len "$1"
  if ((VL <= $2)); then FIT=$1; return; fi
  strip_ansi "$1"
  FIT="${STRIPPED:0:$(($2 - 1))}…"
}
pad_to() {  # pad_to TEXT WIDTH — sets FIT to TEXT padded with spaces to WIDTH visible columns
  vis_len "$1"
  FIT=$1
  local pad
  ((VL >= $2)) || { repv pad ' ' $(($2 - VL)); FIT+=$pad; }
}
WRAPPED=()
wrap() {  # wrap TEXT WIDTH — appends the wrapped lines of TEXT to WRAPPED (hanging indent)
  vis_len "$1"
  if ((VL <= $2)); then WRAPPED+=("$1"); return; fi
  # Wrap the plain text at spaces, keeping runs of spaces (aligned columns) intact.
  strip_ansi "$1"
  local s=$STRIPPED lead="" indent head cut
  [[ $s =~ ^(\ *(• )?) ]] && lead=${BASH_REMATCH[1]}
  repv indent ' ' ${#lead}
  while ((${#s} > $2)); do
    head=${s:0:$(($2 + 1))}
    cut=${head% *}
    ((${#cut} > ${#indent})) || cut=${s:0:$2}  # one long word: hard break
    WRAPPED+=("${cut%"${cut##*[! ]}"}")
    s=${s:${#cut}}
    s="$indent${s#"${s%%[! ]*}"}"
  done
  [[ -z ${s// /} ]] || WRAPPED+=("$s")  # nothing left when the text ended exactly at the edge
}

title_col() {  # the column where step notes start: just past the longest step title
  local t; TITLE_COL=0
  for t in "${STEP_TITLES[@]}"; do ((${#t} > TITLE_COL)) && TITLE_COL=${#t}; done
  TITLE_COL=$((TITLE_COL + 9))
}
TITLE_COL=46

progress_pct() {  # overall progress across the weighted steps, 0-100
  local i w total=0 before=0 cur=0
  ((ALL_DONE)) && { PCT=100; return; }
  for ((i = 0; i < STEPS; i++)); do
    w=${STEP_WEIGHTS[i]:-1}; total=$((total + w))
    ((i < STEP - 1)) && before=$((before + w))
  done
  ((STEP > 0)) && cur=${STEP_WEIGHTS[STEP - 1]:-1}
  ((total > 0)) || { PCT=0; return; }
  local frac=$ACT_PCT; ((frac < 0)) && frac=0; ((frac > 100)) && frac=100
  PCT=$(((before * 100 + cur * frac) / total))
}

tui_draw() {  # redraw the whole screen in place (cheap: a couple of kilobytes)
  ((TUI_ON)) || return 0
  if ((TUI_RESIZED)); then TUI_RESIZED=0; tui_size; fi
  local w=$COLS h=$ROWS i line icon title body_rows
  local -a body=()
  body_rows=$((h - 6))

  if [[ -n $PROMPT_Q ]]; then
    # Question panel: the step, its explanation, then the question with the cursor after it.
    if ((STEPS > 1)); then
      body+=("  ${C}${B}Step $STEP of $STEPS${N}${B} · ${STEP_TITLES[STEP - 1]}${N}" "")
    elif ((STEPS == 1)); then
      body+=("  ${B}${STEP_TITLES[0]}${N}" "")
    fi
    WRAPPED=()
    for line in "${PANEL[@]}"; do
      if [[ $line == $'\x01'* ]]; then fit "  ${line:1}" $((w - 2)); WRAPPED+=("$FIT"); else wrap "  $line" $((w - 2)); fi
    done
    local room=$((body_rows - ${#body[@]} - 3))
    if ((${#WRAPPED[@]} > room)); then
      local more=$((${#WRAPPED[@]} - room + 1))
      WRAPPED=("${WRAPPED[@]:0:$((room - 1))}" "  ${D}… $more more line(s) in $LOG_FILE${N}")
    fi
    body+=("${WRAPPED[@]}")
    ((${#PANEL[@]})) && body+=("")
    [[ -z $PROMPT_ERR ]] || body+=("  ${Y}${PROMPT_ERR}${N}")
    fit "  ${B}${PROMPT_Q}${N} " $((w - 12))
    body+=("$FIT")
    vis_len "$FIT"
    PROMPT_ROW=$((2 + ${#body[@]})) PROMPT_COL=$((VL + 1))
  else
    local show_recent=1 extra=${#RECENT[@]}
    ((STEPS + extra <= body_rows)) || show_recent=0
    for ((i = 0; i < STEPS; i++)); do
      case ${STEP_STATE[i]} in
        done) icon="${G}✓${N}" title=${STEP_TITLES[i]} ;;
        active) icon="${C}${SPIN[SPIN_I % 10]}${N}" title="${B}${STEP_TITLES[i]}${N}" ;;
        skipped) icon="${D}–${N}" title="${D}${STEP_TITLES[i]}${N}" ;;
        failed) icon="${R}✗${N}" title="${R}${STEP_TITLES[i]}${N}" ;;
        *) icon="${D}○${N}" title="${D}${STEP_TITLES[i]}${N}" ;;
      esac
      pad_to "   $icon  $title" "$TITLE_COL"
      line=$FIT
      [[ -z ${STEP_NOTE[i]:-} ]] || line+="${D}${STEP_NOTE[i]}${N}"
      fit "$line" "$w"; body+=("$FIT")
      if ((show_recent)) && [[ ${STEP_STATE[i]} == active ]]; then
        for line in "${RECENT[@]}"; do fit "        $line" "$w"; body+=("$FIT"); done
      fi
    done
  fi

  # Footer: activity, the progress bar, the log location.
  progress_pct
  local bw=$((w - 30)); ((bw > 50)) && bw=50
  local filled=$((PCT * bw / 100)) act=$ACTIVITY
  ((ACT_PCT >= 0 && ACT_PCT < 100 && STEPS > 0)) && act+=" ${D}${ACT_PCT}%${N}"
  if ((BUSY)); then act="${C}${SPIN[SPIN_I % 10]}${N} $act ${D}($((SECONDS - ACT_SINCE))s)${N}"; fi
  local steptxt=""; ((STEPS > 1 && STEP > 0)) && steptxt="Step $STEP of $STEPS"
  fit "  $act" "$w"; local f1=$FIT
  local f2 full empty
  repv full █ "$filled"; repv empty ░ $((bw - filled))
  printf -v f2 '  %s%s%s%s%s %3d%%  %s%s%s' "$G" "$full" "$D" "$empty" "$N" "$PCT" "$D" "$steptxt" "$N"
  fit "  ${D}Full log: $LOG_FILE${N}" "$w"; local f3=$FIT

  local header right=""
  [[ -z $TARGET_VERSION ]] || right="v$TARGET_VERSION"
  pad_to " ${B}${APP_NAME}${N} ${D}·${N} $UI_TITLE" $((w - ${#right} - 1))
  header="$FIT$right"
  local rule; repv rule ─ "$w"; rule="${D}${rule}${N}"

  local out=$'\e[H'"$header"$'\e[K\n'"$rule"$'\e[K\n'
  for ((i = 0; i < body_rows; i++)); do out+="${body[i]:-}"$'\e[K\n'; done
  out+="$rule"$'\e[K\n'"$f1"$'\e[K\n'"$f2"$'\e[K\n'"$f3"$'\e[K\e[J'
  printf '%s' "$out" >&8
  TUI_LAST=${EPOCHREALTIME/./}
}
tui_tick() {  # redraw at most ~12 times a second (for fast progress sources such as apt)
  ((TUI_ON)) || return 0
  local now=${EPOCHREALTIME/./}
  ((now - TUI_LAST < 80000)) || { SPIN_I=$((SPIN_I + 1)); tui_draw; }
}

tui_start() {  # tui_start TITLE — enter the full-screen dashboard (if this terminal can show it)
  UI_TITLE=$1
  ((TUI && !TUI_ON)) || return 0
  exec 8>&1
  STTY_SAVED=$(stty -g <"$TTY_IN" 2>/dev/null || true)
  stty -echo <"$TTY_IN" 2>/dev/null || true  # stray keypresses would scribble over the screen
  TUI_ON=1
  trap 'TUI_RESIZED=1' WINCH
  printf '\e[?1049h\e[?25l\e[H\e[2J' >&8
  tui_draw
}

tui_end() {  # leave the dashboard and print a short summary to the normal terminal
  ((TUI_ON)) || return 0
  TUI_ON=0
  trap - WINCH
  printf '\e[?25h\e[?1049l' >&8
  [[ -z $STTY_SAVED ]] || stty "$STTY_SAVED" <"$TTY_IN" 2>/dev/null || true
  local i icon last=-1
  printf '\n  %s%s%s · %s%s\n' "$B" "$APP_NAME" "$N" "$UI_TITLE" "${TARGET_VERSION:+  ${D}v$TARGET_VERSION${N}}"
  for ((i = 0; i < STEPS; i++)); do
    case ${STEP_STATE[i]} in
      done) icon="${G}✓${N}" ;;
      skipped) icon="${D}–${N}" ;;
      failed) icon="${R}✗${N}" ;;
      active) icon="${Y}•${N}" ;;
      *) continue ;;
    esac
    last=$i
    pad_to "    $icon ${STEP_TITLES[i]}" "$TITLE_COL"
    printf '%s%s%s%s\n' "$FIT" "$D" "${STEP_NOTE[i]:-}" "$N"
  done
  # The last step's own messages (e.g. why it stopped) are worth keeping on screen.
  local shown=""
  if ((last >= 0)) && [[ ${STEP_STATE[last]} != "done" ]]; then
    for i in "${RECENT[@]}"; do printf '        %s\n' "$i"; shown+="$i"$'\n'; done
  fi
  for i in "${WARNINGS[@]}"; do
    [[ $shown == *"$i"* ]] || printf '  %s!%s %s\n' "$Y" "$N" "$i"
  done
}

wait_busy() {  # wait_busy PID [PROBE] — animate until PID exits; returns its exit status
  local pid=$1 probe=${2:-} t=0
  BUSY=1
  while kill -0 "$pid" 2>/dev/null; do
    if [[ -n $probe ]] && ((t % 8 == 0)); then "$probe" || true; fi
    t=$((t + 1)) SPIN_I=$((SPIN_I + 1))
    tui_draw
    sleep 0.12
  done
  BUSY=0
  wait "$pid"
}

# ---- questions -------------------------------------------------------------------------------
ask_line() {  # ask_line QUESTION SECRET(0/1) [ERROR] — reads one line from the terminal into REPLY_TEXT
  REPLY_TEXT=""
  if ((!TUI_ON)); then
    [[ -z ${3:-} ]] || printf '  %s\n' "$3" >/dev/tty
    printf '\n  %s%s%s ' "$B" "$1" "$N" >/dev/tty
    if (($2)); then read -rs REPLY_TEXT <"$TTY_IN" || true; printf '\n' >/dev/tty; else read -r REPLY_TEXT <"$TTY_IN" || true; fi
    return 0
  fi
  local saved=$ACTIVITY saved_pct=$ACT_PCT rc
  PROMPT_Q=$1 PROMPT_ERR=${3:-} ACTIVITY="Waiting for your answer" ACT_PCT=-1 BUSY=0
  while true; do
    tui_draw
    printf '\e[%d;%dH\e[?25h' "$PROMPT_ROW" "$PROMPT_COL" >&8
    stty echo <"$TTY_IN" 2>/dev/null || true
    rc=0
    if (($2)); then read -rs REPLY_TEXT <"$TTY_IN" || rc=$?; else read -r REPLY_TEXT <"$TTY_IN" || rc=$?; fi
    stty -echo <"$TTY_IN" 2>/dev/null || true
    printf '\e[?25l' >&8
    # A window resize interrupts read (status > 128): redraw and ask again.
    if ((rc > 128 && TUI_RESIZED)); then continue; fi
    break
  done
  PROMPT_Q="" PROMPT_ERR="" ACTIVITY=$saved ACT_PCT=$saved_pct
  PANEL=()
  tui_draw
}

cancelled() {  # cancelled QUESTION — the user answered "n" to a confirmation
  log "cancelled at: $1"
  ((STEP == 0)) || { STEP_STATE[STEP - 1]=skipped; STEP_NOTE[STEP - 1]="Cancelled"; }
  tui_end
  printf '\n  %sCancelled.%s Nothing further was changed.\n\n' "$Y" "$N"
  exit 1
}

confirm() {  # confirm "Question" — Y/n; "n" cancels the whole run
  local err=""
  if ((ASSUME_YES)); then log "auto-yes: $1"; PANEL=(); return 0; fi
  if [[ ! -r $TTY_IN ]] || ! { : <"$TTY_IN"; } 2>/dev/null; then
    die "No terminal available to ask: $1" "Run the installer from a terminal, or pass --yes."
  fi
  while true; do
    ask_line "$1 [Y/n]" 0 "$err"
    case "${REPLY_TEXT,,}" in
      ""|y|yes) log "confirmed: $1"; PANEL=(); return 0 ;;
      n|no) cancelled "$1" ;;
      *) err="Please answer y or n." ;;
    esac
  done
}

ask_yn() {  # ask_yn "Question" y|n — returns 0 for yes; never cancels the run
  local def=$2 err=""
  if ((ASSUME_YES)) || [[ ! -r $TTY_IN ]]; then PANEL=(); [[ $def == y ]]; return; fi
  while true; do
    ask_line "$1 $([[ $def == y ]] && echo '[Y/n]' || echo '[y/N]')" 0 "$err"
    case "${REPLY_TEXT,,}" in
      "") log "answer ($def): $1"; [[ $def == y ]]; return ;;
      y|yes) log "answer (y): $1"; return 0 ;;
      n|no) log "answer (n): $1"; return 1 ;;
      *) err="Please answer y or n." ;;
    esac
  done
}

ask() {  # ask VAR "Question" DEFAULT [ERROR] — free-text answer into VAR (default under --yes)
  local -n _ask_var=$1
  if ((ASSUME_YES)) || [[ ! -r $TTY_IN ]]; then _ask_var=$3; PANEL=(); return; fi
  ask_line "$2${3:+ [$3]}" 0 "${4:-}"
  _ask_var=${REPLY_TEXT:-$3}
}

ask_secret() {  # ask_secret VAR "Prompt" — hidden input (not echoed, not logged)
  local -n _secret_var=$1
  _secret_var=""
  [[ -r $TTY_IN ]] || return 0
  ask_line "$2" 1
  _secret_var=$REPLY_TEXT
}

# ---- steps and failures ----------------------------------------------------------------------
status() {  # status STATE MESSAGE — progress for web-UI requests (read by the app)
  ((FROM_REQUEST || CONFIG_MODE)) || return 0
  python3 - "$STATUS_FILE" "$1" "$TARGET_VERSION" "$2" "$LOG_FILE" <<'PY' || true
import json, os, sys, time
path, state, version, message, logf = sys.argv[1:6]
try:
    tail = open(logf, errors="replace").read().splitlines()[-15:]
except OSError:
    tail = []
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump({"state": state, "version": version, "message": message, "log_tail": tail,
               "updated_at": time.time()}, f)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
}

die() {  # die MESSAGE [HINT] [tail] — stop; "tail" also shows the end of the log
  local msg="$1" hint="${2:-}"
  ((MIGRATING == 0)) || migrate_undo
  ((STEP == 0)) || STEP_STATE[STEP - 1]=failed
  tui_end
  printf '\n  %s✗ %s%s\n' "$R$B" "$msg" "$N" >&2
  if [[ ${3:-} == tail ]]; then tail -n 15 "$LOG_FILE" | sed 's/^/    /' >&2; fi
  [[ -n $hint ]] && printf '    %s\n' "$hint" >&2
  printf '    %sFull log: %s%s\n\n' "$D" "$LOG_FILE" "$N" >&2
  log "FAILED: $msg"
  status failed "$msg"
  exit 1
}
on_err() {
  # Command substitutions inherit this trap (set -E); let the main shell report the failure once.
  ((BASH_SUBSHELL == 0)) || return 0
  die "Unexpected error on line $1" "Re-run the installer; it is safe to run again."
}
WORK=""
on_exit() {
  tui_end
  [[ -z $WORK ]] || rm -rf "$WORK"
}
trap 'on_err $LINENO' ERR
trap 'printf "\n"; die "Interrupted"' INT
trap on_exit EXIT

banner() {
  ((TUI)) && return 0  # the dashboard has its own header
  printf '\n%s' "$C$B"
  cat <<'ART'
   __  __           _      ____                  _   _
  |  \/  | ___  ___| |__  / ___|___  _ __ ___   | | | | ___  _ __ ___   ___
  | |\/| |/ _ \/ __| '_ \| |   / _ \| '__/ _ \  | |_| |/ _ \| '_ ` _ \ / _ \
  | |  | |  __/\__ \ | | | |__| (_) | | |  __/  |  _  | (_) | | | | | |  __/
  |_|  |_|\___||___/_| |_|\____\___/|_|  \___|  |_| |_|\___/|_| |_| |_|\___|
ART
  printf '%s\n' "$N"
}

plan() {  # plan "Title 1" "Title 2" ... — declares the steps (set STEP_WEIGHTS first to weight them)
  STEP_TITLES=("$@"); STEPS=$#; STEP=0; STEP_STATE=(); STEP_NOTE=()
  local t i=1
  for t in "$@"; do STEP_STATE+=(pending); STEP_NOTE+=(""); done
  title_col
  if ((TUI_ON)); then tui_draw; return; fi
  printf '  %sThis will:%s\n' "$B" "$N"
  for t in "$@"; do printf '    %s%d.%s %s\n' "$D" "$i" "$N" "$t"; i=$((i + 1)); done
  printf '\n'
}

step() {  # step — finish the current step and start the next declared one
  if ((STEP > 0)) && [[ ${STEP_STATE[STEP - 1]} == active ]]; then STEP_STATE[STEP - 1]="done"; fi
  STEP=$((STEP + 1))
  STEP_STATE[STEP - 1]=active
  RECENT=()
  local title="${STEP_TITLES[$((STEP - 1))]}"
  log "== Step $STEP/$STEPS: $title"
  if ((TUI_ON)); then activity "$title"; return; fi
  local width=24 filled
  filled=$((STEP * width / STEPS))
  printf '\n%s[%s%s]%s %sStep %d of %d%s  %s%s%s\n' "$C" \
    "$(repeat █ "$filled")" "$(repeat ░ $((width - filled)))" "$N" \
    "$D" "$STEP" "$STEPS" "$N" "$B" "$title" "$N"
}

steps_done() {  # mark the run complete (100%)
  if ((STEP > 0)) && [[ ${STEP_STATE[STEP - 1]} == active ]]; then STEP_STATE[STEP - 1]="done"; fi
  ALL_DONE=1 ACTIVITY="Done" ACT_PCT=100 BUSY=0
  tui_draw
}

# ---- running commands ------------------------------------------------------------------------
run() {  # run "Message" cmd... — runs quietly (output goes to the log) with live progress
  local msg="$1"; shift
  log "\$ $*"
  if ((TUI_ON)); then
    activity "$msg"
    "$@" >>"$LOG_FILE" 2>&1 &
    if wait_busy $! "${RUN_PROBE:-}"; then ok "$msg ${D}($((SECONDS - ACT_SINCE))s)${N}"; return; fi
    die "$msg failed" "" tail
  fi
  if [[ ! -t 1 || $FROM_REQUEST -eq 1 || $CONFIG_MODE -eq 1 ]]; then
    "$@" >>"$LOG_FILE" 2>&1 || die "$msg failed" "" tail
    ok "$msg"; return
  fi
  "$@" >>"$LOG_FILE" 2>&1 &
  local pid=$! i=0 start=$SECONDS
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r  %s%s%s %s %s(%ss)%s' "$C" "${SPIN[i++ % 10]}" "$N" "$msg" "$D" $((SECONDS - start)) "$N"
    sleep 0.1
  done
  printf '\r\e[K'
  wait "$pid" || die "$msg failed" "" tail
  ok "$msg ${D}($((SECONDS - start))s)${N}"
}

run_bg() {  # run_bg "Message" cmd... — like run, but returns the status instead of stopping
  local msg="$1" rc=0; shift
  log "\$ $*"
  if ((TUI_ON)); then
    activity "$msg"
    "$@" >>"$LOG_FILE" 2>&1 &
    wait_busy $! || rc=$?
  else
    "$@" >>"$LOG_FILE" 2>&1 || rc=$?
  fi
  return "$rc"
}

bar() {  # bar PERCENT LABEL — progress of the current activity (apt, downloads)
  local pct=${1%.*}
  [[ $pct =~ ^[0-9]+$ ]] || return 0
  ((pct > 100)) && pct=100
  if ((TUI_ON)); then
    ACT_PCT=$pct
    [[ -z ${2:-} ]] || ACTIVITY=$2
    tui_tick
    return 0
  fi
  local width=30 filled=$((pct * 30 / 100))
  printf '\r  %s%s%s%s%s %3d%% %s\e[K' "$G" "$(repeat █ "$filled")" "$D" "$(repeat ░ $((width - filled)))" "$N" "$pct" "${2:0:40}"
}

# ---- helpers -------------------------------------------------------------------------------
installed_version() { if [[ -f $PREFIX/current/VERSION ]]; then tr -d '[:space:]' <"$PREFIX/current/VERSION"; fi; }

version_gt() {  # version_gt A B — true if A > B (semver X.Y.Z)
  [[ "$1" != "$2" && "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" == "$1" ]]
}

valid_version() { [[ $1 =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; }

env_get() { if [[ -f $ENV_FILE ]]; then sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1; fi; }

json_field() {  # json_field FIELD < json
  python3 -c 'import json,sys; v=json.load(sys.stdin).get(sys.argv[1]); print("" if v is None else v)' "$1"
}

download_base() { printf '%s' "${DOWNLOAD_BASE:-https://github.com/$REPO/releases/download}"; }

latest_version() {  # parsed with sed so it works before python3 is installed
  local tag
  tag=$(curl -fsSL --max-time 20 -H 'Accept: application/vnd.github+json' \
    "$API_URL/repos/$REPO/releases/latest" 2>>"$LOG_FILE" |
    sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\{0,1\}\([0-9][0-9.]*\)".*/\1/p' | head -n1) || true
  valid_version "$tag" && printf '%s' "$tag"
}

missing_packages() {  # missing_packages PKG... — prints those not installed
  local p
  for p in "$@"; do
    [[ "$(dpkg-query -W -f='${db:Status-Status}' "$p" 2>/dev/null || true)" == installed ]] || printf '%s\n' "$p"
  done
}

lan_addresses() { hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | head -n 3; }

wait_healthy() {  # wait_healthy PORT EXPECTED_VERSION TIMEOUT
  local port=$1 want=$2 deadline=$((SECONDS + $3)) v restarts base
  # Give up early if systemd is crash-looping the service. Count restarts from now: before
  # systemd 253 (Debian 12) a manual restart does not reset NRestarts, so a failed upgrade's
  # restarts would otherwise make the rolled-back version look broken too.
  base=$(systemctl show -p NRestarts --value "$SERVICE" 2>/dev/null || echo 0)
  [[ $base =~ ^[0-9]+$ ]] || base=0
  while ((SECONDS < deadline)); do
    restarts=$(systemctl show -p NRestarts --value "$SERVICE" 2>/dev/null || echo 0)
    [[ $restarts =~ ^[0-9]+$ ]] || restarts=$base
    if ((restarts - base >= 2)) || systemctl is-failed --quiet "$SERVICE"; then
      log "service is crash-looping (restarts=$restarts)"
      return 1
    fi
    if curl -fsS --max-time 3 "http://127.0.0.1:$port/health/ready" >/dev/null 2>&1; then
      v=$(curl -fsS --max-time 3 "http://127.0.0.1:$port/api/setup/status" 2>/dev/null | json_field version || true)
      [[ -z $want || $v == "$want" ]] && return 0
    fi
    sleep 2
  done
  return 1
}

# ---- steps ---------------------------------------------------------------------------------
preflight() {

  [[ -r /etc/os-release ]] || die "Cannot identify this operating system"
  # shellcheck disable=SC1091
  . /etc/os-release
  local arch; arch=$(uname -m)
  case "${ID:-}:${VERSION_CODENAME:-}" in
    debian:bookworm|debian:trixie|raspbian:bookworm|raspbian:trixie) ok "Operating system: ${PRETTY_NAME:-$ID}"; step_note "${PRETTY_NAME:-$ID}" ;;
    *)
      if [[ " ${ID_LIKE:-} " == *" debian "* || ${ID:-} == debian ]]; then
        warn "Untested OS: ${PRETTY_NAME:-unknown}. Supported: Raspberry Pi OS / Debian 12 (bookworm) and 13 (trixie)."
        confirm "Continue anyway?"
      else
        die "Unsupported operating system: ${PRETTY_NAME:-unknown}" "This installer supports Debian-based systems only."
      fi ;;
  esac
  case "$arch" in
    aarch64|x86_64) ok "Architecture: $arch"; step_note "${STEP_NOTE[STEP - 1]:-} · $arch" ;;
    armv7l|armv6l) die "A 64-bit operating system is required (found 32-bit $arch)" \
      "Re-image the Pi with Raspberry Pi OS (64-bit) using Raspberry Pi Imager." ;;
    *) die "Unsupported CPU architecture: $arch" ;;
  esac
  command -v systemctl >/dev/null && [[ -d /run/systemd/system ]] || die "systemd is required but not running"
  ok "systemd is running"

  local mem_mb disk_mb
  mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
  ((mem_mb >= 900)) && ok "Memory: ${mem_mb} MB" || warn "Memory: ${mem_mb} MB (1 GB or more recommended)"
  disk_mb=$(df -Pm / | awk 'NR==2 {print $4}')
  ((disk_mb >= 1500)) || die "Not enough free disk space (${disk_mb} MB free, 1.5 GB needed)"
  ok "Free disk space: ${disk_mb} MB"

  if [[ -z $FROM_FILE ]]; then
    if ! command -v curl >/dev/null; then
      info "curl is needed to download $APP_NAME but is not installed."
      note "${B}This will run:${N} apt-get update && apt-get install curl ca-certificates"
      confirm "Install curl?"
      run "Refreshing package lists" apt-get update
      run "Installing curl" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl ca-certificates
    fi
    activity "Checking internet access to GitHub"
    curl -fsS --max-time 15 -o /dev/null "$API_URL" >>"$LOG_FILE" 2>&1 ||
      die "Cannot reach $API_URL" "Check the Pi's internet connection and DNS."
    ok "Internet access to GitHub"
  fi
  local cur; cur=$(installed_version || true)
  [[ $MODE == upgrade && -n $cur ]] && info "Installed: v$cur"
  return 0
}

choose_version() {
  local cur; cur=$(installed_version || true)
  if [[ -n $FROM_FILE ]]; then
    [[ -f $FROM_FILE ]] || die "File not found: $FROM_FILE"
    TARGET_VERSION=$(basename "$FROM_FILE" | sed -n 's/^\(meshhome\|meshcore-home\)-\([0-9.]*\)\.tar\.gz$/\2/p')
    valid_version "$TARGET_VERSION" || die "Unexpected package name: $(basename "$FROM_FILE")"
    ok "Package: $FROM_FILE (v$TARGET_VERSION)"; step_note "v$TARGET_VERSION (local file)"
  else
    if [[ -n $WANT_VERSION ]]; then
      TARGET_VERSION=${WANT_VERSION#v}
      valid_version "$TARGET_VERSION" || die "Invalid version: $WANT_VERSION"
    else
      activity "Looking up the latest release"
      TARGET_VERSION=$(latest_version) || die "Could not look up the latest release of $REPO"
    fi
    step_note "v$TARGET_VERSION"
    ok "Release: v$TARGET_VERSION  ${D}https://github.com/$REPO/releases/tag/v$TARGET_VERSION${N}"
  fi
  if [[ $MODE == upgrade ]]; then
    if [[ $TARGET_VERSION == "$cur" ]]; then
      local port; port=$(env_get PORT); port=${port:-$DEFAULT_PORT}
      if systemctl is-active --quiet "$SERVICE" && wait_healthy "$port" "$cur" 5; then
        ok "v$cur is already installed and running — nothing to do."
        status "done" "Already up to date (v$cur)"
        exit 0
      fi
      ((FROM_REQUEST)) && die "v$cur is installed but not running" "Repair it from a terminal: sudo $CLI_NAME update"
      warn "v$cur is installed but not running correctly."
      confirm "Repair the installation by reinstalling v$cur?"
      REPAIR=1
      step_note "Repair v$cur"
      return
    fi
    version_gt "$TARGET_VERSION" "$cur" ||
      die "v$TARGET_VERSION is older than the installed v$cur" "Downgrades are not supported by the installer."
    info "Upgrade: v$cur → v$TARGET_VERSION"; step_note "v$cur → v$TARGET_VERSION"
  fi
}

install_packages() {
  local missing; mapfile -t missing < <(missing_packages "${REQUIRED_PACKAGES[@]}")
  if ((${#missing[@]} == 0)); then ok "All required system packages are already installed"; step_note "Already installed"; return; fi
  ((FROM_REQUEST)) && die "This version needs new system packages (${missing[*]})" \
    "Upgrade from a terminal instead: sudo $CLI_NAME update"
  apt_review_install "Install these packages?" cancel "${missing[@]}"
  step_note "${#missing[@]} installed"
}

# apt_review_install QUESTION ON_DECLINE PKG... — show exactly what apt will install (with
# dependencies, versions and sizes), ask, then install with a progress bar.
# ON_DECLINE: "cancel" ends the installer; "skip" returns 1 so the caller can carry on without.
apt_review_install() {
  local question=$1 on_decline=$2; shift 2
  local -a missing=("$@")

  info "Refreshing package lists (apt-get update) to see exactly what would be installed…"
  run "Refreshing package lists" apt-get update

  # Simulate to list every package apt would add, including dependencies.
  local sim
  if ! sim=$(LC_ALL=C apt-get -s install --no-install-recommends "${missing[@]}" 2>>"$LOG_FILE"); then
    die "apt cannot install: ${missing[*]}" "See the log; your package sources may be incomplete."
  fi
  local -a inst; mapfile -t inst < <(sed -n 's/^Inst \([^ ]*\) .*(\([^ ]*\) .*/\1 \2/p' <<<"$sim")
  local size; size=$({ LC_ALL=C apt-get install --no-install-recommends --assume-no "${missing[@]}" 2>/dev/null || true; } |
    sed -n 's/^\(Need to get .*\)\.$/\1/p;s/^\(After this operation, .*\)\.$/\1/p' | paste -sd ';' - | sed 's/;/; /')

  ((TUI_ON)) || printf '\n'
  note "${B}The following system packages will be installed with apt:${N}"
  local line name ver desc row namew=12
  for name in "${missing[@]}"; do ((${#name} > namew)) && namew=${#name}; done
  for line in "${inst[@]}"; do
    name=${line%% *}; ver=${line#* }
    if printf '%s\n' "${missing[@]}" | grep -qx "$name"; then
      desc=$(apt-cache show --no-all-versions "$name" 2>/dev/null | sed -n 's/^Description\(-en\)\{0,1\}: //p' | head -n1)
      printf -v row '  %s%-*s%s  %s%-18s%s %s' "$B" "$namew" "$name" "$N" "$D" "$ver" "$N" "$desc"
      note_row "$row"
    fi
  done
  local deps=$((${#inst[@]} - ${#missing[@]}))
  if ((deps > 0)); then
    local dep_list
    dep_list=$(for line in "${inst[@]}"; do n=${line%% *}; printf '%s\n' "${missing[@]}" | grep -qx "$n" || printf '%s ' "$n"; done)
    local dep_text="plus $deps supporting package(s): $dep_list"
    if ((TUI_ON)); then
      # On a small screen, name the count and keep the full list in the log rather than overflowing.
      local budget=$((ROWS - 13 - ${#PANEL[@]})) need=$(((${#dep_text} + COLS - 9) / (COLS - 6)))
      if ((need > budget)); then log "  $dep_text"; dep_text="plus $deps supporting package(s), listed in $LOG_FILE"; fi
    fi
    note "  ${D}${dep_text}${N}"
  fi
  [[ -z $size ]] || note "  ${D}${size}${N}"
  if [[ $on_decline == skip ]]; then
    ask_yn "$question" y || return 1
  else
    confirm "$question"
  fi

  # apt reports progress on fd 3 as "pmstatus:package:percent:description". Its exit code is
  # captured in a file so the progress-drawing loop can never mask (or fake) a failure.
  log "\$ apt-get install ${missing[*]}"
  activity "Installing ${missing[*]}" 0
  local rcfile; rcfile=$(mktemp)
  { DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends -o APT::Status-Fd=3 \
      "${missing[@]}" 3>&1 1>>"$LOG_FILE" 2>&1; echo $? >"$rcfile"; } |
    while IFS=: read -r kind _pkg pct desc; do
      if [[ -t 1 && ($kind == pmstatus || $kind == dlstatus) ]]; then bar "$pct" "$desc"; fi
    done
  local rc; rc=$(cat "$rcfile"); rm -f "$rcfile"
  if [[ -t 1 ]] && ((!TUI_ON)); then printf '\r\e[K'; fi
  [[ $rc == 0 ]] || die "Package installation failed" "" tail
  ok "Installed: ${missing[*]}"
}

configure() {
  [[ -n $PORT ]] || ask PORT "Port for the web interface?" "$DEFAULT_PORT"
  [[ $PORT =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) || die "Invalid port: $PORT"
  if ss -Hltn "sport = :$PORT" 2>/dev/null | grep -q .; then
    die "Port $PORT is already in use" "Choose another with --port, or stop whatever is using it."
  fi
  ((TUI_ON)) || printf '\n'
  note "${B}These changes will be made to this system:${N}"
  note "  • Create system user ${B}${APP_USER}${N} (no login, no home directory) to run the app"
  note "  • Create $PREFIX (app), $CONF_DIR (config) and $STATE_DIR (data)"
  note "  • Create PostgreSQL role and database ${B}${DB_NAME}${N} (local socket, OS-user auth, no password)"
  note "  • Install systemd services ${B}${SERVICE}${N} (starts at boot) and $SERVICE-update (in-place upgrades)"
  note "  • Install the ${B}${CLI_LINK}${N} command"
  note "  • Serve the web interface on port ${B}${PORT}${N} (all network interfaces, plain HTTP)"
  confirm "Make these changes?"
  step_note "Port $PORT"
}

fetch_package() {
  WORK=$(mktemp -d /tmp/meshhome.XXXXXX)  # removed by on_exit
  local name
  if [[ -n $FROM_FILE ]]; then
    name=$(basename "$FROM_FILE")
    cp "$FROM_FILE" "$WORK/$name"
    if [[ -f "$(dirname "$FROM_FILE")/SHA256SUMS" ]]; then cp "$(dirname "$FROM_FILE")/SHA256SUMS" "$WORK/"; fi
  else
    local base; base="$(download_base)/v$TARGET_VERSION"
    status downloading "Downloading v$TARGET_VERSION"
    # Releases from 0.10 carry meshhome-X.tar.gz (and the old name, for older installers).
    curl -fsSL --retry 3 -o "$WORK/SHA256SUMS" "$base/SHA256SUMS" || die "Download failed: $base/SHA256SUMS"
    name="meshhome-$TARGET_VERSION.tar.gz"
    grep -q " $name\$" "$WORK/SHA256SUMS" || name="meshcore-home-$TARGET_VERSION.tar.gz"
    log "\$ curl $base/$name"
    if ((TUI_ON)); then
      # curl's progress bar ends each update with a carriage return; feed its percentage to ours.
      activity "Downloading $name" 0
      local rcfile; rcfile=$(mktemp)
      { curl -fL --retry 3 --progress-bar -o "$WORK/$name" "$base/$name" 2>&1 >/dev/null; echo $? >"$rcfile"; } |
        while IFS= read -r -d $'\r' line || [[ -n $line ]]; do
          if [[ $line =~ ([0-9]+)(\.[0-9])?% ]]; then bar "${BASH_REMATCH[1]}"; else log "curl: $line"; fi
        done
      local rc; rc=$(cat "$rcfile"); rm -f "$rcfile"
      [[ $rc == 0 ]] || die "Download failed: $base/$name"
    elif [[ -t 1 && $FROM_REQUEST -eq 0 ]]; then
      curl -fL --retry 3 --progress-bar -o "$WORK/$name" "$base/$name" || die "Download failed: $base/$name"
    else
      curl -fsSL --retry 3 -o "$WORK/$name" "$base/$name" || die "Download failed: $base/$name"
    fi
    ok "Downloaded $name ($(du -h "$WORK/$name" | cut -f1))"
    step_note "$(du -h "$WORK/$name" | cut -f1) · SHA-256 checked"
  fi
  PKG_NAME=$name
  if [[ -f $WORK/SHA256SUMS ]]; then
    (cd "$WORK" && grep " $name\$" SHA256SUMS | sha256sum -c --status) ||
      die "Checksum mismatch for $name" "The download is corrupt or has been tampered with. Nothing was installed."
    ok "Checksum verified (SHA-256)"
  else
    warn "No SHA256SUMS next to the local file; skipping checksum verification"
  fi
}

ensure_user_and_dirs() {
  if ! id "$APP_USER" >/dev/null 2>&1; then
    run "Creating system user $APP_USER" useradd --system --user-group --no-create-home \
      --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$APP_USER"
  fi
  install -d -m 755 -o root -g root "$PREFIX" "$PREFIX/releases"
  install -d -m 750 -o root -g "$APP_USER" "$CONF_DIR"
  install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$STATE_DIR" "$BACKUP_DIR"
  ok "Directories ready"
}

install_release() {
  local dest="$PREFIX/releases/$TARGET_VERSION"
  status installing "Installing v$TARGET_VERSION"
  if [[ "$(readlink -f "$PREFIX/current" 2>/dev/null)" == "$dest" ]]; then
    ((REPAIR)) || die "v$TARGET_VERSION is already the active release"
    systemctl stop "$SERVICE" >>"$LOG_FILE" 2>&1 || true
  fi
  # Build in place at the final path: Python virtualenvs are not relocatable (their scripts
  # hard-code the path), so the directory must not be renamed afterwards. A leftover from an
  # interrupted attempt is simply rebuilt; it only becomes live when `current` points at it.
  rm -rf "$dest" "$dest.partial"
  mkdir -p "$dest"
  run "Unpacking v$TARGET_VERSION" tar -xzf "$WORK/$PKG_NAME" \
    -C "$dest" --strip-components=1 --no-same-owner
  [[ "$(tr -d '[:space:]' <"$dest/VERSION")" == "$TARGET_VERSION" ]] || die "Package version does not match"
  run "Creating Python environment" python3 -m venv "$dest/venv"
  # Progress: pip logs "Collecting <pkg>" once per pinned requirement.
  PIP_TOTAL=$(grep -cvE '^[[:space:]]*(#|$)' "$dest/requirements.txt" || true)
  PIP_FROM=$(($(stat -c %s "$LOG_FILE") + 1))
  RUN_PROBE=pip_probe run "Installing Python packages (this can take a few minutes on a Pi)" \
    "$dest/venv/bin/python" -m pip install --no-cache-dir --disable-pip-version-check --only-binary=:all: \
    -r "$dest/requirements.txt"
  run "Precompiling" "$dest/venv/bin/python" -m compileall -q "$dest/app" "$dest/migrations"
  chmod 755 "$dest/deploy/native/install.sh" "$dest/deploy/native/tls-hook"
  local cli; for cli in meshhome meshcore-home; do [[ -f $dest/deploy/native/$cli ]] && chmod 755 "$dest/deploy/native/$cli"; done
  if [[ -f $dest/deploy/native/radio-hat-run ]]; then chmod 755 "$dest/deploy/native/radio-hat-run"; fi
  ok "v$TARGET_VERSION installed to $dest"
  step_note "v$TARGET_VERSION"
}

PIP_TOTAL=1 PIP_FROM=1
pip_probe() {
  local n
  n=$(tail -c +"$PIP_FROM" "$LOG_FILE" | grep -c '^Collecting ' || true)
  ((PIP_TOTAL > 0)) || PIP_TOTAL=1
  ACT_PCT=$((n * 90 / PIP_TOTAL)); ((ACT_PCT <= 90)) || ACT_PCT=90
  if tail -c +"$PIP_FROM" "$LOG_FILE" | grep -q '^Installing collected packages'; then ACT_PCT=95; fi
}

ensure_database() {
  systemctl enable --now postgresql >>"$LOG_FILE" 2>&1 || die "Could not start PostgreSQL"
  local i
  for i in $(seq 1 30); do runuser -u postgres -- pg_isready -q 2>/dev/null && break; sleep 1; done
  if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$APP_USER'" | grep -q 1; then
    run "Creating database role $APP_USER" runuser -u postgres -- createuser "$APP_USER"
  fi
  if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    run "Creating database $DB_NAME" runuser -u postgres -- createdb -O "$APP_USER" "$DB_NAME"
  fi
  local pgv; pgv=$(runuser -u postgres -- psql -tAc 'SHOW server_version' | cut -d' ' -f1)
  ok "PostgreSQL database ready ($pgv)"
  step_note "PostgreSQL $pgv"
}

write_env() {
  if [[ -f $ENV_FILE ]]; then ok "Keeping existing configuration $ENV_FILE"; return; fi
  cat >"$ENV_FILE" <<EOF
# MeshHome (native install). Created by install.sh; keep this file private.
# The owner account and radio settings live in the database (first-run setup wizard).
DATABASE_URL=postgresql+asyncpg://$APP_USER@/$DB_NAME?host=/var/run/postgresql
HOST=0.0.0.0
PORT=$PORT
MESHHOME_INSTALL_KIND=native
MESHHOME_STATE_DIR=$STATE_DIR
STATIC_DIR=$PREFIX/current/static
UPDATE_REPO=$REPO
LOG_LEVEL=INFO
EOF
  chown root:"$APP_USER" "$ENV_FILE"; chmod 640 "$ENV_FILE"
  ok "Wrote $ENV_FILE"
}

# Units ship as templates (deploy/native/systemd/*.service etc.) and are filled in with this
# install's names, so the same release works before and after `migrate`.
unit_template() {  # unit_template DIR UNIT_NAME — the template a unit comes from
  case $2 in
    "$SERVICE.service") printf '%s/app.service' "$1" ;;
    "$HAT_SERVICE.service") printf '%s/radio.service' "$1" ;;
    "$SERVICE-"*) printf '%s/%s' "$1" "${2#"$SERVICE-"}" ;;
  esac
}
render_unit() {  # render_unit TEMPLATE — prints the unit with this layout's names
  sed -e "s#@HAT_DIR@#$HAT_DIR#g" -e "s#@HAT_DATA@#$HAT_DATA#g" -e "s#@HAT_USER@#$HAT_USER#g" \
    -e "s#@PREFIX@#$PREFIX#g" -e "s#@ENV_FILE@#$ENV_FILE#g" -e "s#@STATE_DIR@#$STATE_DIR#g" \
    -e "s#@APP_USER@#$APP_USER#g" -e "s#@SERVICE@#$SERVICE#g" "$1"
}
put_unit() {  # put_unit DIR UNIT_NAME — install it; returns 0 if it changed, 1 if not (or no template)
  local t tmp; t=$(unit_template "$1" "$2")
  [[ -n $t && -f $t ]] || return 1
  tmp=$(mktemp); render_unit "$t" >"$tmp"
  if cmp -s "$tmp" "$UNIT_DIR/$2"; then rm -f "$tmp"; return 1; fi
  install -m 644 "$tmp" "$UNIT_DIR/$2"; rm -f "$tmp"
  return 0
}

install_units() {  # from the release being activated
  local src="$PREFIX/releases/$TARGET_VERSION/deploy/native/systemd" u
  for u in "$SERVICE.service" "${HELPER_UNITS[@]}"; do put_unit "$src" "$u" || true; done
  systemctl daemon-reload
  ln -sfn "$PREFIX/current/deploy/native/meshhome" "$CLI_LINK"
}

activate() {  # switch current -> new release and (re)start, rolling back on failure
  local new="$PREFIX/releases/$TARGET_VERSION" prev=""
  [[ -L $PREFIX/current ]] && prev=$(readlink -f "$PREFIX/current")
  PORT=${PORT:-$(env_get PORT)}; PORT=${PORT:-$DEFAULT_PORT}
  status restarting "Restarting on v$TARGET_VERSION"
  ln -sfn "$new" "$PREFIX/current.new" && mv -T "$PREFIX/current.new" "$PREFIX/current"
  install_units
  systemctl enable "$SERVICE.service" "$SERVICE-update.path" "$SERVICE-config.path" >>"$LOG_FILE" 2>&1
  systemctl start "$SERVICE-update.path" "$SERVICE-config.path" >>"$LOG_FILE" 2>&1 || true
  if ! systemctl restart "$SERVICE.service" >>"$LOG_FILE" 2>&1; then
    warn "$SERVICE failed to start"
  elif run_check "Started; waiting for v$TARGET_VERSION to become ready" wait_healthy "$PORT" "$TARGET_VERSION" 120; then
    step_note "Running"
    return 0
  fi
  journalctl -u "$SERVICE" -n 30 --no-pager >>"$LOG_FILE" 2>&1 || true
  if [[ -z $prev ]]; then
    # First install: undo activation so simply re-running the installer starts over cleanly.
    systemctl disable --now "$SERVICE.service" >>"$LOG_FILE" 2>&1 || true
    rm -f "$PREFIX/current"
    die "$APP_NAME did not start" "Details: journalctl -u $SERVICE -n 50   (then re-run the installer)"
  fi
  if [[ -n $prev && -d $prev && $prev != "$new" ]]; then
    local prev_v; prev_v=$(basename "$prev")
    warn "v$TARGET_VERSION did not start; rolling back to v$prev_v"
    ln -sfn "$prev" "$PREFIX/current.new" && mv -T "$PREFIX/current.new" "$PREFIX/current"
    local failed=$TARGET_VERSION; TARGET_VERSION=$prev_v
    install_units
    TARGET_VERSION=$failed
    systemctl restart "$SERVICE.service" >>"$LOG_FILE" 2>&1 || true
    if run_bg "Restarting v$prev_v" wait_healthy "$PORT" "$prev_v" 90; then
      rm -rf "$new"  # discard the release that failed
      STEP_STATE[STEP - 1]=failed; STEP_NOTE[STEP - 1]="Rolled back to v$prev_v"
      tui_end
      log "rolled back to v$prev_v"
      status rolled_back "v$failed didn't start, so v$prev_v was restored automatically. Nothing was lost."
      printf '\n  %s✗ Upgrade to v%s failed — v%s was restored and is running.%s\n' "$R$B" "$failed" "$prev_v" "$N" >&2
      printf '    %sDetails: journalctl -u %s   Full log: %s%s\n\n' "$D" "$SERVICE" "$LOG_FILE" "$N" >&2
      exit 1
    fi
  fi
  die "$APP_NAME did not start" "Check: journalctl -u $SERVICE -n 50"
}

run_check() {  # like run(), but returns non-zero instead of exiting
  local msg="$1"; shift
  if run_bg "$msg" "$@"; then ok "$msg"; return 0; fi
  warn "$msg — timed out"; return 1
}

backup_database() {
  status installing "Backing up the database"
  local f; f="$BACKUP_DIR/pre-upgrade-$(installed_version)-$(date +%Y%m%dT%H%M%S).dump"
  run "Backing up the database" runuser -u "$APP_USER" -- pg_dump -Fc -f "$f" "$DB_NAME"
  step_note "$(du -h "$f" | cut -f1) · $(basename "$f")"
  # Keep the newest few pre-upgrade backups.
  { ls -1t "$BACKUP_DIR"/pre-upgrade-*.dump 2>/dev/null || true; } | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f
}

prune_releases() {
  local keep cur; cur=$(readlink -f "$PREFIX/current")
  keep=$(ls -1dt "$PREFIX"/releases/*/ 2>/dev/null | sed 's#/$##' | head -n "$KEEP_RELEASES")
  local d
  for d in "$PREFIX"/releases/*; do
    [[ -d $d && $d != "$cur" ]] || continue
    grep -qx "$d" <<<"$keep" || { rm -rf "$d"; log "pruned $d"; }
  done
}

finish() {
  local port; port=$(env_get PORT); port=${port:-$DEFAULT_PORT}
  steps_done
  tui_end
  status "done" "Upgraded to v$TARGET_VERSION"
  printf '\n  %s%s✓ %s v%s is running.%s\n\n' "$G" "$B" "$APP_NAME" "$TARGET_VERSION" "$N"
  if https_enabled; then
    printf '  Open it from a browser on your network:\n    %s%s%s\n' "$B" "$(public_url)" "$N"
    printf '  %sMake sure %s resolves to this Pi on your network (local DNS or router), e.g. %s%s\n' \
      "$D" "$(env_get HTTPS_HOST)" "$(lan_addresses | head -n1)" "$N"
  else
    printf '  Open it from a browser on your network:\n'
    local ip; for ip in $(lan_addresses); do printf '    %shttp://%s:%s%s\n' "$B" "$ip" "$port" "$N"; done
    printf '    %shttp://%s.local:%s%s\n' "$D" "$(hostname)" "$port" "$N"
    printf '  %sPlain HTTP. Add HTTPS in Settings → Network & HTTPS, or: sudo %s https%s\n' "$D" "$CLI_NAME" "$N"
  fi
  if [[ -f $STATE_DIR/setup-token ]]; then
    printf '\n  First-run setup token (the wizard asks for it): %s%s%s\n' "$B$Y" "$(cat "$STATE_DIR/setup-token")" "$N"
  fi
  printf '\n  Manage it with: %s%s status | logs | update | backup | https | uninstall%s\n\n' "$B" "$CLI_NAME" "$N"
}

uninstall() {
  [[ $EUID -eq 0 ]] || die "Please run as root" "sudo $CLI_NAME uninstall"
  TARGET_VERSION=$(installed_version || true)
  local nginx=0
  [[ -f $NGINX_SITE || -L $NGINX_ENABLED ]] && nginx=1
  local -a titles=("Review what will be removed" "Stop and remove the services" "Remove the application files")
  ((nginx)) && titles+=("Remove the HTTPS site")
  local hat=0
  [[ -f $HAT_UNIT ]] && hat=1 && titles+=("Remove the radio HAT software")
  ((PURGE)) && titles+=("Delete the database and data")
  if ((PURGE)); then tui_start "Uninstall (including all data)"; else tui_start "Uninstall"; fi
  STEP_WEIGHTS=(1 3 3 1 1 3)
  banner
  plan "${titles[@]}"

  step
  ((TUI_ON)) || printf '\n'
  note "${B}This will remove:${N}"
  note "  • The $SERVICE services and the $CLI_LINK command"
  note "  • The application files in $PREFIX"
  ((nginx)) && note "  • The nginx HTTPS site for $(env_get HTTPS_HOST) (nginx itself stays installed)"
  ((hat)) && note "  • The radio HAT software (ZephCore) and its service$( ((PURGE)) && echo ", including the radio's identity" || echo "; the radio's identity in $HAT_DATA is kept")"
  if ((PURGE)); then
    note "  • ${R}The database $DB_NAME (all message history), $CONF_DIR and $STATE_DIR${N}"
    note "  • The system user $APP_USER"
    [[ -f $CF_CREDENTIALS || -d $ACME_DIR ]] && note "  • The saved DNS provider credentials and HTTPS certificate"
  else
    note "${B}Kept:${N} the database, $CONF_DIR and $STATE_DIR (use --purge to delete them too)"
    ((nginx)) && note "  The HTTPS settings and certificate are kept too: reinstalling puts HTTPS back as it was."
  fi
  note "${D}System packages (PostgreSQL, Python) are left installed.${N}"
  confirm "Uninstall $APP_NAME?"

  step
  activity "Stopping $APP_NAME"
  systemctl disable --now "$SERVICE-update.path" "$SERVICE-config.path" "$SERVICE.service" >>"$LOG_FILE" 2>&1 || true
  # ${VAR:?} guards: an empty variable must never turn a removal into a top-level path.
  systemctl disable --now "$SERVICE-acme-renew.timer" >>"$LOG_FILE" 2>&1 || true
  rm -f "${UNIT_DIR:?}/${SERVICE:?}.service" "${CLI_LINK:?}" "$AVAHI_SERVICE"
  # The old command name, kept working by `migrate`.
  [[ $(readlink "/usr/local/bin/$OLD_SERVICE" 2>/dev/null) == "$PREFIX/"* ]] && rm -f "/usr/local/bin/$OLD_SERVICE"
  local u; for u in "$SERVICE.service" "${HELPER_UNITS[@]}" "${ACME_UNITS[@]}"; do rm -rf "${UNIT_DIR:?}/${u:?}" "${UNIT_DIR:?}/${u:?}.d"; done
  systemctl daemon-reload
  ok "Services and the $CLI_LINK command removed"

  step
  run "Removing $PREFIX" rm -rf "${PREFIX:?}" "${LEGO_DIR:?}"

  if ((nginx)); then
    step
    rm -f $NGINX_ENABLED "${NGINX_SITE:?}"
    run_bg "Reloading nginx" systemctl reload nginx || true
    ok "HTTPS site removed"
  fi
  if ((hat)); then
    step
    hat_remove "$PURGE"
  fi
  if ((PURGE)); then
    step
    rm -f "${CF_CREDENTIALS:?}"
    run_bg "Deleting the database $DB_NAME" runuser -u postgres -- dropdb --if-exists "$DB_NAME" || true
    run_bg "Deleting the database role $APP_USER" runuser -u postgres -- dropuser --if-exists "$APP_USER" || true
    rm -rf "${CONF_DIR:?}" "${STATE_DIR:?}"
    userdel "$APP_USER" >>"$LOG_FILE" 2>&1 || true
    ok "Database, configuration, data and system user removed"
  fi
  steps_done
  tui_end
  printf '\n  %s✓ %s has been removed.%s\n\n' "$G" "$APP_NAME" "$N"
}

# ---- network & HTTPS (shared by the terminal wizard, `meshhome https` and the web UI) ------
# Configuration lives in the env file (non-secret) and, for the Cloudflare token, in a root-only
# certbot credentials file. The web UI never gets root: it writes $CONFIG_REQUEST, which the
# meshhome-config path unit hands to `install.sh --apply-config`.
DEFAULT_HTTPS_PORT=443
DEFAULT_PROPAGATION=0  # 0: lego checks propagation itself; otherwise a fixed wait in seconds

env_set() {  # env_set KEY VALUE — add or replace a line in the env file
  if grep -q "^$1=" "$ENV_FILE"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$1" "$2" >>"$ENV_FILE"
  fi
}

port_owner() {  # port_owner PORT — name of the process listening on it, if any
  ss -Hltnp "sport = :$1" 2>/dev/null | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | head -n1
}

run_quiet() {  # like run(), but returns non-zero instead of exiting
  local msg="$1"; shift
  if run_bg "$msg" "$@"; then ok "$msg"; return 0; fi
  warn "$msg — failed"; return 1
}

https_enabled() {  # older installs (v0.6.1–0.6.3) only set HTTPS_HOST
  local e; e=$(env_get HTTPS_ENABLED)
  if [[ -n $e ]]; then [[ $e == 1 ]]; else [[ -n $(env_get HTTPS_HOST) && -f $NGINX_SITE ]]; fi
}

https_packages_installed() { local m; mapfile -t m < <(missing_packages "${HTTPS_PACKAGES[@]}"); ((${#m[@]} == 0)); }

write_network_snapshot() {  # non-secret view of the configuration for the app (world-readable)
  local enabled=0 renew="" pkgs=0 client provider saved
  https_enabled && enabled=1
  https_packages_installed && pkgs=1
  client=$(env_get ACME_CLIENT)
  provider=$(env_get ACME_PROVIDER)
  saved=$(acme_py saved 2>/dev/null || true)
  if [[ $client == lego ]]; then
    renew=$(systemctl is-enabled "$SERVICE-acme-renew.timer" 2>/dev/null || true)
  elif ((enabled)); then
    client=certbot provider=cloudflare  # installed before v0.7.4; moves to lego when next saved
    renew=$(systemctl is-enabled certbot.timer 2>/dev/null || true)
  fi
  [[ -z $saved && -f $CF_CREDENTIALS ]] && saved=cloudflare
  python3 - "$NETWORK_SNAPSHOT" "$(env_get PORT)" "$(env_get HOST)" "$enabled" "$(env_get HTTPS_HOST)" \
    "$(env_get HTTPS_PORT)" "$(env_get HTTPS_REDIRECT)" "$(env_get CERTBOT_EMAIL)" "$(env_get CERTBOT_STAGING)" \
    "$(env_get CF_PROPAGATION)" "$saved" "$pkgs" "$renew" "${provider:-cloudflare}" "${client:-}" <<'PY' || true
import json, os, sys, time
(path, port, host, enabled, https_host, https_port, redirect, email, staging, prop, saved, pkgs, renew,
 provider, client) = sys.argv[1:16]
def num(v, d):
    try:
        return int(v)
    except ValueError:
        return d
data = {
    "app_port": num(port, 8080),
    "bind": host or "0.0.0.0",
    "https_enabled": enabled == "1",
    "hostname": https_host or None,
    "https_port": num(https_port, 443),
    "redirect_http": redirect != "0",
    "email": email or None,
    "staging": staging == "1",
    "dns_provider": provider,
    "credentials_provider": saved or None,  # credentials are saved for this provider (never shown)
    "acme_client": client or None,  # "lego", or "certbot" for installs not yet moved to lego
    "propagation_seconds": num(prop, 0),
    "token_saved": saved == "cloudflare",  # API compatibility (v0.6.4 to v0.7.3)
    "https_packages_installed": pkgs == "1",
    "auto_renew": renew == "enabled",
    "updated_at": time.time(),
}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
}

backup_network_config() {
  install -d -m 700 "$CONF_BACKUP"
  rm -f "$CONF_BACKUP"/*
  cp -p "$ENV_FILE" "$CONF_BACKUP/env"
  [[ -f $NGINX_SITE ]] && cp -p "$NGINX_SITE" "$CONF_BACKUP/nginx-site"
  [[ -f $CF_CREDENTIALS ]] && cp -p "$CF_CREDENTIALS" "$CONF_BACKUP/cf-credentials"
  if [[ -d $ACME_DIR ]]; then
    install -d -m 700 "$CONF_BACKUP/acme"
    find "$ACME_DIR" -maxdepth 1 -type f \( -name '*.env' -o -name '*.json' \) -exec cp -p {} "$CONF_BACKUP/acme/" \;
  fi
  return 0
}

restore_network_config() {  # put the previous env file and nginx site back, and restart both
  warn "Restoring the previous network configuration"
  cp -p "$CONF_BACKUP/env" "$ENV_FILE"
  # A failed attempt with a new token must not lose the token that worked before.
  if [[ -f $CONF_BACKUP/cf-credentials ]]; then cp -p "$CONF_BACKUP/cf-credentials" "$CF_CREDENTIALS"; else rm -f "$CF_CREDENTIALS"; fi
  if [[ -d $ACME_DIR ]]; then
    find "$ACME_DIR" -maxdepth 1 -type f \( -name '*.env' -o -name '*.json' \) -delete
    if [[ -d $CONF_BACKUP/acme ]]; then cp -p "$CONF_BACKUP"/acme/* "$ACME_DIR/" 2>/dev/null || true; fi
  fi
  if [[ -f $CONF_BACKUP/nginx-site ]]; then
    cp -p "$CONF_BACKUP/nginx-site" "$NGINX_SITE"
    ln -sfn "$NGINX_SITE" $NGINX_ENABLED
  else
    rm -f "$NGINX_SITE" $NGINX_ENABLED
  fi
  systemctl reload nginx >>"$LOG_FILE" 2>&1 || true
  systemctl restart "$SERVICE" >>"$LOG_FILE" 2>&1 || true
  wait_healthy "$(env_get PORT)" "" 60 || true
}

# ---- HTTPS certificates with lego (DNS-01 through the owner's DNS provider) --------------------
# lego (https://github.com/go-acme/lego, MIT) gets and renews the Let's Encrypt certificate. It is
# pinned by lego.lock and downloaded on demand. The DNS providers offered are listed in
# dns-providers.json (lego's own provider codes and credential names). Credentials live in a
# root-only dotenv file; a twice-daily timer renews the certificate and reloads nginx.
# Installs made before this used certbot with Cloudflare; they keep working until their HTTPS
# settings are next saved, which moves them to lego with the same Cloudflare token.

acme_src() {  # the release directory with the HTTPS support files (lego.lock, dns-providers.json)
  local d
  for d in ${TARGET_VERSION:+"$PREFIX/releases/$TARGET_VERSION/deploy/native"} "$PREFIX/current/deploy/native" \
           "$(dirname "${BASH_SOURCE[0]}")"; do
    [[ -f $d/dns-providers.json && -f $d/lego.lock ]] && { printf '%s' "$d"; return 0; }
  done
  return 1
}

acme_py() {  # acme_py ACTION ARGS... — provider catalogue and credential handling (see below)
  local src; src=$(acme_src) || { echo "dns-providers.json is missing from this release" >&2; return 1; }
  python3 - "$src/dns-providers.json" "$ACME_DIR" "$@" <<'PY'
import json, os, re, sys
catalog, acme_dir, action, *args = sys.argv[1:]
providers = {p["id"]: p for p in json.load(open(catalog))["providers"]}
creds_path = os.path.join(acme_dir, "credentials.env")

def saved():
    """(provider, {ENV: value}) from the credentials file, or (None, {})."""
    try:
        lines = open(creds_path).read().splitlines()
    except OSError:
        return None, {}
    prov, vals = None, {}
    for line in lines:
        if line.startswith("# provider="):
            prov = line.split("=", 1)[1].strip()
        elif "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            vals[k] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] == "'" else v
    return prov, vals

def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)

if action == "list":  # id<TAB>name
    for p in providers.values():
        print(f"{p['id']}\t{p['name']}")
elif action == "get":  # get ID KEY
    p = providers.get(args[0]) or fail(f"Unknown DNS provider: {args[0]}")
    v = p.get(args[1])
    print("" if v is None else v)
elif action == "fields":  # ENV<TAB>label<TAB>secret<TAB>required<TAB>kind<TAB>default<TAB>choices
    p = providers.get(args[0]) or fail(f"Unknown DNS provider: {args[0]}")
    for f in p["fields"]:
        print("\t".join([f["env"], f["label"], "1" if f.get("secret") else "0", "1" if f.get("required") else "0",
                         f.get("kind", "text"), f.get("default", ""), ",".join(f.get("choices", []))]))
elif action == "saved":  # the provider the saved credentials belong to
    print(saved()[0] or "")
elif action == "write":  # write ID JSON — validate, merge with the saved values of the same provider, save
    pid, new = args[0], json.loads(args[1] or "{}")
    p = providers.get(pid) or fail(f"Unknown DNS provider: {pid}")
    if not isinstance(new, dict):
        fail("Credentials must be a set of named values")
    fields = {f["env"]: f for f in p["fields"]}
    unknown = [k for k in new if k not in fields]
    if unknown:
        fail(f"{p['name']} does not use: {', '.join(unknown)}")
    old_prov, old = saved()
    values = {k: v for k, v in old.items() if k in fields} if old_prov == pid else {}
    for k, v in new.items():
        v = "" if v is None else str(v).strip()
        if v:
            values[k] = v
    os.makedirs(acme_dir, mode=0o700, exist_ok=True)
    out = [f"# provider={pid}", f"# {p['name']} credentials for lego (MeshHome). Root only; never shown again."]
    for env, f in fields.items():
        v = values.get(env, "")
        if f.get("kind") == "json":
            path = os.path.join(acme_dir, f"{env.lower()}.json")
            if v and not v.startswith(acme_dir):  # new JSON content (the saved value is the file's path)
                try:
                    json.loads(v)
                except ValueError:
                    fail(f"{f['label']}: this is not valid JSON")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(v)
                v = path
            if v and not os.path.isfile(v):
                v = ""
        else:
            if not v and f.get("default"):
                v = f["default"]
            if v and (len(v) > 500 or re.search(r"[\x00-\x1f\x7f']", v)):
                fail(f"{f['label']}: unexpected characters or too long")
            if v and f.get("choices") and v not in f["choices"]:
                fail(f"{f['label']}: choose one of {', '.join(f['choices'])}")
        if f.get("required") and not v:
            fail(f"{p['name']}: enter the {f['label']}")
        if v:
            out.append(f"{env}='{v}'")
    tmp = creds_path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, creds_path)
else:
    fail(f"unknown action {action}")
PY
}

acme_provider_name() { acme_py get "$1" name 2>/dev/null || printf '%s' "$1"; }

lego_bin() { printf '%s' "$LEGO_DIR/lego"; }

lego_install() {  # download and verify the pinned lego release (once per version)
  local src; src=$(acme_src) || { warn "lego.lock is missing from this release"; return 1; }
  # shellcheck disable=SC1091
  . "$src/lego.lock"
  if [[ -x $LEGO_DIR/lego-$LEGO_VERSION ]]; then
    ln -sfn "lego-$LEGO_VERSION" "$LEGO_DIR/lego"; return 0
  fi
  local arch asset sha base tmp
  case "$(uname -m)" in
    aarch64) asset=$LEGO_ARM64_ASSET sha=$LEGO_ARM64_SHA256 ;;
    x86_64) asset=$LEGO_AMD64_ASSET sha=$LEGO_AMD64_SHA256 ;;
    *) warn "No lego build for $(uname -m)"; return 1 ;;
  esac
  base=$(override MESHHOME_LEGO_BASE); base=${base:-$LEGO_BASE_URL}
  tmp=$(mktemp -d)
  if ! run_quiet "Downloading lego $LEGO_VERSION (certificate client)" curl -fsSL --retry 3 -o "$tmp/lego.tar.gz" "$base/$asset"; then
    rm -rf "$tmp"; return 1
  fi
  if [[ $(sha256sum "$tmp/lego.tar.gz" | cut -d' ' -f1) != "$sha" ]]; then
    rm -rf "$tmp"; warn "The lego download failed its SHA-256 check, so it was not installed"; return 1
  fi
  tar -xzf "$tmp/lego.tar.gz" -C "$tmp" lego
  install -d -m 755 "$LEGO_DIR"
  install -m 755 "$tmp/lego" "$LEGO_DIR/lego-$LEGO_VERSION"
  rm -rf "$tmp"
  ln -sfn "lego-$LEGO_VERSION" "$LEGO_DIR/lego"
  find "$LEGO_DIR" -maxdepth 1 -name 'lego-*' ! -name "lego-$LEGO_VERSION" -delete
  ok "Installed lego $LEGO_VERSION (checksum verified)"
}

acme_cert_path() { printf '%s/certificates/%s.crt' "$ACME_DATA" "$1"; }
acme_key_path() { printf '%s/certificates/%s.key' "$ACME_DATA" "$1"; }

acme_lego_args() {  # acme_lego_args HOST EMAIL STAGING PROPAGATION — fills LEGO_ARGS for `lego run`
  local host=$1 email=$2 staging=$3 prop=$4 provider lego_code server dns envfile
  provider=$(env_get ACME_PROVIDER); provider=${NEW_PROVIDER:-${provider:-cloudflare}}
  lego_code=$(acme_py get "$provider" lego) || return 1
  server=$(override MESHHOME_ACME_SERVER)
  [[ -n $server ]] || { if [[ $staging == 1 ]]; then server=letsencrypt-staging; else server=letsencrypt; fi; }
  dns=$lego_code envfile=$ACME_CREDENTIALS
  # Testing only: a different DNS-01 provider (e.g. "exec") and its env file.
  if [[ -n $(override MESHHOME_ACME_DNS_OVERRIDE) ]]; then
    dns=$(override MESHHOME_ACME_DNS_OVERRIDE) envfile=$(override MESHHOME_ACME_TEST_ENV_FILE)
  fi
  LEGO_ARGS=(run --accept-tos --server "$server" -d "$host" --cert.name "$host" --dns "$dns"
    --env-file "$envfile" --path "$ACME_DATA" --log.format text --no-random-sleep
    --deploy-hook "$PREFIX/current/deploy/native/tls-hook")
  if [[ -n $email ]]; then LEGO_ARGS+=(-m "$email"); else LEGO_ARGS+=(--account-id meshcore-home); fi
  if [[ ${prop:-0} =~ ^[0-9]+$ ]] && ((prop > 0)); then LEGO_ARGS+=(--dns.propagation.wait "${prop}s"); fi
  return 0
}

acme_env() {  # environment for lego (a private test CA when testing)
  local ca; ca=$(override MESHHOME_ACME_CA)
  if [[ -n $ca ]]; then printf 'LEGO_CA_CERTIFICATES=%s' "$ca"; else printf 'LEGO_LOG_LEVEL=info'; fi
}

acme_units_install() {  # the renewal timer (from the active release)
  local src="$PREFIX/current/deploy/native/systemd" u
  for u in "${ACME_UNITS[@]}"; do put_unit "$src" "$u" || true; done
  systemctl daemon-reload
  systemctl enable --now "$SERVICE-acme-renew.timer" >>"$LOG_FILE" 2>&1 || true
}

acme_units_remove() {
  systemctl disable --now "$SERVICE-acme-renew.timer" >>"$LOG_FILE" 2>&1 || true
  local u; for u in "${ACME_UNITS[@]}"; do rm -f "${UNIT_DIR:?}/${u:?}"; done
  systemctl daemon-reload
}

retire_certbot_site() {  # retire_certbot_site HOST — stop certbot renewing a certificate lego now owns
  local conf="/etc/letsencrypt/renewal/$1.conf"
  if [[ -n $1 && -f $conf ]]; then
    mv "$conf" "$conf.disabled-by-meshcore-home"
    log "certbot renewal for $1 disabled (lego renews this site now)"
  fi
  rm -f "$CF_CREDENTIALS"
}

acme_renew() {  # --acme-renew: run by the renewal timer; renews when due and reloads nginx
  [[ $(env_get ACME_CLIENT) == lego ]] && https_enabled || { log "acme renew: lego not in use"; return 0; }
  local host; host=$(env_get HTTPS_HOST)
  # The client may be missing (removed with the app by a plain uninstall, or never downloaded when a
  # kept or restored certificate was reused): fetch the pinned, checksum-verified version first.
  lego_install >>"$LOG_FILE" 2>&1 || { log "acme renew: could not install lego"; return 1; }
  NEW_PROVIDER=$(env_get ACME_PROVIDER)
  acme_lego_args "$host" "$(env_get CERTBOT_EMAIL)" "$(env_get CERTBOT_STAGING)" "$(env_get CF_PROPAGATION)" || return 1
  [[ ${1:-} == force ]] && LEGO_ARGS+=(--renew-force)
  log "\$ lego ${LEGO_ARGS[*]}"
  if env "$(acme_env)" "$(lego_bin)" "${LEGO_ARGS[@]}" >>"$LOG_FILE" 2>&1; then
    log "acme renew: done for $host"
    return 0
  fi
  log "acme renew: FAILED for $host"
  return 1
}

obtain_certificate() {  # obtain_certificate HOST EMAIL STAGING(0/1) PROPAGATION FORCE(0/1)
  local host=$1 email=$2 staging=$3 prop=$4 force=$5
  if [[ -n $(override MESHHOME_TLS_SELF_SIGNED) ]]; then
    # Testing only: a self-signed certificate instead of Let's Encrypt (browsers will warn).
    warn "MESHCORE_HOME_TLS_SELF_SIGNED is set: using a self-signed TEST certificate"
    install -d -m 755 "$CONF_DIR/tls-test"
    TLS_CERT_PATH="$CONF_DIR/tls-test/fullchain.pem" TLS_KEY_PATH="$CONF_DIR/tls-test/privkey.pem"
    run_quiet "Creating a self-signed test certificate" openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
      -subj "/CN=$host" -addext "subjectAltName=DNS:$host" -keyout "$TLS_KEY_PATH" -out "$TLS_CERT_PATH" || return 1
    chmod 600 "$TLS_KEY_PATH"
    return 0
  fi
  lego_install || return 1
  install -d -m 700 "$ACME_DIR" "$ACME_DATA"
  local provider name
  provider=${NEW_PROVIDER:-$(env_get ACME_PROVIDER)}; name=$(acme_provider_name "${provider:-cloudflare}")
  acme_lego_args "$host" "$email" "$staging" "$prop" || return 1
  [[ $force == 1 ]] && LEGO_ARGS+=(--renew-force)
  local wait="the DNS check can take a few minutes"
  [[ ${prop:-0} =~ ^[0-9]+$ ]] && ((prop > 0)) && wait="about ${prop}s for DNS"
  if ! run_quiet "Requesting a certificate from Let's Encrypt via $name ($wait)" env "$(acme_env)" "$(lego_bin)" "${LEGO_ARGS[@]}"; then
    ((TUI_ON)) || tail -n 8 "$LOG_FILE" | sed 's/^/    /' >&2
    warn "Could not obtain a certificate. Check the $name credentials and their permissions, and that $host is in a zone they manage. Details: $LOG_FILE"
    return 1
  fi
  TLS_CERT_PATH=$(acme_cert_path "$host") TLS_KEY_PATH=$(acme_key_path "$host")
}

nginx_push_location() {  # nginx_push_location APP_PORT — device uploads (bot apps) stay on plain HTTP
  # Devices that send readings to /push/<token> can't follow the redirect to HTTPS, so port 80
  # passes those through to the app. The app checks that they come from the local network.
  printf '    location /push/ {\n        proxy_pass http://127.0.0.1:%s;\n' "$1"
  printf '        proxy_set_header Host $host;\n        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n'
  printf '        client_max_body_size 64k;\n        access_log off;  # the URLs carry device keys\n    }\n'
}

nginx_push_refresh() {  # an HTTPS site written before push uploads existed gets the pass-through
  [[ -f $NGINX_SITE ]] && https_enabled && command -v nginx >/dev/null || return 0
  grep -q 'location /push/' "$NGINX_SITE" && return 0
  grep -qE '^    location / \{ return 301 ' "$NGINX_SITE" || return 0  # no HTTP→HTTPS redirect: nothing on port 80
  local port; port=$(env_get PORT)
  [[ $port =~ ^[0-9]+$ ]] || return 0
  cp -p "$NGINX_SITE" "$NGINX_SITE.before-push"
  awk -v block="$(nginx_push_location "$port")" '/^    location \/ \{ return 301 / && !done { print block; done=1 } { print }' \
    "$NGINX_SITE.before-push" >"$NGINX_SITE"
  if nginx -t >>"$LOG_FILE" 2>&1 && systemctl reload nginx >>"$LOG_FILE" 2>&1; then
    log "nginx: port 80 now passes device uploads (/push/) to the app"
    rm -f "$NGINX_SITE.before-push"
  else
    mv -f "$NGINX_SITE.before-push" "$NGINX_SITE"
    log "nginx: couldn't add the /push/ pass-through; kept the previous site"
  fi
}

write_nginx_site() {  # write_nginx_site HOST HTTPS_PORT REDIRECT(0/1) APP_PORT CERT KEY
  local host=$1 hport=$2 redirect=$3 aport=$4 cert=$5 key=$6 target="https://\$host"
  [[ $hport != 443 ]] && target="https://\$host:$hport"
  {
    printf '# MeshHome — HTTPS front end. Managed by install.sh (Settings → Network & HTTPS).\n'
    printf 'map $http_upgrade $meshcore_connection_upgrade {\n    default upgrade;\n    %s      close;\n}\n' "''"
    if [[ $redirect == 1 ]]; then
      printf 'server {\n    listen 80;\n    listen [::]:80;\n    server_name %s;\n' "$host"
      nginx_push_location "$aport"
      printf '    location / { return 301 %s$request_uri; }\n}\n' "$target"
    fi
    cat <<NGINX
server {
    listen $hport ssl;
    listen [::]:$hport ssl;
    http2 on;
    server_name $host;

    ssl_certificate     $cert;
    ssl_certificate_key $key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:meshcore_ssl:5m;
    add_header Strict-Transport-Security "max-age=31536000" always;
    client_max_body_size 2m;

    location / {
        proxy_pass http://127.0.0.1:$aport;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host \$http_host;
        # WebSocket (live updates)
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$meshcore_connection_upgrade;
        proxy_read_timeout 1h;
    }
}
NGINX
  } >"$NGINX_SITE"
  # nginx before 1.25 (Debian 12) has no "http2 on;" directive: use the older listen flag.
  if ! grep -qE 'nginx/1\.(2[5-9]|[3-9][0-9])' <<<"$(nginx -v 2>&1)"; then
    sed -i -e '/^    http2 on;$/d' -e "s/listen $hport ssl;/listen $hport ssl http2;/" \
      -e "s/listen \[::\]:$hport ssl;/listen [::]:$hport ssl http2;/" "$NGINX_SITE"
  fi
}

validate_network_inputs() {  # validates NEW_* values; prints a reason and returns 1 if invalid
  local fqdn_re='^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$' p
  for p in "$NEW_PORT" "$NEW_HTTPS_PORT"; do
    [[ $p =~ ^[0-9]+$ ]] && ((p >= 1 && p <= 65535)) || { warn "Invalid port: $p"; return 1; }
  done
  [[ $NEW_PROPAGATION =~ ^[0-9]+$ ]] && ((NEW_PROPAGATION == 0 || (NEW_PROPAGATION >= 10 && NEW_PROPAGATION <= 600))) ||
    { warn "The DNS wait must be 0 (automatic) or 10–600 seconds"; return 1; }
  if [[ $NEW_HTTPS == 1 ]]; then
    [[ $NEW_HOST =~ $fqdn_re ]] || { warn "Invalid hostname: $NEW_HOST"; return 1; }
    [[ -z $NEW_EMAIL || $NEW_EMAIL =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || { warn "Invalid email: $NEW_EMAIL"; return 1; }
    ((NEW_PORT != NEW_HTTPS_PORT)) || { warn "The app port and HTTPS port must differ"; return 1; }
    if [[ $NEW_REDIRECT == 1 ]] && ((NEW_PORT == 80 || NEW_HTTPS_PORT == 80)); then
      warn "Port 80 is used for the HTTP→HTTPS redirect; pick other ports or turn the redirect off"; return 1
    fi
  fi
  # Ports must be free, or already held by the right process.
  local cur_port; cur_port=$(env_get PORT)
  if [[ $NEW_PORT != "$cur_port" && -n $(port_owner "$NEW_PORT") ]]; then
    warn "Port $NEW_PORT is already in use by '$(port_owner "$NEW_PORT")'"; return 1
  fi
  if [[ $NEW_HTTPS == 1 ]]; then
    local owner check=("$NEW_HTTPS_PORT")
    [[ $NEW_REDIRECT == 1 ]] && check+=(80)
    for p in "${check[@]}"; do
      owner=$(port_owner "$p")
      [[ -z $owner || $owner == nginx ]] || { warn "Port $p is already in use by '$owner'"; return 1; }
    done
  fi
  return 0
}

apply_network_config() {
  # Inputs: NEW_PORT NEW_HTTPS(0/1) NEW_HOST NEW_HTTPS_PORT NEW_REDIRECT(0/1) NEW_EMAIL NEW_STAGING(0/1)
  #         NEW_PROPAGATION (0 = automatic) NEW_PROVIDER (dns-providers.json id)
  #         NEW_CREDENTIALS (JSON {ENV: value}; empty keeps the saved values)
  #         NEW_TOKEN (older requests: a Cloudflare API token)
  # Returns 1 (after restoring the previous configuration) if anything fails.
  validate_network_inputs || return 1
  local old_port old_host old_https_host old_staging old_client old_provider restart=0
  old_port=$(env_get PORT); old_host=$(env_get HOST)
  old_https_host=$(env_get HTTPS_HOST); old_staging=$(env_get CERTBOT_STAGING)
  old_client=$(env_get ACME_CLIENT); old_provider=$(env_get ACME_PROVIDER)
  NEW_PROVIDER=${NEW_PROVIDER:-${old_provider:-cloudflare}}
  if [[ -n ${NEW_TOKEN:-} ]]; then NEW_PROVIDER=cloudflare NEW_CREDENTIALS=$(python3 -c 'import json,sys; print(json.dumps({"CF_DNS_API_TOKEN": sys.argv[1]}))' "$NEW_TOKEN"); fi
  backup_network_config

  if [[ $NEW_HTTPS == 1 ]]; then
    if ! https_packages_installed; then
      local missing; mapfile -t missing < <(missing_packages "${HTTPS_PACKAGES[@]}")
      status installing "Installing ${missing[*]}"
      run_quiet "Refreshing package lists" apt-get update || return 1
      run_quiet "Installing ${missing[*]}" env DEBIAN_FRONTEND=noninteractive \
        apt-get install -y --no-install-recommends "${missing[@]}" || return 1
    fi
    local saved new_creds=0 pname
    pname=$(acme_provider_name "$NEW_PROVIDER")
    saved=$(acme_py saved 2>/dev/null || true)
    # Moving from certbot: reuse the Cloudflare token saved for it.
    if [[ -z ${NEW_CREDENTIALS:-} && -z $saved && $NEW_PROVIDER == cloudflare && -f $CF_CREDENTIALS ]]; then
      NEW_CREDENTIALS=$(python3 - "$CF_CREDENTIALS" <<'PY'
import json, re, sys
m = re.search(r"dns_cloudflare_api_token\s*=\s*(\S+)", open(sys.argv[1]).read())
print(json.dumps({"CF_DNS_API_TOKEN": m.group(1)}) if m else "")
PY
)
      [[ -n $NEW_CREDENTIALS ]] && log "reusing the Cloudflare token saved for certbot"
    fi
    if [[ -z $(override MESHHOME_TLS_SELF_SIGNED) ]]; then
      if [[ -n ${NEW_CREDENTIALS:-} || $saved != "$NEW_PROVIDER" ]]; then
        local err creds_in=${NEW_CREDENTIALS:-}
        [[ -n $creds_in ]] || creds_in='{}'
        if ! err=$(acme_py write "$NEW_PROVIDER" "$creds_in" 2>&1); then
          warn "${err:-Invalid $pname credentials}"; restore_network_config; return 1
        fi
        new_creds=1
        ok "Saved the $pname credentials (readable by root only)"
      fi
    fi
    TLS_CERT_PATH=$(acme_cert_path "$NEW_HOST") TLS_KEY_PATH=$(acme_key_path "$NEW_HOST")
    local force=0
    [[ -f $TLS_CERT_PATH && ${old_staging:-0} != "$NEW_STAGING" ]] && force=1
    ((new_creds)) && [[ -f $TLS_CERT_PATH ]] && force=1
    if [[ -n $(override MESHHOME_TLS_SELF_SIGNED) || ! -f $TLS_CERT_PATH || $force == 1 || $old_client != lego ]]; then
      status certificate "Requesting a certificate for $NEW_HOST"
      obtain_certificate "$NEW_HOST" "$NEW_EMAIL" "$NEW_STAGING" "$NEW_PROPAGATION" "$force" ||
        { restore_network_config; return 1; }
    else
      ok "Using the existing certificate for $NEW_HOST"
    fi
    write_nginx_site "$NEW_HOST" "$NEW_HTTPS_PORT" "$NEW_REDIRECT" "$NEW_PORT" "$TLS_CERT_PATH" "$TLS_KEY_PATH"
    ln -sfn "$NGINX_SITE" $NGINX_ENABLED
    if ! nginx -t >>"$LOG_FILE" 2>&1; then
      warn "nginx rejected the configuration (see $LOG_FILE)"; restore_network_config; return 1
    fi
    systemctl enable nginx >>"$LOG_FILE" 2>&1 || true
    run_quiet "Reloading nginx" systemctl reload-or-restart nginx || { restore_network_config; return 1; }
    env_set HOST 127.0.0.1
    env_set HTTPS_ENABLED 1
    env_set HTTPS_HOST "$NEW_HOST"
    env_set TLS_CERT "$TLS_CERT_PATH"
    if [[ -z $(override MESHHOME_TLS_SELF_SIGNED) ]]; then
      env_set ACME_CLIENT lego
      env_set ACME_PROVIDER "$NEW_PROVIDER"
      acme_units_install
      # Moved from certbot: lego renews this site now.
      [[ $old_client != lego && -n $old_https_host ]] && retire_certbot_site "$old_https_host"
    fi
  else
    rm -f $NGINX_ENABLED "$NGINX_SITE"
    command -v nginx >/dev/null && { systemctl reload nginx >>"$LOG_FILE" 2>&1 || true; }
    env_set HOST 0.0.0.0
    env_set HTTPS_ENABLED 0
    acme_units_remove
    rm -f "$STATE_DIR/tls-status.json"
  fi
  env_set PORT "$NEW_PORT"
  env_set HTTPS_PORT "$NEW_HTTPS_PORT"
  env_set HTTPS_REDIRECT "$NEW_REDIRECT"
  env_set CERTBOT_EMAIL "$NEW_EMAIL"
  env_set CERTBOT_STAGING "$NEW_STAGING"
  env_set CF_PROPAGATION "$NEW_PROPAGATION"

  [[ $NEW_PORT != "$old_port" || $(env_get HOST) != "$old_host" ]] && restart=1
  if ((restart)); then
    status restarting "Restarting $APP_NAME"
    systemctl restart "$SERVICE" >>"$LOG_FILE" 2>&1 || true
    if ! wait_healthy "$NEW_PORT" "" 60; then
      warn "$APP_NAME did not come back with the new settings"; restore_network_config; return 1
    fi
    ok "$APP_NAME restarted on port $NEW_PORT"
  fi
  if [[ $NEW_HTTPS == 1 ]]; then
    bash "$PREFIX/current/deploy/native/tls-hook" >>"$LOG_FILE" 2>&1 || true
    local -a k=()
    [[ -n $(override MESHHOME_TLS_SELF_SIGNED) ]] && k=(-k)
    if curl -fsS "${k[@]}" --max-time 10 --resolve "$NEW_HOST:$NEW_HTTPS_PORT:127.0.0.1" \
      "https://$NEW_HOST:$NEW_HTTPS_PORT/health/ready" >/dev/null 2>&1; then
      ok "HTTPS is working: $(public_url)"
    else
      warn "nginx is configured, but $(public_url) did not answer locally yet (see $LOG_FILE)"
    fi
  fi
  [[ -n $old_https_host && $old_https_host != "$NEW_HOST" ]] && log "hostname changed from $old_https_host to $NEW_HOST"
  write_network_snapshot
  return 0
}

public_url() {  # the address people should open
  if https_enabled; then
    local hp; hp=$(env_get HTTPS_PORT); hp=${hp:-443}
    if [[ $hp == 443 ]]; then printf 'https://%s' "$(env_get HTTPS_HOST)"; else printf 'https://%s:%s' "$(env_get HTTPS_HOST)" "$hp"; fi
  else
    printf 'http://%s:%s' "$(lan_addresses | head -n1)" "$(env_get PORT)"
  fi
}

load_current_network() {  # NEW_* defaults = current configuration
  NEW_PORT=$(env_get PORT); NEW_PORT=${NEW_PORT:-$DEFAULT_PORT}
  NEW_HTTPS=0; https_enabled && NEW_HTTPS=1
  NEW_HOST=$(env_get HTTPS_HOST)
  NEW_HTTPS_PORT=$(env_get HTTPS_PORT); NEW_HTTPS_PORT=${NEW_HTTPS_PORT:-$DEFAULT_HTTPS_PORT}
  NEW_REDIRECT=$(env_get HTTPS_REDIRECT); NEW_REDIRECT=${NEW_REDIRECT:-1}
  NEW_EMAIL=$(env_get CERTBOT_EMAIL)
  NEW_STAGING=$(env_get CERTBOT_STAGING); NEW_STAGING=${NEW_STAGING:-0}
  NEW_PROPAGATION=$(env_get CF_PROPAGATION); NEW_PROPAGATION=${NEW_PROPAGATION:-$DEFAULT_PROPAGATION}
  NEW_PROVIDER=$(env_get ACME_PROVIDER); NEW_PROVIDER=${NEW_PROVIDER:-cloudflare}
  NEW_CREDENTIALS="" NEW_TOKEN=""
}

https_setup() {  # interactive (terminal) HTTPS setup; returns 1 if not set up — the app stays as it was
  load_current_network
  ((TUI_ON)) || printf '\n'
  note "HTTPS puts nginx in front of $APP_NAME with a trusted Let's Encrypt certificate."
  note "The certificate is validated through your ${B}DNS provider${N}, so the Pi does not need to be reachable from the internet. You need a domain at one of the supported DNS providers, and an API key for it."
  note "${D}(You can also change all of this later in the web interface: Settings → Network & HTTPS.)${N}"

  local fqdn_re='^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$' err=""
  HTTPS_HOST=${HTTPS_HOST:-$NEW_HOST}
  while true; do
    [[ -n $HTTPS_HOST ]] || ask HTTPS_HOST "Hostname for the app (e.g. meshhome.example.com)?" "" "$err"
    HTTPS_HOST=${HTTPS_HOST,,}
    [[ $HTTPS_HOST =~ $fqdn_re ]] && break
    if ((ASSUME_YES)) || [[ ! -r $TTY_IN ]]; then warn "Invalid or missing hostname for HTTPS"; return 1; fi
    err="Enter a full hostname such as meshhome.example.com"; HTTPS_HOST=""
  done
  HTTPS_EMAIL=${HTTPS_EMAIL:-$NEW_EMAIL}
  [[ -n $HTTPS_EMAIL ]] || ask HTTPS_EMAIL "Email for Let's Encrypt expiry notices (optional)?" ""

  # DNS provider: a numbered list, three to a line.
  local -a ids=() names=()
  local id name line="" i
  while IFS=$'\t' read -r id name; do ids+=("$id"); names+=("$name"); done < <(acme_py list)
  ((${#ids[@]})) || { warn "The DNS provider list is missing from this release"; return 1; }
  local current=${MESHHOME_DNS_PROVIDER:-${MESHCORE_HOME_DNS_PROVIDER:-$(env_get ACME_PROVIDER)}}; current=${current:-cloudflare}
  local default=1
  ((TUI_ON)) || printf '\n'
  note "${B}Which DNS provider manages ${HTTPS_HOST#*.}?${N}"
  for ((i = 0; i < ${#ids[@]}; i++)); do
    [[ ${ids[i]} == "$current" ]] && default=$((i + 1))
    local short=${names[i]%% (*}
    printf -v line '%s%3d %-20s' "$line" $((i + 1)) "${short:0:20}"
    if (((i + 1) % 3 == 0)); then note_row "$line"; line=""; fi
  done
  [[ -n $line ]] && note_row "$line"
  note "${D}Not listed? See docs/install-native.md, \"My DNS provider isn't listed\", for delegating validation with one CNAME record.${N}"
  local choice="" pick
  err=""
  while true; do
    ask choice "Number of your DNS provider?" "$default" "$err"
    if [[ $choice =~ ^[0-9]+$ ]] && ((choice >= 1 && choice <= ${#ids[@]})); then break; fi
    if ((ASSUME_YES)) || [[ ! -r $TTY_IN ]]; then choice=$default; break; fi
    err="Enter a number from 1 to ${#ids[@]}"
  done
  pick=${ids[choice - 1]}
  local pname help notes docs
  pname=$(acme_py get "$pick" name); help=$(acme_py get "$pick" help); notes=$(acme_py get "$pick" note); docs=$(acme_py get "$pick" docs)

  # Credentials for that provider (a saved set can be kept).
  local keep=0 saved
  saved=$(acme_py saved 2>/dev/null || true)
  [[ -z $saved && $pick == cloudflare && -f $CF_CREDENTIALS ]] && saved=cloudflare
  ((TUI_ON)) || printf '\n'
  note "${B}$pname${N}: $help"
  [[ -n $notes ]] && note "${Y}$notes${N}"
  note "${D}Details: $docs${N}"
  note "They are stored only in $ACME_DIR (root-only) and used for renewals."
  if [[ $saved == "$pick" ]] && ask_yn "Reuse the $pname credentials saved earlier?" y; then keep=1; fi
  local creds="{}"
  if ((!keep)); then
    local env label secret required kind def choices value
    local -a pairs=()
    while IFS=$'\t' read -r env label secret required kind def choices; do
      value=$(override "$env")  # unattended installs: the value from the environment
      if [[ -z $value ]]; then
        if [[ $kind == json ]]; then
          ask value "$label: path to the JSON key file on this Pi?" ""
          if [[ -n $value && -f $value ]]; then value=$(cat "$value"); elif [[ -n $value ]]; then warn "No such file: $value"; value=""; fi
        elif [[ $secret == 1 ]]; then
          ask_secret value "$label (input hidden):"
        else
          ask value "$label${choices:+ ($choices)}?" "$def"
        fi
      fi
      if [[ -z $value && $required == 1 ]]; then warn "No $label entered; HTTPS not set up."; return 1; fi
      pairs+=("$env" "$value")
    done < <(acme_py fields "$pick")
    creds=$(python3 -c 'import json, sys; a = sys.argv[1:]; print(json.dumps({a[i]: a[i + 1] for i in range(0, len(a), 2) if a[i + 1]}))' "${pairs[@]}")
  fi

  local missing; mapfile -t missing < <(missing_packages "${HTTPS_PACKAGES[@]}")
  if ((${#missing[@]})); then
    apt_review_install "Install these packages for HTTPS?" skip "${missing[@]}" ||
      { warn "HTTPS skipped; $APP_NAME stays on plain HTTP."; return 1; }
  else
    ok "nginx is already installed"
  fi

  ((TUI_ON)) || printf '\n'
  note "${B}These changes will be made for HTTPS:${N}"
  note "  • Download lego (Let's Encrypt client, MIT licence) from GitHub and check its SHA-256"
  note "  • Obtain a Let's Encrypt certificate for ${B}${HTTPS_HOST}${N} through $pname (renewed automatically twice a day as needed)"
  note "  • Add an nginx site on ports 80 and $NEW_HTTPS_PORT for $HTTPS_HOST (port 80 redirects to HTTPS)"
  note "  • Make $APP_NAME listen on 127.0.0.1:$NEW_PORT only, so it is reached through nginx"
  ask_yn "Set up HTTPS?" y || { warn "HTTPS skipped; $APP_NAME stays on plain HTTP."; return 1; }

  NEW_HTTPS=1 NEW_HOST=$HTTPS_HOST NEW_EMAIL=$HTTPS_EMAIL NEW_PROVIDER=$pick NEW_TOKEN=""
  if ((keep)); then NEW_CREDENTIALS=""; else NEW_CREDENTIALS=$creds; fi
  if ! apply_network_config; then
    warn "HTTPS was not set up; $APP_NAME is unchanged. Try again any time with: sudo $CLI_NAME https"
    return 1
  fi
  step_note "$(public_url) · $pname"
}

https_restore_kept() {  # reinstall after a plain uninstall: HTTPS is configured, its nginx site is gone
  # The uninstall kept the settings and the certificate but removed the nginx site and the renewal
  # timer, and the kept settings make the app listen on 127.0.0.1 only. Put HTTPS back from the kept
  # settings (the kept certificate is reused); if that fails, switch to plain HTTP so the app is
  # never left unreachable.
  load_current_network
  NEW_HTTPS=1
  info "Restoring HTTPS for $NEW_HOST from the kept settings"
  if apply_network_config; then
    step_note "$(public_url) (restored)"
    return 0
  fi
  warn "Could not restore HTTPS for $NEW_HOST; switching to plain HTTP so $APP_NAME stays reachable."
  load_current_network
  NEW_HTTPS=0
  if apply_network_config; then
    warn "Set HTTPS up again with: sudo $CLI_NAME https"
  else
    warn "Could not switch to plain HTTP either; see $LOG_FILE"
  fi
  return 1
}

https_disable() {
  if ! https_enabled; then ok "HTTPS is not enabled"; return; fi
  ((TUI_ON)) || printf '\n'
  note "${B}This will${N} remove the nginx site for $(env_get HTTPS_HOST) and make $APP_NAME listen on all interfaces over plain HTTP again. The certificate and saved DNS credentials are kept."
  confirm "Disable HTTPS?"
  load_current_network
  NEW_HTTPS=0
  apply_network_config || die "Could not disable HTTPS; the previous configuration was restored"
  ok "HTTPS disabled — $APP_NAME is on $(public_url)"
}

# ---- backup and restore: the system files the app cannot read ----------------------------------
# The web app makes the database part of a backup itself. These two helper actions pack and unpack
# what only root can read: the network settings, the HTTPS certificate and key, the DNS provider
# credentials (all in $ACME_DIR) and the radio HAT's data ($HAT_DATA). The archive is handed over in
# $STATE_DIR, readable only by the app user, which encrypts it into the backup file at once.
NETWORK_KEYS=(HTTPS_ENABLED HTTPS_HOST PORT HTTPS_PORT HTTPS_REDIRECT CERTBOT_EMAIL CERTBOT_STAGING CF_PROPAGATION ACME_CLIENT ACME_PROVIDER)

system_export() {
  local tmp k v hat=0 was=0 cert="" host
  tmp=$(mktemp -d) || return 1
  chmod 700 "$tmp"
  for k in "${NETWORK_KEYS[@]}"; do
    v=$(env_get "$k"); [[ -n $v ]] && printf '%s=%s\n' "$k" "$v"
  done >"$tmp/network.env"
  if [[ -d $ACME_DIR ]]; then cp -a "$ACME_DIR" "$tmp/acme"; fi
  if [[ -d $HAT_DATA ]] && [[ -n $(ls -A "$HAT_DATA" 2>/dev/null) ]]; then
    # A consistent copy: ZephCore writes its files while running.
    systemctl is-active --quiet "$HAT_SERVICE" 2>/dev/null && was=1
    ((was)) && systemctl stop "$HAT_SERVICE" >>"$LOG_FILE" 2>&1
    cp -a "$HAT_DATA" "$tmp/radio-hat" && hat=1
    ((was)) && systemctl start "$HAT_SERVICE" >>"$LOG_FILE" 2>&1
  fi
  host=$(env_get HTTPS_HOST)
  [[ -n $host && -f $(acme_cert_path "$host") ]] && cert=$(acme_cert_path "$host")
  python3 - "$tmp/system-manifest.json" "$(env_get HTTPS_ENABLED)" "$host" "$(env_get ACME_CLIENT)" \
    "$(env_get ACME_PROVIDER)" "$( [[ -n $(env_get ACME_PROVIDER) ]] && acme_provider_name "$(env_get ACME_PROVIDER)")" \
    "$cert" "$hat" "$(hat_installed_version 2>/dev/null || true)" "$(installed_version)" <<'PY'
import json, ssl, sys, time
out, https, host, client, provider, pname, cert, hat, zeph, app = sys.argv[1:11]
not_after = None
if cert:
    try:
        not_after = ssl._ssl._test_decode_cert(cert).get("notAfter")  # e.g. "Jan  5 12:00:00 2027 GMT"
    except Exception:
        pass
json.dump({
    "https": https == "1", "https_host": host or None, "acme_client": client or None,
    "provider": provider or None, "provider_name": pname or None,
    "cert_included": bool(cert) and client == "lego", "cert_not_after": not_after,
    "hat_data": hat == "1", "zephcore_version": zeph or None, "app_version": app or None,
    "created_at": time.time(),
}, open(out, "w"), indent=1)
PY
  tar -C "$tmp" --numeric-owner -czf "$SYSTEM_EXPORT.tmp" . || { rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
  chown "$APP_USER:$APP_USER" "$SYSTEM_EXPORT.tmp"; chmod 600 "$SYSTEM_EXPORT.tmp"
  mv -f "$SYSTEM_EXPORT.tmp" "$SYSTEM_EXPORT"
}

system_restore() {  # system_restore SHA256 — unpack a backup's system part and apply it
  local want=$1 tmp parsed
  if [[ ! -f $SYSTEM_RESTORE || -L $SYSTEM_RESTORE || $(stat -c %U "$SYSTEM_RESTORE") != "$APP_USER" ]]; then
    warn "No backup system archive to restore"; return 1
  fi
  if [[ $(sha256sum "$SYSTEM_RESTORE" | cut -d' ' -f1) != "$want" ]]; then
    warn "The backup system archive does not match its checksum"; rm -f "$SYSTEM_RESTORE"; return 1
  fi
  tmp=$(mktemp -d) || return 1
  chmod 700 "$tmp"
  # Only regular files and folders, under the expected names, are accepted.
  if ! python3 - "$SYSTEM_RESTORE" "$tmp" <<'PY'
import re, sys, tarfile
src, dest = sys.argv[1:3]
ok = re.compile(r"^(\./)?(network\.env|system-manifest\.json|acme(/[A-Za-z0-9._@+=-]+)*|radio-hat(/[A-Za-z0-9._@+=-]+)*)/?$")
total = 0
with tarfile.open(src, "r:gz") as tar:
    members = []
    for m in tar:
        if m.name in (".", "./"):
            continue
        if not (m.isfile() or m.isdir()) or not ok.match(m.name) or ".." in m.name.split("/"):
            sys.exit(f"unexpected entry in the backup: {m.name!r}")
        total += m.size
        if total > 200 * 1024 * 1024:
            sys.exit("the backup's system files are too large")
        m.uid = m.gid = 0
        m.uname = m.gname = "root"
        members.append(m)
    tar.extractall(dest, members=members, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(dest, members=members)
PY
  then
    warn "The backup's system files were refused"; rm -rf "$tmp"; rm -f "$SYSTEM_RESTORE"; return 1
  fi
  rm -f "$SYSTEM_RESTORE"

  # HTTPS certificate, key, account and DNS credentials. The current ones are kept beside them.
  if [[ -d $tmp/acme ]]; then
    rm -rf "$ACME_DIR.before-restore"
    [[ -d $ACME_DIR ]] && mv "$ACME_DIR" "$ACME_DIR.before-restore"
    cp -a "$tmp/acme" "$ACME_DIR"
    chown -R root:root "$ACME_DIR"; chmod 700 "$ACME_DIR"
    ok "Restored the HTTPS certificate and DNS credentials"
  fi

  # The radio HAT's identity, contacts and channels.
  if [[ -d $tmp/radio-hat ]]; then
    local was=0
    systemctl is-active --quiet "$HAT_SERVICE" 2>/dev/null && was=1
    ((was)) && systemctl stop "$HAT_SERVICE" >>"$LOG_FILE" 2>&1
    rm -rf "$HAT_DATA.before-restore"
    [[ -d $HAT_DATA ]] && mv "$HAT_DATA" "$HAT_DATA.before-restore"
    cp -a "$tmp/radio-hat" "$HAT_DATA"
    if id "$HAT_USER" >/dev/null 2>&1; then chown -R "$HAT_USER:$HAT_USER" "$HAT_DATA"; fi
    chmod 750 "$HAT_DATA"
    ((was)) && systemctl start "$HAT_SERVICE" >>"$LOG_FILE" 2>&1
    ok "Restored the radio HAT's data"
  fi

  # Network settings: applied like a request from Settings → Network (validated, rolled back
  # on failure). With the certificate restored, no new one is requested.
  if [[ -f $tmp/network.env ]]; then
    parsed=$(python3 - "$tmp/network.env" <<'PY'
import re, shlex, sys
allowed = {"HTTPS_ENABLED", "HTTPS_HOST", "PORT", "HTTPS_PORT", "HTTPS_REDIRECT", "CERTBOT_EMAIL",
           "CERTBOT_STAGING", "CF_PROPAGATION", "ACME_CLIENT", "ACME_PROVIDER"}
v = {}
for line in open(sys.argv[1]):
    k, sep, val = line.rstrip("\n").partition("=")
    if sep and k in allowed and not re.search(r"[\x00-\x1f]", val) and len(val) < 300:
        v[k] = val
out = {
    "NEW_HTTPS": "1" if v.get("HTTPS_ENABLED") == "1" else "0", "NEW_HOST": v.get("HTTPS_HOST", "").lower(),
    "NEW_PORT": v.get("PORT", ""), "NEW_HTTPS_PORT": v.get("HTTPS_PORT", "443"),
    "NEW_REDIRECT": "0" if v.get("HTTPS_REDIRECT") == "0" else "1", "NEW_EMAIL": v.get("CERTBOT_EMAIL", ""),
    "NEW_STAGING": "1" if v.get("CERTBOT_STAGING") == "1" else "0", "NEW_PROPAGATION": v.get("CF_PROPAGATION", "0"),
    "NEW_PROVIDER": v.get("ACME_PROVIDER", ""), "RESTORED_CLIENT": v.get("ACME_CLIENT", ""),
}
if not re.fullmatch(r"[a-z0-9]{0,32}", out["NEW_PROVIDER"]):
    out["NEW_PROVIDER"] = ""
for k, val in out.items():
    print(f"{k}={shlex.quote(val)}")
PY
)
    eval "$parsed"
    NEW_CREDENTIALS="" NEW_TOKEN=""
    if [[ $NEW_HTTPS == 1 && -d $tmp/acme && $RESTORED_CLIENT == lego ]]; then
      # Same client and staging as when the certificate was issued: keep it.
      env_set ACME_CLIENT lego
      env_set CERTBOT_STAGING "$NEW_STAGING"
      [[ -n $NEW_PROVIDER ]] && env_set ACME_PROVIDER "$NEW_PROVIDER"
    fi
    if ! apply_network_config; then rm -rf "$tmp"; return 1; fi
  fi
  rm -rf "$tmp"
  write_network_snapshot
}

apply_config_request() {  # --apply-config: run by meshhome-config.service for the web UI
  [[ -f $CONFIG_REQUEST ]] || { log "no config request; nothing to do"; return 0; }
  local parsed
  # Parse and validate with Python, then delete the request at once (it may hold the token).
  parsed=$(python3 - "$CONFIG_REQUEST" <<'PY'
import json, re, shlex, sys
try:
    r = json.load(open(sys.argv[1]))
except Exception:
    print("ACTION=invalid"); sys.exit(0)
def s(k, default=""):
    v = r.get(k, default)
    return default if v is None else str(v)
def flag(k, default):
    v = r.get(k, default)
    return "1" if v in (True, 1, "1") else "0"
action = s("action", "apply")
if action not in ("apply", "renew", "refresh", "hat-install", "hat-remove", "hat-restart", "reboot",
                  "system-export", "system-restore"):
    action = "invalid"
digest = s("sha256").lower()
if action == "system-restore" and not re.fullmatch(r"[0-9a-f]{64}", digest):
    action = "invalid"
token = s("cf_token")
if token and not re.fullmatch(r"[A-Za-z0-9_\-]{20,200}", token):
    action = "invalid"
provider = s("dns_provider", "cloudflare")
if not re.fullmatch(r"[a-z0-9]{2,32}", provider):
    action = "invalid"
creds = r.get("credentials") or {}
if not isinstance(creds, dict) or any(not re.fullmatch(r"[A-Z0-9_]{1,64}", str(k)) or not isinstance(v, str) or len(v) > 20000 for k, v in creds.items()):
    action = "invalid"
creds_json = json.dumps(creds) if creds else ""
out = {
    "ACTION": action,
    "NEW_PORT": s("app_port"), "NEW_HTTPS": flag("https_enabled", False), "NEW_HOST": s("hostname").lower(),
    "NEW_HTTPS_PORT": s("https_port", "443"), "NEW_REDIRECT": flag("redirect_http", True),
    "NEW_EMAIL": s("email"), "NEW_STAGING": flag("staging", False), "NEW_PROPAGATION": s("propagation_seconds", "30"),
    "NEW_TOKEN": token, "NEW_PROVIDER": provider, "RESTORE_SHA256": digest,
}
for k, v in out.items():
    if re.search(r"[\x00-\x1f]", v):
        print("ACTION=invalid"); sys.exit(0)
    print(f"{k}={shlex.quote(v)}")
# JSON keeps any newlines (e.g. a Google key) escaped, so this is one shell word.
print(f"NEW_CREDENTIALS={shlex.quote(creds_json)}")
PY
)
  rm -f "$CONFIG_REQUEST"
  eval "$parsed"
  STATUS_FILE=$CONFIG_STATUS
  case "${ACTION:-invalid}" in
    refresh) write_network_snapshot; return 0 ;;
    system-export)
      status applying "Packing the system settings for a backup"
      if system_export; then status "done" "System settings packed"; else status failed "Could not pack the system settings"; return 1; fi ;;
    system-restore)
      status applying "Restoring the system settings from a backup"
      if system_restore "$RESTORE_SHA256"; then
        status "done" "System settings restored — $APP_NAME is on $(public_url)"
      else
        status failed "System settings were not restored; the previous configuration is still in place"
        return 1
      fi ;;
    hat-install)
      status applying "Setting up the radio HAT"
      if hat_install; then status "done" "Radio HAT set up"; else status failed "Radio HAT not set up"; return 1; fi ;;
    hat-remove)
      status applying "Removing the radio HAT software"
      hat_lock || true
      hat_remove 0; status "done" "Radio HAT software removed" ;;
    hat-restart)
      status applying "Restarting the radio"
      hat_lock || true
      systemctl restart "$HAT_SERVICE" >>"$LOG_FILE" 2>&1 || true
      if hat_wait_ready 30; then hat_status ready "Restarted"; status "done" "Radio restarted"
      else hat_status failed "The radio service did not start"; status failed "The radio service did not start"; return 1; fi ;;
    reboot)
      log "restarting the Pi (requested from the web UI)"
      hat_status rebooting "Restarting the Pi"
      status "done" "Restarting the Pi"
      systemctl reboot ;;
    renew)
      if ! https_enabled; then status failed "HTTPS is not enabled"; return 1; fi
      status certificate "Renewing the certificate for $(env_get HTTPS_HOST)"
      local renewed=1
      if [[ $(env_get ACME_CLIENT) == lego ]]; then
        lego_install >>"$LOG_FILE" 2>&1 || true
        acme_renew force || renewed=0
      else
        run_quiet "Renewing the certificate" certbot renew --cert-name "$(env_get HTTPS_HOST)" --force-renewal || renewed=0
      fi
      if ((renewed)); then
        bash "$PREFIX/current/deploy/native/tls-hook" >>"$LOG_FILE" 2>&1 || true
        write_network_snapshot
        status "done" "Certificate renewed"
      else
        status failed "Certificate renewal failed — see the installer log"
        return 1
      fi ;;
    apply)
      status applying "Applying network settings"
      if apply_network_config; then
        status "done" "Settings applied — $APP_NAME is on $(public_url)"
      else
        write_network_snapshot
        status failed "Settings were not applied; the previous configuration is still in place"
        return 1
      fi ;;
    *) status failed "Ignored an invalid settings request"; return 1 ;;
  esac
}

sync_units() {  # --sync-units: run as root by meshhome.service (ExecStartPre=+) on every start
  # Installs helper units that ship with the running release (so a web upgrade from an older
  # version gains new ones), then refreshes the snapshot the app shows. Never fails the start.
  local src="$PREFIX/current/deploy/native/systemd" u changed=0
  for u in "${HELPER_UNITS[@]}"; do put_unit "$src" "$u" && changed=1; done
  ((changed)) && systemctl daemon-reload || true
  for u in "${HELPER_UNITS[@]}"; do
    [[ $u == *.path && -f $UNIT_DIR/$u ]] || continue
    systemctl is-enabled --quiet "$u" 2>/dev/null || systemctl enable --quiet "$u" 2>/dev/null || true
    systemctl is-active --quiet "$u" 2>/dev/null || systemctl start --no-block "$u" 2>/dev/null || true
  done
  write_network_snapshot
  lan_discovery_retire
  nginx_push_refresh || true
  return 0
}

# ---- optional radio HAT (RAK6421 + RAK13300) driven by ZephCore --------------------------------
# ZephCore is a port of the MeshCore firmware that runs as a Linux program: it drives the SX1262 on
# the HAT over SPI and serves the MeshCore companion protocol on TCP port 5000, where MeshHome
# connects ("Radio HAT on this Pi"). It runs as its own unprivileged service that only this Pi can
# reach. The radio's settings, contacts and channels are managed from MeshHome.
HAT_SPIDEV=/dev/spidev0.0
HAT_PROBLEM=""

override() {  # override MESHHOME_X — a test/mirror override from the environment or the root-owned
  # env file; MESHCORE_HOME_X (the name before the rename) is read too.
  local old="MESHCORE_HOME_${1#MESHHOME_}" v=${!1:-}
  [[ -n $v ]] || v=$(env_get "$1")
  [[ -n $v ]] || v=${!old:-}
  [[ -n $v ]] || v=$(env_get "$old")
  printf '%s' "$v"
}

hat_src() {  # the release directory that holds the radio HAT files being installed
  local d
  for d in ${TARGET_VERSION:+"$PREFIX/releases/$TARGET_VERSION/deploy/native"} "$PREFIX/current/deploy/native"; do
    [[ -f $d/zephcore.lock ]] && { printf '%s' "$d"; return 0; }
  done
  return 1
}

hat_lock() {  # load the pinned ZephCore release (ZEPHCORE_*) from the release being installed
  local d; d=$(hat_src) || return 1
  # shellcheck disable=SC1091
  . "$d/zephcore.lock"
}

pi_model() {
  local m; m=$(override MESHHOME_PI_MODEL)
  [[ -n $m || ! -r /proc/device-tree/model ]] || m=$(tr -d '\0' </proc/device-tree/model)
  printf '%s' "$m"
}

hat_board() {  # pi4 | pi5 | nothing when the HAT is not supported on this computer
  case "$(pi_model)" in
    *"Raspberry Pi 5"* | *"Compute Module 5"*) echo pi5 ;;  # also matches the Pi 500
    *"Raspberry Pi 4"* | *"Compute Module 4"*) echo pi4 ;;  # also matches the Pi 400
  esac
}

hat_product() {  # the HAT's EEPROM identity, if the Pi firmware found one
  local p="" v=""
  [[ -r /proc/device-tree/hat/product ]] && p=$(tr -d '\0' </proc/device-tree/hat/product)
  [[ -r /proc/device-tree/hat/vendor ]] && v=$(tr -d '\0' </proc/device-tree/hat/vendor)
  if [[ -n $p ]]; then printf '%s%s' "$p" "${v:+ ($v)}"; fi
}

glibc_version() { getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}'; }
version_ge() { [[ $(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1) == "$2" ]]; }  # version_ge A B: A >= B

boot_config() {
  local f; f=$(override MESHHOME_BOOT_CONFIG)
  if [[ -n $f ]]; then printf '%s' "$f"; return; fi
  for f in /boot/firmware/config.txt /boot/config.txt; do
    if [[ -f $f ]]; then printf '%s' "$f"; return; fi
  done
}

spi_configured() { local f; f=$(boot_config); [[ -n $f && -f $f ]] && grep -qE '^[[:space:]]*dtparam=spi=on' "$f"; }

meshtastic_present() {
  [[ -x /usr/bin/meshtasticd || -x /usr/sbin/meshtasticd ]] && return 0
  [[ $(systemctl list-unit-files meshtasticd.service --no-legend 2>/dev/null) == *meshtasticd* ]]
}

hat_test_mode() { [[ -n $(override MESHHOME_HAT_TEST) ]]; }  # containers: no SPI device or HAT EEPROM

hat_installed_version() {
  local t; t=$(readlink -f "$HAT_DIR/zephcore" 2>/dev/null || true)
  [[ -n $t && -f $t ]] && basename "$t" | sed 's/^zephcore-//'
}

hat_listening() { [[ -n $(ss -Hltn "sport = :${ZEPHCORE_PORT:-5000}" 2>/dev/null) ]]; }

hat_status() {  # hat_status STATE MESSAGE — facts and progress for the web UI (world-readable, no secrets)
  [[ -d $STATE_DIR ]] || return 0
  python3 - "$HAT_STATUS" "$1" "$2" "${ZEPHCORE_VERSION:-}" "$(hat_installed_version)" "$(hat_board)" \
    "$(pi_model)" "$(hat_product)" "$(glibc_version)" "$(boot_config)" <<'PY' || true
import json, os, sys, time
path, state, message, pinned, installed, board, model, product, glibc, bootcfg = sys.argv[1:11]
data = {
    "state": state, "message": message, "pinned_version": pinned or None,
    "installed_version": installed or None, "board": board or None, "model": model or None,
    "hat_product": product or None, "glibc": glibc or None, "boot_config": bootcfg or None,
    "updated_at": time.time(),
}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
}

hat_check() {  # sets HAT_PROBLEM and returns 1 if the radio HAT software cannot run here
  HAT_PROBLEM=""
  hat_lock || { HAT_PROBLEM="This release does not include the radio HAT files"; return 1; }
  if [[ $(uname -m) != aarch64 ]]; then
    HAT_PROBLEM="The radio HAT needs 64-bit Raspberry Pi OS (found $(uname -m))"; return 1
  fi
  if [[ -z $(hat_board) ]]; then
    local model; model=$(pi_model)
    HAT_PROBLEM="The RAK6421 radio HAT works on a Raspberry Pi 4 or 5 (this computer: ${model:-not a Raspberry Pi})"
    return 1
  fi
  local g; g=$(glibc_version)
  if ! version_ge "${g:-0}" "$ZEPHCORE_MIN_GLIBC"; then
    HAT_PROBLEM="The radio software (ZephCore) needs Raspberry Pi OS 13 \"Trixie\" or Debian 13 (glibc $ZEPHCORE_MIN_GLIBC or newer); this system has glibc ${g:-unknown}. Re-image the SD card with the current 64-bit Raspberry Pi OS, then install MeshHome again."
    return 1
  fi
  if meshtastic_present; then
    HAT_PROBLEM="Meshtastic (meshtasticd) is installed and would take over the radio. Remove it first: sudo apt remove meshtasticd"
    return 1
  fi
}

hat_download() {  # hat_download — fetch and verify the pinned ZephCore build into $HAT_DIR; returns 1 on failure
  local board asset sha base tmp
  board=$(hat_board)
  if [[ $board == pi5 ]]; then asset=$ZEPHCORE_PI5_ASSET sha=$ZEPHCORE_PI5_SHA256; else asset=$ZEPHCORE_PI4_ASSET sha=$ZEPHCORE_PI4_SHA256; fi
  base=$(override MESHHOME_ZEPHCORE_BASE); base=${base:-$ZEPHCORE_BASE_URL}
  tmp=$(mktemp)
  if ! run_bg "Downloading ZephCore $ZEPHCORE_VERSION" curl -fsSL --retry 3 -o "$tmp" "$base/$asset"; then
    rm -f "$tmp"; HAT_PROBLEM="Could not download $base/$asset (check internet access)"; return 1
  fi
  if [[ $(sha256sum "$tmp" | cut -d' ' -f1) != "$sha" ]]; then
    rm -f "$tmp"; HAT_PROBLEM="The ZephCore download failed its SHA-256 check, so it was not installed"; return 1
  fi
  install -d -m 755 "$HAT_DIR"
  install -m 755 "$tmp" "$HAT_DIR/zephcore-$ZEPHCORE_VERSION"
  rm -f "$tmp"
  ok "Downloaded and verified ZephCore $ZEPHCORE_VERSION ($board build)"
}

hat_activate_binary() {  # point $HAT_DIR/zephcore at version $1
  ln -sfn "zephcore-$1" "$HAT_DIR/zephcore.new" && mv -T "$HAT_DIR/zephcore.new" "$HAT_DIR/zephcore"
}

hat_install_files() {  # the start script and unit from the release (also refreshed on upgrades)
  local src; src=$(hat_src) || return 1
  install -m 755 "$src/radio-hat-run" "$HAT_DIR/run"
  put_unit "$src/systemd" "$HAT_SERVICE.service" || true
  if hat_test_mode; then
    # Testing in a container: no SPI device, so start without it (the radio itself stays absent).
    install -d -m 755 "$HAT_UNIT.d"
    printf '[Unit]\nConditionPathExists=\n' >"$HAT_UNIT.d/test.conf"
  fi
  systemctl daemon-reload
}

hat_wait_ready() {  # hat_wait_ready SECONDS — the companion port is listening (no connection is made)
  local end=$((SECONDS + $1))
  while ((SECONDS < end)); do
    hat_listening && return 0
    sleep 1
  done
  return 1
}

hat_install() {  # interactive or web-UI setup; returns 1 (without exiting) if the HAT was not set up
  hat_status checking "Checking this Raspberry Pi"
  if ! hat_check; then
    warn "$HAT_PROBLEM"; hat_status unsupported "$HAT_PROBLEM"; return 1
  fi
  local bootcfg product
  bootcfg=$(boot_config); product=$(hat_product)
  ((TUI_ON)) || printf '\n'
  note "${B}Radio HAT${N} on this $(pi_model)${product:+ · detected: $product}"
  [[ -n $product ]] || hat_test_mode ||
    note "${Y}The Pi did not report a HAT.${N} Check that the RAK6421 is seated, with the radio module in IO slot 1 and the antenna attached."
  note "${B}These changes will be made:${N}"
  note "  • Download ZephCore $ZEPHCORE_VERSION (MeshCore for Linux, MIT licence, about 5 MB) from GitHub, check its SHA-256 and install it in $HAT_DIR"
  spi_configured || note "  • Turn on SPI in ${bootcfg:-the boot configuration} (dtparam=spi=on); the Pi needs ${B}one restart${N} afterwards"
  note "  • Create the system user $HAT_USER, allowed to use the SPI and GPIO devices and nothing else"
  note "  • Install the service $HAT_SERVICE: starts at boot, restarts if it stops, and only this Pi can connect to it (port $ZEPHCORE_PORT)"
  note "  • Keep the radio's identity, contacts and channels in $HAT_DATA"
  note "${Y}ZephCore starts on 869.618 MHz, the EU/UK frequency.${N} Before using it, set your region's frequency in MeshHome → Settings → Node settings."
  ask_yn "Set up the radio HAT?" y || { info "Skipped. Set it up later with: sudo $CLI_NAME radio-hat"; hat_status absent "Not set up"; return 1; }

  hat_status installing "Downloading ZephCore $ZEPHCORE_VERSION"
  if ! hat_download; then warn "$HAT_PROBLEM"; hat_status failed "$HAT_PROBLEM"; return 1; fi
  hat_activate_binary "$ZEPHCORE_VERSION"

  hat_status installing "Setting up the radio service"
  getent group spi >/dev/null || groupadd --system spi
  getent group gpio >/dev/null || groupadd --system gpio
  if ! id "$HAT_USER" >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --home-dir "$HAT_DATA" --shell /usr/sbin/nologin "$HAT_USER"
  fi
  usermod -aG spi,gpio "$HAT_USER"
  install -d -m 750 -o "$HAT_USER" -g "$HAT_USER" "$HAT_DATA"
  chown -R "$HAT_USER:$HAT_USER" "$HAT_DATA"  # e.g. an identity restored from a backup before setup
  install -d -m 755 "$(dirname "$HAT_UDEV_RULE")"
  cat >"$HAT_UDEV_RULE" <<'RULE'
# MeshHome radio HAT: the "spi" and "gpio" groups may use the radio's SPI bus and GPIO lines.
KERNEL=="spidev*", GROUP="spi", MODE="0660"
SUBSYSTEM=="gpio", KERNEL=="gpiochip*", GROUP="gpio", MODE="0660"
RULE
  if command -v udevadm >/dev/null; then
    udevadm control --reload-rules >>"$LOG_FILE" 2>&1 || true
    udevadm trigger --subsystem-match=spidev --subsystem-match=gpio >>"$LOG_FILE" 2>&1 || true
  fi
  ok "System user $HAT_USER with access to SPI and GPIO"

  if ! spi_configured; then
    if [[ -z $bootcfg ]]; then
      warn "No boot configuration file found: turn on SPI with raspi-config (Interface Options → SPI)"
    else
      cp -p "$bootcfg" "$bootcfg.meshcore-home.bak"
      printf '\n# Added by MeshHome for the radio HAT (SPI bus for the LoRa module)\n[all]\ndtparam=spi=on\n' >>"$bootcfg"
      ok "Turned on SPI in $bootcfg (previous version saved as $bootcfg.meshcore-home.bak)"
    fi
  fi

  hat_install_files || { hat_status failed "Radio HAT files are missing from this release"; return 1; }
  systemctl enable "$HAT_SERVICE" >>"$LOG_FILE" 2>&1
  if [[ ! -e $HAT_SPIDEV ]] && ! hat_test_mode; then
    hat_status needs_reboot "Restart the Pi to finish: SPI was just turned on. The radio starts automatically afterwards."
    warn "Restart the Pi to finish setting up the radio HAT (SPI was just turned on): sudo reboot"
    step_note "ZephCore $ZEPHCORE_VERSION · restart the Pi to finish"
    return 0
  fi
  run_bg "Starting the radio" systemctl restart "$HAT_SERVICE" || true
  if hat_wait_ready 30; then
    ok "The radio HAT is running (MeshCore companion on 127.0.0.1:$ZEPHCORE_PORT)"
    step_note "ZephCore $ZEPHCORE_VERSION · running"
    hat_status ready "Running"
    return 0
  fi
  journalctl -u "$HAT_SERVICE" -n 30 --no-pager >>"$LOG_FILE" 2>&1 || true
  warn "The radio service did not start; see: sudo $CLI_NAME radio-hat logs"
  hat_status failed "The radio service did not start. Details: sudo $CLI_NAME radio-hat logs"
  return 1
}

hat_remove() {  # hat_remove PURGE(0/1) — remove the radio HAT software; PURGE also deletes the radio's identity
  systemctl disable --now "$HAT_SERVICE" >>"$LOG_FILE" 2>&1 || true
  rm -f "$HAT_UNIT"
  rm -rf "${HAT_UNIT:?}.d" "${HAT_DIR:?}"
  rm -f "${HAT_UDEV_RULE:?}"
  systemctl daemon-reload
  if (($1)); then
    rm -rf "${HAT_DATA:?}"
    userdel "$HAT_USER" >>"$LOG_FILE" 2>&1 || true
    ok "Radio HAT software and the radio's identity removed"
  else
    ok "Radio HAT software removed (the radio's identity is kept in $HAT_DATA)"
  fi
  hat_status absent "Not set up"
}

hat_sync() {  # --radio-hat-sync: after an upgrade, bring an installed HAT to this release's pinned ZephCore
  [[ -f $HAT_UNIT ]] || return 0
  hat_lock || return 0
  local cur; cur=$(hat_installed_version)
  hat_install_files || return 0
  if [[ $cur == "$ZEPHCORE_VERSION" ]]; then
    if [[ -e $HAT_SPIDEV ]] || hat_test_mode; then systemctl try-restart "$HAT_SERVICE" >>"$LOG_FILE" 2>&1 || true; fi
    hat_status "$( (systemctl is-active --quiet "$HAT_SERVICE") && echo ready || echo installed)" "ZephCore $cur"
    return 0
  fi
  log "radio HAT: ZephCore ${cur:-none} -> $ZEPHCORE_VERSION"
  if ! hat_check; then hat_status failed "Could not update ZephCore: $HAT_PROBLEM"; return 1; fi
  if ! hat_download; then hat_status failed "Could not update ZephCore: $HAT_PROBLEM"; return 1; fi
  hat_activate_binary "$ZEPHCORE_VERSION"
  if [[ ! -e $HAT_SPIDEV ]] && ! hat_test_mode; then hat_status needs_reboot "Restart the Pi to start the radio"; return 0; fi
  systemctl restart "$HAT_SERVICE" >>"$LOG_FILE" 2>&1 || true
  if hat_wait_ready 30; then
    [[ -n $cur && $cur != "$ZEPHCORE_VERSION" ]] && rm -f "$HAT_DIR/zephcore-$cur"
    hat_status ready "Updated to ZephCore $ZEPHCORE_VERSION"
    return 0
  fi
  if [[ -n $cur && -f $HAT_DIR/zephcore-$cur ]]; then  # roll back to the version that worked
    hat_activate_binary "$cur"
    systemctl restart "$HAT_SERVICE" >>"$LOG_FILE" 2>&1 || true
    rm -f "$HAT_DIR/zephcore-$ZEPHCORE_VERSION"
    hat_status ready "ZephCore $ZEPHCORE_VERSION did not start; kept $cur"
  else
    hat_status failed "ZephCore $ZEPHCORE_VERSION did not start"
  fi
  return 1
}

radio_hat_after_upgrade() {  # run the new release's HAT sync (an older installer cannot know its files)
  [[ -f $HAT_UNIT ]] || return 0
  if bash "$PREFIX/current/deploy/native/install.sh" --radio-hat-sync >>"$LOG_FILE" 2>&1; then
    ok "Radio HAT software is up to date"
  else
    warn "The radio HAT software was not updated (the previous version keeps running); see $LOG_FILE"
  fi
}

# ---- optional automatic OS security updates (Debian's unattended-upgrades) -------------------
security_updates_enabled() {
  # Here-strings, not pipes into `grep -q`: with pipefail, grep exiting early makes a long
  # producer (apt-config dump) die of SIGPIPE and the check would wrongly fail.
  local status conf
  status=$(dpkg-query -W -f='${db:Status-Status}' unattended-upgrades 2>/dev/null || true)
  conf=$(apt-config dump 2>/dev/null || true)
  [[ $status == installed ]] && grep -q '^APT::Periodic::Unattended-Upgrade "1";' <<<"$conf"
}

security_updates_setup() {  # returns 1 if skipped; never cancels the installer
  if security_updates_enabled; then ok "Automatic security updates are already enabled"; step_note "Already on"; return 0; fi
  ((TUI_ON)) || printf '\n'
  note "Debian can install ${B}security updates${N} for the operating system automatically (unattended-upgrades), which is recommended for an always-on Pi. It only applies security fixes from your OS repositories; it never upgrades $APP_NAME itself (you choose when to do that)."
  ask_yn "Enable automatic security updates?" y || { info "Skipped. Enable later with: sudo $CLI_NAME security-updates"; return 1; }
  local missing; mapfile -t missing < <(missing_packages unattended-upgrades)
  if ((${#missing[@]})); then
    apt_review_install "Install this package?" skip "${missing[@]}" ||
      { info "Skipped. Enable later with: sudo $CLI_NAME security-updates"; return 1; }
  fi
  # Debian's documented way to switch it on (writes /etc/apt/apt.conf.d/20auto-upgrades).
  echo "unattended-upgrades unattended-upgrades/enable_auto_updates boolean true" | debconf-set-selections
  run "Enabling automatic security updates" env DEBIAN_FRONTEND=noninteractive dpkg-reconfigure -f noninteractive unattended-upgrades
  if security_updates_enabled; then
    ok "Security updates will be installed automatically (daily, via apt-daily-upgrade.timer)"
    step_note "On (daily)"
  else
    warn "unattended-upgrades is installed but not enabled; see $LOG_FILE"
    return 1
  fi
}

read_request() {  # web-UI upgrade: take the version from the request file written by the app
  local req="$STATE_DIR/update-request.json"
  [[ -f $req ]] || { log "no update request; nothing to do"; exit 0; }
  local v; v=$(json_field version <"$req" 2>/dev/null || true)
  rm -f "$req"
  valid_version "$v" || { TARGET_VERSION=""; die "Ignoring invalid update request"; }
  WANT_VERSION=$v
  # One privileged change at a time (shared with web-UI network settings).
  exec 9>$ADMIN_LOCK; flock -w 900 9 || true
  # Mirror overrides live in the root-owned env file (never in the app-writable request).
  API_URL=$(override MESHHOME_API); API_URL=${API_URL:-https://api.github.com}
  DOWNLOAD_BASE=$(override MESHHOME_DOWNLOAD_BASE)
  local repo; repo=$(env_get UPDATE_REPO); REPO=${repo:-$REPO}
  TARGET_VERSION=$v
  status queued "Preparing to upgrade to v$v"
}

# ---- main ----------------------------------------------------------------------------------
# ---- LAN discovery, retired -----------------------------------------------------------------
# Earlier installs could advertise "_meshhome._tcp" (Bonjour) so the MeshHome mobile app, which
# no longer exists, could find them. The advertisement is removed on the next start (sync_units);
# avahi-daemon itself stays installed, as other software may use it.
AVAHI_SERVICE=/etc/avahi/services/meshhome.service

lan_discovery_retire() {
  [[ -f $AVAHI_SERVICE ]] || return 0
  rm -f "$AVAHI_SERVICE" && log "removed the LAN discovery advertisement ($AVAHI_SERVICE): nothing uses it any more"
}

# ---- migrate: move an install from before the rename to the MeshHome names ------------------------
# Renames, in one guided step: the database and its role (meshcore → meshhome), the system users
# (meshcore, meshcore-radio → meshhome, meshhome-radio), the directories under /opt, /etc and
# /var/lib, the env file, the systemd units, the udev rule, the nginx site and the command. Data is
# moved, never copied. Every step records how to undo itself; any failure (or an app that does not
# come back healthy) undoes them all, newest first, and restarts the install as it was.
MIGRATING=0 UNDO=()
undo_push() { local s; printf -v s '%q ' "$@"; UNDO+=("$s"); }  # undo_push CMD ARGS… (run on failure)
migrate_undo() {
  MIGRATING=0
  ((${#UNDO[@]})) || return 0
  warn "Undoing the migration"
  local i
  for ((i = ${#UNDO[@]} - 1; i >= 0; i--)); do
    log "undo: ${UNDO[i]}"
    eval "${UNDO[i]}" >>"$LOG_FILE" 2>&1 || log "undo step failed: ${UNDO[i]}"
  done
  UNDO=()
  log "migration undone"
}
move_path() {  # move_path FROM TO — rename one file or directory (if present), with its undo
  [[ -e $1 || -L $1 ]] || return 0
  mv -T "$1" "$2"
  undo_push mv -T "$2" "$1"
}
edit_file() {  # edit_file FILE SED-ARGS… — edit in place, with its undo (the original is kept)
  [[ -f $1 ]] || return 0
  local f=$1; shift
  cp -p "$f" "$f.premigrate"
  sed -i "$@" "$f"
  undo_push mv -f "$f.premigrate" "$f"
}
MIGRATE_PATHS=(-e "s#/opt/meshcore-home#/opt/meshhome#g" -e "s#/etc/meshcore-home#/etc/meshhome#g"
  -e "s#/var/lib/meshcore-home#/var/lib/meshhome#g" -e "s#/etc/letsencrypt/meshcore-home-#/etc/letsencrypt/meshhome-#g")

migrate_layout() {
  local log_file=$LOG_FILE
  if [[ $LAYOUT == new ]]; then
    [[ -d $NEW_PREFIX ]] || die "$APP_NAME is not installed"
    printf '\n  %s✓ This install already uses the MeshHome names; nothing to migrate.%s\n\n' "$G" "$N"
    return 0
  fi
  TARGET_VERSION=$(installed_version)
  [[ -n $TARGET_VERSION ]] || die "$APP_NAME is not installed"
  [[ -f $PREFIX/current/deploy/native/systemd/app.service ]] ||
    die "v$TARGET_VERSION is too old to migrate" "Upgrade first: sudo $CLI_NAME update   (then run: sudo $CLI_NAME migrate)"
  exec 9>"$ADMIN_LOCK"; flock -n 9 || die "Another change is in progress (an upgrade or a settings change)" "Try again in a few minutes."

  tui_start "Migrate to the MeshHome names"
  banner
  STEP_WEIGHTS=(1 2 6 3 3 3 10)
  plan "Review the changes" "Stop $APP_NAME" "Back up the database" "Rename the database and system users" \
    "Move files and settings" "Install the renamed services" "Start and check"

  step
  local problem="" p
  for p in "$NEW_CONF_DIR" "$NEW_STATE_DIR" /opt/meshhome-acme /opt/meshhome-radio /var/lib/meshhome-radio \
    /etc/nginx/sites-available/meshhome "$UNIT_DIR/meshhome.service"; do
    [[ -e $p ]] && problem="$p already exists"
  done
  for p in "$NEW_USER" meshhome-radio; do id "$p" >/dev/null 2>&1 && problem="The system user $p already exists"; done
  [[ -n $(runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$NEW_USER'" 2>/dev/null) ]] &&
    problem="The database role $NEW_USER already exists"
  [[ -n $(runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='$NEW_USER'" 2>/dev/null) ]] &&
    problem="The database $NEW_USER already exists"
  [[ $(env_get DATABASE_URL) == "postgresql+asyncpg://$OLD_USER@/$OLD_USER?host="* ]] ||
    problem="DATABASE_URL in $ENV_FILE is not the installer's default, so the database can't be renamed automatically"
  [[ -n $problem ]] && die "Can't migrate: $problem" "Nothing was changed."

  local hat=0 https=0 acme=0 certbot=0
  [[ -f $HAT_UNIT ]] && hat=1
  [[ -f $NGINX_SITE ]] && https=1
  [[ -f $UNIT_DIR/$SERVICE-acme-renew.timer ]] && acme=1
  [[ -f $CF_CREDENTIALS ]] && certbot=1
  ((TUI_ON)) || printf '\n'
  note "${B}v$TARGET_VERSION is installed under the names from before the rename to MeshHome.${N} This moves it to the new ones:"
  note "  • Database and role ${B}$OLD_USER${N} → ${B}$NEW_USER${N}; system user ${B}$OLD_USER${N} → ${B}$NEW_USER${N}"
  note "  • $OLD_PREFIX, $OLD_CONF_DIR, $OLD_STATE_DIR → ${B}$NEW_PREFIX, $NEW_CONF_DIR, $NEW_STATE_DIR${N}"
  note "  • Services $OLD_SERVICE* → ${B}$NEW_SERVICE*${N}; command ${B}sudo $NEW_SERVICE${N} (sudo $OLD_SERVICE keeps working)"
  ((https)) && note "  • The nginx HTTPS site and its certificate paths"
  ((hat)) && note "  • The radio HAT: service, system user (meshhome-radio) and files (the radio's identity moves with it)"
  note "Your messages, settings, sign-ins, API keys and HTTPS address stay as they are. $APP_NAME is offline"
  note "for about a minute. A database backup is made first, and if anything fails, every step is undone."
  confirm "Migrate now?"

  step
  local -a units=("$SERVICE.service" "${HELPER_UNITS[@]}") enabled=() u
  ((acme)) && units+=("${ACME_UNITS[@]}")
  ((hat)) && units+=("$HAT_SERVICE.service")
  for u in "${units[@]}"; do systemctl is-enabled --quiet "$u" 2>/dev/null && enabled+=("$u"); done
  log "enabled units: ${enabled[*]}"
  MIGRATING=1
  undo_push systemctl start "$SERVICE.service"
  ((hat)) && undo_push systemctl start "$HAT_SERVICE.service"
  ((acme)) && undo_push systemctl start "$SERVICE-acme-renew.timer"
  undo_push systemctl start "$SERVICE-update.path" "$SERVICE-config.path"
  run "Stopping $APP_NAME" systemctl stop "$SERVICE-update.path" "$SERVICE-config.path" "$SERVICE.service"
  ((acme)) && { systemctl stop "$SERVICE-acme-renew.timer" >>"$LOG_FILE" 2>&1 || true; }
  ((hat)) && run "Stopping the radio HAT" systemctl stop "$HAT_SERVICE.service"

  step; backup_database

  step
  run "Renaming the database $OLD_USER to $NEW_USER" runuser -u postgres -- psql -v ON_ERROR_STOP=1 -qc \
    "ALTER DATABASE \"$OLD_USER\" RENAME TO \"$NEW_USER\"; ALTER ROLE \"$OLD_USER\" RENAME TO \"$NEW_USER\""
  undo_push runuser -u postgres -- psql -qc "ALTER DATABASE \"$NEW_USER\" RENAME TO \"$OLD_USER\"; ALTER ROLE \"$NEW_USER\" RENAME TO \"$OLD_USER\""
  usermod -l "$NEW_USER" -d "$NEW_STATE_DIR" "$OLD_USER" >>"$LOG_FILE" 2>&1
  undo_push usermod -l "$OLD_USER" -d "$OLD_STATE_DIR" "$NEW_USER"
  groupmod -n "$NEW_USER" "$OLD_USER" >>"$LOG_FILE" 2>&1
  undo_push groupmod -n "$OLD_USER" "$NEW_USER"
  if id meshcore-radio >/dev/null 2>&1; then
    usermod -l meshhome-radio -d /var/lib/meshhome-radio meshcore-radio >>"$LOG_FILE" 2>&1
    undo_push usermod -l meshcore-radio -d /var/lib/meshcore-radio meshhome-radio
    groupmod -n meshhome-radio meshcore-radio >>"$LOG_FILE" 2>&1
    undo_push groupmod -n meshcore-radio meshhome-radio
  fi
  ok "Database, role and system users renamed"

  step
  local old_current; old_current=$(readlink "$PREFIX/current")
  move_path "$OLD_PREFIX" "$NEW_PREFIX"
  ln -sfn "$NEW_PREFIX/releases/$(basename "$old_current")" "$NEW_PREFIX/current"
  undo_push ln -sfn "$old_current" "$NEW_PREFIX/current"
  # Virtualenv scripts name their own path; the units use "python -m", but keep the scripts working.
  local f
  while IFS= read -r f; do edit_file "$f" -e "s#$OLD_PREFIX/#$NEW_PREFIX/#g"; done \
    < <(grep -rlI --exclude='*.premigrate' "$OLD_PREFIX/" "$NEW_PREFIX"/releases/*/venv/bin "$NEW_PREFIX"/releases/*/venv/pyvenv.cfg 2>/dev/null || true)
  move_path "$OLD_CONF_DIR/$OLD_SERVICE.env" "$OLD_CONF_DIR/$NEW_SERVICE.env"
  move_path "$OLD_CONF_DIR" "$NEW_CONF_DIR"
  move_path "$OLD_STATE_DIR" "$NEW_STATE_DIR"
  move_path /opt/meshcore-home-acme /opt/meshhome-acme
  move_path /opt/meshcore-home-radio /opt/meshhome-radio
  move_path /var/lib/meshcore-home-radio /var/lib/meshhome-radio
  move_path "/etc/letsencrypt/$OLD_SERVICE-cloudflare.ini" "/etc/letsencrypt/$NEW_SERVICE-cloudflare.ini"
  edit_file "$NEW_CONF_DIR/$NEW_SERVICE.env" "${MIGRATE_PATHS[@]}" \
    -e "s#^DATABASE_URL=postgresql+asyncpg://$OLD_USER@/$OLD_USER?#DATABASE_URL=postgresql+asyncpg://$NEW_USER@/$NEW_USER?#" \
    -e 's#^MESHCORE_INSTALL_KIND=#MESHHOME_INSTALL_KIND=#' -e 's#^MESHCORE_STATE_DIR=#MESHHOME_STATE_DIR=#' \
    -e 's#^MESHCORE_HOME_#MESHHOME_#' -e 's#^UPDATE_REPO=roach0816/MeshCoreHome$#UPDATE_REPO=roach0816/MeshHome#'
  # Saved settings snapshots (for "undo" of a network change) hold the same paths.
  for f in "$NEW_CONF_DIR/.previous/env" "$NEW_CONF_DIR/.previous/nginx-site"; do edit_file "$f" "${MIGRATE_PATHS[@]}"; done
  if ((certbot)); then  # certbot (before v0.7.4) renews with its own copy of the paths
    for f in /etc/letsencrypt/renewal/*.conf; do grep -qs "meshcore-home" "$f" && edit_file "$f" "${MIGRATE_PATHS[@]}"; done
  fi
  if ((https)); then
    move_path "$NGINX_SITE" "/etc/nginx/sites-available/$NEW_SERVICE"
    edit_file "/etc/nginx/sites-available/$NEW_SERVICE" "${MIGRATE_PATHS[@]}"
    if [[ -L $NGINX_ENABLED ]]; then
      rm -f "$NGINX_ENABLED"; undo_push ln -sfn "$NGINX_SITE" "$NGINX_ENABLED"
      ln -sfn "/etc/nginx/sites-available/$NEW_SERVICE" "/etc/nginx/sites-enabled/$NEW_SERVICE"
      undo_push rm -f "/etc/nginx/sites-enabled/$NEW_SERVICE"
    fi
  fi
  move_path "$HAT_UDEV_RULE" "/etc/udev/rules.d/90-$NEW_SERVICE-radio.rules"
  ok "Files moved to $NEW_PREFIX, $NEW_CONF_DIR and $NEW_STATE_DIR"

  step
  # Keep the old unit files aside (restored on undo), then install the new ones from the release.
  local aside; aside=$(mktemp -d "$UNIT_DIR/.premigrate.XXXXXX")
  undo_push systemctl daemon-reload
  undo_push rm -rf "$aside"
  for u in "${units[@]}"; do
    # Drop-in overrides (e.g. meshcore-home.service.d/) keep applying under the new name.
    move_path "$UNIT_DIR/$u.d" "$UNIT_DIR/${u/#$OLD_SERVICE/$NEW_SERVICE}.d"
    [[ -e $UNIT_DIR/$u ]] || continue
    systemctl disable "$u" >>"$LOG_FILE" 2>&1 || true
    [[ " ${enabled[*]} " == *" $u "* ]] && undo_push systemctl enable "$u"  # runs after the move back
    mv "$UNIT_DIR/$u" "$aside/"; undo_push mv "$aside/$u" "$UNIT_DIR/$u"
  done
  local -a old_enabled=("${enabled[@]}") new_units=() new_enabled=()
  use_layout new
  LOG_FILE=$log_file
  local src="$PREFIX/current/deploy/native/systemd"
  new_units=("$SERVICE.service" "${HELPER_UNITS[@]}")
  ((acme)) && new_units+=("${ACME_UNITS[@]}")
  ((hat)) && new_units+=("$HAT_SERVICE.service")
  for u in "${new_units[@]}"; do
    put_unit "$src" "$u" || true
    undo_push rm -f "$UNIT_DIR/$u"
  done
  ((hat)) && [[ -f $HAT_DIR/run ]] && install -m 755 "$src/../radio-hat-run" "$HAT_DIR/run"
  for u in "${old_enabled[@]}"; do new_enabled+=("${u/#$OLD_SERVICE/$NEW_SERVICE}"); done
  systemctl daemon-reload
  if ((${#new_enabled[@]})); then
    systemctl enable "${new_enabled[@]}" >>"$LOG_FILE" 2>&1
    undo_push systemctl disable "${new_enabled[@]}"
  fi
  command -v udevadm >/dev/null && udevadm control --reload-rules >>"$LOG_FILE" 2>&1 || true
  if ((https)); then
    run "Checking the nginx configuration" nginx -t
    undo_push systemctl reload nginx
    systemctl reload nginx >>"$LOG_FILE" 2>&1 || true
  fi
  ln -sfn "$PREFIX/current/deploy/native/meshhome" "$CLI_LINK"
  undo_push rm -f "$CLI_LINK"
  ln -sfn "$PREFIX/current/deploy/native/meshhome" "/usr/local/bin/$OLD_SERVICE"  # the old command keeps working
  undo_push ln -sfn "$OLD_PREFIX/current/deploy/native/meshhome" "/usr/local/bin/$OLD_SERVICE"
  ok "Services installed: ${new_units[*]}"

  step
  undo_push systemctl stop "${new_units[@]}"
  local port; port=$(env_get PORT); port=${port:-$DEFAULT_PORT}
  systemctl start "$SERVICE-update.path" "$SERVICE-config.path" >>"$LOG_FILE" 2>&1 || true
  ((acme)) && { systemctl start "$SERVICE-acme-renew.timer" >>"$LOG_FILE" 2>&1 || true; }
  ((hat)) && { systemctl start "$HAT_SERVICE.service" >>"$LOG_FILE" 2>&1 || true; }
  if ! systemctl start "$SERVICE.service" >>"$LOG_FILE" 2>&1 ||
    ! run_check "Started; waiting for $APP_NAME to become ready" wait_healthy "$port" "$TARGET_VERSION" 120; then
    journalctl -u "$SERVICE" -n 30 --no-pager >>"$LOG_FILE" 2>&1 || true
    systemctl reset-failed "$SERVICE.service" >>"$LOG_FILE" 2>&1 || true
    die "$APP_NAME did not start under the new names" "Every step was undone and the install runs as before. Details: $LOG_FILE"
  fi
  if [[ -n $(override MESHHOME_TEST_MIGRATE_FAIL) ]]; then  # testing only: exercise the undo
    die "Test failure requested (MESHHOME_TEST_MIGRATE_FAIL)" "Every step was undone."
  fi
  MIGRATING=0 UNDO=()

  # Done: tidy up what undo would have needed, and releases from before the rename.
  find "$NEW_PREFIX/releases" "$NEW_CONF_DIR" /etc/letsencrypt/renewal /etc/nginx/sites-available \
    -name '*.premigrate' -delete 2>/dev/null || true
  rm -rf "$aside"
  local d
  for d in "$PREFIX"/releases/*; do
    [[ -d $d && $d != "$(readlink -f "$PREFIX/current")" ]] || continue
    version_gt 0.10.0 "$(basename "$d")" && { rm -rf "$d"; log "pruned $d (from before the rename)"; }
  done
  lan_discovery_retire
  write_network_snapshot
  step_note "Running"
  steps_done
  tui_end
  mv -f "$log_file" "$LOG_FILE" 2>/dev/null || true
  log "migrated to the MeshHome names"
  printf '\n  %s%s✓ Migrated. %s v%s is running as %s.%s\n' "$G" "$B" "$APP_NAME" "$TARGET_VERSION" "$SERVICE" "$N"
  printf '    Manage it with: %ssudo %s status | logs | update | …%s  (sudo %s still works)\n' "$B" "$CLI_NAME" "$N" "$OLD_SERVICE"
  printf '    %sThe database backup from before the move is in %s%s\n\n' "$D" "$BACKUP_DIR" "$N"
}

main() {
  [[ $EUID -eq 0 ]] || { echo "Please run as root:  sudo bash $0" >&2; exit 1; }
  mkdir -p "$(dirname "$LOG_FILE")"; : >>"$LOG_FILE"; chmod 600 "$LOG_FILE"
  log "---- $APP_NAME installer started (args: mode=${MODE:-auto}) ----"

  if [[ $MODE == uninstall ]]; then uninstall; return; fi
  if [[ $MODE == sync-units ]]; then sync_units; return 0; fi
  if [[ $MODE == migrate ]]; then migrate_layout; return; fi
  if [[ $MODE == apply-config ]]; then
    [[ -n $(installed_version) ]] || exit 1
    TARGET_VERSION=$(installed_version)
    # One privileged change at a time (shared with web-UI upgrades).
    exec 9>$ADMIN_LOCK; flock -w 900 9 || exit 1
    apply_config_request || exit 1
    return 0
  fi
  if [[ $MODE == radio-hat-sync ]]; then hat_sync; return; fi
  if [[ $MODE == acme-renew ]]; then acme_renew; return; fi
  if [[ $MODE == radio-hat ]]; then
    [[ -n $(installed_version) ]] || die "$APP_NAME is not installed"
    TARGET_VERSION=$(installed_version)
    if ((HAT_REMOVE)); then
      [[ -f $HAT_UNIT ]] || { ok "The radio HAT is not set up"; return 0; }
      tui_start "Radio HAT"; plan "Remove the radio HAT software"; step
      note "${B}This will remove${N} the radio HAT service and ZephCore$( ((PURGE)) && echo ", and delete the radio's identity, contacts and channels" || echo "; the radio's identity is kept in $HAT_DATA")."
      note "SPI stays turned on. Switch MeshHome to another radio in Settings → Radio connection."
      confirm "Remove the radio HAT software?"
      hat_lock || true
      hat_remove "$PURGE"; steps_done; tui_end
      return
    fi
    tui_start "Radio HAT"
    banner
    plan "Set up the radio HAT"; step
    if hat_install; then
      steps_done; tui_end
      printf '\n  %s✓ Radio HAT set up.%s In MeshHome choose %sSettings → Radio connection → Radio HAT on this Pi%s.\n' "$G" "$N" "$B" "$N"
      [[ -e $HAT_SPIDEV ]] || hat_test_mode || printf '  %sRestart the Pi first (SPI was just turned on): sudo reboot%s\n' "$Y" "$N"
      printf '\n'
    else
      step_skip "Not set up"; tui_end; exit 1
    fi
    return
  fi
  if [[ $MODE == security-updates ]]; then
    tui_start "Security updates"
    plan "Automatic security updates"; step
    if security_updates_setup; then steps_done; tui_end; else step_skip "Not enabled"; tui_end; exit 1; fi
    return
  fi
  if [[ $MODE == https || $MODE == https-disable ]]; then
    [[ -n $(installed_version) ]] || die "$APP_NAME is not installed"
    TARGET_VERSION=$(installed_version)
    if [[ $MODE == https-disable ]]; then
      tui_start "Disable HTTPS"
      plan "Disable HTTPS"; step
      https_disable
      steps_done; tui_end
      return
    fi
    tui_start "HTTPS"
    banner
    plan "Set up HTTPS"; step
    https_setup || { step_skip "Not set up"; tui_end; exit 1; }
    finish
    return
  fi
  ((FROM_REQUEST)) && read_request

  local cur; cur=$(installed_version || true)
  if [[ -z $MODE ]]; then MODE=$([[ -n $cur ]] && echo upgrade || echo install); fi
  [[ $MODE == upgrade && -z $cur ]] && die "Nothing to upgrade: $APP_NAME is not installed" "Run without --upgrade to install."

  if [[ $MODE == upgrade ]]; then
    if ((FROM_REQUEST)); then
      STEP_TITLES=(Check Version Packages Backup Download Install Restart); STEPS=7
    else
      tui_start "Upgrade"
      banner
      ((TUI_ON)) || printf '  %s v%s is installed — this will upgrade it.\n\n' "$APP_NAME" "$cur"
      STEP_WEIGHTS=(3 1 8 5 8 50 25)
      plan "Check this system" "Choose the version" "System packages" "Back up the database" \
        "Download and verify" "Install the new version" "Switch over and restart"
    fi
    step; preflight
    step; choose_version
    step; install_packages
    step; backup_database
    step; fetch_package
    step; install_release
    step; activate; prune_releases
    radio_hat_after_upgrade
    finish
    return
  fi

  tui_start "Installer"
  banner
  local -a titles=("Check this system" "Choose the version" "System packages" "Review system changes"
    "Download and verify" "Install the app" "Set up the database" "Start the service")
  STEP_WEIGHTS=(3 1 20 1 6 40 4 12)
  local offer_hat=0
  if [[ -n $(hat_board) ]]; then offer_hat=1; titles+=("Radio HAT (optional)"); STEP_WEIGHTS+=(8); fi
  titles+=("HTTPS (optional)" "Automatic security updates (optional)"); STEP_WEIGHTS+=(7 6)
  plan "${titles[@]}"
  step; preflight
  step; choose_version
  step; install_packages
  step; configure
  step; fetch_package
  step; ensure_user_and_dirs; install_release
  step; ensure_database; write_env
  step; activate; prune_releases
  if ((offer_hat)); then
    step
    local product; product=$(hat_product)
    if [[ -n $product ]]; then
      note "A HAT was detected: ${B}$product${N}."
      if ask_yn "Set up the radio HAT (RAK6421) as this Pi's MeshCore radio?" y; then hat_install || step_skip "Not set up"; else step_skip "Skipped"; fi
    elif ask_yn "Set up a RAK6421 radio HAT on this Pi? (no HAT was detected)" n; then
      hat_install || step_skip "Not set up"
    else
      info "Skipped. Set it up later with: sudo $CLI_NAME radio-hat"
      step_skip "Skipped"
    fi
  fi
  step
  if https_enabled && [[ ! -f $NGINX_SITE ]]; then
    # Kept from a plain uninstall: restore it rather than asking again.
    https_restore_kept || step_skip "Plain HTTP"
  elif https_enabled; then
    ok "HTTPS is already set up for $(env_get HTTPS_HOST)"
    step_note "$(public_url)"
  elif [[ -n $HTTPS_HOST ]] || ask_yn "Set up HTTPS with a trusted certificate now? (needs a domain and a DNS provider API key)" n; then
    https_setup || step_skip "Not set up"
  else
    info "Skipped. You can add it later with: sudo $CLI_NAME https"
    step_skip "Skipped"
  fi
  step; security_updates_setup || step_skip "Skipped"
  finish
}

main
