"""Diagnostics for discovering download endpoints (run from CI, printed to the log)."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .http import Http


def dump_forms(html: str, base: str, limit: int = 80) -> list[str]:
    out = []
    soup = BeautifulSoup(html, "lxml")
    for form in soup.find_all("form"):
        out.append(f"FORM id={form.get('id')} method={form.get('method')} action={urljoin(base, form.get('action') or '')[:160]}")
        for el in form.find_all(["input", "select", "textarea", "button"]):
            name = el.get("name") or el.get("id")
            if not name:
                continue
            if el.name == "select":
                opts = [(o.get("value"), o.get_text(strip=True)[:40]) for o in el.find_all("option")][:limit]
                out.append(f"   SELECT {name} options={opts}")
            else:
                out.append(f"   {el.name.upper()} type={el.get('type')} name={name} value={str(el.get('value', ''))[:limit]!r}")
    return out


def dump_inline_js(html: str, pattern: str) -> list[str]:
    out = []
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script"):
        txt = script.string or script.get_text()
        if txt and re.search(pattern, txt):
            for m in re.finditer(r"[^\n]*" + pattern + r"[^\n]*", txt):
                out.append("   JS: " + m.group(0).strip()[:400])
    return out


def probe_bamosz(isin: str) -> str:
    http = Http()
    out: list[str] = []
    page = f"https://www.bamosz.hu/alapoldal?isin={isin}"
    r = http.get(page)
    html = r.text
    out.append(f"== {page} status {r.status_code} len {len(html)}")
    out.extend(dump_forms(html, page))
    out.extend(dump_inline_js(html, r"kuka|download|fileDownload|separator|submit\(\)"))
    m = re.search(r'chartDataValue"[^>]*value="(\[\[.{0,400})', html)
    out.append("chartDataValue head: " + (m.group(1)[:400] if m else "not found"))
    # count chart points
    m2 = re.search(r'chartDataValue"[^>]*value="(\[\[[^"]*\]\])', html)
    if m2:
        pts = re.findall(r"\[(\d{13}),\s*([\d.]+)\]", m2.group(1))
        out.append(f"chartDataValue points: {len(pts)} first={pts[:1]} last={pts[-1:]}")
    for method, url, data in [
        ("POST", "https://www.bamosz.hu/bamosz-public-alapoldal-portlet/kuka.download", {"isin": isin, "separator": ";"}),
        ("GET", "https://www.bamosz.hu/bamosz-public-alapoldal-portlet/kuka.download", {"isin": isin, "separator": ";"}),
        ("POST", "https://www.bamosz.hu/a", {"isin": isin, "separator": ";"}),
    ]:
        try:
            if method == "POST":
                resp = http._session.post(url, data=data, timeout=40)
            else:
                resp = http._session.get(url, params=data, timeout=40)
            body = resp.text
            ctype = resp.headers.get("content-type", "")
            out.append(f"== {method} {url} {data} -> {resp.status_code} {ctype} len {len(body)}")
            out.append("   head: " + body[:600].replace("\n", " | "))
            out.append("   tail: " + body[-300:].replace("\n", " | "))
        except Exception as exc:
            out.append(f"== {method} {url} -> ERROR {exc}")
    # letoltesek page
    page2 = "https://www.bamosz.hu/letoltesek"
    r = http.get(page2)
    out.append(f"== {page2} status {r.status_code} len {len(r.text)}")
    out.extend(dump_forms(r.text, page2, limit=40)[:120])
    out.extend(dump_inline_js(r.text, r"data\.download|selectedAlap|selectedOption|selectedData|intervallumra")[:60])
    try:
        js = http.get("https://www.bamosz.hu/bamosz-public-letoltes-portlet/js/letoltes.js").text
        out.append("== letoltes.js:\n" + js[:6000])
    except Exception as exc:
        out.append(f"letoltes.js ERROR {exc}")
    return "\n".join(out)


def probe_url(url: str, post: dict | None = None, head: int = 3000) -> str:
    http = Http()
    try:
        if post:
            resp = http._session.post(url, data=post, timeout=40)
        else:
            resp = http._session.get(url, timeout=40)
        return f"== {url} {post or ''} -> {resp.status_code} {resp.headers.get('content-type', '')}\n{resp.text[:head]}"
    except Exception as exc:
        return f"== {url} -> ERROR {exc}"


def probe_forms(url: str) -> str:
    http = Http()
    r = http.get(url, allow_statuses=(200, 404, 403))
    out = [f"== {url} -> {r.status_code} len {len(r.text)}"]
    out.extend(dump_forms(r.text, url, limit=120))
    out.extend(dump_inline_js(r.text, r"currentRate|mutualUnitValues|datepicker|fund_name|ajax|json")[:40])
    soup = BeautifulSoup(r.text, "lxml")
    for t in soup.find_all("table")[:6]:
        rows = t.find_all("tr")
        cells = [[c.get_text(" ", strip=True)[:30] for c in rw.find_all(["th", "td"])][:8] for rw in rows[:4]]
        out.append(f"TABLE class={t.get('class')} id={t.get('id')} rows={len(rows)} first={cells}")
    return "\n".join(out)
