#!/usr/bin/env python3
"""
Remote job finder: searches several job sources at once and ranks the results
by how many of YOUR skills each posting mentions.

Sources: Remotive, RemoteOK, Jobicy, Arbeitnow, Himalayas, The Muse,
We Work Remotely, Working Nomads, and Reddit (only with approved API credentials).

Usage:
    pip install requests
    python job_finder.py python figma "customer support" --limit 20

Reddit is optional. Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET and
REDDIT_USER_AGENT to enable it.
"""
import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import xml.etree.ElementTree as ET

import requests

TIMEOUT = 20
CACHE_SECONDS = 1800  # reuse results for 30 minutes (be polite to free APIs)
SUBREDDITS = ["forhire", "remotejs", "jobbit", "hiring", "remotework"]
SCAM_PATTERNS = [
    r"registration fee|training fee|pay (a |an )?(fee|deposit)",
    r"gift cards?|wire transfer|western union|cash(ier)?'?s? che(c)?k",
    r"telegram|whatsapp",
    r"guaranteed income|be your own boss",
]
_cache = {}


def log(msg):
    print(msg, file=sys.stderr)


def get_json(url, params=None, headers=None):
    key = (url, tuple(sorted((params or {}).items())))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    r = requests.get(url, params=params, timeout=TIMEOUT,
                     headers=headers or {"User-Agent": "job-finder/0.2"})
    r.raise_for_status()
    data = r.json()
    _cache[key] = (time.time(), data)
    return data


def get_bytes(url, params=None):
    key = ("bytes", url, tuple(sorted((params or {}).items())))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    r = requests.get(url, params=params, timeout=TIMEOUT, headers={"User-Agent": "job-finder/0.2"})
    r.raise_for_status()
    _cache[key] = (time.time(), r.content)
    return r.content


