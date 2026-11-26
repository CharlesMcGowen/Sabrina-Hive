# Sabrina Console release kit

The installed desktop application keeps its interface and Python bridge in
`sabrina_console_desktop/`. Its read-only release mission writes its report to
`sabrina_console_desktop/artifacts/missions/`. The material prepared for release
is maintained separately in `release/console-kit/`.

| Release file | Purpose |
| --- | --- |
| `.env.example` | Configuration template with an empty Gemini key, the local model name, and execution disabled |
| `REPOSITORY.md` | Repository layout and release instructions |
| `skills/github-console-release/SKILL.md` | Steps for preparing and inspecting the local release |
| `scripts/build_github_release.py` | Local ZIP builder and content verifier |

Run `python scripts/build_github_release.py` from this directory. The command
writes `dist/sabrina-console-release-kit.zip`, reopens the archive to check its
entries and contents, and prints the SHA-256 digest. The archive contains the
four files in the table. Publishing a GitHub release is a separate operator
action.
