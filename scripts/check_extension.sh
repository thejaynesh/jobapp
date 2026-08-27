#!/usr/bin/env bash
#
# Syntax-check the extension, as modules.
#
# `node --check foo.js` parses as a CommonJS *script*, and a script permits
# things a module forbids — most usefully, declaring the same function twice.
# That is not a hypothetical: a duplicate `hostOf` passed `node --check` and
# then failed the extension outright with
#
#     Service worker registration failed. Status code: 15
#     Uncaught SyntaxError: Identifier 'hostOf' has already been declared
#
# which takes the whole agent down — no polling, no harvest, no browsing — and
# says nothing about which file or why until you open the service worker.
#
# Copying to .mjs makes Node parse module semantics, which catches it.
# background.js is genuinely a module (`"type": "module"` in the manifest);
# the others are not, but nothing here relies on script-only behaviour and a
# stricter parse is the point.
#
# Only syntax. It cannot check chrome.* usage, permissions, or whether a
# handler does what it claims — load the unpacked extension for that.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v node >/dev/null 2>&1; then
  echo "check_extension: node is not installed; skipping." >&2
  exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

failed=0
for file in extension/*.js; do
  name="$(basename "$file" .js)"
  cp "$file" "$work/$name.mjs"
  if node --check "$work/$name.mjs" 2>"$work/$name.err"; then
    echo "ok    $file"
  else
    echo "FAIL  $file"
    sed "s#$work/$name.mjs#$file#g" "$work/$name.err" | head -5
    failed=1
  fi
done

exit "$failed"
