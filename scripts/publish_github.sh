#!/usr/bin/env bash
# Публикация проекта на GitHub.
#
#   ./scripts/publish_github.sh git@github.com:USER/pto-agent.git
#   ./scripts/publish_github.sh https://github.com/USER/pto-agent.git
#   ./scripts/publish_github.sh USER/pto-agent            # если установлен gh
#
# Что делает: проверяет, что всё закоммичено, ставит remote origin, пушит main.
# Секреты не запрашиваются: аутентификацию делаете вы (gh auth login / SSH-ключ / PAT).
set -euo pipefail

REPO="${1:-}"
if [ -z "$REPO" ]; then
  echo "usage: $0 <url-репозитория | owner/repo>" >&2
  exit 2
fi

cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain)" ]; then
  echo "В рабочей папке есть незакоммиченные изменения — сначала закоммитьте:" >&2
  git status --short | head -20 >&2
  exit 1
fi

# owner/repo → создаём репозиторий через gh (если он есть) и берём его URL
if [[ "$REPO" != *"/"*".git" && "$REPO" != *"://"* ]]; then
  if command -v gh >/dev/null 2>&1; then
    echo "создаю репозиторий через gh: $REPO"
    gh repo create "$REPO" --private --source=. --remote=origin --push
    echo "готово: $(gh repo view "$REPO" --json url -q .url)"
    exit 0
  fi
  REPO="git@github.com:${REPO}.git"
fi

if git remote get-url origin >/dev/null 2>&1; then
  echo "origin уже есть: $(git remote get-url origin) — обновляю"
  git remote set-url origin "$REPO"
else
  git remote add origin "$REPO"
fi

echo "пуш main → $REPO"
git push -u origin main
echo
echo "дальше:"
echo "  1) в README замените USER в бейджах и ссылках на ваш аккаунт"
echo "  2) Settings → Actions → разрешить workflow (CI запустится сам)"
echo "  3) GitHub → Settings → Secrets, если планируете деплой (для демо не нужны)"
