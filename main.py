# -*- coding: utf-8 -*-
"""
中央競馬 “今日の堅そう” ランキング TOP5（Yahoo!スポーツナビ版）
- スコアは全レース横断で計算し、高い順に採用
- 「堅い基準」を満たすレースのみ最大5件
- 足りない分は「※閾値対象外につき参考」で補完（スコア上位から）
- ◎/○ の間は改行、単勝オッズ併記（未取得は「オッズ未確定」）
- タイトル: 「開催場 R 距離」＋ 発走時刻
- ヘッダーに「※オッズは取得時点のものです」

GitHub Actions:
  - 土曜/日曜の 7:00 JST 実行（cronは UTC 22:00 の金曜/土曜）
"""

import os, re, time, unicodedata, datetime as dt, random
from typing import Optional, Dict, List, Tuple
import requests
from bs4 import BeautifulSoup

# ===== 基本設定 =====
TOP_K = 5
TIMEOUT = 12
SLEEP_SEC = 0.25
DISCORD_CHUNK = 1800

# 閾値（“堅い基準”）— 必要なら環境変数で調整
O1_MAX   = float(os.getenv("O1_MAX",   "2.0"))  # 1人気の最大許容オッズ
GAP_MIN  = float(os.getenv("GAP_MIN",  "0.7"))  # (2人気 - 1人気) の最小差
HC_MAX   = int  (os.getenv("HC_MAX",   "12"))   # 小頭数しきい値
EXCLUDE_WORDS = [w.strip() for w in os.getenv(
    "EXCLUDE_WORDS",
    "新馬,2歳,２歳,未勝利,重賞,オープン,障害,混合"
).split(",") if w.strip()]

# 表示/UA
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36 CenterKeiba/1.5"),
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    "Referer": "https://sports.yahoo.co.jp/keiba/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# 丸数字
CIRCLES = {i: chr(9311+i) for i in range(1, 21)}  # ①..⑳
def circled(n: int) -> str: return CIRCLES.get(n, f"[{n}]")

# 開催場
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

# ===== 日付解決（JST・週末運用） =====
def resolve_target_date() -> str:
    env = (os.getenv("TARGET_DATE") or "").strip()
    if re.fullmatch(r"\d{8}", env):  # YYYYMMDD
        return env
    # 近い土日を採用（Actions は 7:00 JST 実行を想定）
    now = now_jst()
    d = now.date()
    # 当日が土日ならその日、違えば次の土日まで進める
    while d.weekday() not in (5, 6):  # 5=Sat, 6=Sun
        d += dt.timedelta(days=1)
    return d.strftime("%Y%m%d")

# ====== 取得系（スポナビ） ======
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
        if target in page:
            out.append(url)
        time.sleep(0.1 + random.random()*0.2)
    return out

def parse_race_list_page(html: str) -> List[str]:
    """各レースの race_id(10桁) を収集（タイトル/時刻はオッズページで取得）"""
    soup = BeautifulSoup(html, "html.parser")
    ids=[]
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        m = RACE_INDEX_RE.search(href)
        if not m: continue
        rid = m.group(1)
        if rid not in ids: ids.append(rid)
    return ids

# ===== 出馬表（denma）から馬番→馬名（表示用はノイズ除去） =====
NAME_NOISE_RE = re.compile(r"\s*[牡牝セ騸]\d(?:/)?[^\s（）()]*")  # 例: " 牝2/鹿毛" など
def clean_horse_name(nm: str) -> str:
    nm = clean_line(nm)
    nm = NAME_NOISE_RE.sub("", nm)
    nm = re.sub(r"\s*(?:[（(].*?[）)])\s*$", "", nm)  # 末尾の（注記）除去
    return nm.strip()

