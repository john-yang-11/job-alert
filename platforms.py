"""ATS (applicant tracking system) job-board integrations.

Each fetch_* function takes a company slug and returns a list of normalized
postings: {"id": str, "title": str, "url": str}. Raises PlatformError for
anything that isn't a live, existing board (wrong slug, network error, etc.)
so callers can try the next platform/slug guess.
"""

import re
import time

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; internship-alerts/1.0)"}
TIMEOUT = 20


class PlatformError(Exception):
    pass


class PlatformThrottled(PlatformError):
    """Rate-limiting/timeouts prevented a confident answer (vs. a clean miss).

    Distinct from PlatformError so callers can retry later instead of caching a
    false negative -- see discover_workday and resolve_companies.resolve_new.
    """


def _get_json(url: str, params: dict | None = None) -> dict | list:
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise PlatformError(f"{url} -> {e}")
    if resp.status_code != 200:
        raise PlatformError(f"{url} -> HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise PlatformError(f"{url} -> non-JSON response")


def _names_match(company_name: str, candidate_name: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    a, b = norm(company_name), norm(candidate_name)
    return bool(a and b and (a in b or b in a))


def fetch_greenhouse(slug: str, company_name: str) -> list[dict]:
    board = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
    if not isinstance(board, dict) or not _names_match(company_name, board.get("name", "")):
        raise PlatformError(f"greenhouse board name mismatch for slug {slug!r}")
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if jobs is None:
        raise PlatformError("greenhouse: no 'jobs' key")
    return [
        {"id": f"gh:{slug}:{j['id']}", "title": j.get("title", ""), "url": j.get("absolute_url", "")}
        for j in jobs
    ]


def fetch_lever(slug: str, company_name: str) -> list[dict]:
    data = _get_json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if not isinstance(data, list):
        raise PlatformError("lever: response is not a list")
    return [
        {"id": f"lever:{slug}:{p['id']}", "title": p.get("text", ""), "url": p.get("hostedUrl", "")}
        for p in data
    ]


def fetch_ashby(slug: str, company_name: str) -> list[dict]:
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if jobs is None:
        raise PlatformError("ashby: no 'jobs' key")
    return [
        {"id": f"ashby:{slug}:{j['id']}", "title": j.get("title", ""), "url": j.get("jobUrl", "")}
        for j in jobs
    ]


def fetch_smartrecruiters(slug: str, company_name: str) -> list[dict]:
    data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings")
    content = data.get("content") if isinstance(data, dict) else None
    if content is None:
        raise PlatformError("smartrecruiters: no 'content' key")
    if any(not isinstance(p, dict) for p in content):
        raise PlatformError("smartrecruiters: malformed 'content' entries (likely wrong slug)")
    out = []
    for p in content:
        job_id = p.get("id")
        company = (p.get("company") or {}).get("identifier", slug)
        out.append({
            "id": f"sr:{slug}:{job_id}",
            "title": p.get("name", ""),
            # "ref" is the internal API URL, not a public page; the public job page
            # lives at jobs.smartrecruiters.com/<company identifier>/<posting id>
            "url": f"https://jobs.smartrecruiters.com/{company}/{job_id}",
        })
    return out


def fetch_workday(slug: str, company_name: str) -> list[dict]:
    # Workday needs three coordinates, not one, so the cache stores them packed
    # as "tenant|wd|site" (see discover_workday). Page size is hard-capped at 20
    # by the API and searchText is a fuzzy relevance match (it won't reduce the
    # set to real interns), so we paginate and let check_companies filter titles.
    try:
        tenant, wd, site = slug.split("|")
    except ValueError:
        raise PlatformError(f"workday: malformed slug {slug!r}")
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    headers = {**HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
    out, offset, total = [], 0, None
    for _ in range(WORKDAY_MAX_PAGES):
        try:
            resp = requests.post(
                api, headers=headers, timeout=TIMEOUT,
                json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": "intern"},
            )
        except requests.RequestException as e:
            raise PlatformError(f"{api} -> {e}")
        if resp.status_code != 200:
            raise PlatformError(f"{api} -> HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise PlatformError(f"{api} -> non-JSON response")
        postings = data.get("jobPostings") or []
        for p in postings:
            path = p.get("externalPath", "")
            out.append({
                "id": f"wd:{tenant}:{path}",
                "title": p.get("title", ""),
                "url": f"{base}/{site}{path}",
            })
        # Workday reports the match count only on the first page; later pages
        # report total=0, so pin it once and page against that.
        if total is None:
            total = data.get("total", 0)
        offset += 20
        if offset >= total or not postings:
            break
    return out


# --- Workday discovery -------------------------------------------------------
# Unlike the 4 ATSs, Workday coordinates can't be guessed as one slug: it needs
# tenant + datacenter (wdN) + an arbitrary site name. But the cxs API leaks
# enough to brute-force cheaply: a valid tenant+wd with a bogus site returns 404
# (a wrong tenant/wd returns 422), so we lock tenant+wd first, then try common
# site-name patterns for a 200.
# Workday relevance-ranks an "intern" search, so SWE-intern titles cluster on the
# first page or two -- measured across several boards, all hits landed on page 0.
# 6 pages (120 postings) is a generous safety margin while keeping each board to
# ~6 requests instead of 25 (the searchText fuzzy-matches hundreds of postings,
# so scanning them all every run was almost entirely wasted work).
WORKDAY_MAX_PAGES = 6
WORKDAY_WDS = ["wd1", "wd5", "wd3", "wd12", "wd10", "wd2", "wd101", "wd103", "wd105"]
# Workday throttles bursts of probes, which turns real boards into false "not
# found"s. Pace the probes and back off on 429/timeout so the 404/200 signals
# stay trustworthy.
WORKDAY_PROBE_DELAY = 0.3
WORKDAY_RETRY_STATUSES = {429, 503}
WORKDAY_PROBE_ATTEMPTS = 3


def _workday_tenants(name: str) -> list[str]:
    lower = name.lower()
    nospace = re.sub(r"[^a-z0-9]", "", lower)
    words = re.sub(r"[^a-z0-9 ]", "", lower).split()
    cands = [nospace]
    if words:
        cands += ["".join(words), words[0]]
    out = []
    for c in cands:
        if c and c not in out:
            out.append(c)
    return out


def _workday_sites(tenant: str, name: str) -> list[str]:
    cap, upper = tenant.capitalize(), tenant.upper()
    # brand slugs often derive from the full multi-word name, not the tenant
    # (e.g. "Capital One" -> site "Capital_One"), so build those variants too
    words = re.sub(r"[^a-z0-9 ]", "", name.lower()).split()
    titled = [w.capitalize() for w in words]
    brand = ["_".join(titled), "".join(titled), "_".join(words)] if len(words) > 1 else []
    pats = [
        "External", "Careers", "careers", "jobs", "Jobs", "ExternalCareers",
        "ExternalCareerSite", "External_Career_Site", "external_experienced",
        "External_Careers", "Global_Careers",
        f"{cap}External", f"{cap}ExternalCareerSite", f"{cap}Careers",
        f"{upper}ExternalCareerSite", f"{cap}_Careers", f"{cap}_External_Career_Site",
        *brand,
    ]
    out = []
    for p in pats:
        if p not in out:
            out.append(p)
    return out


def _workday_probe(tenant: str, wd: str, site: str) -> int:
    """POST the jobs endpoint and return its HTTP status, retrying transient
    throttling (429/503/timeout). Raises PlatformThrottled if no definitive
    (non-throttled) response comes back -- so a throttle is never mistaken for a
    clean 422 'not here'."""
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    headers = {**HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
    for attempt in range(WORKDAY_PROBE_ATTEMPTS):
        try:
            resp = requests.post(
                url, headers=headers, timeout=TIMEOUT,
                json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            )
            if resp.status_code not in WORKDAY_RETRY_STATUSES:
                return resp.status_code
        except requests.RequestException:
            pass
        time.sleep(WORKDAY_PROBE_DELAY * (attempt + 2))  # linear back-off
    raise PlatformThrottled(f"{tenant}.{wd}/{site}: no definitive response")


def discover_workday(name: str) -> str | None:
    """Return a packed 'tenant|wd|site' slug for a Workday company, or None if
    it's definitively not on Workday. Raises PlatformThrottled if throttling
    made the answer uncertain, so the caller can retry instead of caching a miss.
    """
    throttled = False
    for tenant in _workday_tenants(name):
        live_wd = None
        for wd in WORKDAY_WDS:
            time.sleep(WORKDAY_PROBE_DELAY)
            try:
                if _workday_probe(tenant, wd, "BogusSite_zz99") == 404:
                    live_wd = wd  # tenant+datacenter valid, site just wrong
                    break
            except PlatformThrottled:
                throttled = True
        if not live_wd:
            continue
        for site in _workday_sites(tenant, name):
            time.sleep(WORKDAY_PROBE_DELAY)
            try:
                if _workday_probe(tenant, live_wd, site) == 200:
                    return f"{tenant}|{live_wd}|{site}"
            except PlatformThrottled:
                throttled = True
    if throttled:
        raise PlatformThrottled(f"{name}: Workday probing throttled; inconclusive")
    return None


# --- Bespoke per-company integrations ---------------------------------------
# For big companies on fully-custom career sites (no standard ATS/Workday board),
# we hit their own JSON API directly. One fetcher per company, matched by name in
# resolve_companies.CUSTOM_COMPANIES rather than by slug-guessing.
AMAZON_MAX_PAGES = 3


def fetch_amazon(slug: str, company_name: str) -> list[dict]:
    # amazon.jobs exposes a public JSON search. Its intern volume is huge, so we
    # query the SWE-intern term server-side and sort newest-first; the shared
    # is_swe_intern title filter in check_companies stays the final authority.
    api = "https://www.amazon.jobs/en/search.json"
    out, offset, limit = [], 0, 100
    for _ in range(AMAZON_MAX_PAGES):
        data = _get_json(api, params={
            "base_query": "software engineer intern",
            "sort": "recent",
            "result_limit": limit,
            "offset": offset,
        })
        if not isinstance(data, dict):
            raise PlatformError("amazon: unexpected response shape")
        jobs = data.get("jobs") or []
        for j in jobs:
            path = j.get("job_path", "")
            out.append({
                "id": f"amazon:{path}",
                "title": j.get("title", ""),
                "url": f"https://www.amazon.jobs{path}",
            })
        offset += limit
        if offset >= data.get("hits", 0) or not jobs:
            break
    return out


def verify_smartrecruiters(slug: str, company_name: str) -> bool:
    # unlike the other 3 platforms, this endpoint returns HTTP 200 with an empty
    # content list for ANY slug -- even ones that don't correspond to a real
    # company -- so a 200 alone is not proof the slug is real. Require at least
    # one actual open posting as evidence before accepting the slug guess.
    try:
        data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings")
    except PlatformError:
        return False
    return isinstance(data, dict) and data.get("totalFound", 0) > 0


# fetchers by platform name; check_companies.py looks up the resolved platform here
PLATFORMS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
    "amazon": fetch_amazon,
}

# platforms resolvable by guessing a single slug + verifying against the live API,
# in probe order (cheaper/more common first). Workday is excluded: its coordinates
# aren't a single guessable slug, so resolve_companies.py handles it via
# discover_workday() only after these four miss.
ATS_ORDER = ["greenhouse", "lever", "ashby", "smartrecruiters"]

# platforms whose mere HTTP success doesn't prove the slug is real need an extra
# existence check before resolve_companies.py trusts a slug guess
VERIFIERS = {
    "smartrecruiters": verify_smartrecruiters,
}
