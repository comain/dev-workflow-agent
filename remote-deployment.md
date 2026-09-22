# Deployment

The process listens on the address you pass to `dev`, and keeps its database,
artifacts, and workspaces under `DFA_DATA_DIR`. That directory is not part of
the git checkout.

Secrets stay in an untracked environment file, mode `0600`. A Maven Central
mirror, when the build environment needs one, is `DFA_MAVEN_CENTRAL_MIRROR_URL`.

Pull updates with `deploy/git-pull-main.sh` after exporting `REMOTE_URL` and
`REPO`. The script fast-forwards `main`. It does not store credentials.
