"""Orchestrate a daily blog publish: generate → render → push.

Fully fail-soft: this runs inside the daily reflection job, so any error
(model, git, network) is swallowed and reported as a status string — it must
never break trading or reflection. Returns a human-readable status, or None if
the blog is disabled.
"""

from __future__ import annotations

import os

from ..config import Config
from .generator import BlogGenerator
from .publish import GitPublisher
from .site import SiteBuilder


def _build_client(cfg: Config):
    if not cfg.llm.enabled:
        return None
    try:
        # Reuse the same provider fallback chain as the signals (Groq → Cerebras
        # → OpenRouter), so a single provider's rate-limit doesn't drop the post
        # back to the bare stats template.
        from ..factory import _build_llm_client

        return _build_llm_client(cfg)
    except Exception:
        return None


def publish_blog(cfg: Config, report, push: bool = True) -> str | None:
    """Generate today's post and publish it. Returns a status message, or None
    if blogging is disabled. ``push=False`` builds the site locally without
    pushing (preview). Never raises."""
    if not cfg.blog.enabled:
        return None

    work_dir = os.path.join(cfg.data_dir, "blog-site")
    can_push = push and bool(cfg.blog.repo_url and cfg.github_token)

    try:
        publisher = None
        site_root = work_dir
        if can_push:
            publisher = GitPublisher(work_dir, cfg.blog.repo_url, cfg.github_token, cfg.blog.branch)
            site_root = publisher.prepare()

        site_dir = os.path.join(site_root, cfg.blog.subdir) if cfg.blog.subdir else site_root
        post = BlogGenerator(_build_client(cfg), include_dollars=cfg.blog.include_dollars).write(report)
        SiteBuilder(site_dir, cfg.blog.title, cfg.blog.base_url).add_post(post)
    except Exception as exc:  # generation/site build failed — give up cleanly
        return f"blog: failed to build post ({exc})"

    if publisher is None:
        return f"blog: built locally at {site_dir} (set blog.repo_url + GITHUB_TOKEN to publish)"

    try:
        pushed = publisher.commit_and_push(f"daily post {post.date}")
    except Exception as exc:
        return f"blog: built locally but push failed ({exc})"

    where = cfg.blog.base_url or "GitHub Pages"
    return f"blog: published {post.date} → {where}" if pushed else f"blog: no changes to publish ({post.date})"
