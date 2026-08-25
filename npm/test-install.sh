#!/usr/bin/env sh
# Exercise the npx installer against a real packed tarball.
#
# Run it where node and npm exist, having already packed and globally
# installed the package:
#
#   npm pack && npm install -g ./veripp-skill-*.tgz && sh npm/test-install.sh
#
# The CI job and tests/npx_docker.sh both do that for you. It checks the
# installer's behaviour, not just that it exits 0: where files land, that
# install.sh keeps its executable bit, that SKILL.md arrives byte-for-byte,
# that --dry-run writes nothing, and that a failure is a clear message rather
# than a stack trace.
pass=0; fail=0

# Resolve the source skill before anything cds elsewhere. This used to be a
# hardcoded /tmp/skills path that existed only in the Docker harness, so on a
# GitHub runner the comparison was against a file that was not there --
# reported as "SKILL.md differs from source", which is a true statement about
# the wrong thing.
SKILL_SOURCE="${SKILL_SOURCE:-$PWD/skills/veripp/SKILL.md}"
if [ ! -f "$SKILL_SOURCE" ]; then
  echo "cannot find the source skill at $SKILL_SOURCE" >&2
  echo "run from the repository root, or set SKILL_SOURCE" >&2
  exit 2
fi
ck(){ if [ "$2" = "$3" ]; then echo "  ok   $1"; pass=$((pass+1));
      else echo "  FAIL $1 (want rc=$2 got rc=$3)"; fail=$((fail+1)); fi; }
has(){ if [ -e "$1" ]; then echo "  ok   $2"; pass=$((pass+1));
       else echo "  FAIL $2 ($1 missing)"; fail=$((fail+1)); fi; }
no(){ if [ ! -e "$1" ]; then echo "  ok   $2"; pass=$((pass+1));
      else echo "  FAIL $2 ($1 should not exist)"; fail=$((fail+1)); fi; }


V=veripp-skill
echo "== node $(node --version) =="

mkdir -p /tmp/proj && cd /tmp/proj
"$V" >/dev/null 2>&1; ck "default install exits 0" 0 $?
has /tmp/proj/.claude/skills/veripp/SKILL.md "installs into ./.claude/skills/veripp"
has /tmp/proj/.claude/skills/veripp/install.sh "carries the installer script"
[ -x /tmp/proj/.claude/skills/veripp/install.sh ] \
  && { echo "  ok   install.sh stays executable"; pass=$((pass+1)); } \
  || { echo "  FAIL install.sh lost its executable bit"; fail=$((fail+1)); }
cmp -s "$SKILL_SOURCE" /tmp/proj/.claude/skills/veripp/SKILL.md \
  && { echo "  ok   SKILL.md copied byte-for-byte"; pass=$((pass+1)); } \
  || { echo "  FAIL SKILL.md differs from source"; fail=$((fail+1)); }

"$V" >/dev/null 2>&1; ck "refuses to overwrite without --force" 1 $?
"$V" --force >/dev/null 2>&1; ck "--force overwrites" 0 $?

mkdir -p /tmp/g && HOME=/tmp/g "$V" --global >/dev/null 2>&1
ck "--global exits 0" 0 $?
has /tmp/g/.claude/skills/veripp/SKILL.md "--global uses \$HOME"

"$V" --dir /tmp/custom >/dev/null 2>&1; ck "--dir exits 0" 0 $?
has /tmp/custom/veripp/SKILL.md "--dir installs where told"

"$V" --dry-run --dir /tmp/never >/dev/null 2>&1; ck "--dry-run exits 0" 0 $?
no /tmp/never "--dry-run writes nothing"

"$V" --help >/dev/null 2>&1; ck "--help exits 0" 0 $?
"$V" --nonsense >/dev/null 2>&1; ck "unknown option is a usage error" 2 $?
"$V" --dir >/dev/null 2>&1; ck "--dir without a value is a usage error" 2 $?

# Must run as a normal user: root ignores permission bits, so as root this
# check silently succeeds and proves nothing.
rm -rf /tmp/locked && mkdir -p /tmp/locked && chmod 555 /tmp/locked
if [ "$(id -u)" = "0" ] && id node >/dev/null 2>&1; then
  RUNNER="su node -s /bin/sh -c"
else
  RUNNER="sh -c"
fi
$RUNNER "$V --dir /tmp/locked/sub >/dev/null 2>&1"
ck "unwritable target is a clean error" 1 $?
out=$($RUNNER "$V --dir /tmp/locked/sub" 2>&1); case "$out" in
  *Error*|*stack*|*"at Object"*) echo "  FAIL raw stack trace shown to the user"; fail=$((fail+1)) ;;
  *) echo "  ok   no stack trace on a permission failure"; pass=$((pass+1)) ;;
esac

out=$(cd /tmp/proj && "$V" --force 2>&1)
case "$out" in *"Restart your agent"*) echo "  ok   says what to do next"; pass=$((pass+1)) ;;
  *) echo "  FAIL no next step"; fail=$((fail+1)) ;; esac
case "$out" in *"not the verifier"*) echo "  ok   distinguishes skill from verifier"; pass=$((pass+1)) ;;
  *) echo "  FAIL does not say it is only the skill"; fail=$((fail+1)) ;; esac

echo "-- $pass passed, $fail failed"
[ "$fail" -eq 0 ]
