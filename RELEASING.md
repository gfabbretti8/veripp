# Releasing

A release is a tag. Pushing `vX.Y.Z` runs `.github/workflows/release.yml`,
which re-tests everything on that exact commit and then publishes to PyPI
with the repository's `PYPI_TOKEN` secret. Nothing is uploaded by hand.

```bash
# 1. bump the version
$EDITOR pyproject.toml            # version = "X.Y.Z"
git commit -am "X.Y.Z: <what changed>"
git push

# 2. tag it -- this is the release
git tag vX.Y.Z
git push origin vX.Y.Z
```

## What the workflow guarantees before anything leaves the machine

* the full test suite passes **on the tagged commit**, with a real ESBMC --
  a green `ci` run on the same commit is not trusted, because workflows race
  and a tag pushed seconds after a bad commit would publish before `ci`
  finished failing;
* `veripp doctor` passes -- a release built against a checker that silently
  misses a bug class would ship proofs that are not proofs;
* the tag matches `pyproject.toml`'s version, so a mislabelled build is
  refused rather than published;
* `twine check` accepts both artifacts (catches a README that will not
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
