# -*- coding: utf-8 -*-
"""
中央競馬 “今日の堅そう” ランキング TOP5（Yahoo!スポーツナビ版）
- 参照サイト例
  * 月間: https://sports.yahoo.co.jp/keiba/schedule/monthly/?month=8&year=2025
  * 日別: https://sports.yahoo.co.jp/keiba/race/list/25040207
  * 出馬表: https://sports.yahoo.co.jp/keiba/race/denma/<race_id>
  * 単勝/複勝: https://sports.yahoo.co.jp/keiba/race/odds/tfw/<race_id>

実行タイミング（JST想定）:
  - 金曜 22:00 → 土曜開催分
  - 土曜 22:00 → 日曜開催分
  （TARGET_DATE=YYYYMMDD を渡せば任意日付で動作）

出力：
  “中央競馬 今日の堅そうランキング(最大5件)” を整形し、Discord/LINEに通知
"""

import os, re, time, unicodedata, datetime as dt, random
from typing import Optional, Dict, List
from urllib.parse import urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

# ===== 基本設定 =====
TOP_K = 5
TIMEOUT = 12
SLEEP_SEC = 0.25
MAX_WORKERS = 1  # Yahoo側は順次取得で十分＆安全運転
DISCORD_CHUNK = 1800

# 的中率モードのしきい値（必要なら環境変数で微調整）
HIT_O1_MAX = float(os.getenv("HIT_O1_MAX", "1.8"))   # 1人気の最大許容オッズ
HIT_GAP_MIN = float(os.getenv("HIT_GAP_MIN", "0.7"))  # 2-1人気の最小ギャップ
HIT_MAX_HEADS = int(os.getenv("HIT_MAX_HEADS", "10")) # 小頭数しきい値

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36 CenterKeiba/1.0"),
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    "Referer": "https://sports.yahoo.co.jp/keiba/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

CIRCLES = {i: chr(9311+i) for i in range(1, 21)}  # ①…⑳
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

# ===== 日付解決（JSTの週末運用） =====
def resolve_target_date() -> str:
    env = (os.getenv("TARGET_DATE") or "").strip()
    if re.fullmatch(r"\d{8}", env):  # YYYYMMDD
        return env
    # 金曜/土曜22:00運用 → 翌開催日（基本：土日）を推定
    now = now_jst()
    d = now.date()
    if now.hour >= 21:  # 21時以降は翌日想定（ゆとりを持たせる）
        d = d + dt.timedelta(days=1)
    # 週末補正（最も近い土日へ寄せる）
    while d.weekday() not in (5, 6):  # 5=Sat, 6=Sun
        d += dt.timedelta(days=1)
    return d.strftime("%Y%m%d")

# ===== スポナビ：月間→日別リストURLの抽出 =====
MONTHLY_URL = "https://sports.yahoo.co.jp/keiba/schedule/monthly/"

LIST_RE = re.compile(r"/keiba/race/list/(\d{8})")
RACE_INDEX_RE = re.compile(r"/keiba/race/index/(\d{10})")
ODDS_TANPUKU_RE = re.compile(r"/keiba/race/odds/tfw/(\d{10})")
DENMA_RE = re.compile(r"/keiba/race/denma/(\d{10})")

def fetch(session, url, timeout=TIMEOUT) -> Optional[str]:
    r = session.get(url, timeout=timeout, allow_redirects=True)
    if not r.ok:
        return None
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding or "utf-8"
    txt = r.text
    # JS無効の案内はOK。エラーページや403系なら除外
    bad = ("エラーが発生" in txt) or ("指定のページは見つかりません" in txt)
    return None if bad else txt

def monthly_url_for(yyyymmdd: str) -> str:
    y, m = int(yyyymmdd[:4]), int(yyyymmdd[4:6])
    return f"{MONTHLY_URL}?month={m}&year={y}"

def find_day_list_urls(session, yyyymmdd: str) -> List[str]:
    """月間ページから “当日の日別レース一覧URL(各競馬場)” を抽出。"""
    html = fetch(session, monthly_url_for(yyyymmdd))
    if not html:
        return []
    # ページ内の全 list/******** を拾う → 実際に開いて日付見出しに当日が含まれるものに限定
    list_ids = list(dict.fromkeys(LIST_RE.findall(html)))
    out = []
    target = f"{int(yyyymmdd[:4])}年{int(yyyymmdd[4:6])}月{int(yyyymmdd[6:8])}日"
    for lid in list_ids:
        url = f"https://sports.yahoo.co.jp/keiba/race/list/{lid}"
        page = fetch(session, url)
        if not page:
            continue
        if target in page:  # 見出しテキストに “YYYY年M月D日”
            out.append(url)
        time.sleep(0.1 + random.random()*0.2)
    return out