def strip_html(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def has_skill(text, skill):
    pattern = r"(?<![a-z0-9+#])" + re.escape(skill.lower()) + r"(?![a-z0-9+#])"
    return re.search(pattern, text.lower()) is not None


def score(text, skills):
    hits = [s for s in skills if has_skill(text, s)]
    flags = [p for p in SCAM_PATTERNS if re.search(p, text, re.I)]
    return hits, bool(flags)


YES_LOC = re.compile(r"worldwide|world[- ]?wide|anywhere|global|international|africa|nigeria|emea|any (location|country)|all countries", re.I)
GENERIC_LOC = re.compile(r"^\W*(remote|flexible|flexible\s*/\s*remote|remote work|work from home|n/?a|none)?\W*$", re.I)
TZ_LOC = re.compile(r"utc|gmt|time ?zone", re.I)
NO_TEXT = re.compile(
    r"(must|should|need to) (be|reside|live|work)[^.]{0,25}(in|from|within) (the )?(us|u\.s\.|usa|united states|uk|united kingdom|canada|europe|eu)\b"
    r"|authori[sz]ed to work in (the )?(us|u\.s\.|united states|uk|canada|eu)"
    r"|right to work in (the )?(us|u\.s\.|united states|uk|canada|eu)"
    r"|\b(us|u\.s\.|usa|uk|canada|europe|eu)[- ]only\b"
    r"|(us|u\.s\.|uk|canadian|eu) (residents|citizens) only", re.I)
YES_TEXT = re.compile(
    r"work from anywhere|anywhere in the world"
    r"|(hiring|remote|candidates|applicants)[^.]{0,25}(worldwide|globally)|\bnigeria\b", re.I)


def location_status(job):
    """'yes' = open to worldwide/Nigeria, 'no' = restricted elsewhere, 'maybe' = not stated."""
    loc = (job.get("location") or "").strip()
    text = job.get("text", "")[:3000]
    if loc and not GENERIC_LOC.match(loc):
        if YES_LOC.search(loc):
            return "yes"
        if TZ_LOC.search(loc):
            return "maybe"
        return "no"
    if NO_TEXT.search(text):
        return "no"
    if YES_TEXT.search(text):
        return "yes"
    return "maybe"


# ---------------------------------------------------------------- sources
def from_reddit(terms):
    cid, secret = os.getenv("REDDIT_CLIENT_ID"), os.getenv("REDDIT_CLIENT_SECRET")
    ua = os.getenv("REDDIT_USER_AGENT", "job-finder/0.2")
    if not (cid and secret):
        log("[reddit] skipped: no REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET set")
        return []
    try:
        tok = requests.post("https://www.reddit.com/api/v1/access_token", auth=(cid, secret),
                            data={"grant_type": "client_credentials"},
                            headers={"User-Agent": ua}, timeout=TIMEOUT)
        tok.raise_for_status()
        token = tok.json()["access_token"]
    except Exception as exc:
        log(f"[reddit] auth failed ({exc}). Check credentials and API approval.")
        return []
    headers = {"Authorization": f"bearer {token}", "User-Agent": ua}
    out = []
    for sub in SUBREDDITS:
        try:
            r = requests.get(f"https://oauth.reddit.com/r/{sub}/search", headers=headers, timeout=TIMEOUT,
                             params={"q": "hiring " + " ".join(terms[:2]), "restrict_sr": 1,
                                     "sort": "new", "t": "week", "limit": 25})
            r.raise_for_status()
        except Exception as exc:
            log(f"[reddit] r/{sub} failed: {exc}")
            continue
        for child in r.json().get("data", {}).get("children", []):
            d = child["data"]
            title = d.get("title", "")
            if re.search(r"for hire", title, re.I):
                continue
            out.append({"source": f"reddit r/{sub}", "title": title, "company": d.get("author", ""),
                        "url": "https://www.reddit.com" + d.get("permalink", ""), "pay": "",
                        "text": title + " " + d.get("selftext", "")})
    return out


def from_remotive(terms):
    out = []
    for term in terms[:8]:
        try:
            jobs = get_json("https://remotive.com/api/remote-jobs", {"search": term, "limit": 30}).get("jobs", [])
        except Exception as exc:
            log(f"[remotive] '{term}' failed: {exc}")
            continue
        for j in jobs:
            out.append({"source": "remotive.com", "title": j.get("title", ""),
                        "company": j.get("company_name", ""), "url": j.get("url", ""),
                        "pay": j.get("salary", ""), "location": j.get("candidate_required_location", ""),
                        "text": " ".join([j.get("title", ""), strip_html(j.get("description", "")),
                                          " ".join(j.get("tags", []))])})
    return out


def from_remoteok():
    try:
        data = get_json("https://remoteok.com/api")[1:]  # first item is a legal notice
    except Exception as exc:
        log(f"[remoteok] failed: {exc}")
        return []
    out = []
    for j in data:
        lo, hi = j.get("salary_min"), j.get("salary_max")
        out.append({"source": "remoteok.com", "title": j.get("position", ""),
                    "company": j.get("company", ""), "url": j.get("url", ""),
                    "pay": f"${lo:,}-${hi:,}" if lo and hi else "", "location": str(j.get("location", "")),
                    "text": " ".join([j.get("position", ""), strip_html(j.get("description", "")),
                                      " ".join(j.get("tags", []))])})
    return out


def from_jobicy(terms):
    out = []
    for term in terms[:4]:
        try:
            jobs = get_json("https://jobicy.com/api/v2/remote-jobs", {"count": 50, "tag": term}).get("jobs", [])
        except Exception as exc:
            log(f"[jobicy] '{term}' failed: {exc}")
            continue
        for j in jobs:
            lo, hi = j.get("annualSalaryMin"), j.get("annualSalaryMax")
            out.append({"source": "jobicy.com", "title": j.get("jobTitle", ""),
                        "company": j.get("companyName", ""), "url": j.get("url", ""),
                        "pay": f"{j.get('salaryCurrency', '')} {lo}-{hi}/yr".strip() if lo and hi else "",
                        "location": ", ".join(j["jobGeo"]) if isinstance(j.get("jobGeo"), list) else str(j.get("jobGeo", "")),
                        "text": " ".join([j.get("jobTitle", ""), str(j.get("jobLevel", "")),
                                          strip_html(j.get("jobExcerpt", "")),
                                          strip_html(j.get("jobDescription", ""))])})
    return out


def from_arbeitnow():
    out = []
    for page in (1, 2, 3):
        try:
            jobs = get_json("https://www.arbeitnow.com/api/job-board-api", {"page": page}).get("data", [])
        except Exception as exc:
            log(f"[arbeitnow] page {page} failed: {exc}")
            continue
        for j in jobs:
            if not j.get("remote"):
                continue
            out.append({"source": "arbeitnow.com", "title": j.get("title", ""),
                        "company": j.get("company_name", ""), "url": j.get("url", ""), "pay": "",
                        "location": j.get("location", ""),
                        "text": " ".join([j.get("title", ""), strip_html(j.get("description", "")),
                                          " ".join(j.get("tags", []))])})
    return out


def from_himalayas(terms):
    jobs = []
    for term in terms[:3]:
        try:
            jobs += get_json("https://himalayas.app/jobs/api/search", {"q": term}).get("jobs", [])
        except Exception as exc:
            log(f"[himalayas] search '{term}' failed: {exc}")
    if not jobs:  # fall back to the plain latest-jobs list
        for offset in (0, 20, 40):
            try:
                jobs += get_json("https://himalayas.app/jobs/api", {"limit": 20, "offset": offset}).get("jobs", [])
            except Exception as exc:
                log(f"[himalayas] list failed: {exc}")
                break
    out = []
    for j in jobs:
        lo, hi = j.get("minSalary"), j.get("maxSalary")
        out.append({"source": "himalayas.app", "title": j.get("title", ""),
                    "company": j.get("companyName") or j.get("company_name", ""),
                    "url": j.get("applicationLink") or j.get("guid") or j.get("url", ""),
                    "pay": f"{j.get('currency', '')} {lo}-{hi}".strip() if lo and hi else "",
                    "location": ", ".join(map(str, j.get("locationRestrictions") or [])),
                    "text": " ".join([j.get("title", ""), str(j.get("seniority", "")),
                                      strip_html(j.get("excerpt", "")),
                                      strip_html(j.get("description", ""))])})
    return out


def from_muse(entry_level=True):
    out = []
    levels = ["Entry Level", "Internship"] if entry_level else [None]
    for level in levels:
        for page in (1, 2):
            params = {"page": page, "location": "Flexible / Remote"}
            if level:
                params["level"] = level
            try:
                results = get_json("https://www.themuse.com/api/public/jobs", params).get("results", [])
            except Exception as exc:
                log(f"[themuse] {level or 'all'} page {page} failed: {exc}")
                continue
            for j in results:
                out.append({"source": "themuse.com", "title": j.get("name", ""),
                            "company": (j.get("company") or {}).get("name", ""),
                            "url": (j.get("refs") or {}).get("landing_page", ""), "pay": "",
                            "location": ", ".join(l.get("name", "") for l in j.get("locations", [])),
                            "text": " ".join([j.get("name", ""), strip_html(j.get("contents", "")),
                                              " ".join(l.get("name", "") for l in j.get("levels", []))])})
    return out


WWR_FEEDS = [
    "remote-jobs.rss",
    "categories/remote-customer-support-jobs.rss",
    "categories/remote-sales-and-marketing-jobs.rss",
    "categories/remote-management-and-finance-jobs.rss",
    "categories/all-other-remote-jobs.rss",
]


def from_wwr():
    out = []
    for feed in WWR_FEEDS:
        try:
            root = ET.fromstring(get_bytes("https://weworkremotely.com/" + feed))
        except Exception as exc:
            log(f"[weworkremotely] {feed} failed: {exc}")
            continue
        for item in root.iter("item"):
            raw = (item.findtext("title") or "").strip()
            company, _, title = raw.partition(": ")
            if not title:
                company, title = "", raw
            url = (item.findtext("link") or "").strip()
            if not url:
                continue
            out.append({"source": "weworkremotely.com", "title": title, "company": company, "url": url,
                        "pay": "", "location": (item.findtext("region") or "").strip(),
                        "text": " ".join([title, item.findtext("category") or "",
                                          strip_html(item.findtext("description"))])})
    return out


def from_workingnomads():
    try:
        data = get_json("https://www.workingnomads.com/api/exposed_jobs/")
    except Exception as exc:
        log(f"[workingnomads] failed: {exc}")
        return []
    if isinstance(data, dict):
        data = data.get("jobs") or data.get("results") or []
    out = []
    for j in data:
        url = j.get("url") or j.get("job_url") or ""
        if not url:
            continue
        tags = j.get("tags")
        tags = ", ".join(map(str, tags)) if isinstance(tags, list) else str(tags or "")
        out.append({"source": "workingnomads.com", "title": j.get("title", ""),
                    "company": j.get("company_name") or j.get("company") or "", "url": url, "pay": "",
                    "location": str(j.get("location") or ""),
                    "text": " ".join([j.get("title", ""), str(j.get("category_name") or ""), tags,
                                      strip_html(j.get("description", ""))])})
    return out


def fetch_all(skills, terms, api_terms, entry_level=True):
    """Run every source at the same time. Returns (jobs, {source: count})."""
    tasks = {
        "reddit": lambda: from_reddit(terms),
        "remotive": lambda: from_remotive(api_terms),
        "remoteok": from_remoteok,
        "jobicy": lambda: from_jobicy(terms),
        "arbeitnow": from_arbeitnow,
        "himalayas": lambda: from_himalayas(terms),
        "themuse": lambda: from_muse(entry_level),
        "wwr": from_wwr,
        "nomads": from_workingnomads,
    }
    jobs, counts = [], {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {name: pool.submit(fn) for name, fn in tasks.items()}
        for name, fut in futures.items():
            try:
                found = fut.result()
            except Exception as exc:
                log(f"[{name}] crashed: {exc}")
                found = []
            counts[name] = len(found)
            jobs += found
    return jobs, counts


def main():
    ap = argparse.ArgumentParser(description="Rank remote jobs by your skills")
    ap.add_argument("skills", nargs="+", help="your skills, e.g. python figma seo")
    ap.add_argument("--limit", type=int, default=15, help="results to show")
    args = ap.parse_args()
    skills = [s.lower() for s in args.skills]

    jobs, counts = fetch_all(skills, skills, skills, entry_level=False)
    print("Searched: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    seen, ranked = set(), []
    for job in jobs:
        if job["url"] in seen:
            continue
        seen.add(job["url"])
        hits, flagged = score(job["text"], skills)
        if hits and not flagged:
            ranked.append((len(hits), hits, job))
    ranked.sort(key=lambda t: t[0], reverse=True)
    if not ranked:
        print("No matches. Try broader skills.")
        return
    for n, hits, job in ranked[: args.limit]:
        print(f"\n[{n}/{len(skills)} skills] {job['title']} - {job['company']}")
        print(f"  matches: {', '.join(hits)}   source: {job['source']}")
        if job["pay"]:
            print(f"  pay: {job['pay']}")
        print(f"  {job['url']}")
    print("\nPlease credit remoteok.com, remotive.com, jobicy.com, themuse.com and weworkremotely.com if you republish listings.")


if __name__ == "__main__":
    main()
