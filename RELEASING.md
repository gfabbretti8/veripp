# Releasing

A release is a tag. Pushing `vX.Y.Z` runs `.github/workflows/release.yml`,
which re-tests everything on that exact commit, builds the wheels, and then
publishes to PyPI with the repository's `PYPI_TOKEN` secret. Nothing is
uploaded by hand.

```bash
# 1. bump the version -- pyproject.toml, package.json and .claude-plugin/*.json
#    must agree (a test checks), and CHANGELOG.md gets a section
$EDITOR pyproject.toml            # version = "X.Y.Z"
git commit -am "X.Y.Z: <what changed>"
git push

# 2. rehearse: run the release workflow by hand (Actions -> release -> Run
#    workflow). Without a tag it builds and tests everything and stops short
#    of the upload.

# 3. tag it -- this is the release
git tag vX.Y.Z
git push origin vX.Y.Z
```

The same tag runs `image.yml`, which publishes the container image for both
architectures.

## What goes up

Four files, always together: the sdist, the pure `py3-none-any` wheel, and a
`manylinux_2_17` wheel for x86_64 and for aarch64 that carries the ESBMC
checker at `veripp/bin/esbmc`. pip takes the platform wheel where one
matches, so `pip install veripp` is the whole installation on Linux, and the
pure wheel everywhere else. See `checker/README.md` for how the platform
wheels are made.

## What the workflow guarantees before anything leaves the machine

* the full test suite passes **on the tagged commit**, with a real ESBMC --
  a green `ci` run on the same commit is not trusted, because workflows race
  and a tag pushed seconds after a bad commit would publish before `ci`
  finished failing;
* `veripp doctor` passes -- a release built against a checker that silently
  misses a bug class would ship proofs that are not proofs;
* each platform wheel, pip-installed in a clean container on a native runner
  of its architecture, finds its bundled checker, passes `veripp doctor` and
  finds the bug in `examples/off_by_one.cpp`;
* the tag matches `pyproject.toml`'s version, so a mislabelled build is
  refused rather than published;
* the set is complete -- sdist, pure wheel, both platform wheels -- since a
  subset would leave some users unable to resolve the package or silently
  without a checker;
* `twine check` accepts every artifact (catches a README that will not
  render on the project page);
* the sdist carries `examples/`, which the README tells people to run.

After the upload it installs `veripp==X.Y.Z` from the real index with `uvx`
and fails the run if the package never becomes installable -- so a red
`release` run means users cannot install it, and a green one means they can.

A re-run of the workflow (or a re-pushed tag) is a no-op on the upload step
(`skip-existing`), not an error.

## If the release run fails

Nothing was published unless the "Publish to PyPI" step ran. Fix the
problem, delete and re-push the tag:

```bash
git tag -d vX.Y.Z && git push origin :refs/tags/vX.Y.Z
git tag vX.Y.Z && git push origin vX.Y.Z
```

PyPI versions are immutable: once `X.Y.Z` is on the index, a fix means
`X.Y.Z+1`, never a re-upload of the same number.
