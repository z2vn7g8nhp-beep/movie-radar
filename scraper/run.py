"""电影雷达：每次运行抓取一轮数据，写到 docs/data/，发现变化就推送。

- 开票提醒：只看还没上映的片子，在关注影院第一次出现排片时提醒一次。
- 评分：豆瓣、IMDb、烂番茄（专业 + 观众）。
- 想看人数：猫眼、淘票票。

用法：
  python scraper/run.py              正常运行（按间隔决定抓哪些）
  python scraper/run.py --force      忽略间隔，全部重抓
  python scraper/run.py --dry-run    不发推送，只打印
  python scraper/run.py --test-push  只发一条测试推送
  python scraper/run.py --no-sprint  星标电影快上映时也不多盯几分钟
"""
import argparse
import base64
import json
import os
import random
import re
import secrets
import sys
import time
import traceback
import requests
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
START = NOW


def tick():
    """冲刺盯票时一轮跑好几分钟，每次检查前把"现在"更新一下。"""
    global NOW, NOW_S, TODAY
    NOW = datetime.now(TZ)
    NOW_S = NOW.isoformat(timespec="seconds")
    TODAY = NOW.date().isoformat()


# 关注的系列：片名（中文）或英文名里出现这些词就算。网页也用这一份（写进 movies.json）。
FOLLOW_PRESETS = {
    "漫威": {"zh": ["漫威", "复仇者联盟", "蜘蛛侠", "钢铁侠", "美国队长", "雷神", "黑豹", "银河护卫队", "奇异博士", "蚁人",
                   "死侍", "X战警", "神奇四侠", "雷霆特工队", "雷霆特攻队", "毒液", "刀锋战士", "永恒族", "尚气", "黑寡妇",
                   "惊奇队长", "惊奇少女", "金刚狼", "绿巨人", "夜魔侠"],
             "en": ["Marvel", "Avengers", "Spider-Man", "Iron Man", "Captain America", "Thor", "Black Panther",
                    "Guardians of the Galaxy", "Doctor Strange", "Ant-Man", "Deadpool", "X-Men", "Fantastic Four",
                    "Thunderbolts", "Venom", "Blade", "Eternals", "Shang-Chi", "Black Widow", "The Marvels",
                    "Captain Marvel", "Wolverine", "Hulk", "Daredevil"]},
    "DC": {"zh": ["超人", "蝙蝠侠", "神奇女侠", "海王", "闪电侠", "正义联盟", "自杀小队", "沙赞", "蓝甲虫", "绿灯侠",
                  "女超人", "黑亚当", "泥脸"],
           "en": ["Superman", "Batman", "Wonder Woman", "Aquaman", "The Flash", "Justice League", "Joker",
                  "Suicide Squad", "Shazam", "Blue Beetle", "Green Lantern", "Supergirl", "Black Adam", "Clayface",
                  "Peacemaker"]},
}


def _plain(s):
    return re.sub(r"[\s·•・.\-－—_]", "", str(s or "")).lower()


