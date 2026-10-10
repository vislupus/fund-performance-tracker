"""Diagnostics: fetch a page and summarise links, forms, tables and script URLs.

Used from CI logs to discover download endpoints of sites that cannot be
inspected from the development environment:  python -m fund_tracker inspect <url>
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .http import Http

KEYWORDS = ("letolt", "letölt", "csv", "xls", "download", "export", "resource", "portlet", "arfolyam",
            "árfolyam", "history", "histor", "chart", "json", "ajax", "api", "nav", "цена", "стойност", "istor")


def inspect(url: str, http: Http | None = None, max_items: int = 60) -> str:
    http = http or Http()
    resp = http.get(url, headers={"Accept-Language": "hu,bg,en;q=0.8"}, allow_statuses=(200, 301, 302, 403, 404))
    html = resp.text
    out = [f"URL: {url}", f"status: {resp.status_code}  length: {len(html)}"]
    soup = BeautifulSoup(html, "lxml")
    out.append(f"title: {(soup.title.get_text(strip=True) if soup.title else '')[:120]}")

    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        text = a.get_text(" ", strip=True)[:60]
        if any(k in (href + " " + text).lower() for k in KEYWORDS):
            links.append(f"  A  {text!r} -> {href[:220]}")
    out.append(f"links with keywords ({len(links)}):")
    out.extend(links[:max_items])

    out.append("forms:")
    for form in soup.find_all("form")[:20]:
        action = urljoin(url, form.get("action") or "")
        fields = [f"{i.get('name')}={i.get('value', '')!s:.30}" for i in form.find_all(["input", "select"]) if i.get("name")]
        out.append(f"  FORM {form.get('method', 'get').upper()} {action[:200]} id={form.get('id')} fields={fields[:25]}")

    out.append("iframes/scripts with keywords:")
    for tag in soup.find_all(["iframe", "script"]):
        src = tag.get("src")
        if src and any(k in src.lower() for k in KEYWORDS):
            out.append(f"  {tag.name.upper()} {urljoin(url, src)[:220]}")

    out.append("URLs inside inline scripts / attributes:")
    found = set()
    for m in re.finditer(r"""(?:https?:)?//[^\s"'<>\\]+|/[A-Za-z0-9_./\-]*(?:" + "|".join(KEYWORDS) + r")[^\s"'<>\\]*""", html, re.I):
        u = m.group(0)
        if any(k in u.lower() for k in KEYWORDS) and u not in found:
            found.add(u)
    out.extend(f"  {u[:240]}" for u in sorted(found)[:max_items])

    out.append("tables (first rows):")
    for t in soup.find_all("table")[:12]:
        rows = t.find_all("tr")
        cells = [[c.get_text(" ", strip=True)[:25] for c in r.find_all(["th", "td"])][:8] for r in rows[:3]]
        out.append(f"  TABLE class={t.get('class')} id={t.get('id')} rows={len(rows)} first={cells}")
    out.append("data-* attributes mentioning urls:")
    for tag in soup.find_all(attrs=True):
        for k, v in tag.attrs.items():
            if k.startswith("data-") and isinstance(v, str) and ("/" in v and any(kw in v.lower() for kw in KEYWORDS)):
                out.append(f"  {tag.name} {k}={v[:200]}")
                break
    return "\n".join(out)
