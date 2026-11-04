"""电影雷达：每次运行抓取一轮数据，写到 docs/data/，发现变化就推送。

- 开票提醒：只看还没上映的片子，在关注影院第一次出现排片时提醒一次。
- 评分：豆瓣、IMDb、烂番茄（专业 + 观众）。
- 想看人数：猫眼、淘票票。

用法：
  python scraper/run.py              正常运行（按间隔决定抓哪些）
  python scraper/run.py --force      忽略间隔，全部重抓
  python scraper/run.py --dry-run    不发推送，只打印
  python scraper/run.py --test-push  只发一条测试推送
"""
import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources import (IMDb, Douban, Maoyan, RottenTomatoes, SourceError,  # noqa: E402
                     Taopiaopiao, Wikidata, norm_name)
import notify  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data"
STATE_FILE = ROOT / "state" / "state.json"

NOW = datetime.now(TZ)
NOW_S = NOW.isoformat(timespec="seconds")
TODAY = NOW.date().isoformat()


def ts(s):
    return datetime.fromisoformat(s) if s else None


def ago(s):
    t = ts(s)
    return (NOW - t) if t else timedelta(days=9999)


def load_json(p, default):
    try:
        return json.loads(Path(p).read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def save_json(p, obj, pretty=False):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False, indent=1 if pretty else None,
                     separators=None if pretty else (",", ":"))
    p.write_text(txt + "\n", "utf-8")


def img(url, w=300):
    """猫眼海报加缩略参数，省流量。"""
    if not url:
        return url
    url = url.replace("http://", "https://")
    if "pipi.cn" in url and "imageMogr2" in url:
        return url.split("?")[0] + f"?imageMogr2/thumbnail/{w}x/quality/85"
    return url.replace("/w.h/", f"/{w}.{int(w * 1.4)}/")


def md(d):
    try:
        x = datetime.fromisoformat(d)
        return f"{x.month}月{x.day}日"
    except (TypeError, ValueError):
        return d or ""


def site_url(cfg):
    if cfg.get("site_url"):
        return cfg["site_url"]
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return None


def load_watch():
    """网页上点的星标存在仓库根目录的 watch.json 里。"""
    w = load_json(ROOT / "watch.json", {}) or {}
    stars = {}
    for x in w.get("stars") or []:
        if isinstance(x, dict) and x.get("id"):
            stars[str(x["id"])] = x.get("nm") or ""
        elif isinstance(x, (int, str)) and str(x).isdigit():
            stars[str(x)] = ""
    mode = w.get("ticketing") if w.get("ticketing") in ("all", "starred") else "all"
    return stars, mode


