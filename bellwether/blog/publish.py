"""Publish the static site to a git remote (GitHub Pages).

Pull-/push-based over HTTPS with a token — no inbound ports, no secrets on the
remote beyond what GitHub already holds. The token is injected into the remote
URL at call time and scrubbed from any error message so it never lands in logs.
"""

from __future__ import annotations

import os
import re
import subprocess


class GitPublishError(RuntimeError):
    pass


class GitPublisher:
    def __init__(self, work_dir: str, repo_url: str, token: str = "", branch: str = "main"):
        self._dir = work_dir
        self._repo_url = repo_url
        self._token = token
        self._branch = branch

    def _authed_url(self) -> str:
        url = self._repo_url
        if self._token and url.startswith("https://"):
            # https://github.com/... -> https://x-access-token:TOKEN@github.com/...
            return "https://x-access-token:" + self._token + "@" + url[len("https://"):]
        return url

    def _scrub(self, text: str) -> str:
        if self._token:
            text = text.replace(self._token, "***")
        return re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", text)

    def _git(self, *args: str, cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", *args], cwd=cwd or self._dir,
            capture_output=True, text=True, timeout=120,
        )
        if check and proc.returncode != 0:
            raise GitPublishError(self._scrub(f"git {' '.join(args)} failed: {proc.stderr.strip()}"))
        return proc

    def prepare(self) -> str:
        """Ensure ``work_dir`` is a clone of the remote on the target branch.

        The publish branch (e.g. ``gh-pages``) is kept as an **orphan** holding
        only the site, so it never carries the repo's source code and the code
        branch (``main``) is never touched. An existing publish branch is
        checked out as-is so prior posts are preserved. Returns the working-tree
        path to write the site into."""
        authed = self._authed_url()
        if not os.path.isdir(os.path.join(self._dir, ".git")):
            os.makedirs(os.path.dirname(self._dir) or ".", exist_ok=True)
            clone = self._git("clone", authed, self._dir, cwd=".", check=False)
            if clone.returncode != 0:
                # Empty/new remote: initialize locally instead.
                os.makedirs(self._dir, exist_ok=True)
                self._git("init")
                self._git("remote", "add", "origin", authed)
        else:
            self._git("remote", "set-url", "origin", authed)

        # Identity (required for commits in a fresh container).
        self._git("config", "user.email", "bot@bellwether.local")
        self._git("config", "user.name", "Bellwether Bot")

        self._git("fetch", "origin", check=False)
        remote_has = self._git(
            "rev-parse", "--verify", "--quiet", f"origin/{self._branch}", check=False
        ).returncode == 0
        if remote_has:
            # Existing publish branch — check it out so prior posts survive.
            self._git("checkout", "-B", self._branch, f"origin/{self._branch}")
        else:
            # First publish: orphan branch with ONLY the site (drop any files
            # inherited from the default branch's checkout).
            self._git("checkout", "--orphan", self._branch, check=False)
            self._git("rm", "-rf", ".", check=False)
        return self._dir

    def commit_and_push(self, message: str) -> bool:
        """Commit any changes and push. Returns False if there was nothing to commit."""
        self._git("add", "-A")
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False  # no changes
        self._git("commit", "-m", message)
        self._git("push", "origin", f"HEAD:{self._branch}")
        return True
