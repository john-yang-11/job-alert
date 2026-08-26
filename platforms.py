"""ATS (applicant tracking system) job-board integrations.

Each fetch_* function takes a company slug and returns a list of normalized
postings: {"id": str, "title": str, "url": str}. Raises PlatformError for
anything that isn't a live, existing board (wrong slug, network error, etc.)
so callers can try the next platform/slug guess.
"""

import re
import socket
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

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


def verify_greenhouse(slug: str, company_name: str) -> bool:
    """Board-name match, for the slug-guessing path only.

    This is a guard against a *guess* landing on some unrelated company's board,
    so it belongs here rather than in fetch_greenhouse: a board we found linked
    from the company's own careers page is theirs no matter what it's called, and
    name-checking it on every fetch just breaks it forever. Hudson River
    Trading's board is "HRT Talent Community" and Twitter's careers site now
    points at xAI's -- both correct, both rejected by a name match.
    """
    try:
        board = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
    except PlatformError:
        return False
    if not isinstance(board, dict) or not _names_match(company_name, board.get("name", "")):
        return False
    try:
        return bool(fetch_greenhouse(slug, company_name))
    except PlatformError:
        return False


def fetch_greenhouse(slug: str, company_name: str) -> list[dict]:
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if jobs is None:
        raise PlatformError("greenhouse: no 'jobs' key")
    return [
        {
            "id": f"gh:{slug}:{j['id']}",
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "location": (j.get("location") or {}).get("name", "") or "",
        }
        for j in jobs
    ]


def fetch_lever(slug: str, company_name: str) -> list[dict]:
    data = _get_json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if not isinstance(data, list):
        raise PlatformError("lever: response is not a list")
    return [
        {
            "id": f"lever:{slug}:{p['id']}",
            "title": p.get("text", ""),
            "url": p.get("hostedUrl", ""),
            "location": (p.get("categories") or {}).get("location", "") or "",
            "country": p.get("country", "") or "",  # ISO code when Lever provides it
        }
        for p in data
    ]


# Vendors hand out sandbox/demo boards under a real company's name -- e.g.
# lever/"linkedin" is "LinkedIn Partner Sandbox - RSC testing" (23 fake postings
# like "Anirban jobReq 3 - public"), and smartrecruiters/"uber" holds one posting
# titled "Test UAT". Slug-guessing happily accepted those, so the watcher looked
# like it was covering LinkedIn and Uber while reading a demo tenant -- a silent
# miss, worse than an unresolved company (which at least shows up in the report).
SANDBOX_RE = re.compile(r"\b(sandbox|demo|dummy|test(ing)?|staging|uat|qa)\b", re.IGNORECASE)
# "QA Engineer" and "Test Engineer" are real jobs, so the posting-title check
# uses the unambiguous markers only, and only condemns a board small enough that
# every posting matching one can't be coincidence. A demo tenant holds a handful
# of rows; a real board with 30 QA roles is just a company that hires QA.
SANDBOX_POSTING_RE = re.compile(r"\b(sandbox|demo|dummy|staging|uat|test)\b", re.IGNORECASE)
SANDBOX_MAX_POSTINGS = 5


def _looks_like_sandbox(titles: list[str]) -> bool:
    return (0 < len(titles) <= SANDBOX_MAX_POSTINGS
            and all(SANDBOX_POSTING_RE.search(t or "") for t in titles))


