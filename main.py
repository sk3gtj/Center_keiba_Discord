# -*- coding: utf-8 -*-
"""
中央競馬 “今日の堅そう” ランキング（Yahoo!スポーツナビ版・venue確定）
- 会場名は「各レースのオッズページ(tfw)」から直接抽出（最優先）
- listページの会場ヒントはフォールバックとしてのみ使用
- スコアは全レース横断で計算し、高い順に採用
- “堅い基準”を満たすレースのみ最大5件（不足時は件数が減る）
- 重複は race_id で排除、◎/○ は改行、単勝オッズ併記
"""

import os, re, time, unicodedata, datetime as dt, random
from typing import Optional, Dict, List, Tuple
import requests
from bs4 import BeautifulSoup

TOP_K = 5
TIMEOUT = 12
SLEEP_SEC = 0.25
DISCORD_CHUNK = 1800

# “堅い基準”（環境変数で調整可）
O1_MAX   = float(os.getenv("O1_MAX",   "2.0"))
GAP_MIN  = float(os.getenv("GAP_MIN",  "0.7"))
HC_MAX   = int  (os.getenv("HC_MAX",   "12"))
EXCLUDE_WORDS = [w.strip() for w in os.getenv(
    "EXCLUDE_WORDS",
    "新馬,2歳,２歳,未勝利,重賞,オープン,障害,混合"
).split(",") if w.strip()]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36 CenterKeiba/1.8"),
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    "Referer": "https://sports.yahoo.co.jp/keiba/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

CIRCLES = {i: chr(9311+i) for i in range(1, 21)}  # ①..⑳
def circled(n: int) -> str: return CIRCLES.get(n, f"[{n}]")

JRA_VENUES = ("札幌","函館","福島","新潟","東京","中山","中京","京都","阪神","小倉")

def now_jst(): return dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
def now_jst_str(): return now_jst().strftime("%Y-%m-%d (%a) %H:%M JST")
def norm(s: str) -> str: return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", unicodedata.normalize("NFKC", s or ""))
def clean_line(s: str) -> str: return re.sub(r"\s+", " ", norm(s)).strip()

def make_session():
    s = requests.Session(); s.headers.update(HEADERS)
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(total=2, backoff_factor=0.4, status_forcelist=[429,500,502,503,504])
        s.mount("http://", HTTPAdapter(max_retries=retry))
        s.mount("https://", HTTPAdapter(max_retries=retry))
    except Exception:
        pass
    return s

def resolve_target_date() -> str:
    env = (os.getenv("TARGET_DATE") or "").strip()
    if re.fullmatch(r"\d{8}", env):  # YYYYMMDD
        return env
    d = now_jst().date()
    while d.weekday() not in (5, 6):
        d += dt.timedelta(days=1)
    return d.strftime("%Y%m%d")

MONTHLY_URL = "https://sports.yahoo.co.jp/keiba/schedule/monthly/"
LIST_RE = re.compile(r"/keiba/race/list/(\d{8})")
RACE_INDEX_RE = re.compile(r"/keiba/race/index/(\d{10})")
_ODDS_FLOAT_RE = re.compile(r"\b(\d{1,3}\.\d)\b")

def fetch(session, url, timeout=TIMEOUT) -> Optional[str]:
    r = session.get(url, timeout=timeout, allow_redirects=True)
    if not r.ok:
        return None
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding or "utf-8"
    txt = r.text
    bad = ("エラーが発生" in txt) or ("指定のページは見つかりません" in txt)
    return None if bad else txt

def monthly_url_for(yyyymmdd: str) -> str:
    y, m = int(yyyymmdd[:4]), int(yyyymmdd[4:6])
    return f"{MONTHLY_URL}?month={m}&year={y}"

def find_day_list_urls(session, yyyymmdd: str) -> List[str]:
    html = fetch(session, monthly_url_for(yyyymmdd))
    if not html: return []
    list_ids = list(dict.fromkeys(LIST_RE.findall(html)))
    out = []
    target = f"{int(yyyymmdd[:4])}年{int(yyyymmdd[4:6])}月{int(yyyymmdd[6:8])}日"
    for lid in list_ids:
        url = f"https://sports.yahoo.co.jp/keiba/race/list/{lid}"
        page = fetch(session, url)
        if not page: continue
        if target in page:
            out.append(url)
        time.sleep(0.1 + random.random()*0.2)
    return out