def fetch_denma_names(session, rid: str) -> Dict[int,str]:
    """出馬表（denma）から {馬番: 馬名(クリーン)} を取得"""
    url = f"https://sports.yahoo.co.jp/keiba/race/denma/{rid}"
    html = fetch(session, url)
    if not html: return {}
    soup = BeautifulSoup(html, "html.parser")
    names={}
    for tr in soup.find_all("tr"):
        tds = [clean_line(td.get_text(" ")) for td in tr.find_all(["td","th"])]
        if len(tds) < 3: continue
        try:
            uma = int(tds[1])  # [枠, 馬, 馬名...]
        except:
            continue
        name = clean_horse_name(tds[2] or "")
        if name and 1 <= uma <= 20:
            names[uma] = name
    return names

# ===== オッズ(tfw)からメタ/オッズ取得（メインの真実ソース） =====
def parse_meta_from_odds_page_text(txt: str, rid: str) -> dict:
    """
    tfwページから: venue / R(rid末尾2桁) / course距離 / post_time / race_name を抽出
    タイトルは「<venue> <R> <course or race_name>」
    """
    t = clean_line(txt)
    # venue
    venue = ""
    for v in JRA_VENUES:
        if v in t:
            venue = v; break
    # R：rid末尾2桁
    rnum = int(rid[-2:])
    R = f"{rnum}R"
    # race_name（保険）
    race_name = ""
    mname = re.search(r"(?:^|[\s#])([一-龥ぁ-んァ-ンA-Za-z0-9･・\-]+特別|[一-龥ぁ-んァ-ンA-Za-z0-9･・\-]+ステークス|サラ系[^\s]+|新馬戦|未勝利|オープン|G[ⅠI]{1,3}|L)\b", t)
    if mname:
        race_name = mname.group(1)
    # course/distance
    mcd = re.search(r"(芝|ダート|障害)[^0-9]{0,6}([1-4]\d{2,3})m", t)
    course = f"{mcd.group(1)}{mcd.group(2)}" if mcd else ""
    # 発走時刻
    mtime = (re.search(r"(\d{1,2}:\d{2})\s*発走", t) or
             re.search(r"発走[：:\s]*([0-2]?\d:[0-5]\d)", t))
    post = f"{mtime.group(1)}発走" if mtime else ""
    title = " ".join(x for x in [venue, R, course or race_name] if x)
    return {"venue": venue, "R": R, "course": course, "post_time": post, "race_name": race_name, "title": title or f"{venue} {R}".strip()}

def fetch_odds_and_meta(session, rid: str) -> Tuple[Dict[int,float], dict]:
    """tfwから {馬番:単勝} と メタ情報 を返す"""
    url = f"https://sports.yahoo.co.jp/keiba/race/odds/tfw/{rid}"
    html = fetch(session, url)
    if not html: return {}, {"title":"（レース）","post_time":""}
    soup = BeautifulSoup(html, "html.parser")
    odds={}
    for tr in soup.find_all("tr"):
        tds = [clean_line(td.get_text(" ")) for td in tr.find_all("td")]
        if len(tds) < 4:  # [枠, 馬, 馬名, 単勝, 複勝...]
            continue
        try:
            uma = int(tds[1])
        except:
            continue
        # 単勝（最初の小数）
        m = _ODDS_FLOAT_RE.search(" ".join(tds[3:5]))
        if not m:
            continue
        try:
            odds[uma] = float(m.group(1))
        except:
            pass
    meta = parse_meta_from_odds_page_text(" ".join(x for x in soup.stripped_strings), rid)
    return odds, meta

# ===== スコアリング・券種 =====
def is_hard_race(title: str, headcount: Optional[int], o1, o2) -> bool:
    """“堅い基準”を満たすかどうか"""
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
    """本命の強さ・人気差・小頭数を重視"""
    s=0.0
    try:
        if o1 is not None: s += max(0.0, (6.0 - float(o1))) * 12
        if (o1 is not None) and (o2 is not None): s += max(0.0, float(o2)-float(o1)) * 10
        if hc is not None: s += max(0.0, (18 - int(hc))) * 2.0
    except: pass
    return round(s,2)