def verify_lever(slug: str, company_name: str) -> bool:
    """Lever's posting API exposes no company name to match on, so confirm the
    slug via its public board page title and reject obvious sandbox tenants."""
    try:
        postings = fetch_lever(slug, company_name)
    except PlatformError:
        return False
    if not postings:
        return False
    try:
        resp = requests.get(f"https://jobs.lever.co/{slug}", headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException:
        return True  # page unreachable: fall back to trusting the API response
    if resp.status_code != 200:
        return True
    # the board's own name is the strongest signal, and the place the weaker
    # markers ("testing", "QA") are unambiguous -- no real board is titled that
    title = re.search(r"<title>(.*?)</title>", resp.text, re.S)
    if title and SANDBOX_RE.search(title.group(1)):
        return False
    # a board whose every posting reads like test data is a sandbox too
    return not _looks_like_sandbox([p["title"] for p in postings])


def fetch_ashby(slug: str, company_name: str) -> list[dict]:
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if jobs is None:
        raise PlatformError("ashby: no 'jobs' key")
    return [
        {
            "id": f"ashby:{slug}:{j['id']}",
            "title": j.get("title", ""),
            "url": j.get("jobUrl", ""),
            "location": j.get("location", "") or "",
            # full country name when Ashby's address block is populated
            "country": ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry", "") or "",
        }
        for j in jobs
    ]


def fetch_smartrecruiters(slug: str, company_name: str) -> list[dict]:
    data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings")
    content = data.get("content") if isinstance(data, dict) else None
    if content is None:
        raise PlatformError("smartrecruiters: no 'content' key")
    if any(not isinstance(p, dict) for p in content):
        raise PlatformError("smartrecruiters: malformed 'content' entries (likely wrong slug)")
    if _looks_like_sandbox([p.get("name", "") for p in content]):
        raise PlatformError(f"smartrecruiters: {slug!r} looks like a sandbox tenant")
    out = []
    for p in content:
        job_id = p.get("id")
        company = (p.get("company") or {}).get("identifier", slug)
        loc = p.get("location") or {}
        out.append({
            "id": f"sr:{slug}:{job_id}",
            "title": p.get("name", ""),
            # "ref" is the internal API URL, not a public page; the public job page
            # lives at jobs.smartrecruiters.com/<company identifier>/<posting id>
            "url": f"https://jobs.smartrecruiters.com/{company}/{job_id}",
            "location": loc.get("fullLocation", "") or "",
            "country": loc.get("country", "") or "",  # ISO alpha-2, e.g. "us"
        })
    return out


INTERN_FACET_RE = re.compile(r"\bintern(ship)?s?\b", re.IGNORECASE)  # not "international"


def _workday_post(api: str, headers: dict, applied: dict, offset: int, search: str) -> dict:
    try:
        resp = requests.post(
            api, headers=headers, timeout=TIMEOUT,
            json={"appliedFacets": applied, "limit": 20, "offset": offset, "searchText": search},
        )
    except requests.RequestException as e:
        raise PlatformError(f"{api} -> {e}")
    if resp.status_code != 200:
        raise PlatformError(f"{api} -> HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise PlatformError(f"{api} -> non-JSON response")


def _workday_intern_facet(data: dict) -> tuple[str, str] | None:
    """Find the board's intern filter (facetParameter, value_id) from a response's
    facets, e.g. workerSubType -> "Intern". IDs are tenant-specific but the
    descriptor is stable, so we discover it per board. None if the board has none."""
    for f in data.get("facets", []):
        param = f.get("facetParameter")
        if not param:
            continue
        for v in f.get("values", []):
            if INTERN_FACET_RE.search(v.get("descriptor") or "") and v.get("id"):
                return param, v["id"]
    return None


def _workday_collect(api: str, job_base: str, tenant: str) -> list[dict]:
    # Most boards expose an "Intern" facet (e.g. workerSubType); applying it
    # returns exactly the intern roles server-side -- far better than a fuzzy
    # "intern" text search, which buries real intern roles behind experienced
    # ones on big boards. Fall back to the text search for a board with no facet.
    headers = {**HEADERS, "Content-Type": "application/json", "Accept": "application/json"}

    first = _workday_post(api, headers, {}, 0, "")
    facet = _workday_intern_facet(first)
    if facet:
        applied, search = {facet[0]: [facet[1]]}, ""
    else:
        applied, search = {}, "intern"

    out, offset, total = [], 0, None
    for _ in range(WORKDAY_MAX_PAGES):
        data = _workday_post(api, headers, applied, offset, search)
        postings = data.get("jobPostings") or []
        for p in postings:
            path = p.get("externalPath", "")
            # externalPath looks like "/job/San-Jose-California-US/Title-Slug_R123";
            # the first segment is the location slug, hyphenated ("City-State-US"/
            # "City-Country") -- no clean structured location from this endpoint.
            parts = path.split("/")
            loc = parts[2] if len(parts) > 2 else ""
            out.append({
                "id": f"wd:{tenant}:{path}",
                "title": p.get("title", ""),
                "url": f"{job_base}{path}",
                "location": loc,
            })
        # Workday reports the match count only on the first page; later pages
        # report total=0, so pin it once and page against that.
        if total is None:
            total = data.get("total", 0)
        offset += 20
        if offset >= total or not postings:
            break
    return out


def fetch_workday(slug: str, company_name: str) -> list[dict]:
    # Workday needs three coordinates, not one, so the cache stores them packed
    # as "tenant|wd|site" (see discover_workday).
    try:
        tenant, wd, site = slug.split("|")
    except ValueError:
        raise PlatformError(f"workday: malformed slug {slug!r}")
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    return _workday_collect(f"{base}/wday/cxs/{tenant}/{site}/jobs", f"{base}/{site}", tenant)


def fetch_workdaysite(slug: str, company_name: str) -> list[dict]:
    """Workday's other public host shape: wdN.myworkdaysite.com/recruiting/
    <tenant>/<site> (Snap uses it). Same cxs API, different URL layout, and the
    datacenter leads the hostname instead of trailing the tenant -- so it can't
    be packed into the myworkdayjobs "tenant|wd|site" slug. Stored as
    "wd|tenant|site"."""
    try:
        wd, tenant, site = slug.split("|")
    except ValueError:
        raise PlatformError(f"workdaysite: malformed slug {slug!r}")
    base = f"https://{wd}.myworkdaysite.com"
    return _workday_collect(f"{base}/wday/cxs/{tenant}/{site}/jobs",
                            f"{base}/recruiting/{tenant}/{site}", tenant)


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
# Transient by definition -- retry rather than read them as "site not here".
# Deliberately excludes 403: Workday returns it when blocking a burst, but
# treating it as retryable would make probes raise PlatformThrottled in bulk,
# which is the uncacheable-forever state this whole path exists to avoid.
WORKDAY_RETRY_STATUSES = {429, 502, 503, 504}
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
        # Campus sites first, on purpose. A tenant can run two boards -- a big
        # corporate one and a small campus one -- and the first site that answers
        # with any postings wins. Mastercard resolved to CorporateCareers (1,108
        # reqs, whose intern facet is 19 Latin America consulting roles and zero
        # US) while its actual "Software Engineer Intern, Summer 2027" sat on
        # /Campus, unwatched. This tool only ever wants interns, so when a campus
        # board exists it is always the better answer.
        "Campus", "CampusCareers", "Campus_Careers", "Students",
        "University", "EarlyCareers", "Early_Careers",
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


def _workday_host(tenant: str, wd: str) -> str:
    return f"{tenant}.{wd}.myworkdayjobs.com"


def _host_exists(host: str) -> bool:
    """Whether the hostname resolves at all.

    Not every Workday datacenter answers for every tenant -- wd2 and wd101
    currently have no record for any tenant we probe, so they NXDOMAIN every
    time. That surfaced as a ConnectionError, which _workday_probe could not
    tell apart from throttling, so it raised PlatformThrottled; discover_workday
    then reported every company that ISN'T on Workday as inconclusive rather
    than a clean miss. resolve_new never caches an inconclusive result, so those
    companies were retried on every single run, forever, consuming the whole
    per-run budget and starving newly added companies. Checking DNS first turns
    that permanent failure back into the definitive negative it always was --
    and skips the HTTP attempts and back-off sleeps entirely.
    """
    try:
        socket.getaddrinfo(host, 443)
        return True
    except socket.gaierror:
        return False
    except Exception:
        return True  # anything else: let the HTTP probe make the call


def _workday_probe(tenant: str, wd: str, site: str) -> tuple[int, int]:
    """POST the jobs endpoint and return (HTTP status, total postings), retrying
    transient throttling (429/503/timeout). Raises PlatformThrottled if no
    definitive (non-throttled) response comes back -- so a throttle is never
    mistaken for a clean 422 'not here'. `total` is 0 for any non-200."""
    url = f"https://{_workday_host(tenant, wd)}/wday/cxs/{tenant}/{site}/jobs"
    headers = {**HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
    for attempt in range(WORKDAY_PROBE_ATTEMPTS):
        try:
            resp = requests.post(
                url, headers=headers, timeout=TIMEOUT,
                json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            )
            if resp.status_code not in WORKDAY_RETRY_STATUSES:
                total = 0
                if resp.status_code == 200:
                    try:
                        total = (resp.json() or {}).get("total", 0) or 0
                    except ValueError:
                        total = 0
                return resp.status_code, total
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
            # no DNS record means this datacenter has nothing for this tenant --
            # a permanent answer, not a throttle (see _host_exists)
            if not _host_exists(_workday_host(tenant, wd)):
                continue
            time.sleep(WORKDAY_PROBE_DELAY)
            try:
                if _workday_probe(tenant, wd, "BogusSite_zz99")[0] == 404:
                    live_wd = wd  # tenant+datacenter valid, site just wrong
                    break
            except PlatformThrottled:
                throttled = True
        if not live_wd:
            continue
        for site in _workday_sites(tenant, name):
            time.sleep(WORKDAY_PROBE_DELAY)
            try:
                # A 200 alone isn't enough: tenants keep empty stub sites around
                # (qualcomm|wd12|External answers 200 with zero reqs), and since a
                # resolved entry is trusted forever, accepting one silently parks
                # the company on a board that can never alert. Require postings as
                # evidence, the same bar verify_smartrecruiters sets.
                status, total = _workday_probe(tenant, live_wd, site)
                if status == 200 and total > 0:
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
                "location": j.get("city", "") or "",
                "country": j.get("country_code", "") or "",  # ISO alpha-3, e.g. "USA"
            })
        offset += limit
        if offset >= data.get("hits", 0) or not jobs:
            break
    return out


CAPITALONE_MAX_PAGES = 5
CAPITALONE_WORKDAY = "capitalone|wd12|Capital_One"
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def fetch_capitalone(slug: str, company_name: str) -> list[dict]:
    # Capital One's tech interns can show up on their public careers site
    # (capitalonecareers.com, server-rendered) OR their Workday board, so this
    # unions both and dedups -- the redundant "scraper on top of the board" case.
    # The careers site is a Phenom SSR page whose job links encode everything we
    # need: /job/<location>/<title-slug>/<category>/<id>.
    out, seen = [], set()

    def add(job_id: str, title: str, url: str, location: str = "") -> None:
        if job_id in seen:
            return
        seen.add(job_id)
        out.append({"id": job_id, "title": title, "url": url, "location": location})

    base = "https://www.capitalonecareers.com"
    headers = {**HEADERS, "User-Agent": _BROWSER_UA}
    for pg in range(1, CAPITALONE_MAX_PAGES + 1):
        try:
            resp = requests.get(f"{base}/search-jobs/software%20engineer%20intern",
                                headers=headers, params={"p": pg}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise PlatformError(f"capitalonecareers -> {e}")
        if resp.status_code != 200:
            raise PlatformError(f"capitalonecareers -> HTTP {resp.status_code}")
        before = len(seen)
        for path in re.findall(r'href="(/job/[^"]+)"', resp.text):
            parts = path.strip("/").split("/")
            if len(parts) < 5:
                continue
            add(f"capitalone:{parts[-1]}", parts[-3].replace("-", " "), base + path,
                parts[-4].replace("-", " "))
        if len(seen) == before:   # no new links -> past the last results page
            break

    # also fold in the Workday board (facet-filtered to interns); best-effort
    try:
        for j in fetch_workday(CAPITALONE_WORKDAY, company_name):
            add(j["id"], j["title"], j["url"], j.get("location", ""))
    except PlatformError:
        pass
    return out


# --- Server-rendered XML job feeds (Radancy et al.) --------------------------
# Big-employer career sites built on Radancy (Wells Fargo, Uber, DraftKings,
# Caterpillar, Fidelity, Nutanix, ...) are JS-rendered -- nothing to scrape and
# no JSON API -- but they publish the whole req list as an XML feed at
# /jobs/xml/?rss=true. Two dialects are in the wild and a host serves one or the
# other: RSS <channel><item> (title/link/guid only) and Radancy's own
# <source><job> (adds city/state/country). The feed URL *is* the slug, since
# there's no tenant identifier to rebuild it from.
JOBFEED_PATHS = ("/jobs/xml/?rss=true", "/en/jobs/xml/?rss=true",
                 "/careers-home/jobs/xml/?rss=true")


def _feed_entries(url: str) -> list[ET.Element]:
    try:
        resp = requests.get(url, headers={**HEADERS, "Accept": "application/xml,text/xml,*/*"},
                            timeout=TIMEOUT)
    except requests.RequestException as e:
        raise PlatformError(f"{url} -> {e}")
    if resp.status_code != 200:
        raise PlatformError(f"{url} -> HTTP {resp.status_code}")
    if not resp.content.lstrip().startswith(b"<?xml"):
        raise PlatformError(f"{url} -> not an XML feed")
    # Parse the raw bytes, not resp.text. These hosts send "Content-Type:
    # text/xml" with no charset, so requests falls back to ISO-8859-1 (RFC 2616)
    # while the document itself declares utf-8 -- decoding via .text turned every
    # en-dash and curly quote into mojibake ("Engineer \xe2\x80\x93 Hardware").
    # ElementTree reads the XML declaration, so bytes decode correctly.
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        raise PlatformError(f"{url} -> malformed XML ({e})")
    return root.findall(".//item") or root.findall(".//job")


def fetch_jobfeed(slug: str, company_name: str) -> list[dict]:
    entries = _feed_entries(slug)
    if not entries:
        raise PlatformError(f"{slug} -> feed has no entries")
    host = urlsplit(slug).netloc
    out = []
    for e in entries:
        url = (e.findtext("url") or e.findtext("link") or "").strip()
        title = (e.findtext("title") or "").strip()
        if not title:
            continue
        job_id = (e.findtext("requisitionid") or e.findtext("referencenumber")
                  or e.findtext("guid") or url).strip()
        # <job> carries structured city/state/country; <item> carries none at
        # all, and is_us_location treats an empty location as "don't drop it".
        loc = ", ".join(p for p in ((e.findtext("city") or "").strip(),
                                    (e.findtext("state") or "").strip()) if p)
        out.append({
            "id": f"feed:{host}:{job_id}",
            "title": title,
            "url": url,
            "location": loc,
            "country": (e.findtext("country") or "").strip(),
        })
    return out


# --- Radancy JSON careers API ------------------------------------------------
# The same vendor also ships a JSON API on some sites (GitHub, AMD) where others
# only expose the XML feed above: <base>/api/jobs?limit=100&page=N, each row
# wrapped in {"data": {...}} with a structured full_location/country -- better
# than the feed, which is why this is tried first. limit caps at 100 (larger
# values return nothing at all, not an error), so it has to page.
CAREERSITE_PAGE_SIZE = 100
CAREERSITE_MAX_PAGES = 12


def _careersite_page(base: str, page: int) -> dict:
    data = _get_json(f"{base}/api/jobs",
                     params={"limit": CAREERSITE_PAGE_SIZE, "page": page})
    if not isinstance(data, dict) or "jobs" not in data:
        raise PlatformError(f"{base}/api/jobs -> unexpected response shape")
    return data


def fetch_careersite(slug: str, company_name: str) -> list[dict]:
    base = slug.rstrip("/")
    host = urlsplit(base).netloc
    out = []
    for page in range(1, CAREERSITE_MAX_PAGES + 1):
        data = _careersite_page(base, page)
        rows = data.get("jobs") or []
        for row in rows:
            j = row.get("data") or {}
            job_id = j.get("req_id") or j.get("slug")
            if not job_id:
                continue
            out.append({
                "id": f"cs:{host}:{job_id}",
                "title": j.get("title", ""),
                # apply_url points at the ATS login wall; this is the readable page
                "url": f"{base}/careers-home/jobs/{j.get('slug', job_id)}",
                "location": j.get("full_location", "") or j.get("city", "") or "",
                "country": j.get("country", "") or "",
            })
        if len(rows) < CAREERSITE_PAGE_SIZE or len(out) >= data.get("totalCount", 0):
            break
    return out


# --- Board discovery from a company's own careers page -----------------------
# Slug-guessing only finds boards whose slug looks like the company name, which
# misses the ones that matter most: Samsung's Workday tenant is "sec", Hudson
# River Trading's Greenhouse board is "hrttalentcommunity", US Bank's site is
# "US_Bank_Careers". But every one of those career sites links to its own board,
# so read the coordinates off the page instead of guessing them.
DISCOVERY_TIMEOUT = 10
DOMAIN_STOPWORDS = {"inc", "corp", "corporation", "llc", "ltd", "co", "the",
                    "group", "company"}
# path segments that show up where a slug would be but aren't one
NON_SLUGS = {"embed", "job_board", "www", "en", "us", "job", "jobs", "search", "careers"}

_WD_RE = re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/[^\"'\s]*?/)?"
                    r"(?:[a-zA-Z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")
# note: unlike myworkdayjobs.com, this host is just the datacenter -- the tenant
# lives in the path (wd1.myworkdaysite.com/recruiting/snapchat/snap)
_WDSITE_RE = re.compile(r"(wd\d+)\.myworkdaysite\.com/(?:[a-zA-Z]{2}-[A-Z]{2}/)?"
                        r"(?:recruiting/)?([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)")
_BOARD_RES = (
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/"
                              r"(?:embed/job_board\?for=)?([a-zA-Z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([a-zA-Z0-9_-]+)")),
)