def parse_venue_from_list_page(soup: BeautifulSoup) -> str:
    # 参照のみ（なくてもOK）。確実なのは各raceのoddsページ。
    text = clean_line(" ".join(x for x in soup.stripped_strings))
    # 見出しの直近付近を優先できればベストだが、保険として最初に出てくる場名を返す
    for v in JRA_VENUES:
        if re.search(rf"\b{re.escape(v)}\b", text):
            return v
    return ""

def parse_race_list_page(html: str, venue_hint: str) -> List[Tuple[str,str]]:
    soup = BeautifulSoup(html, "html.parser")
    ids=[]
    seen=set()
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        m = RACE_INDEX_RE.search(href)
        if not m: continue
        rid = m.group(1)
        if rid in seen: continue
        seen.add(rid)
        ids.append((rid, venue_hint))
    return ids

NAME_NOISE_RE = re.compile(r"\s*[牡牝セ騸]\d(?:/)?[^\s（）()]*")
def clean_horse_name(nm: str) -> str:
    nm = clean_line(nm)
    nm = NAME_NOISE_RE.sub("", nm)
    nm = re.sub(r"\s*(?:[（(].*?[）)])\s*$", "", nm)
    return nm.strip()

def fetch_denma_names(session, rid: str) -> Dict[int,str]:
    url = f"https://sports.yahoo.co.jp/keiba/race/denma/{rid}"
    html = fetch(session, url)
    if not html: return {}
    soup = BeautifulSoup(html, "html.parser")
    names={}
    for tr in soup.find_all("tr"):
        tds = [clean_line(td.get_text(" ")) for td in tr.find_all(["td","th"])]
        if len(tds) < 3: continue
        try:
            uma = int(tds[1])
        except:
            continue
        name = clean_horse_name(tds[2] or "")
        if name and 1 <= uma <= 20:
            names[uma] = name
    return names

def parse_meta_from_odds_page_text(txt: str, rid: str) -> dict:
    """
    odds(tfw)のページテキストから venue/R/距離/発走 を抽出。
    venue は JRA_VENUES の“単語境界一致”で検出（最優先ソース）。
    """
    t = clean_line(txt)
    venue = ""
    for v in JRA_VENUES:
        if re.search(rf"\b{re.escape(v)}\b", t):
            venue = v; break
    R = f"{int(rid[-2:])}R"
    mname = re.search(r"(?:^|[\s#])([一-龥ぁ-んァ-ンA-Za-z0-9･・\-]+特別|[一-龥ぁ-んァ-ンA-Za-z0-9･・\-]+ステークス|サラ系[^\s]+|新馬戦|未勝利|オープン|G[ⅠI]{1,3}|L)\b", t)
    race_name = mname.group(1) if mname else ""
    mcd = re.search(r"(芝|ダート|障害)[^0-9]{0,6}([1-4]\d{2,3})m", t)
    course = f"{mcd.group(1)}{mcd.group(2)}" if mcd else ""
    mtime = (re.search(r"(\d{1,2}:\d{2})\s*発走", t) or re.search(r"発走[：:\s]*([0-2]?\d:[0-5]\d)", t))
    post = f"{mtime.group(1)}発走" if mtime else ""
    return {"venue": venue, "R": R, "course": course, "post_time": post, "race_name": race_name}

def fetch_odds_and_meta(session, rid: str) -> Tuple[Dict[int,float], dict]:
    url = f"https://sports.yahoo.co.jp/keiba/race/odds/tfw/{rid}"
    html = fetch(session, url)
    if not html: return {}, {"venue":"", "R":"", "course":"", "post_time":"", "race_name":""}
    soup = BeautifulSoup(html, "html.parser")
    odds={}
    for tr in soup.find_all("tr"):
        tds = [clean_line(td.get_text(" ")) for td in tr.find_all("td")]
        if len(tds) < 4:
            continue
        try:
            uma = int(tds[1])
        except:
            continue
        m = _ODDS_FLOAT_RE.search(" ".join(tds[3:5]))
        if not m: 
            continue
        try:
            odds[uma] = float(m.group(1))
        except:
            pass
    meta = parse_meta_from_odds_page_text(" ".join(x for x in soup.stripped_strings), rid)
    return odds, meta

