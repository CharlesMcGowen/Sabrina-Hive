---
name: github-console-release
description: Build and verify the local Sabrina Console release kit for operator review.
---

# Prepare a console release

1. Use the four release files listed in `REPOSITORY.md`. Keep the Gemini key
   placeholder empty and `EXECUTION_AUTHORIZED=false` in `.env.example`.
2. Run `python scripts/build_github_release.py` in the release-kit directory.
3. Record the ZIP path and the SHA-256 digest printed after verification.
4. Give the verified ZIP to the operator for review. A successful build records
   a local artifact. Publication requires a separate decision.
