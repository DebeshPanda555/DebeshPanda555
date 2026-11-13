#!/usr/bin/env python3
"""
Refresh the data the cards are drawn from. Standard library only.

  data/contributions.json   the contribution calendar (last year, one entry per day)
  data/repos.json           language / stars / forks / pushed_at of the featured repos
                            (curated text such as tagline and stack is never touched)

Auth: reads GH_TOKEN, falling back to GITHUB_TOKEN. The default Actions token only sees
PUBLIC contributions. To include private ones, create a classic PAT with `read:user`,
store it as the repo secret PROFILE_TOKEN, and turn on "Include private contributions on my
profile" in your GitHub profile settings.

If the contribution fetch fails this exits non-zero WITHOUT writing anything, so a broken
run never replaces good data (and the workflow stops before it commits).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGIN = os.environ.get("PROFILE_LOGIN", "DebeshPanda555")
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def request(url: str, payload: dict | None = None) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "profile-cards"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_contributions() -> dict:
    if not TOKEN:
        sys.exit("No GH_TOKEN / GITHUB_TOKEN set; the GraphQL API needs one.")
    try:
        res = request("https://api.github.com/graphql", {"query": QUERY, "variables": {"login": LOGIN}})
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub GraphQL request failed: HTTP {e.code} {e.reason}")
    if res.get("errors") or not res.get("data", {}).get("user"):
        sys.exit(f"GitHub GraphQL returned errors: {json.dumps(res.get('errors') or res)[:400]}")
    cal = res["data"]["user"]["contributionsCollection"]["contributionCalendar"]
    days = [{"date": d["date"], "count": d["contributionCount"]}
            for w in cal["weeks"] for d in w["contributionDays"]]
    if not days:
        sys.exit("GitHub returned an empty contribution calendar.")
    return {
        "login": LOGIN,
        "generated": dt.datetime.now(dt.timezone.utc).date().isoformat(),
        "sample": False,
        "total": cal["totalContributions"],
        "days": days,
    }


def refresh_repos(path: Path) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    for repo in doc["repos"]:
        if repo.get("fetch") is False:
            continue
        try:
            info = request(f"https://api.github.com/repos/{repo.get('owner', LOGIN)}/{repo['name']}")
        except urllib.error.HTTPError as e:
            print(f"warning: {repo['name']}: HTTP {e.code}; keeping the previous values", file=sys.stderr)
            continue
        repo["language"] = info.get("language") or repo.get("language")
        repo["stars"] = info.get("stargazers_count")
        repo["forks"] = info.get("forks_count")
        repo["pushed_at"] = info.get("pushed_at")
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    contributions = fetch_contributions()  # exits before writing anything on failure
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "contributions.json").write_text(
        json.dumps(contributions, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"contributions.json: {len(contributions['days'])} days, {contributions['total']} contributions")
    refresh_repos(ROOT / "data" / "repos.json")
    print("repos.json refreshed")


if __name__ == "__main__":
    main()
