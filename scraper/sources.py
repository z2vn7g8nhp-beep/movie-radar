"""数据源：猫眼（手机版接口）、淘票票（mtop H5 接口）、IMDb（搜索建议 + 官方评分数据集）。"""
import gzip
import hashlib
import io
import json
import random
import re
import time
from urllib.parse import quote

import requests

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


class SourceError(Exception):
    pass


def norm_name(s):
    """把片名标准化，用于在猫眼和淘票票之间配对。"""
    s = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", (s or "")).lower()
    s = s.replace("&", "and")
    s = re.sub(r"[\s:：·・,，.。!！?？'\"“”‘’《》()（）\[\]【】\-—_/、~～]", "", s)
    return s


def nap(a=0.3, b=0.9):
    time.sleep(random.uniform(a, b))


# ---------------------------------------------------------------- 猫眼
class Maoyan:
    BASE = "https://m.maoyan.com"

    def __init__(self, ci):
        self.ci = ci
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": "https://m.maoyan.com/",
                               "Accept": "application/json, text/plain, */*"})
        self.s.cookies.set("ci", str(ci), domain="m.maoyan.com")

    def _get(self, path, params):
        nap()
        for attempt in range(2):
            try:
                r = self.s.get(self.BASE + path, params=params, timeout=15)
                break
            except requests.RequestException as e:
                if attempt:
                    raise SourceError(f"猫眼连接失败：{e.__class__.__name__}")
                nap(2, 4)
        if r.status_code != 200:
            raise SourceError(f"猫眼返回 HTTP {r.status_code}（可能被限流）")
        try:
            return r.json()
        except ValueError:
            raise SourceError("猫眼返回的不是数据（可能触发了验证码）")

    def _more(self, ids):
        out = []
        for i in range(0, len(ids), 10):
            chunk = ids[i:i + 10]
            d = self._get("/ajax/moreComingList",
                          {"token": "", "ci": self.ci, "movieIds": ",".join(map(str, chunk))})
            out += d.get("coming") or []
        return out

    def now_showing(self):
        d = self._get("/ajax/movieOnInfoList", {"token": "", "ci": self.ci})
        first = d.get("movieList") or []
        if not first and not d.get("movieIds"):
            raise SourceError("猫眼热映列表为空")
        have = {m["id"] for m in first}
        rest = [i for i in d.get("movieIds") or [] if i not in have]
        return first + self._more(rest)

    def coming(self):
        d = self._get("/ajax/comingList", {"ci": self.ci, "token": "", "limit": 10})
        first = d.get("coming") or []
        have = {m["id"] for m in first}
        rest = [i for i in d.get("movieIds") or [] if i not in have]
        return first + self._more(rest)

    def detail(self, mid):
        nap(0.8, 1.6)
        d = self._get("/ajax/detailmovie", {"movieId": mid})
        m = d.get("detailMovie")
        if not m:
            raise SourceError(f"猫眼详情为空 {mid}")
        return m

    def cinema(self, cid):
        d = self._get("/ajax/cinemaDetail", {"cinemaId": cid})
        sd = d.get("showData")
        if not isinstance(sd, dict) or "movies" not in sd:
            raise SourceError(f"影院 {cid} 排片数据为空")
        info = d.get("cinemaData") or {}
        movies = {}
        for m in sd.get("movies") or []:
            days = []
            for sh in m.get("shows") or []:
                plist = [{"tm": p.get("tm"), "th": p.get("th"), "tp": p.get("tp"),
                          "lang": p.get("lang"), "pr": p.get("sellPr") or p.get("vipPrice") or "",
                          "seq": p.get("seqNo") or ""} for p in sh.get("plist") or []]
                if plist:
                    days.append({"date": sh.get("showDate"), "shows": plist})
            if days:
                movies[str(m["id"])] = {
                    "nm": m.get("nm"), "img": m.get("img"), "desc": m.get("desc"),
                    "sc": _f(m.get("sc")), "wish": m.get("wish"), "days": days,
                }
        return {"name": info.get("nm") or sd.get("cinemaName"), "addr": info.get("addr"),
                "lat": info.get("lat"), "lng": info.get("lng"), "movies": movies}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- 淘票票