def _company_domains(name: str) -> list[str]:
    words = [w for w in re.sub(r"[^a-z0-9 &]", " ", name.lower()).split()
             if w and w not in DOMAIN_STOPWORDS]
    if not words:
        return []
    out = ["".join(words) + ".com"]
    if len(words) > 1:
        out += ["-".join(words) + ".com", words[0] + ".com"]
    return out[:3]


def _careers_urls(name: str) -> list[str]:
    urls = [f"https://{sub}{d}{path}"
            for d in _company_domains(name)
            for sub, path in (("careers.", ""), ("jobs.", ""), ("www.", "/careers"))]
    # some companies put careers on the .careers TLD instead of a subdomain of
    # their .com (github.careers), which no amount of guessing around ".com"
    # reaches
    words = [w for w in re.sub(r"[^a-z0-9 &]", " ", name.lower()).split()
             if w and w not in DOMAIN_STOPWORDS]
    if words:
        urls.append(f"https://www.{''.join(words)}.careers")
    return urls


def _extract_board(html: str) -> tuple[str, str] | None:
    m = _WD_RE.search(html)
    if m and m.group(3) not in NON_SLUGS:
        return "workday", f"{m.group(1)}|{m.group(2)}|{m.group(3)}"
    m = _WDSITE_RE.search(html)
    if m and m.group(3) not in NON_SLUGS:
        return "workdaysite", f"{m.group(1)}|{m.group(2)}|{m.group(3)}"
    for platform, rx in _BOARD_RES:
        m = rx.search(html)
        if m and m.group(1) not in NON_SLUGS:
            return platform, m.group(1)
    return None


