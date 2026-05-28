import os
import re
import requests
from collections import defaultdict
from datetime import datetime, timezone

TOKEN = os.environ["GH_PAT"]
USERNAME = "nitishkumar71"

GRAPHQL_URL = "https://api.github.com/graphql"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

# Yearly contributionsCollection — user-level metadata, bypasses org PAT restrictions
YEARLY_QUERY = """
query($from: DateTime!, $to: DateTime!) {
  user(login: "%s") {
    contributionsCollection(from: $from, to: $to) {
      commitContributionsByRepository(maxRepositories: 100) {
        repository { isPrivate owner { login avatarUrl __typename } }
        contributions { totalCount }
      }
      pullRequestContributionsByRepository(maxRepositories: 100) {
        repository { isPrivate owner { login avatarUrl __typename } }
        contributions { totalCount }
      }
      issueContributionsByRepository(maxRepositories: 100) {
        repository { isPrivate owner { login avatarUrl __typename } }
        contributions { totalCount }
      }
    }
  }
}
""" % USERNAME

# Targeted search per org — used only to get the exact latest activity date for sorting
LATEST_ACTIVITY_QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 1) {
    nodes {
      ... on PullRequest { createdAt }
      ... on Issue { createdAt }
    }
  }
}
"""

USER_CREATED_QUERY = '{ user(login: "%s") { createdAt } }' % USERNAME


def get_creation_year():
    resp = requests.post(GRAPHQL_URL, headers=HEADERS, json={"query": USER_CREATED_QUERY})
    resp.raise_for_status()
    created_at = resp.json()["data"]["user"]["createdAt"]
    return int(created_at[:4])


def fetch_yearly(year):
    variables = {
        "from": f"{year}-01-01T00:00:00Z",
        "to": f"{year}-12-31T23:59:59Z",
    }
    resp = requests.post(GRAPHQL_URL, headers=HEADERS, json={"query": YEARLY_QUERY, "variables": variables})
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]["user"]["contributionsCollection"]


def fetch_latest_activity_date(org_login):
    """Two search calls per org — latest PR and latest issue — returns whichever is more recent."""
    dates = []
    for type_filter in ("pr", "issue"):
        q = f"author:{USERNAME} org:{org_login} type:{type_filter} sort:created-desc"
        resp = requests.post(GRAPHQL_URL, headers=HEADERS, json={"query": LATEST_ACTIVITY_QUERY, "variables": {"q": q}})
        resp.raise_for_status()
        data = resp.json()
        nodes = (data.get("data") or {}).get("search", {}).get("nodes", [])
        if nodes and nodes[0].get("createdAt"):
            dates.append(nodes[0]["createdAt"])
    return max(dates) if dates else ""


def build_orgs():
    orgs = defaultdict(lambda: {
        "prs": 0, "issues": 0, "commits": 0,
        "avatar": "", "url": "", "latest_year": 0,
    })

    def record(owner, login, year):
        orgs[login]["avatar"] = owner["avatarUrl"]
        orgs[login]["url"] = f"https://github.com/{login}"
        if year > orgs[login]["latest_year"]:
            orgs[login]["latest_year"] = year

    creation_year = get_creation_year()
    current_year = datetime.now(timezone.utc).year

    # Walk every year so no contributions are missed regardless of PAT restrictions
    for year in range(creation_year, current_year + 1):
        collection = fetch_yearly(year)

        for entry in collection["pullRequestContributionsByRepository"]:
            repo = entry["repository"]
            if repo["isPrivate"]:
                continue
            owner = repo["owner"]
            if owner["__typename"] != "Organization":
                continue
            login = owner["login"]
            orgs[login]["prs"] += entry["contributions"]["totalCount"]
            record(owner, login, year)

        for entry in collection["issueContributionsByRepository"]:
            repo = entry["repository"]
            if repo["isPrivate"]:
                continue
            owner = repo["owner"]
            if owner["__typename"] != "Organization":
                continue
            login = owner["login"]
            orgs[login]["issues"] += entry["contributions"]["totalCount"]
            record(owner, login, year)

        for entry in collection["commitContributionsByRepository"]:
            repo = entry["repository"]
            if repo["isPrivate"]:
                continue
            owner = repo["owner"]
            if owner["__typename"] != "Organization":
                continue
            login = owner["login"]
            orgs[login]["commits"] += entry["contributions"]["totalCount"]
            record(owner, login, year)

    # Keep orgs where the user has raised at least one PR or one issue
    orgs_with_contribs = {k: v for k, v in orgs.items() if v["prs"] > 0 or v["issues"] > 0}

    # Refine sort key: exact latest PR or issue date per org (two search calls each)
    for login, data in orgs_with_contribs.items():
        exact_date = fetch_latest_activity_date(login)
        data["latest_at"] = exact_date or f"{data['latest_year']}-12-31T23:59:59Z"

    return orgs_with_contribs


def generate_markdown(orgs):
    if not orgs:
        return "_No public organization PR contributions found._\n"

    sorted_orgs = sorted(orgs.items(), key=lambda x: x[1]["latest_at"], reverse=True)

    cols = 4
    rows = []
    current_row = []

    for login, data in sorted_orgs:
        cell = (
            f'    <td align="center" width="160">\n'
            f'      <a href="{data["url"]}">\n'
            f'        <img src="{data["avatar"]}&s=80" width="60px" alt="{login}"/><br/>\n'
            f"        <b>{login}</b>\n"
            f"      </a><br/>\n"
            f'      <sub>🔀 {data["prs"]} &nbsp; 📝 {data["commits"]} &nbsp; 🐛 {data["issues"]}</sub>\n'
            f"    </td>"
        )
        current_row.append(cell)
        if len(current_row) == cols:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    table = "<table>\n"
    for row in rows:
        table += "  <tr>\n" + "\n".join(row) + "\n  </tr>\n"
    table += "</table>"

    return (
        f"{table}\n\n"
        f"<sub>🔀 Pull Requests &nbsp;&nbsp; 📝 Commits &nbsp;&nbsp; 🐛 Issues"
        f" &nbsp;&nbsp; (all time, public organizations only)</sub>\n"
    )


def update_readme(markdown):
    readme_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "README.md")
    )
    with open(readme_path) as f:
        content = f.read()

    pattern = r"(<!-- ORG-CONTRIBUTIONS:START -->).*?(<!-- ORG-CONTRIBUTIONS:END -->)"
    updated = re.sub(pattern, f"\\1\n{markdown}\n\\2", content, flags=re.DOTALL)

    with open(readme_path, "w") as f:
        f.write(updated)


if __name__ == "__main__":
    orgs = build_orgs()
    update_readme(generate_markdown(orgs))
    print(f"Done — {len(orgs)} org(s) with both PR and issue contributions")
