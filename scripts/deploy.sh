#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  echo "Create one with: cp env.sample .env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required variable: $name" >&2
    exit 1
  fi
}

keychain_read_optional() {
  local account="$1"
  local service="$2"
  if [[ -n "$account" && -n "$service" ]] && command -v security >/dev/null 2>&1; then
    security find-generic-password -a "$account" -s "$service" -w
  fi
}

DEPLOY_SSH_PASSWORD="${DEPLOY_SSH_PASSWORD:-$(keychain_read_optional "${DEPLOY_SSH_PASSWORD_KEYCHAIN_ACCOUNT:-}" "${DEPLOY_SSH_PASSWORD_KEYCHAIN_SERVICE:-}")}"
DOCKERHUB_PASSWORD="${DOCKERHUB_PASSWORD:-$(keychain_read_optional "${DOCKERHUB_PASSWORD_KEYCHAIN_ACCOUNT:-}" "${DOCKERHUB_PASSWORD_KEYCHAIN_SERVICE:-}")}"
PUBLISH_DOCKERHUB="${PUBLISH_DOCKERHUB:-0}"
APP_IMAGE="${APP_IMAGE:-bobsearch:local}"
APP_PUBLIC_PORT="${APP_PUBLIC_PORT:-7788}"

require_var DEPLOY_SSH_USER
require_var DEPLOY_SSH_HOST
require_var DEPLOY_TARGET_DIR
require_var DEPLOY_SSH_PASSWORD

if [[ "$PUBLISH_DOCKERHUB" == "1" ]]; then
  require_var DOCKERHUB_USER
  require_var DOCKERHUB_PASSWORD
fi

expect_ssh() {
  local timeout="$1"
  local command="$2"
  expect <<EOF
set timeout $timeout
spawn ssh -o StrictHostKeyChecking=no $DEPLOY_SSH_USER@$DEPLOY_SSH_HOST "$command"
expect -re {password:}
send -- "$DEPLOY_SSH_PASSWORD\r"
expect eof
EOF
}

expect_scp() {
  local timeout="$1"
  local source="$2"
  local target="$3"
  expect <<EOF
set timeout $timeout
spawn scp -O -o StrictHostKeyChecking=no "$source" $DEPLOY_SSH_USER@$DEPLOY_SSH_HOST:$target
expect -re {password:}
send -- "$DEPLOY_SSH_PASSWORD\r"
expect eof
EOF
}

expect_ssh 120 "mkdir -p '$DEPLOY_TARGET_DIR'"

tmp_archive="$(mktemp -t bobsearch.XXXXXX.tar.gz)"
tar -C "$ROOT" \
  --exclude '.env' \
  --exclude '.env.*' \
  --exclude '.pytest_cache' \
  --exclude '__pycache__' \
  --exclude '.venv' \
  --exclude 'dist' \
  --exclude 'build' \
  -czf "$tmp_archive" .

expect_scp 300 "$tmp_archive" /tmp/bobsearch.tar.gz
rm -f "$tmp_archive"

expect_ssh 300 "find '$DEPLOY_TARGET_DIR' -mindepth 1 ! -name '.env' -exec rm -rf {} + && tar -C '$DEPLOY_TARGET_DIR' -xzf /tmp/bobsearch.tar.gz && rm -f /tmp/bobsearch.tar.gz"
expect_scp 120 "$ENV_FILE" "$DEPLOY_TARGET_DIR/.env"
expect_ssh 120 "chmod 600 '$DEPLOY_TARGET_DIR/.env'"

compose_profiles_prefix=""
if [[ -n "${COMPOSE_PROFILES:-}" ]]; then
  compose_profiles_prefix="COMPOSE_PROFILES='$COMPOSE_PROFILES'"
fi

expect <<EOF
set timeout 900
spawn ssh -o StrictHostKeyChecking=no $DEPLOY_SSH_USER@$DEPLOY_SSH_HOST "cd '$DEPLOY_TARGET_DIR' && echo '$DEPLOY_SSH_PASSWORD' | sudo -S sh -c '$compose_profiles_prefix docker compose up -d --build --remove-orphans'"
expect -re {password:}
send -- "$DEPLOY_SSH_PASSWORD\r"
expect eof
EOF

if [[ "$PUBLISH_DOCKERHUB" == "1" ]]; then
  tmp_dockerhub_password="$(mktemp)"
  printf '%s' "$DOCKERHUB_PASSWORD" > "$tmp_dockerhub_password"
  chmod 600 "$tmp_dockerhub_password"
  expect_scp 120 "$tmp_dockerhub_password" /tmp/bobsearch.dockerhub.pass
  rm -f "$tmp_dockerhub_password"

  expect <<EOF
set timeout 900
spawn ssh -o StrictHostKeyChecking=no $DEPLOY_SSH_USER@$DEPLOY_SSH_HOST "cd '$DEPLOY_TARGET_DIR' && chmod 600 /tmp/bobsearch.dockerhub.pass && echo '$DEPLOY_SSH_PASSWORD' | sudo -S sh -c 'docker login --username \"$DOCKERHUB_USER\" --password-stdin < /tmp/bobsearch.dockerhub.pass && docker push \"$APP_IMAGE\"; docker logout; rm -f /tmp/bobsearch.dockerhub.pass'"
expect -re {password:}
send -- "$DEPLOY_SSH_PASSWORD\r"
expect eof
EOF
  echo "Pushed Docker image: $APP_IMAGE"
fi

echo "Deployed to http://$DEPLOY_SSH_HOST:$APP_PUBLIC_PORT"
