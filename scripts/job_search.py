"""
Daily job search: queries the Adzuna Jobs API for listings matching each
resume's target roles and writes data/jobs.json for the "Job Hunt" buttons
on the Personal page.

Adzuna is free (signup at developer.adzuna.com) but requires an app_id and
app_key. Required environment variables (set as GitHub repo secrets):
  ADZUNA_APP_ID
  ADZUNA_APP_KEY

Run by .github/workflows/job-search.yml.
"""

import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

OUT_FILE = Path("data/jobs.json")
ET = ZoneInfo("America/New_York")
COUNTRY = "us"
RESULTS_PER_QUERY = 10
MAX_PER_RESUME = 12
MAX_AGE_DAYS = 21  # skip stale postings

# Each resume searches 1-2 broad queries nationwide (no location filter -
# most of these roles are remote-friendly, and Indiana-only would be sparse).
QUERIES = {
    "technical": ["data analyst", "cybersecurity analyst"],
    "editor": ["video editor", "youtube editor"],
}


def search_adzuna(app_id, app_key, query, page=1):
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": RESULTS_PER_QUERY,
        "what": query,
        "sort_by": "date",
        "max_days_old": MAX_AGE_DAYS,
        "content-type": "application/json",
    }
    url = f"https://api.adzuna.com/v1/api/jobs/{COUNTRY}/search/{page}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.load(r)
        return data.get("results", []) or []
    except Exception as e:
        print(f"  WARN: Adzuna query '{query}' failed: {e}", file=sys.stderr)
        return []


def normalize(job):
    loc = (job.get("location") or {}).get("display_name")
    company = (job.get("company") or {}).get("display_name")
    category = (job.get("category") or {}).get("label")
    return {
        "id": job.get("id"),
        "title": job.get("title"),
        "company": company,
        "location": loc,
        "url": job.get("redirect_url"),
        "created": job.get("created"),
        "salary_min": job.get("salary_min"),
        "salary_max": job.get("salary_max"),
        "category": category,
        "contract_type": job.get("contract_type"),
    }


def build_list(app_id, app_key, queries):
    seen = set()
    jobs = []
    for q in queries:
        for raw in search_adzuna(app_id, app_key, q):
            j = normalize(raw)
            if not j["id"] or j["id"] in seen:
                continue
            seen.add(j["id"])
            jobs.append(j)
    jobs.sort(key=lambda j: j.get("created") or "", reverse=True)
    return jobs[:MAX_PER_RESUME]


def main():
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        print("ERROR: ADZUNA_APP_ID / ADZUNA_APP_KEY not set - skipping job search", file=sys.stderr)
        sys.exit(1)

    result = {"updated": dt.datetime.now(ET).strftime("%b %d, %Y %I:%M %p ET")}
    for resume_id, queries in QUERIES.items():
        jobs = build_list(app_id, app_key, queries)
        result[resume_id] = jobs
        print(f"{resume_id}: {len(jobs)} listings")

    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(result, indent=1))
    print(f"Wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