class Taopiaopiao:
    APPKEY = "12574478"
    BASE = "https://acs.m.taopiaopiao.com/h5/"

    def __init__(self, citycode):
        self.city = str(citycode)
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": "https://m.taopiaopiao.com/"})

    def call(self, api, v, data):
        d = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        last = None
        for _ in range(3):
            nap(0.2, 0.6)
            tk = (self.s.cookies.get("_m_h5_tk") or "").split("_")[0]
            t = str(int(time.time() * 1000))
            sign = hashlib.md5(f"{tk}&{t}&{self.APPKEY}&{d}".encode()).hexdigest()
            params = dict(jsv="2.7.2", appKey=self.APPKEY, t=t, sign=sign, api=api, v=v,
                          type="originaljson", dataType="json", timeout="10000", data=d)
            for attempt in range(2):
                try:
                    j = self.s.get(self.BASE + api.lower() + f"/{v}/", timeout=15, params=params).json()
                    break
                except (requests.RequestException, ValueError) as e:
                    if attempt:
                        raise SourceError(f"淘票票连接失败：{e.__class__.__name__}")
                    nap(2, 4)
            ret = " ".join(j.get("ret") or [])
            last = ret
            if "TOKEN" in ret:  # 第一次拿 token，或 token 过期，重签一次
                continue
            if not ret.startswith("SUCCESS"):
                raise SourceError(f"淘票票接口报错：{ret[:60]}")
            return (j.get("data") or {}).get("returnValue")
        raise SourceError(f"淘票票签名失败：{(last or '')[:60]}")

    def _base(self):
        return {"citycode": self.city, "cityCode": self.city, "platform": "8"}

    def now_showing(self):
        return self.call("mtop.film.MtopShowAPI.getShowsByCityCode", "4.0",
                         {**self._base(), "pageIndex": 1, "pageSize": 100}) or []

    def coming(self):
        return self.call("mtop.film.MtopShowAPI.getAllCitySoonShows", "1.0",
                         {**self._base(), "pageIndex": 1, "pageSize": 100}) or []

    def search(self, keyword):
        r = self.call("mtop.film.MtopShowAPI.getShowByKeyword", "5.0",
                      {**self._base(), "keyword": keyword})
        return r if isinstance(r, dict) and r.get("id") else None

    @staticmethod
    def brief(x):
        sf = x.get("scoreAndFavor") or {}
        poster = x.get("poster") or ""
        if poster and not poster.startswith("http"):
            poster = "https://gw.alicdn.com/" + poster
        return {
            "id": x.get("id"), "name": x.get("showName"), "nameEn": x.get("showNameEn"),
            "sc": _f(x.get("remark")), "scCount": x.get("remarkCount") or 0,
            "wish": x.get("wantCount") or sf.get("favorCount") or 0,
            "openTime": x.get("openTime"), "poster": poster,
            "url": f"https://m.taopiaopiao.com/main/moviemain/pages/show-detail/index.html?showid={x.get('id')}",
        }


# ---------------------------------------------------------------- IMDb
class IMDb:
    RATINGS = "https://datasets.imdbws.com/title.ratings.tsv.gz"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0 (movie-radar personal project)"})

    def find(self, title, years):
        """用英文片名 + 年份在 IMDb 搜索建议里找最匹配的一部，找不到返回 None。"""
        q = title.strip()
        if not q:
            return None
        nap(0.2, 0.5)
        try:
            r = self.s.get(f"https://v3.sg.media-imdb.com/suggestion/x/{quote(q.lower())}.json", timeout=15)
            items = r.json().get("d") or []
        except (requests.RequestException, ValueError) as e:
            raise SourceError(f"IMDb 搜索失败：{e.__class__.__name__}")
        target = norm_name(q)
        best, best_score = None, -99
        for x in items:
            if not str(x.get("id", "")).startswith("tt"):
                continue
            if x.get("qid") not in ("movie", "tvMovie", "video", "tvSpecial", None):
                continue
            score = 0
            n = norm_name(x.get("l"))
            if n == target:
                score += 10
            elif n.startswith(target) or target.startswith(n):
                score += 3
            y = x.get("y")
            if y and years:
                diff = min(abs(int(y) - yy) for yy in years)
                score += {0: 5, 1: 4}.get(diff, 1 if diff <= 3 else -6)
            if score > best_score:
                best, best_score = x, score
        if best and best_score >= 13:
            return best["id"]
        return None

    def last_modified(self):
        r = self.s.head(self.RATINGS, timeout=20)
        return r.headers.get("last-modified")

    def ratings(self, ids):
        ids = set(ids)
        out = {}
        if not ids:
            return out
        r = self.s.get(self.RATINGS, timeout=120)
        r.raise_for_status()
        with gzip.open(io.BytesIO(r.content), "rt", encoding="utf-8") as f:
            next(f, None)
            for line in f:
                tid, _, rest = line.partition("\t")
                if tid in ids:
                    rating, votes = rest.strip().split("\t")
                    out[tid] = [float(rating), int(votes)]
        return out