# ===== 日別レース一覧 → 各レースID, 発走時刻, 競馬場名/タイトル =====
def parse_race_list_page(html: str):
    """各Rの {race_id, post_time, title, course} を返す"""
    soup = BeautifulSoup(html, "html.parser")
    text_all = " ".join(x for x in soup.stripped_strings)
    # 見出しの中に “2025年8月16日（土） 札幌/新潟/中京” のタブ
    venues = []
    for a in soup.select("a[href]"):
        t = (a.get_text() or "").strip()
        if t in ("札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"):
            venues.append(t)
    venue_str = " / ".join(sorted(set(venues)))  # 情報用

    rows = []
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        m = RACE_INDEX_RE.search(href)
        if not m:
            continue
        rid = m.group(1)  # 10桁
        # 直近のテキストから発走時刻とレース名を推定
        label = clean_line(a.get_text(" "))
        # 発走時刻は前後に “HH:MM” が並ぶので、親要素テキストを使う
        parent_txt = clean_line(a.find_parent().get_text(" ")) if a.find_parent() else label
        tm = re.search(r"([01]?\d|2[0-3]):([0-5]\d)", parent_txt)
        post = f"{int(tm.group(1)):02d}:{tm.group(2)}発走" if tm else ""
        # レース名・距離など（リンクテキストで十分）
        title = label if label else "（レース名不明）"
        rows.append({"race_id": rid, "post_time": post, "title": title, "venues": venue_str})
    # 重複排除
    uniq = []
    seen=set()
    for r in rows:
        if r["race_id"] in seen: continue
        seen.add(r["race_id"]); uniq.append(r)
    return uniq

# ===== 出馬表（馬番→馬名）/頭数 =====
def fetch_denma(session, rid: str) -> Dict[int,str]:
    url = f"https://sports.yahoo.co.jp/keiba/race/denma/{rid}"
    html = fetch(session, url)
    if not html: return {}
    soup = BeautifulSoup(html, "html.parser")
    out={}
    for tr in soup.find_all("tr"):
        tds = [clean_line(td.get_text(" ")) for td in tr.find_all(["td","th"])]
        if len(tds) < 3: continue
        # 枠番, 馬番, 馬名 ... の並びを想定
        try:
            umaban = int(tds[1])
        except:
            continue
        name = tds[2] or ""
        if name and 1 <= umaban <= 20:
            out[umaban] = name
    return out

def count_head(session, rid: str) -> Optional[int]:
    url = f"https://sports.yahoo.co.jp/keiba/race/denma/{rid}"
    html = fetch(session, url)
    if not html: return None
    nums=set()
    for m in re.finditer(r">\s*([1-2]?\d)\s*</", html):
        try:
            n=int(m.group(1))
            if 1<=n<=20: nums.add(n)
        except: pass
    return max(nums) if nums else None

# ===== オッズ（単勝） =====
def fetch_odds_tan(session, rid: str) -> Dict[int,float]:
    """
    単勝/複勝ページ（tfw）から “馬番→単勝” を直接取得
    """
    url = f"https://sports.yahoo.co.jp/keiba/race/odds/tfw/{rid}"
    html = fetch(session, url)
    if not html: return {}
    soup = BeautifulSoup(html, "html.parser")
    result={}
    for tr in soup.find_all("tr"):
        tds = [clean_line(td.get_text(" ")) for td in tr.find_all("td")]
        if len(tds) < 4:  # 枠番, 馬番, 馬名, 単勝, 複勝…
            continue
        try:
            uma = int(tds[1])
        except:
            continue
        # 単勝（小数っぽいものを最初に）
        m = re.search(r"(\d{1,3}\.\d)", " ".join(tds[3:5]))
        if not m:  # まれにオッズ未掲載タイミング
            continue
        try:
            result[uma] = float(m.group(1))
        except:
            pass
    return result

# ===== スコアリング・券種 =====
UPSET_WORDS = ["新馬", "2歳", "２歳", "重賞", "オープン", "障害", "混合"]

def is_low_variance_race(title: str, headcount: Optional[int], o1, o2) -> bool:
    t = title or ""
    if any(w in t for w in UPSET_WORDS):
        return False
    try:
        if o1 is None: return False
        o1f = float(o1)
        if o1f > HIT_O1_MAX: return False
        if o2 is None: return False
        gap = float(o2) - o1f
        if gap < HIT_GAP_MIN: return False
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
    header = f"{now_jst_str()} 中央競馬 “今日の堅そう”ランキング(最大{TOP_K}件)"
    if not items:
        return header + "\n見送り：収集0件 or 解析不可（サイト構造変更の可能性）"
    lines = [header]
    for idx, it in enumerate(items[:TOP_K], 1):
        title = clean_line(it.get('title') or "")
        ptime = clean_line(it.get('post_time') or "")
        left = "1人気想定"
        right = "2人気想定"
        if it.get("no1"):
            left = f"{circled(it['no1'])}" + (f" {it.get('name1')}" if it.get('name1') else "")
            if it.get("o1") is not None: left += f"（{it['o1']}）"
        if it.get("no2"):
            right = f"{circled(it['no2'])}" + (f" {it.get('name2')}" if it.get('name2') else "")
            if it.get("o2") is not None: right += f"（{it['o2']}）"
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
        # 出馬表（馬番→馬名）/頭数
        names = fetch_denma(session, rid)
        headcount = count_head(session, rid)
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
        title = meta_hint.get("title") or "（レース）"
        post = meta_hint.get("post_time") or ""
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
        msg = f"{now_jst_str()} 中央競馬 “今日の堅そう”ランキング(最大{TOP_K}件)\n見送り：収集0件 or 解析不可（サイト構造変更の可能性）"
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
        msg = f"{now_jst_str()} 中央競馬 “今日の堅そう”ランキング(最大{TOP_K}件)\n見送り：収集0件 or 解析不可（サイト構造変更の可能性）"
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