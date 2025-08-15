# -*- coding: utf-8 -*-
"""
中央競馬 “今日の堅そう” ランキング TOP5（Yahoo!スポーツナビ版・表示強化）
- タイトル: 「開催場 〇R コース/距離」形式
- 発走時刻: HH:MM発走 を表示
- ◎/○ に単勝オッズを併記
- ヘッダーに「※オッズは取得時点のものです」を追加
"""

import os, re, time, unicodedata, datetime as dt, random
from typing import Optional, Dict, List
import requests
from bs4 import BeautifulSoup

# ===== 基本設定 =====
TOP_K = 5
TIMEOUT = 12
SLEEP_SEC = 0.25
DISCORD_CHUNK = 1800

HIT_O1_MAX = float(os.getenv("HIT_O1_MAX", "1.8"))
HIT_GAP_MIN = float(os.getenv("HIT_GAP_MIN", "0.7"))
HIT_MAX_HEADS = int(os.getenv("HIT_MAX_HEADS", "10"))

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36 CenterKeiba/1.1"),
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    "Referer": "https://sports.yahoo.co.jp/keiba/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

CIRCLES = {i: chr(9311+i) for i in range(1, 21)}  # ①..⑳
JRA_VENUES = ("札幌","函館","福島","新潟","東京","中山","中京","京都","阪神","小倉")

def circled(n: int) -> str: return CIRCLES.get(n, f"[{n}]")
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

# ===== 日付解決 =====
def resolve_target_date() -> str:
    env = (os.getenv("TARGET_DATE") or "").strip()
    if re.fullmatch(r"\d{8}", env):  # YYYYMMDD
        return env
    # 金/土の夜運用前提で、近い土日を採用
    now = now_jst()
    d = now.date()
    if now.hour >= 21:
        d = d + dt.timedelta(days=1)
    while d.weekday() not in (5, 6):  # Sat/Sun
        d += dt.timedelta(days=1)
    return d.strftime("%Y%m%d")

# ====== 取得系（スポナビ） ======
MONTHLY_URL = "https://sports.yahoo.co.jp/keiba/schedule/monthly/"
LIST_RE = re.compile(r"/keiba/race/list/(\d{8})")
RACE_INDEX_RE = re.compile(r"/keiba/race/index/(\d{10})")

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
    """月間ページから当日の日別レース一覧URL（各競馬場）を抽出"""
    html = fetch(session, monthly_url_for(yyyymmdd))
    if not html: return []
    list_ids = list(dict.fromkeys(LIST_RE.findall(html)))
    out = []
    target = f"{int(yyyymmdd[:4])}年{int(yyyymmdd[4:6])}月{int(yyyymmdd[6:8])}日"
    for lid in list_ids:
        url = f"https://sports.yahoo.co.jp/keiba/race/list/{lid}"
        page = fetch(session, url)
        if not page: continue
        if target in page:  # ページ見出しに対象日付が入っている
            out.append(url)
        time.sleep(0.1 + random.random()*0.2)
    return out