# ---------------------------------------------------------------- Wikidata（IMDb 编号 → 烂番茄 / 豆瓣 编号）
class Wikidata:
    URL = "https://query.wikidata.org/sparql"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "movie-radar/1.0 (personal, non-commercial)",
                               "Accept": "application/sparql-results+json"})

    def lookup(self, imdb_ids):
        out = {}
        ids = [i for i in imdb_ids if re.fullmatch(r"tt\d+", i or "")]
        for i in range(0, len(ids), 120):
            chunk = ids[i:i + 120]
            values = " ".join(f'"{x}"' for x in chunk)
            q = ("SELECT ?imdb ?rt ?db WHERE { VALUES ?imdb { %s } ?f wdt:P345 ?imdb . "
                 "OPTIONAL { ?f wdt:P1258 ?rt } OPTIONAL { ?f wdt:P4529 ?db } }") % values
            try:
                r = self.s.get(self.URL, params={"query": q}, timeout=40)
                rows = r.json()["results"]["bindings"]
            except (requests.RequestException, ValueError, KeyError) as e:
                raise SourceError(f"Wikidata 查询失败：{e.__class__.__name__}")
            for row in rows:
                d = out.setdefault(row["imdb"]["value"], {})
                if "rt" in row and row["rt"]["value"].startswith("m/"):
                    d.setdefault("rt", row["rt"]["value"])
                if "db" in row:
                    d.setdefault("douban", row["db"]["value"])
        return out


# ---------------------------------------------------------------- 豆瓣
class Douban:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": "https://m.douban.com/movie/"})

    def _json(self, url, params=None):
        nap(0.8, 1.6)
        try:
            r = self.s.get(url, params=params, timeout=15)
        except requests.RequestException as e:
            raise SourceError(f"豆瓣连接失败：{e.__class__.__name__}")
        if r.status_code in (403, 418, 429):
            raise SourceError(f"豆瓣返回 HTTP {r.status_code}（被限流）")
        try:
            return r.json()
        except ValueError:
            raise SourceError("豆瓣返回的不是数据（可能要求登录或验证）")

    def find(self, name, years):
        """按中文片名 + 年份找豆瓣条目，片名要完全一致，年份差不超过 1 年。"""
        items = self._json("https://movie.douban.com/j/subject_suggest", {"q": name})
        target = norm_name(name)
        best = None
        for x in items if isinstance(items, list) else []:
            if x.get("type") != "movie" or norm_name(x.get("title")) != target:
                continue
            y = int(x["year"]) if str(x.get("year", "")).isdigit() else None
            if years and y and min(abs(y - yy) for yy in years) > 1:
                continue
            if best is None or (y and years and min(abs(y - yy) for yy in years) == 0):
                best = x
        return best["id"] if best else None

    def rating(self, did):
        d = self._json(f"https://m.douban.com/rexxar/api/v2/movie/{did}", {"ck": "", "for_mobile": 1})
        if "title" not in d:
            raise SourceError(f"豆瓣条目 {did} 无数据")
        rt = d.get("rating") or {}
        return {"sc": float(rt.get("value") or 0), "count": int(rt.get("count") or 0),
                "reason": d.get("null_rating_reason") or "",
                "url": f"https://m.douban.com/movie/subject/{did}/"}


# ---------------------------------------------------------------- 烂番茄
class RottenTomatoes:
    BASE = "https://www.rottentomatoes.com"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
                                             "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"})

    def _html(self, url, params=None):
        nap(0.6, 1.2)
        try:
            r = self.s.get(url, params=params, timeout=20)
        except requests.RequestException as e:
            raise SourceError(f"烂番茄连接失败：{e.__class__.__name__}")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise SourceError(f"烂番茄返回 HTTP {r.status_code}")
        return r.text

    def find(self, title, years):
        html = self._html(self.BASE + "/search", {"search": title})
        if not html:
            return None
        target = norm_name(title)
        for attrs, body in re.findall(r"<search-page-media-row([^>]*)>(.*?)</search-page-media-row>", html, re.S):
            a = dict(re.findall(r'([a-z-]+)="([^"]*)"', attrs))
            url = re.search(r'href="https://www\.rottentomatoes\.com/(m/[^"/?]+)', body)
            name = re.search(r'data-qa="info-name"[^>]*>\s*([^<]+?)\s*<', body)
            if not url or not name or norm_name(name.group(1)) != target:
                continue
            y = a.get("release-year")
            if years and y and y.isdigit() and min(abs(int(y) - yy) for yy in years) > 1:
                continue
            return url.group(1)
        return None

    def scores(self, slug):
        html = self._html(f"{self.BASE}/{slug}")
        if html is None:
            return None
        m = re.search(r'<script[^>]*id="media-scorecard-json"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            raise SourceError("烂番茄页面结构变了")
        d = json.loads(m.group(1))

        def pct(x):
            v = (x or {}).get("scorePercent") or ""
            return int(v.rstrip("%")) if v.rstrip("%").isdigit() else None
        cs, aus = d.get("criticsScore") or {}, d.get("audienceScore") or {}
        return {"critics": pct(cs), "criticsCount": cs.get("reviewCount") or cs.get("ratingCount"),
                "audience": pct(aus), "url": f"{self.BASE}/{slug}"}
