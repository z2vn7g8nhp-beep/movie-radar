"""电影雷达：每次运行抓取一轮数据，写到 docs/data/，发现开票等变化就推送。

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
from sources import IMDb, Maoyan, SourceError, Taopiaopiao, norm_name  # noqa: E402
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


def site_url(cfg):
    if cfg.get("site_url"):
        return cfg["site_url"]
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return None


class Radar:
    def __init__(self, cfg, args):
        self.cfg = cfg
        self.args = args
        self.state = load_json(STATE_FILE, {})
        st = self.state
        for k, v in {"version": 1, "bootstrapped": False, "last": {}, "movies": {}, "detail_at": {},
                     "tpp_kw": {}, "imdb_map": {}, "imdb_ratings": {}, "cinemas": {},
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
        for w in self.cfg.get("watch") or []:
            w = str(w).strip()
            if not w:
                continue
            if w == str(mid) or norm_name(w) == norm_name(nm) or (len(w) >= 2 and w in (nm or "")):
                return True
        return False

    def at_my_cinemas(self, mid):
        return any(mid in (c.get("movies") or {}) for c in self.state["cinemas"].values())

    def event(self, kind, title, body, mid=None, cid=None):
        e = {"at": NOW_S, "type": kind, "title": title, "body": body}
        if mid:
            e["mid"] = mid
        if cid:
            e["cid"] = cid
        self.events.append(e)

    # ------------------------------------------------------------ 猫眼列表 + 详情
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
                    "status": status, "showst": m.get("showst"),
                    "ver": m.get("version") or m.get("ver") or rec.get("ver"),
                    "listed_at": NOW_S, "rank": rank, "showInfo": m.get("showInfo") or "",
                })
                rec.setdefault("maoyan", {})
                rec["maoyan"].update({"sc": float(m.get("sc") or 0), "wish": m.get("wish") or 0})
                if mid not in known:
                    known[mid] = NOW_S
                    if status == "coming":
                        new_coming.append(rec)
        for mid, rec in mv.items():
            if mid not in seen and rec.get("status") in ("now", "coming"):
                rec["status"] = "gone"
        # 太久不出现的片子从状态里清掉
        for mid in [k for k, r in mv.items() if r.get("status") == "gone"
                    and ago(r.get("listed_at")) > timedelta(days=14) and not self.at_my_cinemas(k)]:
            mv.pop(mid, None)
        self.state["last"]["lists"] = NOW_S
        self.mark("maoyan", True, f"热映 {len(now)} 部 / 待映 {len(coming)} 部")

        if self.state["bootstrapped"] and self.cfg["notify"].get("new_coming_movies"):
            for rec in new_coming[:5]:
                self.event("coming", "🆕 新片定档", f"《{rec['nm']}》{rec.get('rt') or ''} 上映", mid=str(rec["id"]))

    def update_details(self, limit=6):
        """猫眼详情接口连续调用 10 次左右就会被限流，所以每轮只补几部，慢慢补全。"""
        mv = self.state["movies"]
        at = self.state["detail_at"]

        def need(mid, r):
            age = ago(at.get(mid))
            ttl = timedelta(hours=12 if r.get("status") == "now" else 48)
            return r.get("status") != "gone" and age > ttl

        cands = [(mid, r) for mid, r in mv.items() if need(mid, r)]
        cands.sort(key=lambda x: (not self.at_my_cinemas(x[0]), x[1].get("status") != "now",
                                  -(x[1].get("maoyan", {}).get("wish") or 0)))
        done = 0
        if self.args.force:
            limit = max(limit, 9)
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
                "ver": d.get("ver") or r.get("ver"), "years": years, "oriLang": d.get("oriLang"),
                "video": d.get("videourl") or None, "img": d.get("img") or r.get("img"),
            })
            r.setdefault("maoyan", {}).update({
                "sc": float(d.get("sc") or 0), "wish": d.get("wish") or r["maoyan"].get("wish") or 0,
                "snum": d.get("snum") or 0, "watched": d.get("watched") or 0,
                "label": d.get("scoreLabel") or "",
            })
            at[mid] = NOW_S
            done += 1
        if done:
            print(f"  更新详情 {done} 部（剩余 {max(0, len(cands) - done)} 部下次再取）")

    # ------------------------------------------------------------ 影院排片（开票检测）
    def update_cinemas(self):
        notify_cfg = self.cfg["notify"]
        for c in self.cfg["cinemas"]:
            cid = str(c["id"])
            try:
                data = self.my.cinema(c["id"])
            except SourceError as e:
                self.mark(f"cinema:{cid}", False, str(e))
                continue
            old = self.state["cinemas"].get(cid, {})
            old_movies = old.get("movies", {})
            new_movies = {}
            opened = []
            for mid, m in data["movies"].items():
                dates = [d["date"] for d in m["days"]]
                prev = old_movies.get(mid)
                new_movies[mid] = {
                    "nm": m["nm"], "img": m["img"], "desc": m["desc"], "sc": m["sc"], "wish": m["wish"],
                    "dates": dates, "count": sum(len(d["shows"]) for d in m["days"]),
                    "days": m["days"], "seen": NOW_S,
                    # 首次运行时已经在排片的片子不算"新开票"，first 留空
                    "first": prev.get("first", "") if prev else (NOW_S if old.get("ok_once") else ""),
                }
                # 列表里没有的片子（特殊场次、重映）也补进电影库
                rec = self.state["movies"].setdefault(mid, {"id": int(mid), "nm": m["nm"], "img": m["img"],
                                                             "status": "now", "listed_at": NOW_S,
                                                             "maoyan": {"sc": m["sc"], "wish": m["wish"] or 0}})
                if rec.get("status") == "gone":
                    rec["status"] = "now"
                    rec["listed_at"] = NOW_S
                if not prev and old.get("ok_once"):
                    opened.append(new_movies[mid])
                    new_movies[mid]["mid"] = mid
                elif prev and self.watched(mid, m["nm"]) and notify_cfg.get("new_dates_for_watched"):
                    added = sorted(set(dates) - set(prev.get("dates") or []))
                    if added:
                        self.event("dates", f"📅 {c['short']} 加场",
                                   f"《{m['nm']}》新增 {'、'.join(md(d) for d in added)} 的场次", mid=mid, cid=cid)
            # 暂时没出现的片子保留 3 天，防止接口偶尔漏数据导致重复提醒
            for mid, pm in old_movies.items():
                if mid not in new_movies and ago(pm.get("seen")) < timedelta(days=3):
                    new_movies[mid] = {**pm, "days": [], "dates": [], "count": 0, "gone": True}
            if opened and notify_cfg.get("new_ticketing", True):
                for m in opened:
                    first_day = md(m["dates"][0]) if m["dates"] else ""
                    body = f"《{m['nm']}》{first_day}起 · 已排 {m['count']} 场"
                    if self.watched(m["mid"], m["nm"]):
                        body = "⭐ " + body
                    self.event("ticketing", f"🎟 {c['short']} 开票了", body, mid=m["mid"], cid=cid)
            self.state["cinemas"][cid] = {
                "name": data["name"] or c["name"], "addr": data["addr"], "lat": data["lat"], "lng": data["lng"],
                "movies": new_movies, "at": NOW_S, "ok_once": True,
            }
            n_live = sum(1 for m in new_movies.values() if not m.get("gone"))
            self.mark(f"cinema:{cid}", True, f"{n_live} 部在排片" + (f"，新开票 {len(opened)} 部" if opened else ""))

    # ------------------------------------------------------------ 淘票票
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
        matched = 0
        missing = []
        for mid, r in self.state["movies"].items():
            if r.get("status") == "gone":
                continue
            b = idx.get(norm_name(r.get("nm")))
            if b:
                r["tpp"] = {**b, "at": NOW_S}
                matched += 1
            else:
                missing.append((mid, r))
        # 列表里没配上的，对重要的片子用关键词搜一下（每次最多 6 部，轮换）
        kw = self.state["tpp_kw"]
        missing = [x for x in missing if self.at_my_cinemas(x[0]) or self.watched(x[0], x[1].get("nm"))
                   or x[1].get("status") == "now"]
        missing.sort(key=lambda x: kw.get(x[0], ""))
        searched = 0
        for mid, r in missing[:6]:
            kw[mid] = NOW_S
            try:
                hit = self.tpp.search(r.get("nm"))
            except SourceError:
                break
            searched += 1
            if hit and norm_name(hit.get("showName")) == norm_name(r.get("nm")):
                r["tpp"] = {**Taopiaopiao.brief(hit), "at": NOW_S}
                matched += 1
        self.state["last"]["tpp"] = NOW_S
        self.mark("tpp", True, f"配上 {matched} 部" + (f"（另搜索 {searched} 部）" if searched else ""))

    # ------------------------------------------------------------ IMDb
    def update_imdb(self):
        mv = self.state["movies"]
        mp = self.state["imdb_map"]
        overrides = self.cfg.get("imdb_overrides") or {}
        looked = 0
        try:
            for mid, r in mv.items():
                ov = overrides.get(mid) or overrides.get(r.get("nm") or "")
                if ov:
                    mp[mid] = {"id": ov, "at": NOW_S}
                    continue
                if r.get("status") == "gone" or not r.get("enm"):
                    continue
                cur = mp.get(mid)
                if cur and (cur.get("id") or ago(cur.get("at")) < timedelta(days=3)):
                    continue
                if looked >= 30:
                    continue
                tid = None
                for name in [x.strip() for x in r["enm"].split(",") if x.strip()][:3]:
                    tid = self.imdb.find(name, r.get("years") or [])
                    if tid:
                        break
                mp[mid] = {"id": tid, "at": NOW_S}
                looked += 1
            ids = {v["id"] for v in mp.values() if v.get("id")}
            lm = self.imdb.last_modified()
            have = self.state["imdb_ratings"]
            if lm != self.state["last"].get("imdb_lastmod") or any(i not in have for i in ids):
                rt = self.imdb.ratings(ids)
                self.state["imdb_ratings"] = rt
                self.state["last"]["imdb_lastmod"] = lm
        except Exception as e:  # noqa: BLE001
            self.mark("imdb", False, f"{e.__class__.__name__}: {str(e)[:60]}")
            return
        self.state["last"]["imdb"] = NOW_S
        self.mark("imdb", True, f"已匹配 {len(ids)} 部（评分数据 {lm or '未知'}）")

    # ------------------------------------------------------------ 开分提醒
    def check_scores(self):
        mode = self.cfg["notify"].get("score_open", "my_cinemas")
        scored = self.state["scored"]
        ratings = self.state["imdb_ratings"]
        for mid, r in self.state["movies"].items():
            if r.get("status") == "gone":
                continue
            imdb_id = (self.state["imdb_map"].get(mid) or {}).get("id")
            # None = 这个来源还没配上这部片；False = 配上了但还没分；True = 有分
            cur = {
                "my": (r.get("maoyan") or {}).get("sc", 0) > 0,
                "tpp": ((r.get("tpp") or {}).get("sc", 0) > 0) if r.get("tpp") else None,
                "imdb": (imdb_id in ratings) if imdb_id else None,
            }
            prev = scored.get(mid)
            merged = {}
            for k, v in cur.items():
                pv = (prev or {}).get(k)
                merged[k] = True if (v is True or pv is True) else (v if v is not None else pv)
            scored[mid] = merged
            if prev is None or mode == "off" or not self.state["bootstrapped"]:
                continue
            relevant = (mode == "all" or self.watched(mid, r.get("nm"))
                        or (mode == "my_cinemas" and self.at_my_cinemas(mid)))
            if not relevant:
                continue
            parts = []
            if cur["my"] and prev.get("my") is False:
                parts.append(f"猫眼 {r['maoyan']['sc']:.1f}")
            if cur["tpp"] and prev.get("tpp") is False:
                parts.append(f"淘票票 {r['tpp']['sc']:.1f}")
            if cur["imdb"] and prev.get("imdb") is False:
                parts.append(f"IMDb {ratings[imdb_id][0]:.1f}")
            if parts:
                self.event("score", "⭐ 开分了", f"《{r.get('nm')}》" + " / ".join(parts), mid=mid)

    # ------------------------------------------------------------ 输出
    def write_outputs(self):
        st = self.state
        hist = st["history"]
        day = hist.setdefault(TODAY, {})
        prev_days = sorted(d for d in hist if d < TODAY)
        yday = hist.get(prev_days[-1], {}) if prev_days else {}
        ratings = st["imdb_ratings"]
        cinema_ids = [str(c["id"]) for c in self.cfg["cinemas"]]
        movies = []
        for mid, r in st["movies"].items():
            here = {cid: st["cinemas"].get(cid, {}).get("movies", {}).get(mid) for cid in cinema_ids}
            here = {k: v for k, v in here.items() if v and not v.get("gone")}
            if r.get("status") == "gone" and not here:
                continue
            my = r.get("maoyan") or {}
            tp = r.get("tpp") or {}
            day[mid] = [my.get("wish") or 0, tp.get("wish") or 0]
            y = yday.get(mid)
            imdb_id = (st["imdb_map"].get(mid) or {}).get("id")
            rt = ratings.get(imdb_id) if imdb_id else None
            movies.append({
                "id": r["id"], "nm": r.get("nm"), "enm": r.get("enm"), "img": img(r.get("img")),
                "rt": r.get("rt"), "status": "now" if r.get("status") == "gone" else r.get("status"),
                "showst": r.get("showst"), "ver": r.get("ver"), "cat": r.get("cat"), "dir": r.get("dir"),
                "star": r.get("star"), "dur": r.get("dur"), "src": r.get("src"), "dra": r.get("dra"),
                "pubDesc": r.get("pubDesc"), "video": r.get("video"),
                "rank": r.get("rank", 999), "showInfo": r.get("showInfo", ""),
                "maoyan": {"sc": my.get("sc", 0), "wish": my.get("wish", 0), "snum": my.get("snum", 0),
                           "watched": my.get("watched", 0), "label": my.get("label", ""),
                           "url": f"https://m.maoyan.com/movie/{r['id']}"},
                "tpp": ({"sc": tp.get("sc", 0), "wish": tp.get("wish", 0), "scCount": tp.get("scCount", 0),
                         "url": tp.get("url")} if tp else None),
                "imdb": ({"id": imdb_id, "sc": rt[0] if rt else None, "votes": rt[1] if rt else None,
                          "url": f"https://m.imdb.com/title/{imdb_id}/"} if imdb_id else None),
                "delta": {"maoyan": (my.get("wish", 0) - y[0]) if y else None,
                          "tpp": (tp.get("wish", 0) - y[1]) if (y and tp and y[1]) else None},
                "cinemas": {cid: {"dates": v["dates"], "count": v["count"], "first": v["first"]}
                            for cid, v in here.items()},
                "watch": self.watched(mid, r.get("nm")),
            })
        movies.sort(key=lambda m: (m["status"] != "now", m["rank"] if m["status"] == "now" else 0,
                                   m.get("rt") or "9999", -(m["maoyan"]["wish"] or 0)))
        # 历史只留 30 天
        for d in sorted(hist)[:-30]:
            hist.pop(d, None)
        history = {}
        for d in sorted(hist):
            for mid, v in hist[d].items():
                history.setdefault(mid, []).append([d, v[0], v[1]])
        live = {str(m["id"]) for m in movies}
        history = {k: v for k, v in history.items() if k in live and len(v) > 1}

        cinemas = []
        schedule = {}
        for c in self.cfg["cinemas"]:
            cid = str(c["id"])
            cs = st["cinemas"].get(cid, {})
            cinemas.append({"id": c["id"], "short": c["short"], "name": cs.get("name") or c["name"],
                            "addr": cs.get("addr"), "lat": cs.get("lat"), "lng": cs.get("lng"),
                            "at": cs.get("at")})
            schedule[cid] = {mid: v["days"] for mid, v in (cs.get("movies") or {}).items() if v.get("days")}

        st["events"] = (self.events + st["events"])[:150]
        save_json(OUT / "movies.json", {"updated": NOW_S, "city": self.cfg["city"]["name"],
                                        "cinemas": cinemas, "movies": movies})
        save_json(OUT / "schedule.json", {"updated": NOW_S, "cinemas": schedule})
        save_json(OUT / "history.json", history)
        save_json(OUT / "events.json", st["events"][:100])
        save_json(OUT / "status.json", {"updated": NOW_S, "sources": self.status}, pretty=True)
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
                link = f"{url}#m={mids[title][0]}"
            notify.push(title, body, link, level)

    # ------------------------------------------------------------ main
    def run(self):
        first = not self.state["bootstrapped"]
        steps = [("lists", self.update_lists), ("cinemas", self.update_cinemas), ("details", self.update_details),
                 ("tpp", self.update_tpp), ("imdb", self.update_imdb)]
        for name, fn in steps:
            if name not in ("cinemas", "details") and not self.due(name):
                continue
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.mark(name if name != "lists" else "maoyan", False, f"程序出错：{e.__class__.__name__}")
        self.check_scores()
        if first:
            ok = [k for k, v in self.status.items() if v.get("ok")]
            if any(k.startswith("cinema:") for k in ok):
                self.state["bootstrapped"] = True
                n = sum(len(c.get("movies", {})) for c in self.state["cinemas"].values())
                self.events = [{"at": NOW_S, "type": "system", "title": "✅ 电影雷达已启动",
                                "body": f"正在盯 {len(self.cfg['cinemas'])} 家影院（{n} 部在排片），之后有新开票会第一时间提醒你"}]
        self.write_outputs()
        self.send()


def md(d):
    try:
        x = datetime.fromisoformat(d)
        return f"{x.month}月{x.day}日"
    except (TypeError, ValueError):
        return d or ""


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