def follow_hits(rules, r):
    """这部片命中了哪些关注（系列或影人、关键词）。r 里用到 nm / enm / dir / star。"""
    hits = []
    nm, enm = r.get("nm") or "", (r.get("enm") or r.get("enm_tpp") or "")
    people = [_plain(x) for x in re.split(r"[,，/]", f"{r.get('dir') or ''},{r.get('star') or ''}") if x.strip()]
    for rule in rules or []:
        rule = str(rule).strip()
        if not rule:
            continue
        p = FOLLOW_PRESETS.get(rule)
        if p:
            if any(w in nm for w in p["zh"]) or any(re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", enm, re.I) for w in p["en"]):
                hits.append(rule)
            continue
        k = _plain(rule)
        if len(k) >= 2 and (k in _plain(nm) or k in _plain(enm) or any(k in x for x in people)):
            hits.append(rule)
    return hits


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


def buy_url(ci, cid, mid):
    """猫眼这家影院的排片页，并选好这部电影：点开就能挑场次、选座。"""
    return f"https://m.maoyan.com/mtrade/cinema/cinema?cinemaId={cid}&ci={ci}&movieId={mid}"


def site_url(cfg):
    if cfg.get("site_url"):
        return cfg["site_url"]
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return None


def load_watch():
    """星标和开票提醒模式存在私有仓库的 watch.json（环境变量 DATA_REPO / DATA_TOKEN）。
    没配私有仓库时退回读本仓库根目录的 watch.json。返回 (星标, 模式, 是否读取成功)。"""
    repo, token = os.environ.get("DATA_REPO", "").strip(), os.environ.get("DATA_TOKEN", "").strip()
    w, ok = None, True
    if repo and token:
        try:
            r = requests.get(f"https://api.github.com/repos/{repo}/contents/watch.json", timeout=20,
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw"})
            if r.status_code == 200:
                w = r.json()
            elif r.status_code == 404:
                w = {}
            else:
                ok = False
                print(f"[失败] 读取私有仓库星标 HTTP {r.status_code}")
        except (requests.RequestException, ValueError) as e:
            ok = False
            print(f"[失败] 读取私有仓库星标：{e.__class__.__name__}")
    if w is None:
        w = load_json(ROOT / "watch.json", {}) or {}
    stars = {}
    for x in w.get("stars") or []:
        if isinstance(x, dict) and x.get("id"):
            stars[str(x["id"])] = x.get("nm") or ""
        elif isinstance(x, (int, str)) and str(x).isdigit():
            stars[str(x)] = ""
    mode = w.get("ticketing") if w.get("ticketing") in ("all", "starred") else "all"
    follow = [str(x).strip() for x in (w.get("follow") or []) if str(x).strip()]
    mute = {str(x) for x in (w.get("mute") or [])}
    plans = [p for p in (w.get("plans") or []) if isinstance(p, dict) and p.get("id") and p.get("date")]
    return {"stars": stars, "mode": mode, "ok": ok, "follow": follow, "mute": mute, "plans": plans}


def data_repo(method, path, body=None, raw=False):
    """读写私有数据仓库里的文件；没配 DATA_REPO / DATA_TOKEN 时返回 None。"""
    repo, token = os.environ.get("DATA_REPO", "").strip(), os.environ.get("DATA_TOKEN", "").strip()
    if not (repo and token):
        return None
    return requests.request(method, f"https://api.github.com/repos/{repo}/contents/{path}", json=body, timeout=20,
                            headers={"Authorization": f"Bearer {token}",
                                     "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json"})


def calendar_secret():
    """日历订阅和主屏小组件用的私密链接。秘钥存在私有仓库的 calendar.json，第一次自动生成。
    文件只放进网页（不进公开的 radar-data 分支），只有知道链接的人能打开。"""
    try:
        r = data_repo("GET", "calendar.json", raw=True)
        if r is None:
            return None
        if r.status_code == 200:
            s = (r.json() or {}).get("secret") or ""
            return s if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", s) else None
        if r.status_code == 404:
            s = secrets.token_hex(12)
            body = json.dumps({"secret": s, "note": "电影雷达日历订阅和小组件的私密链接，删掉这个文件会换一个新链接"}, ensure_ascii=False)
            w = data_repo("PUT", "calendar.json", {"message": "生成日历私密链接",
                                                   "content": base64.b64encode(body.encode()).decode()})
            if w is not None and w.status_code in (200, 201):
                return s
            print(f"[失败] 写入 calendar.json HTTP {w.status_code if w is not None else '-'}")
            return None
        print(f"[失败] 读取 calendar.json HTTP {r.status_code}")
    except (requests.RequestException, ValueError) as e:
        print(f"[失败] 读取 calendar.json：{e.__class__.__name__}")
    return None


def ics_text(s):
    return str(s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line):
    """iCalendar 每行不超过 75 字节，超出的折行，续行以空格开头。"""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode())
            cur = b""
        cur += b
    out.append(cur.decode())
    return "\r\n ".join(out)


class Radar:
    def __init__(self, cfg, args):
        self.cfg = cfg
        self.args = args
        w = load_watch()
        self.stars, self.ticket_mode, self.watch_ok = w["stars"], w["mode"], w["ok"]
        self.follow, self.mute, self.plans = w["follow"], w["mute"], w["plans"]
        self.state = load_json(STATE_FILE, {})
        st = self.state
        for k, v in {"version": 2, "bootstrapped": False, "last": {}, "movies": {}, "detail_at": {},
                     "tpp_kw": {}, "imdb_map": {}, "imdb_ratings": {}, "wiki_at": {},
                     "douban": {}, "rt": {}, "opened": {}, "cinemas_ok": {},
                     "history": {}, "events": [], "scored": {}, "known": {}, "fails": {},
                     "plan_done": {}, "push_pending": []}.items():
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
        """星标的，或命中"关注的系列 / 影人"的（手动取消过的除外）。"""
        mid = str(mid)
        if mid in self.stars:
            return True
        if mid in self.mute:
            return False
        for w in self.cfg.get("watch") or []:
            w = str(w).strip()
            if not w:
                continue
            if w == mid or norm_name(w) == norm_name(nm) or (len(w) >= 2 and w in (nm or "")):
                return True
        if self.follow:
            r = self.state["movies"].get(mid) or {"nm": nm}
            return bool(follow_hits(self.follow, r))
        return False

    def opened_anywhere(self, mid):
        return any(mid in o for o in self.state["opened"].values())

    def unreleased(self, mid):
        """还没上映：上映日期是今天及以后；没有具体日期的看是否在猫眼待映列表里。
        猫眼列表里还没有、只在影院排片里出现的片子（status=cinema），排片全在明天以后才算预售。"""
        r = self.state["movies"].get(mid) or {}
        rt = r.get("rt") or ""
        if len(rt) == 10:
            return rt >= TODAY
        if r.get("status") == "cinema":
            return (r.get("cin_first") or "") > TODAY
        return r.get("status") == "coming"

    def important(self, mid, r):
        """评分优先刷新、开分要提醒的片子：已在关注影院开票的、关注列表里的。"""
        return self.opened_anywhere(mid) or self.watched(mid, r.get("nm"))

    def event(self, kind, title, body, mid=None, cid=None, push=True, star=False, buy=None):
        """所有动态都记进网页的"动态"列表；push=True 的才推送到手机。"""
        e = {"at": NOW_S, "type": kind, "title": title, "body": body, "push": push, "_star": star}
        if mid:
            e["mid"] = mid
        if cid:
            e["cid"] = cid
        if buy:
            e["buy"] = buy
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
        return [(mid, r) for mid, r in self.state["movies"].items()
                if r.get("status") in ("now", "coming") or (r.get("status") == "cinema" and self.unreleased(mid))]

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
        for mid in [k for k, r in mv.items() if r.get("status") in ("gone", "cinema")
                    and ago(r.get("listed_at")) > timedelta(days=14)]:
            mv.pop(mid, None)
        self.state["last"]["lists"] = NOW_S
        self.mark("maoyan", True, f"热映 {len(now)} 部 / 待映 {len(coming)} 部")
        if self.state["bootstrapped"]:
            for rec in new_coming[:10]:
                mid = str(rec["id"])
                star = self.watched(mid, rec.get("nm"))
                # 关注的系列 / 影人有新片定档，不管"新片定档"开关都推送
                self.event("coming", "🆕 新片定档", f"《{rec['nm']}》{rec.get('rt') or ''} 上映", mid=mid,
                           push=bool(self.cfg["notify"].get("new_coming_movies")) or star, star=star)

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
                dates = sorted(d["date"] for d in m["days"] if d.get("date"))
                rec = self.state["movies"].get(mid)
                if rec is None:
                    # 猫眼列表里还没有的片子（新片刚开预售时常这样）：先记一条，排片全在明天以后就当预售处理。
                    # 以前这里直接记成"已上映"，会漏掉这种开票提醒。
                    rec = self.state["movies"][mid] = {"id": int(mid), "nm": m["nm"], "img": m["img"],
                                                       "status": "cinema", "listed_at": NOW_S,
                                                       "cin_first": dates[0] if dates else "",
                                                       "wish_my": m.get("wish") or 0}
                elif rec.get("status") == "cinema":
                    rec["listed_at"] = NOW_S
                    if dates and (not rec.get("cin_first") or dates[0] < rec["cin_first"]):
                        rec["cin_first"] = dates[0]
                if mid in reg:
                    if self.unreleased(mid):
                        reg[mid]["shows"] = self.compact_shows(m["days"])
                        reg[mid]["count"] = sum(len(d["shows"]) for d in m["days"])
                    continue
                if not self.unreleased(mid):
                    continue  # 已上映的片子不管
                reg[mid] = {"at": "" if first_time else NOW_S, "first": dates[0] if dates else "",
                            "count": sum(len(d["shows"]) for d in m["days"]),
                            "shows": self.compact_shows(m["days"])}
                if not first_time:
                    new.append((mid, m, reg[mid]))
            # 上映超过 7 天的从登记表里清掉；上映了的不再留场次
            week_ago = (NOW - timedelta(days=7)).date().isoformat()
            for mid in list(reg):
                r0 = self.state["movies"].get(mid) or {}
                rt = r0.get("rt") or ""
                if (len(rt) == 10 and rt < week_ago) or (not r0) or (len(rt) != 10 and (reg[mid].get("first") or "9") < week_ago):
                    reg.pop(mid)
                elif not self.unreleased(mid):
                    reg[mid].pop("shows", None)
            self.state["cinemas_ok"][cid] = {"at": NOW_S, "name": data["name"] or c["name"], "addr": data["addr"]}
            if new:
                for mid, m, o in new:
                    star = self.watched(mid, m["nm"])
                    rt = (self.state["movies"].get(mid) or {}).get("rt")
                    body = f"《{m['nm']}》首场 {md(o['first'])}"
                    if rt and len(rt) == 10 and o["first"] != rt:
                        body += f"，{md(rt)}正式上映"
                    self.event("ticketing", f"🎟 {c['short']} 开票了", body, mid=mid, cid=cid,
                               push=self.ticket_mode == "all" or star, star=star,
                               buy=buy_url(self.cfg["city"]["maoyan_ci"], cid, mid))
            # 只数还没上映的，和网页上"已开票"里显示的一致
            n_pre = sum(1 for mid in data["movies"] if mid in reg and self.unreleased(mid))
            self.mark(f"cinema:{cid}", True, f"未上映已开票 {n_pre} 部" + (f"，新开票 {len(new)} 部" if new else ""))

    @staticmethod
    def compact_shows(days, max_days=4, max_per_day=40):
        """场次表：最近几天，每场的时间、影厅、版本、语言、价格。"""
        out = []
        for d in sorted(days, key=lambda x: x.get("date") or "")[:max_days]:
            shows = []
            for p in d.get("shows")[:max_per_day]:
                s = {"t": p.get("tm") or "", "h": p.get("th") or "", "v": p.get("tp") or "", "l": p.get("lang") or ""}
                pr = str(p.get("pr") or "").strip()
                if re.fullmatch(r"\d+(\.\d+)?", pr):
                    s["p"] = pr.rstrip("0").rstrip(".") if "." in pr else pr
                shows.append(s)
            shows.sort(key=lambda s: s["t"])
            out.append({"d": d.get("date"), "s": shows})
        return out

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
        if not self.watch_ok:
            print("  星标数据没读到，这轮跳过开分检查")
            return
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
            if prev is None or not st["bootstrapped"]:
                continue
            star = self.watched(mid, r.get("nm"))
            push = mode != "off" and (mode == "all" or star or (mode == "opened" and self.opened_anywhere(mid)))
            parts = []
            if cur["douban"] and prev.get("douban") is False:
                d = st["douban"][mid]
                parts.append(f"豆瓣 {d['sc']:.1f}（{d['count']} 人评）")
            if cur["imdb"] and prev.get("imdb") is False:
                parts.append(f"IMDb {st['imdb_ratings'][st['imdb_map'][mid]['id']][0]:.1f}")
            if cur["rt"] and prev.get("rt") is False:
                parts.append(f"烂番茄 {st['rt'][mid]['critics']}%")
            if parts:
                self.event("score", "📊 开分了", f"《{r.get('nm')}》" + " / ".join(parts),
                           mid=mid, push=push, star=star)

    # ------------------------------------------------------------ 数据源健康
    def check_health(self, minutes=60):
        names = {"maoyan": "猫眼电影列表"}
        names.update({f"cinema:{c['id']}": f"{c['short']} 开票检查" for c in self.cfg["cinemas"]})
        if os.environ.get("DATA_REPO") and os.environ.get("DATA_TOKEN"):
            names["data"] = "读取星标（私有仓库）"
            self.mark("data", self.watch_ok, "" if self.watch_ok else "读取失败，检查 DATA_TOKEN 是否过期")
        fails = self.state["fails"]
        for src, name in names.items():
            stv = self.status.get(src)
            if not stv or stv.get("at") != NOW_S:
                continue  # 这轮没检查这一项
            f = fails.get(src)
            if not stv.get("ok"):
                if not f:
                    fails[src] = {"since": NOW_S, "alerted": False}
                elif not f["alerted"] and ago(f["since"]) >= timedelta(minutes=minutes):
                    self.event("health", "⚠️ 检查失败", f"{name}已经连续失败 {int(ago(f['since']).total_seconds() // 3600) or 1} 小时：{stv.get('msg')}。"
                               f"这段时间开票提醒可能会漏，会继续自动重试。", push=True)
                    f["alerted"] = True
            elif f:
                if f["alerted"]:
                    self.event("health", "✅ 已恢复", f"{name}恢复正常了。", push=True)
                fails.pop(src, None)

    # ------------------------------------------------------------ 输出
    def write_outputs(self):
        st = self.state
        hist = st["history"]
        day = hist.setdefault(TODAY, {})
        prev_days = sorted(d for d in hist if d < TODAY)
        yday = hist.get(prev_days[-1], {}) if prev_days else {}
        cinema_ids = [str(c["id"]) for c in self.cfg["cinemas"]]
        movies, details = [], {}
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
            pub = r.get("pubDesc") or ""
            tm = re.search(r"\d{4}-\d{2}-\d{2} (\d{1,2}:\d{2})", pub)
            if r.get("dra") or r.get("video"):
                details[mid] = {"dra": r.get("dra"), "video": r.get("video")}
            movies.append({
                "id": r["id"], "nm": r.get("nm"), "enm": self.en_name(r).split(",")[0] or None,
                "img": img(r.get("img")), "rt": r.get("rt"), "status": r.get("status"),
                "unreleased": self.unreleased(mid), "ver": r.get("ver"), "cat": r.get("cat"),
                "dir": r.get("dir"), "star": r.get("star"), "dur": r.get("dur"), "src": r.get("src"),
                "re": "重映" in pub, "rtm": tm.group(1) if tm else None, "video": bool(r.get("video")),
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

        public = [{k: v for k, v in e.items() if not k.startswith("_")} for e in self.events]
        st["events"] = (public + [{k: v for k, v in e.items() if k != "star"} for e in st["events"]])[:150]
        # 列表用的 movies.json 不带剧情简介和预告片地址（打开详情页时再读 details.json），首屏更快
        save_json(OUT / "movies.json", {"updated": NOW_S, "city": self.cfg["city"]["name"], "ci": self.cfg["city"]["maoyan_ci"],
                                        "cinemas": cinemas, "follow_presets": FOLLOW_PRESETS, "movies": movies})
        save_json(OUT / "details.json", details)
        save_json(OUT / "history.json", history)
        save_json(OUT / "events.json", st["events"][:100])
        save_json(OUT / "status.json", {"updated": NOW_S, "sources": self.status}, pretty=True)
        (OUT / "schedule.json").unlink(missing_ok=True)
        save_json(STATE_FILE, st)
        try:
            self.write_private(movies, cinemas)
        except Exception as e:  # noqa: BLE001  日历出错不影响其他
            traceback.print_exc()
            print(f"[失败] 生成日历：{e.__class__.__name__}")

    # ------------------------------------------------------------ 日历订阅 + 小组件数据（私密链接）
    def write_private(self, movies, cinemas):
        secret = calendar_secret()
        if not secret:
            return
        cal = ROOT / "docs" / "cal"
        cal.mkdir(parents=True, exist_ok=True)
        url = site_url(self.cfg) or ""
        if not self.watch_ok and url:
            # 这轮没读到星标：把网上现有的文件原样留着，免得日历被清空
            for ext in ("ics", "json"):
                try:
                    r = requests.get(f"{url}cal/{secret}.{ext}", timeout=20)
                    if r.status_code == 200:
                        (cal / f"{secret}.{ext}").write_bytes(r.content)
                except requests.RequestException:
                    pass
            return
        short = {str(c["id"]): c["short"] for c in cinemas}
        starred = [m for m in movies if str(m["id"]) in self.stars or self.watched(str(m["id"]), m.get("nm"))]
        stamp = NOW.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//movie-radar//CN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
                 "X-WR-CALNAME:电影雷达 · 星标", "X-WR-TIMEZONE:Asia/Shanghai", "X-WR-CALDESC:星标电影的上映日和提前场",
                 "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]

        def day(d, uid, summary, desc, link):
            d0 = datetime.fromisoformat(d)
            lines.extend(["BEGIN:VEVENT", f"UID:{uid}@movie-radar", f"DTSTAMP:{stamp}",
                          f"DTSTART;VALUE=DATE:{d0:%Y%m%d}", f"DTEND;VALUE=DATE:{d0 + timedelta(days=1):%Y%m%d}",
                          f"SUMMARY:{ics_text(summary)}", f"DESCRIPTION:{ics_text(desc)}", "TRANSP:TRANSPARENT"]
                         + ([f"URL:{link}"] if link else [])
                         + ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{ics_text('明天 ' + summary)}", "TRIGGER:-PT4H", "END:VALARM",
                            "END:VEVENT"])
        # 观影计划：带具体时间的日程
        plan_of = {}
        for p in self.plans:
            start = self.plan_start(p)
            if not start or start < NOW - timedelta(days=2):
                continue
            mv0 = next((m for m in movies if str(m["id"]) == str(p.get("mid"))), None)
            dur = int((mv0 or {}).get("dur") or p.get("dur") or 120)
            u0, u1 = start.astimezone(ZoneInfo("UTC")), (start + timedelta(minutes=dur + 15)).astimezone(ZoneInfo("UTC"))
            where = " ".join(x for x in (p.get("cinema"), p.get("seat")) if x)
            lines.extend(["BEGIN:VEVENT", f"UID:plan-{p['id']}@movie-radar", f"DTSTAMP:{stamp}",
                          f"DTSTART:{u0:%Y%m%dT%H%M%SZ}", f"DTEND:{u1:%Y%m%dT%H%M%SZ}",
                          f"SUMMARY:{ics_text('🍿 《' + (p.get('nm') or '') + '》')}",
                          f"LOCATION:{ics_text(where)}", f"DESCRIPTION:{ics_text('观影计划 · 电影雷达')}", "END:VEVENT"])
            plan_of.setdefault(str(p.get("mid")), f"{start:%-m.%d %H:%M}")
        items = []
        for m in starred:
            mid, rt, op = str(m["id"]), m.get("rt") or "", m.get("opened") or {}
            link = f"{url}#m{mid}" if url else m["links"]["maoyan"]
            status = "；".join(f"{short.get(c, c)}首场 {md(o.get('first'))}" for c, o in op.items()) or "你的影院都还没开票"
            if len(rt) == 10:
                day(rt, f"rel-{mid}", f"《{m['nm']}》上映", f"{status}\n{link}", link)
                for c, o in op.items():
                    if o.get("first") and o["first"] < rt:
                        day(o["first"], f"pre-{mid}-{c}", f"《{m['nm']}》{short.get(c, c)}提前场", f"正式上映 {md(rt)}\n{link}", link)
            if m.get("unreleased"):
                items.append({"id": m["id"], "nm": m["nm"], "img": m["img"], "rt": rt, "wish": m["wish"]["maoyan"],
                              "opened": [{"c": short.get(c, c), "first": o.get("first")} for c, o in op.items()],
                              "plan": plan_of.get(mid)})
        lines.append("END:VCALENDAR")
        (cal / f"{secret}.ics").write_text("\r\n".join(ics_fold(x) for x in lines) + "\r\n", "utf-8")
        items.sort(key=lambda x: (x["rt"] if len(x["rt"]) == 10 else (x["rt"] or "9999") + "-99"))
        save_json(cal / f"{secret}.json", {"updated": NOW_S, "site": url, "items": items})
        print(f"  日历：星标 {len(starred)} 部，待映 {len(items)} 部")

    # ------------------------------------------------------------ 观影计划：开场前一小时左右提醒
    @staticmethod
    def plan_start(p):
        try:
            t = (p.get("time") or "").strip() or "00:00"
            return datetime.fromisoformat(f"{p['date']}T{t}").replace(tzinfo=TZ)
        except (KeyError, ValueError):
            return None

    def check_plans(self, lead=70):
        """只推送，不写进公开的动态列表（计划是私密的）。"""
        done = self.state["plan_done"]
        for k in [k for k, v in done.items() if ago(v) > timedelta(days=3)]:
            done.pop(k, None)
        if self.args.dry_run:
            return
        url = site_url(self.cfg)
        for p in self.plans:
            start = self.plan_start(p)
            if not start or not (p.get("time") or "").strip() or str(p["id"]) in done:
                continue
            left = start - NOW
            if timedelta(0) < left <= timedelta(minutes=lead):
                mins = int(left.total_seconds() // 60)
                where = " · ".join(x for x in (p.get("cinema"), p.get("seat")) if x)
                body = f"《{p.get('nm') or ''}》{p.get('time')} 开场" + (f"\n{where}" if where else "")
                notify.push(f"🍿 还有 {mins} 分钟开场" if mins < 60 else "🍿 一小时后开场", body,
                            f"{url}#m{p['mid']}" if url and p.get("mid") else url, "timeSensitive")
                done[str(p["id"])] = NOW_S

    # ------------------------------------------------------------ 冲刺盯票
    def sprint_targets(self):
        """星标（含关注命中）、还没上映、离上映不到 N 天、还有影院没开票的片子。"""
        sp = self.cfg.get("sprint") or {}
        if sp.get("enabled") is False or not self.watch_ok:
            return []
        days = int(sp.get("days_before", 30))
        cids = [str(c["id"]) for c in self.cfg["cinemas"]]
        out = []
        for mid, r in self.live():
            rt = r.get("rt") or ""
            if len(rt) != 10 or not self.unreleased(mid) or not self.watched(mid, r.get("nm")):
                continue
            left = (datetime.fromisoformat(rt).date() - NOW.date()).days
            if 0 <= left <= days and any(mid not in self.state["opened"].get(c, {}) for c in cids):
                out.append(r.get("nm"))
        return out

    def sprint(self):
        """星标电影快上映、还没开票时：这一轮不马上结束，每隔 1 分钟左右再查一次两家影院，
        查到开票立刻推送。外部定时每 5 分钟叫一次，所以几乎不间断。只查影院排片，不碰别的接口。"""
        sp = self.cfg.get("sprint") or {}
        budget = int(sp.get("seconds", 225))
        gap = int(sp.get("interval_seconds", 70))
        targets = self.sprint_targets()
        if not targets or self.args.dry_run or self.args.no_sprint:
            return
        print(f"  冲刺盯票：{len(targets)} 部")
        rounds = fails = 0
        while True:
            left = budget - (datetime.now(TZ) - START).total_seconds()
            if left < gap + 15:
                break
            time.sleep(gap + random.uniform(-8, 8))
            tick()
            self.events = []
            self.update_cinemas()
            rounds += 1
            bad = [c for c in self.cfg["cinemas"] if not self.status.get(f"cinema:{c['id']}", {}).get("ok")]
            fails = fails + 1 if bad else 0
            self.check_plans()
            if self.events:
                self.write_outputs()
                self.send()
            if fails >= 2 or not self.sprint_targets():
                break   # 连着失败两次（可能被限流）就先停，下一轮再说
        self.mark("sprint", True, f"冲刺盯票 {rounds} 次")
        self.events = []
        self.write_outputs()

    # ------------------------------------------------------------ 推送
    def send(self):
        to_push = [e for e in self.events if e.get("push", True)
                   and not (e["type"] == "ticketing" and not self.cfg["notify"].get("new_ticketing", True))]
        if self.args.dry_run:
            for e in self.events:
                print(f"[dry-run {'推送' if e in to_push else '只记录'}] {e['title']} | {e['body']}")
            return
        if not to_push:
            return
        url = site_url(self.cfg)
        groups, evs = {}, {}
        for e in to_push:
            key = (e["title"], bool(e.get("_star")))
            groups.setdefault(key, []).append(("⭐ " if e.get("_star") else "") + e["body"])
            evs.setdefault(key, []).append(e)
        alert = self.cfg["notify"].get("starred_ticket_alert", "call")
        for i, ((title, star), bodies) in enumerate(sorted(groups.items(), key=lambda kv: not kv[0][1])):
            if i >= 6:
                self.push("🎬 电影雷达", f"还有 {len(groups) - 6} 条动态，打开网页查看", url)
                break
            body = "\n".join(bodies[:8]) + (f"\n…等 {len(bodies)} 条" if len(bodies) > 8 else "")
            ticket = "开票" in title
            level, call = ("timeSensitive" if ticket or "失败" in title else None), False
            if ticket and star and alert in ("call", "critical"):
                call = True
                if alert == "critical":
                    level = "critical"
            link, one = url, evs[(title, star)][0] if len(bodies) == 1 else None
            if one and one.get("buy"):
                link = one["buy"]  # 只有一部新开票：点推送直接进猫眼这家影院的排片页
            elif one and url and one.get("mid"):
                link = f"{url}#m{one['mid']}"
            self.push(title, body, link, level, call)

    def push(self, title, body, link=None, level=None, call=False):
        """发一条推送；没发出去的存起来，下一轮再补发（开票提醒不能就这么丢了）。"""
        ok = notify.push(title, body, link, level, call=call)
        if ok:
            self.mark("push", True, "推送正常")
        else:
            self.state["push_pending"].append({"at": NOW_S, "title": title, "body": body, "url": link,
                                               "level": level, "call": call})
            self.mark("push", False, "推送没发出去，下一轮会自动补发；一直这样请检查 BARK_KEY")
        return ok

    def retry_pushes(self):
        """补发上一轮没发出去的推送；超过 12 小时的就不补了，免得半夜收到过期消息。"""
        if self.args.dry_run:
            return
        pend, self.state["push_pending"] = self.state.get("push_pending") or [], []
        pend = [p for p in pend if ago(p["at"]) < timedelta(hours=12)]
        for p in pend[:6]:
            at = datetime.fromisoformat(p["at"]).astimezone(TZ)
            self.push(p["title"], f"{p['body']}\n（补发，原本在 {at:%H:%M} 推送）", p.get("url"), p.get("level"),
                      p.get("call", False))

    def backfill_missed(self):
        """一次性：以前 BARK_SERVER 为空导致推送全没发出去，把最近两天、首场还没过的开票提醒补发一条汇总。"""
        st = self.state
        if st.get("push_fix_backfill") or self.args.dry_run:
            return
        lines, star_any = [], False
        for e in st["events"]:
            if e.get("type") != "ticketing" or not e.get("push") or ago(e["at"]) > timedelta(hours=48):
                continue
            mid = str(e.get("mid") or "")
            if mid and not self.unreleased(mid):
                continue
            star = bool(mid) and self.watched(mid, (st["movies"].get(mid) or {}).get("nm"))
            star_any = star_any or star
            cin = e["title"].replace("🎟", "").replace("开票了", "").strip()
            lines.append(f"{'⭐ ' if star else ''}{cin}：{e['body']}")
        if lines:
            ok = self.push("📮 补发：之前漏推的开票", "推送之前坏了，这些开票没提醒到你，抱歉！\n" + "\n".join(lines[:8]),
                           site_url(self.cfg), "timeSensitive", star_any)
            if not ok:
                st["push_pending"].pop()  # 下一轮重新走补发汇总，不重复排队
                return
        st["push_fix_backfill"] = NOW_S

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
        self.check_health()
        if first and any(k.startswith("cinema:") and v.get("ok") for k, v in self.status.items()):
            self.state["bootstrapped"] = True
            n = sum(len(o) for o in self.state["opened"].values())
            self.events = [{"at": NOW_S, "type": "system", "title": "✅ 电影雷达已启动",
                            "body": f"正在盯 {len(self.cfg['cinemas'])} 家影院（未上映已开票 {n} 部），"
                                    f"之后有新片开票会第一时间提醒你"}]
        self.check_plans()
        self.retry_pushes()
        self.backfill_missed()
        self.send()
        self.write_outputs()
        self.events = []
        self.sprint()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-push", action="store_true")
    ap.add_argument("--no-sprint", action="store_true", help="不进入冲刺盯票")
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