def parse_race_list_page(html: str):
    """
    各Rの {race_id, title(仮), post_time(仮)} を返す。
    正式タイトル/時刻は出馬表ページでも再抽出（こちらは保険）。
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        m = RACE_INDEX_RE.search(href)
        if not m: continue
        rid = m.group(1)  # 10桁
        label = clean_line(a.get_text(" ")) or "（レース）"
        # 近傍の “HH:MM” を拾う（親要素テキスト）
        parent_txt = clean_line(a.find_parent().get_text(" ")) if a.find_parent() else label
        tm = re.search(r"([01]?\d|2[0-3]):([0-5]\d)", parent_txt)
        post = f"{int(tm.group(1)):02d}:{tm.group(2)}発走" if tm else ""
        rows.append({"race_id": rid, "title": label, "post_time": post})
    # ユニーク
    seen=set(); uniq=[]
    for r in rows:
        if r["race_id"] in seen: continue
        seen.add(r["race_id"]); uniq.append(r)
    return uniq

# ===== 出馬表ページからメタ・馬名・頭数 =====
def parse_meta_from_denma_text(txt: str) -> dict:
    """
    出馬表（denma）ページ全テキストから、
    - 開催場 / R / コース距離（芝/ダ/障 + 数字）/ 発走時刻（“発走”近傍）を抽出
    """
    t = clean_line(txt)
    # 開催場
    venue = ""
    for v in JRA_VENUES:
        if v in t:
            venue = v
            break
    # R
    m_r = re.search(r"(\d{1,2})R", t)
    R = f"{m_r.group(1)}R" if m_r else ""
    # コース/距離
    m_cd = re.search(r"(芝|ダート|障害)\s*([1-4]\d{2,3})", t)
    course = f"{m_cd.group(1)}{m_cd.group(2)}" if m_cd else ""
    # 発走時刻（発走の近傍）
    m_time = (re.search(r"(\d{1,2}:\d{2})\s*発走", t) or
              re.search(r"発走[：:\s]*([0-2]?\d:[0-5]\d)", t) or
              re.search(r"([01]?\d|2[0-3])時([0-5]\d)分\s*発走", t) or
              re.search(r"発走[：:\s]*([01]?\d|2[0-3])時([0-5]\d)分", t))
    if m_time:
        if len(m_time.groups()) == 2 and "時" in m_time.group(0):
            hh = int(m_time.group(1)); mm = int(m_time.group(2))
            post = f"{hh:02d}:{mm:02d}発走"
        else:
            post = f"{m_time.group(1)}発走"
    else:
        post = ""
    title = " ".join(x for x in [venue, R, course] if x)
    return {"venue": venue, "R": R, "course": course, "post_time": post, "title": title or "（レース）"}

def fetch_denma(session, rid: str):
    """
    出馬表（denma）を取得して
    - names: {馬番: 馬名}
    - meta: {title, post_time}
    - headcount: int
    """
    url = f"https://sports.yahoo.co.jp/keiba/race/denma/{rid}"
    html = fetch(session, url)
    if not html: return {}, {"title": "（レース）", "post_time": ""}, None
    soup = BeautifulSoup(html, "html.parser")
    names={}
    for tr in soup.find_all("tr"):
        tds = [clean_line(td.get_text(" ")) for td in tr.find_all(["td","th"])]
        if len(tds) < 3: continue
        # 枠番, 馬番, 馬名 … の並びが多い
        try:
            uma = int(tds[1])
        except:
            continue
        name = tds[2] or ""
        if name and 1 <= uma <= 20:
            names[uma] = name
    headcount = max(names.keys()) if names else None
    meta = parse_meta_from_denma_text(" ".join(x for x in soup.stripped_strings))
    return names, meta, headcount

# ===== 単勝オッズ =====
def fetch_odds_tan(session, rid: str) -> Dict[int,float]:
    """
    単勝/複勝ページ（tfw）から “馬番→単勝” を取得
    """
    url = f"https://sports.yahoo.co.jp/keiba/race/odds/tfw/{rid}"
    html = fetch(session, url)
    if not html: return {}
    soup = BeautifulSoup(html, "html.parser")
    result={}
    for tr in soup.find_all("tr"):
        tds = [clean_line(td.get_text(" ")) for td in tr.find_all("td")]
        if len(tds) < 4:  # [枠, 馬, 馬名, 単勝, 複勝...]
            continue
        try:
            uma = int(tds[1])
        except:
            continue
        # 単勝（小数を拾う）
        m = re.search(r"(\d{1,3}\.\d)", " ".join(tds[3:5]))
        if not m: 
            continue
        try:
            result[uma] = float(m.group(1))
        except:
            pass
    return result

# ===== スコアリング・券種 =====
UPSET_WORDS = ["新馬","2歳","２歳","重賞","オープン","障害","混合","未勝利"]

def is_low_variance_race(title: str, headcount: Optional[int], o1, o2) -> bool:
    t = title or ""
    if any(w in t for w in UPSET_WORDS):
        return False
    try:
        if o1 is None or o2 is None: return False
        o1f = float(o1)
        o2f = float(o2)
        if o1f > HIT_O1_MAX: return False
        if (o2f - o1f) < HIT_GAP_MIN: return False
        if (headcount is not None) and headcount > HIT_MAX_HEADS: return False
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
            if gap is not None and gap >= 1.0 and (hc is not None and hc <= HIT_MAX_HEADS):
                choice = "馬連（1点）"
        elif o1f is not None and o1f <= 2.0 and (hc is not None and hc <= HIT_MAX_HEADS):
            choice = "ワイド（1点）"
        if hc is not None and hc >= 12:
            choice = "複勝（1点）"
    except: pass
    return choice

# ===== 出力 =====
def build_message(items):
    header = f"{now_jst_str()} 中央競馬 “今日の堅そう”ランキング(最大{TOP_K}件)\n※オッズは取得時点のものです"
    if not items:
        return header + "\n見送り：収集0件 or 解析不可（サイト構造変更の可能性）"
    lines = [header]
    for idx, it in enumerate(items[:TOP_K], 1):
        title = clean_line(it.get('title') or "")
        ptime = clean_line(it.get('post_time') or "")
        # ◎○表記（馬番＋馬名＋単勝）
        left = "1人気想定"
        right = "2人気想定"
        if it.get("no1"):
            left = f"{circled(it['no1'])}" + (f" {it.get('name1')}" if it.get('name1') else "")
            if it.get("o1") is not None: left += f"（単勝{it['o1']}）"
        if it.get("no2"):
            right = f"{circled(it['no2'])}" + (f" {it.get('name2')}" if it.get('name2') else "")
            if it.get("o2") is not None: right += f"（単勝{it['o2']}）"
        bet = pick_primary_bet(it.get("o1"), it.get("o2"), it.get("headcount"), idx)
        lines.append(f"【{idx}位】{title} {ptime}".rstrip())
        lines.append(f"◎{left}  ○{right}")
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

# ===== 1レース処理 =====
def process_one(session, rid: str, meta_hint: dict):
    try:
        # 出馬表（馬番→馬名）/頭数/正式メタ
        names, meta_denma, headcount = fetch_denma(session, rid)
        # 単勝オッズ
        odds = fetch_odds_tan(session, rid)
        o1=no1=nm1=None; o2=no2=nm2=None
        if odds:
            pairs = sorted([(v,k) for k,v in odds.items()], key=lambda x: x[0])
            if len(pairs)>=1:
                o1, no1 = pairs[0][0], pairs[0][1]
                nm1 = names.get(no1)
            if len(pairs)>=2:
                o2, no2 = pairs[1][0], pairs[1][1]
                nm2 = names.get(no2)
        # タイトル/時刻は denma 優先、無ければ list のヒント
        title = meta_denma.get("title") or meta_hint.get("title") or "（レース）"
        post = meta_denma.get("post_time") or meta_hint.get("post_time") or ""
        row = {
            "title": title,
            "post_time": post,
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

# ===== メイン =====
def main():
    ymd = resolve_target_date()
    print(f"[INFO] TARGET_DATE={ymd} (JST)")
    sess = make_session()

    # 1) 月間→当日の日別リストURL（各競馬場）
    list_urls = find_day_list_urls(sess, ymd)
    if not list_urls:
        msg = f"{now_jst_str()} 中央競馬 “今日の堅そう”ランキング(最大{TOP_K}件)\n※オッズは取得時点のものです\n見送り：収集0件 or 解析不可（サイト構造変更の可能性）"
        print(msg); send_discord(msg, os.getenv("DISCORD_WEBHOOK_URL","")); send_line(msg, os.getenv("LINE_NOTIFY_TOKEN","")); return

    # 2) 各会場の “レース一覧” から race_id 群を収集
    meta_rows=[]
    for url in list_urls:
        html = fetch(sess, url)
        if not html: continue
        rows = parse_race_list_page(html)
        meta_rows.extend(rows)
        time.sleep(0.2)
    # 重複除去
    seen=set(); metas=[]
    for r in meta_rows:
        if r["race_id"] in seen: continue
        seen.add(r["race_id"]); metas.append(r)

    if not metas:
        msg = f"{now_jst_str()} 中央競馬 “今日の堅そう”ランキング(最大{TOP_K}件)\n※オッズは取得時点のものです\n見送り：収集0件 or 解析不可（サイト構造変更の可能性）"
        print(msg); send_discord(msg, os.getenv("DISCORD_WEBHOOK_URL","")); send_line(msg, os.getenv("LINE_NOTIFY_TOKEN","")); return

    # 3) 各レース解析（順次）
    results=[]
    for m in metas:
        row = process_one(sess, m["race_id"], m)
        if row: results.append(row)

    # 4) 的中率モード：安全レースのみ抽出
    safe = [it for it in results if is_low_variance_race(it.get("title",""), it.get("headcount"), it.get("o1"), it.get("o2"))]
    if safe: results = safe

    # 5) スコア降順 → 出力
    results.sort(key=lambda x: x["score"], reverse=True)
    msg = build_message(results)

    # 6) 通知
    print("----- 通知本文 -----")
    print(msg)
    send_discord(msg, os.getenv("DISCORD_WEBHOOK_URL",""))
    send_line(msg, os.getenv("LINE_NOTIFY_TOKEN",""))

if __name__ == "__main__":
    main()