class Radar:
    def __init__(self, cfg, args):
        self.cfg = cfg
        self.args = args
        self.stars, self.ticket_mode = load_watch()
        self.state = load_json(STATE_FILE, {})
        st = self.state
        for k, v in {"version": 2, "bootstrapped": False, "last": {}, "movies": {}, "detail_at": {},
                     "tpp_kw": {}, "imdb_map": {}, "imdb_ratings": {}, "wiki_at": {},
                     "douban": {}, "rt": {}, "opened": {}, "cinemas_ok": {},
                     "history": {}, "events": [], "scored": {}, "known": {}}.items():
            st.setdefault(k, v)
        self.status = load_json(OUT / "status.json", {}).get("sources", {})
        self.events = []
        self.my = Maoyan(cfg["city"]["maoyan_ci"])
        self.tpp = Taopiaopiao(cfg["city"]["tpp_citycode"])
        self.imdb = IMDb()
        self.iv = {"lists": 25, "tpp": 25, "imdb": 180, **cfg.get("intervals_minutes", {})}

    # ------------------------------------------------------------ helpers
    def due(self, name):
        if self.args.force:
            return True
        return ago(self.state["last"].get(name)) >= timedelta(minutes=self.iv[name] - 2)

    def mark(self, src, ok, msg=""):
        prev = self.status.get(src, {})
        self.status[src] = {"ok": ok, "msg": msg, "at": NOW_S,
                            "last_ok": NOW_S if ok else prev.get("last_ok")}
        print(f"[{'OK' if ok else '失败'}] {src} {msg}")

    def watched(self, mid, nm):
        if str(mid) in self.stars:
            return True
        for w in self.cfg.get("watch") or []:
            w = str(w).strip()
            if not w:
                continue
            if w == str(mid) or norm_name(w) == norm_name(nm) or (len(w) >= 2 and w in (nm or "")):
                return True
        return False

    def opened_anywhere(self, mid):
        return any(mid in o for o in self.state["opened"].values())

    def unreleased(self, mid):
        """还没上映：上映日期是今天及以后；没有具体日期的看是否在猫眼待映列表里。"""
        r = self.state["movies"].get(mid) or {}
        rt = r.get("rt") or ""
        if len(rt) == 10:
            return rt >= TODAY
        return r.get("status") == "coming"

    def important(self, mid, r):
        """评分优先刷新、开分要提醒的片子：已在关注影院开票的、关注列表里的。"""
        return self.opened_anywhere(mid) or self.watched(mid, r.get("nm"))

    def event(self, kind, title, body, mid=None, cid=None):
        e = {"at": NOW_S, "type": kind, "title": title, "body": body}
        if mid:
            e["mid"] = mid
        if cid:
            e["cid"] = cid
        self.events.append(e)

    @staticmethod
    def en_name(r):
        return (r.get("enm") or r.get("enm_tpp") or "").strip()

    @staticmethod
    def years_of(r):
        if r.get("years"):
            return r["years"]
        return [int(r["rt"][:4])] if (r.get("rt") or "")[:4].isdigit() else []

    def live(self):
        return [(mid, r) for mid, r in self.state["movies"].items() if r.get("status") in ("now", "coming")]

    def priority(self, item):
        mid, r = item
        return (not self.important(mid, r), r.get("status") != "coming",
                (r.get("rt") or "9999") if r.get("status") == "coming" else "",
                r.get("rank", 999))

    # ------------------------------------------------------------ 猫眼列表
    def update_lists(self):
        mv = self.state["movies"]
        try:
            now = self.my.now_showing()
            coming = self.my.coming()
        except SourceError as e:
            self.mark("maoyan", False, str(e))
            return
        seen = set()
        known = self.state["known"]
        new_coming = []
        for status, lst in (("coming", coming), ("now", now)):  # now 覆盖 coming
            for rank, m in enumerate(lst):
                mid = str(m["id"])
                seen.add(mid)
                rec = mv.setdefault(mid, {"id": m["id"]})
                rec.update({
                    "nm": m.get("nm"), "img": m.get("img") or rec.get("img"),
                    "rt": m.get("rt") or rec.get("rt"), "star": m.get("star") or rec.get("star"),
                    "status": status, "ver": m.get("version") or m.get("ver") or rec.get("ver"),
                    "listed_at": NOW_S, "rank": rank, "wish_my": m.get("wish") or 0,
                })
                if mid not in known:
                    known[mid] = NOW_S
                    if status == "coming":
                        new_coming.append(rec)
        for mid, rec in mv.items():
            if mid not in seen and rec.get("status") in ("now", "coming"):
                rec["status"] = "gone"
        for mid in [k for k, r in mv.items() if r.get("status") == "gone"
                    and ago(r.get("listed_at")) > timedelta(days=14)]:
            mv.pop(mid, None)
        self.state["last"]["lists"] = NOW_S
        self.mark("maoyan", True, f"热映 {len(now)} 部 / 待映 {len(coming)} 部")
        if self.state["bootstrapped"] and self.cfg["notify"].get("new_coming_movies"):
            for rec in new_coming[:5]:
                self.event("coming", "🆕 新片定档", f"《{rec['nm']}》{rec.get('rt') or ''} 上映", mid=str(rec["id"]))

    # ------------------------------------------------------------ 猫眼详情（英文名、年份、简介等）
    def update_details(self, limit=6):
        """猫眼详情接口连续调用 10 次左右就会被限流，所以每轮只补几部，慢慢补全。"""
        at = self.state["detail_at"]
        cands = [x for x in self.live()
                 if ago(at.get(x[0])) > timedelta(hours=24 if x[1].get("status") == "coming" else 72)]
        cands.sort(key=self.priority)
        if self.args.force:
            limit = max(limit, 9)
        done = 0
        for mid, r in cands[:limit]:
            try:
                d = self.my.detail(mid)
            except SourceError as e:
                print(f"  详情失败 {mid}: {e}")
                break
            years = sorted({int(s[:4]) for s in (d.get("frt") or "").split(",") if s[:4].isdigit()}
                           | ({int(d["rt"][:4])} if (d.get("rt") or "")[:4].isdigit() else set()))
            r.update({
                "enm": d.get("enm"), "cat": d.get("cat"), "dir": d.get("dir"), "star": d.get("star") or r.get("star"),
                "dur": d.get("dur"), "src": d.get("src"), "dra": d.get("dra"), "pubDesc": d.get("pubDesc"),
                "ver": d.get("ver") or r.get("ver"), "years": years,
                "video": d.get("videourl") or None, "img": d.get("img") or r.get("img"),
                "wish_my": d.get("wish") or r.get("wish_my") or 0,
            })
            at[mid] = NOW_S
            done += 1
        if done:
            print(f"  猫眼详情 {done} 部（还有 {max(0, len(cands) - done)} 部待补）")

    # ------------------------------------------------------------ 影院：只盯未上映影片的首次开票
    def update_cinemas(self):
        opened = self.state["opened"]
        for c in self.cfg["cinemas"]:
            cid = str(c["id"])
            try:
                data = self.my.cinema(c["id"])
            except SourceError as e:
                self.mark(f"cinema:{cid}", False, str(e))
                continue
            reg = opened.setdefault(cid, {})
            first_time = not self.state["cinemas_ok"].get(cid)
            new = []
            for mid, m in data["movies"].items():
                if mid in reg:
                    continue
                # 列表里没有的片子先补一条最基本的记录
                self.state["movies"].setdefault(mid, {"id": int(mid), "nm": m["nm"], "img": m["img"],
                                                      "status": "now", "listed_at": NOW_S,
                                                      "wish_my": m.get("wish") or 0})
                if not self.unreleased(mid):
                    continue  # 已上映的片子不管
                dates = [d["date"] for d in m["days"]]
                reg[mid] = {"at": "" if first_time else NOW_S, "first": dates[0] if dates else "",
                            "count": sum(len(d["shows"]) for d in m["days"])}
                if not first_time:
                    new.append((mid, m, reg[mid]))
            # 上映超过 7 天的从登记表里清掉
            for mid in list(reg):
                rt = (self.state["movies"].get(mid) or {}).get("rt") or ""
                if len(rt) == 10 and rt < (NOW - timedelta(days=7)).date().isoformat():
                    reg.pop(mid)
            self.state["cinemas_ok"][cid] = {"at": NOW_S, "name": data["name"] or c["name"], "addr": data["addr"]}
            if new and self.cfg["notify"].get("new_ticketing", True):
                for mid, m, o in new:
                    if self.ticket_mode == "starred" and not self.watched(mid, m["nm"]):
                        continue
                    rt = (self.state["movies"].get(mid) or {}).get("rt")
                    body = f"《{m['nm']}》首场 {md(o['first'])}"
                    if rt and len(rt) == 10 and o["first"] != rt:
                        body += f"，{md(rt)}正式上映"
                    if self.watched(mid, m["nm"]):
                        body = "⭐ " + body
                    self.event("ticketing", f"🎟 {c['short']} 开票了", body, mid=mid, cid=cid)
            n_pre = sum(1 for mid in data["movies"] if mid in reg)
            self.mark(f"cinema:{cid}", True, f"未上映已开票 {n_pre} 部" + (f"，新开票 {len(new)} 部" if new else ""))

    # ------------------------------------------------------------ 淘票票：只取想看人数
    def update_tpp(self):
        try:
            lst = self.tpp.now_showing() + self.tpp.coming()
        except SourceError as e:
            self.mark("tpp", False, str(e))
            return
        idx = {}
        for x in lst:
            b = Taopiaopiao.brief(x)
            idx.setdefault(norm_name(b["name"]), b)
        matched, missing = 0, []
        for mid, r in self.live():
            b = idx.get(norm_name(r.get("nm")))
            if b:
                r["tpp"] = {"wish": b["wish"], "url": b["url"], "at": NOW_S}
                if b.get("nameEn"):
                    r["enm_tpp"] = b["nameEn"]
                matched += 1
            else:
                missing.append((mid, r))
        kw = self.state["tpp_kw"]
        missing = [x for x in missing if self.important(*x) or x[1].get("status") == "coming"]
        missing.sort(key=lambda x: (not self.important(*x), kw.get(x[0], "")))
        searched = 0
        for mid, r in missing[:6]:
            kw[mid] = NOW_S
            try:
                hit = self.tpp.search(r.get("nm"))
            except SourceError:
                break
            searched += 1
            if hit and norm_name(hit.get("showName")) == norm_name(r.get("nm")):
                b = Taopiaopiao.brief(hit)
                r["tpp"] = {"wish": b["wish"], "url": b["url"], "at": NOW_S}
                matched += 1
        self.state["last"]["tpp"] = NOW_S
        self.mark("tpp", True, f"配上 {matched} 部" + (f"（另搜索 {searched} 部）" if searched else ""))

    # ------------------------------------------------------------ IMDb
    def update_imdb(self):
        mp = self.state["imdb_map"]
        overrides = self.cfg.get("imdb_overrides") or {}
        looked = 0
        try:
            for mid, r in sorted(self.live(), key=self.priority):
                ov = overrides.get(mid) or overrides.get(r.get("nm") or "")
                if ov:
                    mp[mid] = {"id": ov, "at": NOW_S}
                    continue
                if not self.en_name(r):
                    continue
                cur = mp.get(mid)
                if cur and (cur.get("id") or ago(cur.get("at")) < timedelta(days=3)):
                    continue
                if looked >= 30:
                    continue
                tid = None
                for name in [x.strip() for x in self.en_name(r).split(",") if x.strip()][:3]:
                    tid = self.imdb.find(name, self.years_of(r))
                    if tid:
                        break
                mp[mid] = {"id": tid, "at": NOW_S}
                looked += 1
            ids = {v["id"] for v in mp.values() if v.get("id")}
            lm = self.imdb.last_modified()
            if lm != self.state["last"].get("imdb_lastmod") or any(i not in self.state["imdb_ratings"] for i in ids):
                self.state["imdb_ratings"] = self.imdb.ratings(ids)
                self.state["last"]["imdb_lastmod"] = lm
        except Exception as e:  # noqa: BLE001
            self.mark("imdb", False, f"{e.__class__.__name__}: {str(e)[:60]}")
            return
        self.state["last"]["imdb"] = NOW_S
        self.mark("imdb", True, f"已匹配 {len(ids)} 部")

    # ------------------------------------------------------------ Wikidata：用 IMDb 编号找豆瓣、烂番茄条目
    def update_wikidata(self):
        mp, wat = self.state["imdb_map"], self.state["wiki_at"]
        todo = {}
        for mid, v in mp.items():
            tid = v.get("id")
            if not tid or ago(wat.get(mid)) < timedelta(days=3):
                continue
            if (self.state["douban"].get(mid) or {}).get("id") and (self.state["rt"].get(mid) or {}).get("id"):
                continue
            todo[tid] = mid
        if not todo:
            return
        batch = list(todo.items())[:120]
        try:
            res = Wikidata().lookup([t for t, _ in batch])
        except SourceError as e:
            print(f"  {e}")
            return
        for tid, mid in batch:
            wat[mid] = NOW_S
            hit = res.get(tid) or {}
            if hit.get("douban") and not (self.state["douban"].get(mid) or {}).get("id"):
                self.state["douban"][mid] = {"id": hit["douban"], "at": ""}
            if hit.get("rt") and not (self.state["rt"].get(mid) or {}).get("id"):
                self.state["rt"][mid] = {"id": hit["rt"], "at": ""}

    # ------------------------------------------------------------ 豆瓣
    def update_douban(self, find_cap=8, fetch_cap=8):
        db = Douban()
        store = self.state["douban"]
        items = sorted(self.live(), key=self.priority)
        found = fetched = 0
        try:
            for mid, r in items:
                if found >= find_cap:
                    break
                cur = store.get(mid)
                if cur and (cur.get("id") or ago(cur.get("tried")) < timedelta(days=2)):
                    continue
                if not r.get("nm"):
                    continue
                did = db.find(r["nm"], self.years_of(r))
                store[mid] = {"id": did, "tried": NOW_S, "at": ""}
                found += 1

            def ttl(mid, r):
                return timedelta(hours=2 if self.important(mid, r) else 8)
            due = [(mid, r) for mid, r in items if (store.get(mid) or {}).get("id")
                   and ago(store[mid].get("at")) > ttl(mid, r)]
            due.sort(key=lambda x: (not self.important(*x), store[x[0]].get("at") or ""))
            for mid, r in due[:fetch_cap]:
                store[mid].update(db.rating(store[mid]["id"]))
                store[mid]["at"] = NOW_S
                fetched += 1
        except SourceError as e:
            self.mark("douban", False, str(e))
            return
        have = sum(1 for v in store.values() if v.get("sc"))
        self.mark("douban", True, f"有分 {have} 部" + (f"，本轮更新 {fetched} 部" if fetched else ""))

    # ------------------------------------------------------------ 烂番茄
    def update_rt(self, find_cap=5, fetch_cap=6):
        rtc = RottenTomatoes()
        store = self.state["rt"]
        items = sorted(self.live(), key=self.priority)
        found = fetched = 0
        try:
            for mid, r in items:
                if found >= find_cap:
                    break
                cur = store.get(mid)
                if cur and (cur.get("id") or ago(cur.get("tried")) < timedelta(days=3)):
                    continue
                enm = self.en_name(r).split(",")[0].strip()
                # 烂番茄基本只收录外语片；没有英文名、或纯国产片就不搜了
                if not enm or (r.get("src") or "").startswith(("中国大陆", "中国香港", "中国台湾")):
                    continue
                slug = rtc.find(enm, self.years_of(r))
                store[mid] = {"id": slug, "tried": NOW_S, "at": ""}
                found += 1

            def ttl(mid, r):
                return timedelta(hours=4 if self.important(mid, r) else 12)
            due = [(mid, r) for mid, r in items if (store.get(mid) or {}).get("id")
                   and ago(store[mid].get("at")) > ttl(mid, r)]
            due.sort(key=lambda x: (not self.important(*x), store[x[0]].get("at") or ""))
            for mid, r in due[:fetch_cap]:
                sc = rtc.scores(store[mid]["id"])
                if sc is None:
                    store[mid] = {"id": None, "tried": NOW_S, "at": ""}
                    continue
                store[mid].update(sc)
                store[mid]["at"] = NOW_S
                fetched += 1
        except SourceError as e:
            self.mark("rt", False, str(e))
            return
        have = sum(1 for v in store.values() if v.get("critics") is not None or v.get("audience") is not None)
        self.mark("rt", True, f"有分 {have} 部" + (f"，本轮更新 {fetched} 部" if fetched else ""))

    # ------------------------------------------------------------ 开分提醒
    def ratings_of(self, mid):
        """None = 这个来源还没配上这部片（或还没取过分）；False = 配上了但没分；True = 有分。"""
        st = self.state
        imdb_id = (st["imdb_map"].get(mid) or {}).get("id")
        db = st["douban"].get(mid) or {}
        rt = st["rt"].get(mid) or {}
        return {
            "douban": (db.get("sc", 0) > 0) if db.get("id") and db.get("at") else None,
            "imdb": (imdb_id in st["imdb_ratings"]) if imdb_id else None,
            "rt": (rt.get("critics") is not None) if rt.get("id") and rt.get("at") else None,
        }

    def check_scores(self):
        mode = self.cfg["notify"].get("score_open", "watch")
        scored = self.state["scored"]
        st = self.state
        for mid, r in self.live():
            cur = self.ratings_of(mid)
            prev = scored.get(mid)
            merged = {}
            for k, v in cur.items():
                pv = (prev or {}).get(k)
                merged[k] = True if (v is True or pv is True) else (v if v is not None else pv)
            scored[mid] = merged
            if prev is None or mode == "off" or not st["bootstrapped"]:
                continue
            relevant = (mode == "all" or self.watched(mid, r.get("nm"))
                        or (mode == "opened" and self.opened_anywhere(mid)))
            if not relevant:
                continue
            parts = []
            if cur["douban"] and prev.get("douban") is False:
                d = st["douban"][mid]
                parts.append(f"豆瓣 {d['sc']:.1f}（{d['count']} 人评）")
            if cur["imdb"] and prev.get("imdb") is False:
                parts.append(f"IMDb {st['imdb_ratings'][st['imdb_map'][mid]['id']][0]:.1f}")
            if cur["rt"] and prev.get("rt") is False:
                parts.append(f"烂番茄 {st['rt'][mid]['critics']}%")
            if parts:
                self.event("score", "⭐ 开分了", f"《{r.get('nm')}》" + " / ".join(parts), mid=mid)

    # ------------------------------------------------------------ 输出
    def write_outputs(self):
        st = self.state
        hist = st["history"]
        day = hist.setdefault(TODAY, {})
        prev_days = sorted(d for d in hist if d < TODAY)
        yday = hist.get(prev_days[-1], {}) if prev_days else {}
        cinema_ids = [str(c["id"]) for c in self.cfg["cinemas"]]
        movies = []
        for mid, r in self.live():
            tp = r.get("tpp") or {}
            wm, wt = r.get("wish_my") or 0, tp.get("wish") or 0
            day[mid] = [wm, wt]
            y = yday.get(mid)
            imdb_id = (st["imdb_map"].get(mid) or {}).get("id")
            ir = st["imdb_ratings"].get(imdb_id) if imdb_id else None
            db = st["douban"].get(mid) or {}
            rt = st["rt"].get(mid) or {}
            opened = {cid: st["opened"][cid][mid] for cid in cinema_ids if mid in st["opened"].get(cid, {})}
            movies.append({
                "id": r["id"], "nm": r.get("nm"), "enm": self.en_name(r).split(",")[0] or None,
                "img": img(r.get("img")), "rt": r.get("rt"), "status": r.get("status"),
                "unreleased": self.unreleased(mid), "ver": r.get("ver"), "cat": r.get("cat"),
                "dir": r.get("dir"), "star": r.get("star"), "dur": r.get("dur"), "src": r.get("src"),
                "dra": r.get("dra"), "pubDesc": r.get("pubDesc"), "video": r.get("video"),
                "rank": r.get("rank", 999),
                "wish": {"maoyan": wm, "tpp": wt if tp else None},
                "delta": {"maoyan": (wm - y[0]) if y else None,
                          "tpp": (wt - y[1]) if (y and tp and y[1]) else None},
                "douban": ({"sc": db.get("sc", 0), "count": db.get("count", 0), "reason": db.get("reason", ""),
                            "url": db.get("url") or f"https://m.douban.com/movie/subject/{db['id']}/"}
                           if db.get("id") else None),
                "imdb": ({"sc": ir[0] if ir else None, "votes": ir[1] if ir else None,
                          "url": f"https://m.imdb.com/title/{imdb_id}/"} if imdb_id else None),
                "tomato": ({"critics": rt.get("critics"), "audience": rt.get("audience"), "count": rt.get("criticsCount"),
                        "url": rt.get("url") or f"https://www.rottentomatoes.com/{rt['id']}"}
                       if rt.get("id") else None),
                "links": {"maoyan": f"https://m.maoyan.com/movie/{r['id']}", "tpp": tp.get("url")},
                "opened": opened,
                "watch": self.watched(mid, r.get("nm")),
            })
        movies.sort(key=lambda m: (m["status"] != "now", m["rank"] if m["status"] == "now" else 0,
                                   m.get("rt") or "9999", -(m["wish"]["maoyan"] or 0)))
        for d in sorted(hist)[:-30]:
            hist.pop(d, None)
        live = {str(m["id"]) for m in movies}
        history = {}
        for d in sorted(hist):
            for mid, v in hist[d].items():
                if mid in live:
                    history.setdefault(mid, []).append([d, v[0], v[1]])
        history = {k: v for k, v in history.items() if len(v) > 1}

        cinemas = []
        for c in self.cfg["cinemas"]:
            cs = st["cinemas_ok"].get(str(c["id"]), {})
            cinemas.append({"id": c["id"], "short": c["short"], "name": cs.get("name") or c["name"],
                            "addr": cs.get("addr"), "at": cs.get("at")})

        st["events"] = (self.events + st["events"])[:150]
        save_json(OUT / "movies.json", {"updated": NOW_S, "city": self.cfg["city"]["name"],
                                        "cinemas": cinemas, "movies": movies,
                                        "stars": list(self.stars), "ticketing": self.ticket_mode})
        save_json(OUT / "history.json", history)
        save_json(OUT / "events.json", st["events"][:100])
        save_json(OUT / "status.json", {"updated": NOW_S, "sources": self.status}, pretty=True)
        (OUT / "schedule.json").unlink(missing_ok=True)
        save_json(STATE_FILE, st)

    # ------------------------------------------------------------ 推送
    def send(self):
        if not self.events:
            return
        url = site_url(self.cfg)
        if self.args.dry_run:
            for e in self.events:
                print(f"[dry-run] {e['title']} | {e['body']}")
            return
        groups, mids = {}, {}
        for e in self.events:
            groups.setdefault(e["title"], []).append(e["body"])
            mids.setdefault(e["title"], []).append(e.get("mid"))
        for i, (title, bodies) in enumerate(groups.items()):
            if i >= 6:
                notify.push("🎬 电影雷达", f"还有 {len(groups) - 6} 条动态，打开网页查看", url)
                break
            body = "\n".join(bodies[:8]) + (f"\n…等 {len(bodies)} 条" if len(bodies) > 8 else "")
            level = "timeSensitive" if "开票" in title else None
            link = url
            if url and len(bodies) == 1 and mids[title][0]:
                link = f"{url}#m{mids[title][0]}"
            notify.push(title, body, link, level)

    # ------------------------------------------------------------ main
    def run(self):
        first = not self.state["bootstrapped"]
        steps = [("lists", self.update_lists, True), ("details", self.update_details, False),
                 ("cinemas", self.update_cinemas, False), ("tpp", self.update_tpp, True),
                 ("imdb", self.update_imdb, True), ("wikidata", self.update_wikidata, False),
                 ("douban", self.update_douban, False), ("rt", self.update_rt, False)]
        for name, fn, gated in steps:
            if gated and not self.due(name):
                continue
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.mark({"lists": "maoyan"}.get(name, name), False, f"程序出错：{e.__class__.__name__}")
        self.check_scores()
        if first and any(k.startswith("cinema:") and v.get("ok") for k, v in self.status.items()):
            self.state["bootstrapped"] = True
            n = sum(len(o) for o in self.state["opened"].values())
            self.events = [{"at": NOW_S, "type": "system", "title": "✅ 电影雷达已启动",
                            "body": f"正在盯 {len(self.cfg['cinemas'])} 家影院（未上映已开票 {n} 部），"
                                    f"之后有新片开票会第一时间提醒你"}]
        self.write_outputs()
        self.send()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-push", action="store_true")
    args = ap.parse_args()
    cfg = load_json(ROOT / "config.json", None)
    if not cfg:
        sys.exit("找不到 config.json")
    if args.test_push:
        ok = notify.push("🎬 电影雷达测试", "推送通了！以后开票会这样提醒你。", site_url(cfg))
        sys.exit(0 if ok else 1)
    Radar(cfg, args).run()


if __name__ == "__main__":
    main()