_JOB_LINK_RE = re.compile(r"""href=["']([^"'#]*?/(?:jobs|job-search|search-jobs|openings)/?)["']""",
                          re.IGNORECASE)
MAX_JOB_SUBPAGES = 2


def _job_links(page_url: str, html: str) -> list[str]:
    """Same-host links that look like the actual listings page. Career landing
    pages are often pure marketing (Snap's links to its board live one click in,
    on /jobs), so discovery follows a couple of them."""
    base = urlsplit(page_url)
    out = []
    for href in _JOB_LINK_RE.findall(html):
        url = urljoin(page_url, href)
        parts = urlsplit(url)
        if parts.netloc != base.netloc or parts.path.rstrip("/") == base.path.rstrip("/"):
            continue
        if url not in out:
            out.append(url)
    return out


def discover_from_careers_page(name: str) -> tuple[str, str] | None:
    """Load the company's careers page and read its job board's coordinates off
    it. Returns (platform, slug) or None. Unverified -- the caller should
    confirm against the live API before caching it."""
    headers = {**HEADERS, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9"}

    def load(url: str):
        try:
            resp = requests.get(url, headers=headers, timeout=DISCOVERY_TIMEOUT,
                                allow_redirects=True)
        except requests.RequestException:
            return None
        return resp if resp.status_code < 400 else None

    for url in _careers_urls(name):
        resp = load(url)
        if resp is None:
            continue
        found = _extract_board(resp.text)
        if found:
            return found
        for link in _job_links(resp.url, resp.text)[:MAX_JOB_SUBPAGES]:
            sub = load(link)
            if sub is None:
                continue
            found = _extract_board(sub.text)
            if found:
                return found
        # no embedded board: the site may still expose its own reqs directly
        host = f"{urlsplit(resp.url).scheme}://{urlsplit(resp.url).netloc}"
        try:
            if (_careersite_page(host, 1).get("totalCount") or 0) > 0:
                return "careersite", host
        except PlatformError:
            pass
        for path in JOBFEED_PATHS:
            feed = host + path
            try:
                if _feed_entries(feed):
                    return "jobfeed", feed
            except PlatformError:
                continue
    return None


def verify_discovered(platform: str, slug: str, company_name: str) -> None:
    """Liveness-check a board found on the company's own careers page, raising
    PlatformError if it isn't real.

    Deliberately the plain fetcher and not VERIFIERS: those are ownership gates
    for *guessed* slugs, and a board linked from the company's own careers page
    is already known to be theirs. Running them here would reject correct
    answers -- see verify_greenhouse.
    """
    PLATFORMS[platform](slug, company_name)


def verify_smartrecruiters(slug: str, company_name: str) -> bool:
    # unlike the other 3 platforms, this endpoint returns HTTP 200 with an empty
    # content list for ANY slug -- even ones that don't correspond to a real
    # company -- so a 200 alone is not proof the slug is real. Require at least
    # one actual open posting as evidence before accepting the slug guess.
    # Goes through fetch_ rather than the raw endpoint so the sandbox-tenant
    # guard applies here too -- postings alone aren't proof it's the real board.
    try:
        return bool(fetch_smartrecruiters(slug, company_name))
    except PlatformError:
        return False


# fetchers by platform name; check_companies.py looks up the resolved platform here
PLATFORMS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
    "workdaysite": fetch_workdaysite,
    "jobfeed": fetch_jobfeed,
    "careersite": fetch_careersite,
    "amazon": fetch_amazon,
    "capitalone": fetch_capitalone,
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
    "lever": verify_lever,
    "greenhouse": verify_greenhouse,
}