def is_hard_race(title: str, headcount: Optional[int], o1, o2) -> bool:
    t = title or ""
    if any(w in t for w in EXCLUDE_WORDS):
        return False
    try:
        if o1 is None or o2 is None: return False
        o1f = float(o1); o2f = float(o2)
        if o1f > O1_MAX: return False
        if (o2f - o1f) < GAP_MIN: return False
        if (headcount is not None) and headcount > HC_MAX: return False
        return True
    except:
        return False

def score(o1, o2, hc):
    s=0.0
    try:
        if o1 is not None: s += max(0.0, (6.0 - float(o1))) * 12
        if (o1 is not None) and (o2 is not None): s += max(0.0, float(o2)-float(o1)) * 10
        if hc is not None: s += max(0.0, (18 - int(hc))) * 2.0
    except: pass
    return round(s,2)

def pick_primary_bet(o1, o2, hc, rank):
    defaults = {1:"単勝（1点）", 2:"ワイド（1点）", 3:"複勝（1点）", 4:"単勝（1点）", 5:"ワイド（1点）"}
    choice = defaults.get(rank, "単勝（1点）")
    try:
        o1f = float(o1) if o1 is not None else None
        o2f = float(o2) if o2 is not None else None
        gap = (o2f - o1f) if (o1f is not None and o2f is not None) else None
        if o1f is not None and o1f <= 1.5:
            choice = "単勝（1点）"
            if gap is not None and gap >= 1.0 and (hc is not None and hc <= HC_MAX):
                choice = "馬連（1点）"
        elif o1f is not None and o1f <= 2.0 and (hc is not None and hc <= HC_MAX):
            choice = "ワイド（1点）"
        if hc is not None and hc >= 12:
            choice = "複勝（1点）"
    except: pass
    return choice

def build_message(items):
    header = f"{now_jst_str()} 中央競馬 “今日の堅そう”ランキング(最大{TOP_K}件)\n※オッズは取得時点のものです"
    if not items:
        return header + "\n見送り：閾値を満たすレースがありません（オッズ未公開/対象外の可能性）"
    lines = [header]
    for idx, it in enumerate(items[:TOP_K], 1):
        title = clean_line(it.get('title') or "")
        ptime = clean_line(it.get('post_time') or "")
        if it.get("no1"):
            left = f"{circled(it['no1'])}" + (f" {it.get('name1')}" if it.get('name1') else "")
            left += f"（単勝{it['o1']}）" if it.get("o1") is not None else "（オッズ未確定）"
        else:
            left = "1人気想定（オッズ未確定）" if it.get("o1") is None else f"1人気想定（単勝{it['o1']}）"
        if it.get("no2"):
            right = f"{circled(it['no2'])}" + (f" {it.get('name2')}" if it.get('name2') else "")
            right += f"（単勝{it['o2']}）" if it.get("o2") is not None else "（オッズ未確定）"
        else:
            right = "2人気想定（オッズ未確定）" if it.get("o2") is None else f"2人気想定（単勝{it['o2']}）"
        bet = pick_primary_bet(it.get("o1"), it.get("o2"), it.get("headcount"), idx)
        lines.append(f"【{idx}位】{title} {ptime}".rstrip())
        lines.append(f"◎{left}")
        lines.append(f"○{right}")
        lines.append(f"推奨：{bet}")
        lines.append(f"参考：頭数={it.get('headcount')} / score={it.get('score')}")
        if idx < min(len(items), TOP_K): lines.append("---")
    return "\n".join(lines)

def send_discord(msg: str, webhook: str):
    webhook=(webhook or "").strip()
    if not webhook:
        print("[Discord] Webhook未設定のためスキップ"); return
    try:
        requests.post(webhook, json={"content": f"[ping] {now_jst_str()} - notifier alive"}, timeout=12).raise_for_status()
    except Exception as e:
        print(f"[Discord] ping失敗: {e}"); return
    for i in range(0, len(msg), DISCORD_CHUNK):
        part = msg[i:i+DISCORD_CHUNK]
        try:
            requests.post(webhook, json={"content": part}, timeout=20).raise_for_status()
        except Exception as e:
            print(f"[Discord] 送信エラー: {e}"); break

