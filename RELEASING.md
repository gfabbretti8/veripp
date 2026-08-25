# Releasing

Everything below has been validated except the upload itself, which needs
credentials this repository does not carry.

## Before tagging

```bash
uv run pytest -q                 # all tests, including the ESBMC-backed ones
uv run veripp doctor             # must not report a soundness hole
./demo/cve-2019-13223/run.sh     # the demo the README leads with
```

The soundness check matters most: a release built against an ESBMC that
silently misses a class of bug would ship proofs that are not proofs. `doctor`
exits non-zero in that case for exactly this reason.

## Build and check

```bash
uv build
uv run --with twine twine check dist/*
```

Both artifacts must pass. `twine check` catches the failure that is otherwise
invisible until the project page is live: a README that does not render.

## Upload

```bash
uv run --with twine twine upload dist/*
```

Use a PyPI API token (`__token__` as the username). Then confirm the thing
users will actually do works:

```bash
uvx veripp doctor
```

## The container image

The image is what the README, the skill and `veripp doctor` all point people
at, so it has to exist before those instructions are true.

Tagging `v*` runs `.github/workflows/image.yml`, which builds both
architectures on native runners, smoke-tests each one, pushes them by digest,
and stitches a manifest. To rehearse without tagging, run the workflow
manually with `push: false` — it builds and smoke-tests and pushes nothing:

```bash
gh workflow run image.yml -f push=false
```

Rehearsed and green: amd64 on `ubuntu-24.04` (446 MB) and arm64 on
`ubuntu-24.04-arm` (543 MB), 15/15 smoke tests each, the in-build `veripp
doctor` passing both soundness probes on both, and the login/push/digest
steps correctly skipped. Note that the arm64 runner was available on a
private repository, which is not something to take for granted.

A green run here is necessary and not sufficient. An earlier one passed
13/13 on an arm64 image that could not parse a single line of real code,
because every smoke fixture was self-contained. Two of the fifteen checks
now include real system headers, which is what catches that class of
failure — but before a release, still scan one real library through the
built image and confirm it produces proofs and counterexamples rather than
`parse_error`.

Two things about this build are worth remembering before changing it:

- **The arm64 leg compiles ESBMC from source**, because no sound prebuilt
  arm64 Linux ESBMC exists — the only one anywhere is the Homebrew bottle at
  8.4, which carries [esbmc#6508](https://github.com/esbmc/esbmc/issues/6508)
  and silently misses out-of-bounds writes. `tests/test_delivery.py` fails if
  that stage ever turns into a download. Measured at ~3.5 minutes on four
  native arm64 cores; the cost is in the apt and clone steps around it, not
  the compile.
- **It builds from `master`, not `weekly`.** The arm64 build is only possible
  because of [esbmc#5252](https://github.com/esbmc/esbmc/pull/5252) (ARM SVE
  builtin types), merged 2026-06-09 — after the `weekly` tag was last cut.
  Despite the name, `weekly` is not rebuilt weekly; check its date before
  assuming it contains anything.
- **It must be built on `ubuntu-24.04-arm`, not under qemu.** Emulating that
  compile turns a long build into an unusable one.

The Dockerfile runs `veripp doctor` as a build step, so an image whose ESBMC
cannot detect a planted bug fails the build rather than shipping. That check is
the reason to trust the image at all; do not move it behind a flag.

The manifest job is the one part of the release that building an image cannot
test: it only exists once two per-architecture digests have been pushed and
stitched. Getting it wrong yields a tag that silently serves one architecture
to everyone, which is worse than no tag. Rehearse it against a throwaway local
registry — nothing leaves the machine:

```bash
./tests/manifest_rehearsal.sh
```

That builds both architectures, pushes them by digest, stitches the manifest,
asserts both are in it, checks the tag resolves per host architecture, and
runs the smoke test against the image pulled back through the manifest.
Verified passing: an OCI image index carrying `linux/amd64` and `linux/arm64`,
resolving correctly both ways, 13/13 on the pulled image.

To build and check one architecture locally:

```bash
docker buildx build --platform linux/amd64 --load -t veripp:test .
./tests/image_smoketest.sh veripp:test
```

On a VM-backed runtime (colima, Lima, Docker Desktop), point the smoke test at
a directory the VM actually shares, or the bind mount comes up empty and every
case fails with "file not found":

```bash
mkdir -p ~/tmp
SMOKE_TMPDIR=$HOME/tmp PLATFORM=linux/amd64 ./tests/image_smoketest.sh veripp:test
```

## After the image is published

Two things are NOT done by tagging, and the README's instructions are wrong
until the first one is:

**1. Make the package public.** A package inherits its repository's
visibility, so a private repo publishes a private image and
`docker run ghcr.io/gfabbretti8/veripp` fails for everyone else. There is no
REST endpoint for this — change it in the UI:

    https://github.com/users/gfabbretti8/packages/container/veripp/settings

Confirm it worked without any credentials at all:

```bash
curl -s "https://ghcr.io/token?scope=repository:gfabbretti8/veripp:pull&service=ghcr.io" \
  | grep -q '"token"' && echo public || echo still private
```

While it is private that request returns `UNAUTHORIZED`.

**2. To pull it yourself**, `gh auth token` is not enough — it lacks
`read:packages`, and the pull fails with `403 Forbidden` even though
`docker login` succeeds:

```bash
gh auth refresh -s read:packages
gh auth token | docker login ghcr.io -u <you> --password-stdin
```

The release workflow does not need either of these: it pushes with
`GITHUB_TOKEN` and verifies the manifest in-job, which is what the
"Verify both architectures are in the manifest" step reports.

## Changelog and release notes

`CHANGELOG.md` is the client-facing record. Add the section before tagging,
then publish the release page from that same section so the two cannot drift:

    scripts/release-notes.py 0.1.1 > /tmp/notes.md
    gh release create v0.1.1 --title "v0.1.1" --notes-file /tmp/notes.md --latest

Tags alone are close to invisible: the repository page lists Releases, not
tags, so a tag without a release page is a version nobody finds.

## Version

`pyproject.toml` is the only place the version lives. Bump it before building;
PyPI refuses a re-upload of an existing version, so a mistake costs a new
number rather than a fix.

## What the README promises

The README is the PyPI project page, so its links must be absolute — relative
ones resolve on GitHub and 404 on PyPI. It also tells people to run
`examples/*.cpp`, so those ship in the sdist; check they are still there after
any packaging change:

```bash
tar tzf dist/*.tar.gz | grep examples/
```
