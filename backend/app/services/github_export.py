"""Export campaign session pages to a GitHub repository.

Commits one Markdown file per finished session (and the campaign index)
via the GitHub Contents API — no git binary needed. Each file is created
or updated in a single commit; existing files are updated in place using
their current blob SHA.
"""

import base64
import logging

import httpx
from sqlalchemy.orm import Session

from .. import models
from . import export as export_service

log = logging.getLogger("tablecast.github")


class GithubError(RuntimeError):
    pass


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _put_file(client: httpx.Client, cfg: models.GithubExport, path: str,
              content: str, message: str) -> None:
    url = f"{cfg.api_base.rstrip('/')}/repos/{cfg.repo}/contents/{path}"
    params = {"ref": cfg.branch}
    # Look up the existing blob SHA so we update rather than 409.
    sha = None
    r = client.get(url, params=params, headers=_headers(cfg.token))
    if r.status_code == 200:
        sha = r.json().get("sha")
    elif r.status_code not in (404,):
        raise GithubError(f"GitHub GET {path} failed: {r.status_code} {r.text[:200]}")

    body = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": cfg.branch,
    }
    if sha:
        body["sha"] = sha
    r = client.put(url, json=body, headers=_headers(cfg.token))
    if r.status_code not in (200, 201):
        raise GithubError(f"GitHub PUT {path} failed: {r.status_code} {r.text[:200]}")


def push_campaign(db: Session, campaign: models.Campaign, cfg: models.GithubExport) -> int:
    """Commit the campaign index + one page per finished session. Returns the
    number of files written."""
    prefix = cfg.path_prefix.strip("/")
    prefix = f"{prefix}/" if prefix else ""

    sessions = [s for s in campaign.sessions if s.status == "ended"]
    sessions.sort(key=lambda s: s.id)

    written = 0
    with httpx.Client(timeout=60) as client:
        # index
        index_lines = [f"# {campaign.name}", ""]
        if campaign.description:
            index_lines += [campaign.description, ""]
        index_lines += ["## Sessions", ""]
        for game in sessions:
            fname = f"{export_service.safe_filename(game.title)}.md"
            index_lines.append(f"- [{game.title}](./{fname})")
        _put_file(client, cfg, f"{prefix}README.md", "\n".join(index_lines) + "\n",
                  f"Tablecast: update {campaign.name} index")
        written += 1

        for game in sessions:
            md = export_service.session_markdown(db, game)
            fname = f"{export_service.safe_filename(game.title)}.md"
            _put_file(client, cfg, f"{prefix}{fname}", md,
                      f"Tablecast: {game.title}")
            written += 1

    return written


def run_export(session_factory, campaign_id: int) -> None:
    """Background thread entry point; records status on the config row."""
    db = session_factory()
    try:
        campaign = db.get(models.Campaign, campaign_id)
        cfg = db.query(models.GithubExport).filter_by(campaign_id=campaign_id).first()
        if campaign is None or cfg is None:
            return
        try:
            count = push_campaign(db, campaign, cfg)
            cfg.last_status = f"Pushed {count} file(s)"
            cfg.last_pushed_at = models.utcnow()
        except GithubError as exc:
            cfg.last_status = str(exc)[:300]
            log.error("github export failed for campaign %s: %s", campaign_id, exc)
        except Exception as exc:  # network, auth, etc.
            cfg.last_status = f"Error: {exc}"[:300]
            log.exception("github export crashed for campaign %s", campaign_id)
        db.commit()
    finally:
        db.close()