def send_line(msg: str, token: str):
    token=(token or "").strip()
    if not token:
        print("[LINE] トークン未設定のためスキップ"); return
    try:
        requests.post("https://notify-api.line.me/api/notify",
                      headers={"Authorization": f"Bearer {token}"},
                      data={"message": msg}, timeout=15).raise_for_status()
    except Exception as e:
        print(f"[LINE] 送信エラー: {e}")

def process_one(session, rid: str, venue_hint: str):
    try:
        odds, meta = fetch_odds_and_meta(session, rid)
        names = fetch_denma_names(session, rid) if odds else {}
        o1=no1=nm1=None; o2=no2=nm2=None; headcount = max(names.keys()) if names else None
        if odds:
            pairs = sorted([(v,k) for k,v in odds.items()], key=lambda x: x[0])
            if len(pairs)>=1:
                o1, no1 = pairs[0][0], pairs[0][1]; nm1 = names.get(no1)
            if len(pairs)>=2:
                o2, no2 = pairs[1][0], pairs[1][1]; nm2 = names.get(no2)

        # ★ venue は odds ページの抽出を最優先
        venue = (meta.get("venue") or "").strip()
        if not venue:
            venue = (venue_hint or "").strip()

        R = meta.get("R","")
        course = meta.get("course","")
        post = meta.get("post_time","")
        title = " ".join(x for x in [venue, R, course] if x)

        row = {
            "rid": rid,
            "title": title or "（レース）",
            "post_time": post or "",
            "headcount": headcount,
            "o1": o1, "o2": o2,
            "no1": no1, "name1": nm1,
            "no2": no2, "name2": nm2,
        }
        row["score"] = score(o1, o2, headcount)
        return row
    except Exception as e:
        print(f"[WARN] 解析失敗: {rid} -> {e}")
        return None
    finally:
        time.sleep(SLEEP_SEC)

def main():
    ymd = resolve_target_date()
    print(f"[INFO] TARGET_DATE={ymd} (JST)")
    sess = make_session()

    list_urls = find_day_list_urls(sess, ymd)
    if not list_urls:
        msg = f"{now_jst_str()} 中央競馬 “今日の堅そう”ランキング(最大{TOP_K}件)\n※オッズは取得時点のものです\n見送り：収集0件 or 解析不可（サイト構造変更の可能性）"
        print(msg); send_discord(msg, os.getenv("DISCORD_WEBHOOK_URL","")); send_line(msg, os.getenv("LINE_NOTIFY_TOKEN","")); return

    race_items: List[Tuple[str,str]] = []  # (rid, venue_hint)
    seen=set()
    for url in list_urls:
        html = fetch(sess, url)
        if not html: continue
        soup = BeautifulSoup(html, "html.parser")
        venue_hint = parse_venue_from_list_page(soup)  # あくまで保険
        for rid, v in parse_race_list_page(html, venue_hint):
            if rid in seen: continue
            seen.add(rid)
            race_items.append((rid, v))
        time.sleep(0.2)

    if not race_items:
        msg = f"{now_jst_str()} 中央競馬 “今日の堅そう”ランキング(最大{TOP_K}件)\n※オッズは取得時点のものです\n見送り：収集0件 or 解析不可（サイト構造変更の可能性）"
        print(msg); send_discord(msg, os.getenv("DISCORD_WEBHOOK_URL","")); send_line(msg, os.getenv("LINE_NOTIFY_TOKEN","")); return

    results=[]
    for rid, venue in race_items:
        row = process_one(sess, rid, venue)
        if row: results.append(row)

    results.sort(key=lambda x: x["score"], reverse=True)

    picked=[]
    for it in results:
        if not is_hard_race(it.get("title",""), it.get("headcount"), it.get("o1"), it.get("o2")):
            continue
        picked.append(it)
        if len(picked) >= TOP_K:
            break

    msg = build_message(picked)
    print("----- 通知本文 -----")
    print(msg)
    send_discord(msg, os.getenv("DISCORD_WEBHOOK_URL",""))
    send_line(msg, os.getenv("LINE_NOTIFY_TOKEN",""))

if __name__ == "__main__":
    main()