def pick_primary_bet(o1, o2, hc, rank):
    """保守的：基本は単勝。断然+差+小頭数で馬連、情報薄や多頭数は複勝/ワイド"""
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

# ===== 出力 =====
def build_message(items):
    header = f"{now_jst_str()} 中央競馬 “今日の堅そう”ランキング(最大{TOP_K}件)\n※オッズは取得時点のものです"
    if not items:
        return header + "\n見送り：収集0件 or 解析不可（サイト構造変更の可能性）"
    lines = [header]
    for idx, it in enumerate(items[:TOP_K], 1):
        title = clean_line(it.get('title') or "")
        ptime = clean_line(it.get('post_time') or "")
        # ◎/○（改行＆未確定対応）
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
        if it.get("reference_only"):
            lines.append("※閾値対象外につき参考")
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
def process_one(session, rid: str):
    try:
        # 1) オッズ(tfw)からオッズ＆正式メタ
        odds, meta = fetch_odds_and_meta(session, rid)
        # 2) 馬名（denma）— 表示用にクリーン
        names = fetch_denma_names(session, rid) if odds else {}
        # 3) 上位2頭
        o1=no1=nm1=None; o2=no2=nm2=None; headcount = max(names.keys()) if names else None
        if odds:
            pairs = sorted([(v,k) for k,v in odds.items()], key=lambda x: x[0])
            if len(pairs)>=1:
                o1, no1 = pairs[0][0], pairs[0][1]
                nm1 = names.get(no1)
            if len(pairs)>=2:
                o2, no2 = pairs[1][0], pairs[1][1]
                nm2 = names.get(no2)
        row = {
            "title": meta.get("title") or "（レース）",
            "post_time": meta.get("post_time") or "",
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
    race_ids=[]
    for url in list_urls:
        html = fetch(sess, url)
        if not html: continue
        ids = parse_race_list_page(html)
        for rid in ids:
            if rid not in race_ids: race_ids.append(rid)
        time.sleep(0.2)

    if not race_ids:
        msg = f"{now_jst_str()} 中央競馬 “今日の堅そう”ランキング(最大{TOP_K}件)\n※オッズは取得時点のものです\n見送り：収集0件 or 解析不可（サイト構造変更の可能性）"
        print(msg); send_discord(msg, os.getenv("DISCORD_WEBHOOK_URL","")); send_line(msg, os.getenv("LINE_NOTIFY_TOKEN","")); return

    # 3) 各レース解析（順次）
    results=[]
    for rid in race_ids:
        row = process_one(sess, rid)
        if row: results.append(row)

    # 4) スコア順に全レースを並べる
    results.sort(key=lambda x: x["score"], reverse=True)

    # 5) “堅い基準”で抽出（オッズが出てるレースのみ判定可能）
    hard = []
    for it in results:
        if is_hard_race(it.get("title",""), it.get("headcount"), it.get("o1"), it.get("o2")):
            hard.append({**it, "reference_only": False})

    # 6) 不足分を“参考”で補完（スコア上位から）
    picked = []
    picked_ids=set()
    for it in hard[:TOP_K]:
        picked.append(it); picked_ids.add(id(it))
    if len(picked) < TOP_K:
        for it in results:
            if id(it) in picked_ids:  # 既に採用済み（同一オブジェクト）ならスキップ
                continue
            # 閾値対象外として補完
            picked.append({**it, "reference_only": True})
            if len(picked) >= TOP_K:
                break

    # 7) 出力
    msg = build_message(picked)
    print("----- 通知本文 -----")
    print(msg)
    send_discord(msg, os.getenv("DISCORD_WEBHOOK_URL",""))
    send_line(msg, os.getenv("LINE_NOTIFY_TOKEN",""))

if __name__ == "__main__":
    main()