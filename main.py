# -*- coding: utf-8 -*-
"""
中央競馬（JRA） “今日の堅そう” ランキング TOP5 を生成し、Discord/LINE へ通知。
- 収集：sp/PCのトップ/リストから race_id 抽出 → 少なければ「開催検出→全Rスキャン」。
- 解析：
    * 馬番→馬名：出馬表(shutuba)で確定（まずここから）
    * 予想オッズ：yoso行の“馬名”で突き合わせて取得（枠番/斤量の混同を回避）
    * 発走時刻：yoso → index → shutuba の順で広域探索して抽出
    * ◎/○：丸数字（①②…）＋馬名、単勝オッズ
- 的中率モード：
    * 断然人気×人気差×小頭数を優先し、波乱ワードを回避
    * 推奨券種は単勝/ワイド/複勝中心、馬連は“断然＋差”で限定
- 備考：
    * JRA向けにドメイン・場名・文言を調整（NARドメインは使用しない）
    * デフォルト対象日は「翌日」。環境変数 TARGET_DATE=YYYYMMDD を指定すると固定可。
"""

import os, re, time, unicodedata, datetime as dt, random
from urllib.parse import urlunparse, urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

# ===== 基本設定 =====
TOP_K = 5
TIMEOUT = 10
SLEEP_SEC = 0.25
SCAN_R_MAX = 12              # JRAは原則12R
PROBE_R = [1, 2, 3]          # 開催検出用の探索R
VENUES = r"(札幌|函館|新潟|東京|中山|中京|京都|阪神|小倉)"
DISCORD_CHUNK = 1800

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "16"))
ANALYZE_WORKERS = int(os.getenv("ANALYZE_WORKERS", "10"))
FALLBACK_BUDGET_SEC = int(os.getenv("FALLBACK_BUDGET_SEC", "180"))

# --- 的中率モードのしきい値（環境変数で調整可） ---
HIT_O1_MAX   = float(os.getenv("HIT_O1_MAX", "1.8"))  # 1番人気の最大許容オッズ（JRA向けにやや厳しめ初期値）
HIT_GAP_MIN  = float(os.getenv("HIT_GAP_MIN", "1.0")) # 2-1人気の最小ギャップ
HIT_MAX_HEADS= int(os.getenv("HIT_MAX_HEADS", "10"))  # 小頭数しきい値（JRAは10までをボーナス寄り）

# sp優先で通りやすいUA（JRA）
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0 Mobile Safari/537.36 KeibaNotifier/central-1.0"),
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    "Referer": "https://race.sp.netkeiba.com/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# 丸数字（1〜20）
CIRCLES = {i: chr(9311+i) for i in range(1, 21)}  # ①=9312
def circled(n: int) -> str: return CIRCLES.get(n, f"[{n}]")

# ---------- ユーティリティ ----------
def now_jst(): return dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
def now_jst_str(): return now_jst().strftime("%Y-%m-%d (%a) %H:%M JST")
def norm(s: str) -> str: return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", unicodedata.normalize("NFKC", s or ""))
def clean_line(s: str) -> str: return re.sub(r"\s+", " ", norm(s)).strip()

def resolve_target_date() -> str:
    """
    TARGET_DATE=YYYYMMDD があればそれを使う。
    指定が無い場合は “翌日” を対象（例：金曜22:00→土曜、土曜22:00→日曜）。
    """
    env = (os.getenv("TARGET_DATE") or "").strip()
    if re.fullmatch(r"\d{8}", env):
        return env
    return (now_jst() + dt.timedelta(days=1)).strftime("%Y%m%d")

def make_session():
    s = requests.Session(); s.headers.update(HEADERS)
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(total=2, backoff_factor=0.4, status_forcelist=[429,500,502,503,504])
        s.mount("http://", HTTPAdapter(max_retries=retry)); s.mount("https://", HTTPAdapter(max_retries=retry))
    except Exception: pass
    s.cookies.set("device", "sp", domain=".netkeiba.com")
    return s

def fetch(session, url, timeout=TIMEOUT):
    r = session.get(url, timeout=timeout, allow_redirects=True)
    if not r.ok: return None
    if not r.encoding or r.encoding.lower() in ("iso-8859-1","ascii"):
        r.encoding = r.apparent_encoding or "utf-8"
    text = r.text
    bad = ("Access Denied" in text) or ("Please enable JavaScript" in text) or ("お探しのページは見つかりません" in text)
    return None if bad else text

def normalize_name(s: str) -> str:
    """比較用に馬名を正規化：全角半角/記号/空白を削る"""
    s = norm(s)
    s = re.sub(r"[\s・\-’'\.!！?？／/]", "", s)
    return s.lower()

# ---------- 収集（race_id 抽出） ----------
RACE_ID_RE = re.compile(r"race_id=(\d{12})\b")
LINK_RE    = re.compile(r"(shutuba|odds|yoso|index|result)\.html", re.I)

def candidate_index_urls(d: str):
    """
    JRA向けの候補リスト（sp優先）。タブ指定が無くても race_id を拾えるので広めにスキャン。
    """
    return [
        # sp版（中央）
        f"https://race.sp.netkeiba.com/top/race_list.html?kaisai_date={d}",
        f"https://race.sp.netkeiba.com/",
        # PC版（保険）
        f"https://race.netkeiba.com/top/race_list.html?kaisai_date={d}",
        f"https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={d}",
        f"https://race.netkeiba.com/top/?kaisai_date={d}",
    ]

def collect_race_ids_from_lists(session, d: str):
    ids=[]
    for url in candidate_index_urls(d):
        try:
            html = fetch(session, url)
            if not html: continue
            ids += [m.group(1) for m in RACE_ID_RE.finditer(html) if m.group(1).startswith(d)]
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                if not LINK_RE.search(href): continue
                m = RACE_ID_RE.search(href)
                if m and m.group(1).startswith(d): ids.append(m.group(1))
        except Exception as e:
            print(f"[WARN] index fail: {url} -> {e}")
        time.sleep(0.12 + random.random()*0.2)
    # ユニーク
    seen,out=set(),[]
    for x in ids:
        if x in seen: continue
        seen.add(x); out.append(x)
    print(f"[INFO] 一覧から収集: {len(out)}件 ({d})")
    return out

# ---------- フォールバック（開催検出→全R） ----------
def rid_for(d: str, cc: int, rr: int) -> str:
    """
    JRAの race_id は 12桁（YYYYMMDD + 開催コード2桁 + レース番号2桁） で表現されるケースがある。
    ここでは NAR同様の「年 + 開催コード2桁 + 月日 + R」を使って総当たり探索。
    """
    return f"{d[:4]}{cc:02d}{d[4:]}{rr:02d}"

def candidate_odds_urls(rid: str):
    for host, path in [
        ("race.sp.netkeiba.com","/odds/yoso.html"),
        ("race.netkeiba.com","/odds/yoso.html"),
        ("race.sp.netkeiba.com","/odds/index.html"),
        ("race.netkeiba.com","/odds/index.html"),
    ]:
        yield urlunparse(("https", host, path, "", urlencode({"race_id": rid}), ""))

def candidate_shutuba_urls(rid: str):
    for host in ["race.sp.netkeiba.com","race.netkeiba.com"]:
        yield urlunparse(("https", host, "/race/shutuba.html","", urlencode({"race_id": rid}), ""))

def looks_odds(text: str) -> bool:
    t = norm(text)
    return ("予想オッズ" in t) or ("オッズ" in t) or ("単勝" in t)

def try_one_rid(session, rid: str) -> bool:
    # 1) オッズ系で存在確認
    for u in candidate_odds_urls(rid):
        html = fetch(session, u)
        if html and looks_odds(html): return True
    # 2) 出馬表で存在確認（Hotfix）
    for u in candidate_shutuba_urls(rid):
        html = fetch(session, u)
        if html and ("出馬表" in norm(html) or "騎手" in norm(html) or "馬名" in norm(html)):
            return True
    return False

def bruteforce_race_ids(session, d: str):
    start=time.time()
    print(f"[INFO] フォールバック総当たり開始: {d}（予算{FALLBACK_BUDGET_SEC}s）")
    active=set()
    # phase1: 開催検出（予算40%）
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs={}
        for cc in range(1,100):
            if time.time()-start > FALLBACK_BUDGET_SEC*0.4: break
            for rr in PROBE_R:
                rid = rid_for(d, cc, rr)
                futs[ex.submit(try_one_rid, session, rid)] = cc
        for fut in as_completed(futs):
            try:
                if fut.result(): active.add(futs[fut])
            except: pass
            if time.time()-start > FALLBACK_BUDGET_SEC*0.4: break
    if not active:
        print("[INFO] 開催検出0件"); return []
    # phase2: 検出開催の全R（予算いっぱい）
    found=[]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs={}
        for cc in sorted(active):
            for rr in range(1, SCAN_R_MAX+1):
                if time.time()-start > FALLBACK_BUDGET_SEC: break
                rid = rid_for(d, cc, rr)
                futs[ex.submit(try_one_rid, session, rid)] = rid
        for fut in as_completed(futs):
            try:
                if fut.result(): found.append(futs[fut])
            except: pass
            if time.time()-start > FALLBACK_BUDGET_SEC: break
    return list(dict.fromkeys(found))

# ---------- 解析（shutuba:馬名 → yoso:オッズ） ----------
def fetch_odds_html(session, rid: str):
    # yoso優先 → index補助
    for u in candidate_odds_urls(rid):
        if "/odds/yoso.html" not in u: continue
        html = fetch(session, u)
        if html and looks_odds(html): return html
    for u in candidate_odds_urls(rid):
        if "/odds/index.html" not in u: continue
        html = fetch(session, u)
        if html and "オッズ" in norm(html): return html
    return None

def fetch_shutuba_html(session, rid: str):
    for u in candidate_shutuba_urls(rid):
        html = fetch(session, u)
        if html and ("出馬表" in norm(html) or "馬名" in norm(html) or "騎手" in norm(html)):
            return html
    return None

def parse_race_meta_from_text(txt: str):
    t = clean_line(txt)
    m = re.search(rf"{VENUES}[^0-9]{{0,6}}(\d{{1,2}}R)", t)
    title = f"{m.group(1)} {m.group(2)}" if m else (re.search(r"(\d{1,2}R)", t).group(1) if re.search(r"(\d{1,2}R)", t) else "（開催/レース不明）")
    tm = re.search(r"(\d{1,2}:\d{2})\s*発走", t) or re.search(r"発走[：:\s]*([0-2]?\d:[0-5]\d)", t)
    post = f"{tm.group(1)}発走" if tm else ""
    hc=None
    hm=re.search(r"(\d{1,2})頭", t)
    if hm:
        try: hc=int(hm.group(1))
        except: pass
    return {"title": title, "post_time": post, "headcount": hc}

def parse_meta(html: str):
    soup = BeautifulSoup(html, "html.parser")
    return parse_race_meta_from_text(" ".join(x for x in soup.stripped_strings))

def parse_shutuba_names(html: str):
    """shutubaから 馬番→馬名（DOM変化に強い保険つき）。"""
    soup = BeautifulSoup(html, "html.parser")
    out={}
    for tr in soup.find_all("tr"):
        no=None; name=None
        # 馬番
        if tr.has_attr("data-umaban"):
            try: no=int(tr["data-umaban"])
            except: pass
        if no is None:
            m=re.search(r"\b(\d{1,2})\b", clean_line(tr.get_text(" ")))
            if m:
                try:
                    n=int(m.group(1))
                    if 1<=n<=20: no=n
                except: pass
        # 馬名（aタグ優先）
        a = tr.find("a")
        if a and a.get_text(strip=True):
            cand = clean_line(a.get_text(" "))
            if not any(k in cand for k in ["馬券","オッズ","騎手","厩舎","斤量","発走","枠"]):
                name=cand
        # テキスト保険
        if not name:
            txt=clean_line(tr.get_text(" "))
            for nm in re.findall(r"[一-龥ぁ-んァ-ヴーA-Za-z0-9][一-龥ぁ-んァ-ヴーA-Za-z0-9・\-’'\. ]{1,}", txt):
                t=nm.strip()
                if re.fullmatch(r"\d+", t): continue
                if any(k in t for k in ["馬券","オッズ","人気","斤量","発走","枠","タイム","指数","騎手","厩舎","調教師","頭","R "]):
                    continue
                name=t; break
        if (no and (1<=no<=20) and name):
            out.setdefault(no, name)
    return out  # {馬番: 馬名}

def parse_yoso_odds_by_name(html: str, names_map: dict):
    """
    yoso行から “馬名” を見つけ、それに紐づく最初の小数(単勝)を拾う。
    → 馬番は shutuba の names_map から逆引き（枠番混同を回避）
    """
    soup = BeautifulSoup(html, "html.parser")
    out = {}  # {馬番: 単勝}
    rev = {normalize_name(v): k for k, v in names_map.items()}

    for tr in soup.find_all("tr"):
        txt = clean_line(tr.get_text(" "))
        if not txt: continue

        # 候補の“自然語”から馬名を推定
        cand_name = None
        for nm in re.findall(r"[一-龥ぁ-んァ-ンﾞﾟA-Za-z0-9][一-龥ぁ-んァ-ンﾞﾟA-Za-z0-9・\-’'\. ]{1,}", txt):
            nm = nm.strip()
            if re.fullmatch(r"\d+", nm): continue
            if any(k in nm for k in ["馬券","オッズ","人気","斤量","発走","枠","タイム","指数","騎手","厩舎","調教師","頭","R "]):
                continue
            cand_name = nm
            break
        if not cand_name:
            continue

        key = normalize_name(cand_name)
        target_no = None
        if key in rev:
            target_no = rev[key]
        else:
            for rkey, no in rev.items():
                if key in rkey or rkey in key:
                    target_no = no; break
        if not target_no:
            continue

        # 単勝小数（斤量kg/㎏の直前・直後を除外）
        decimals = re.findall(r"(?<!kg)(?<!㎏)\b(\d{1,2}\.\d)\b(?!\s*(?:kg|㎏))", txt)
        odds = None
        for ds in decimals:
            try:
                v = float(ds)
                if 1.0 <= v <= 200.0:
                    odds = v; break
            except:
                pass
        if odds is None:
            continue

        out.setdefault(target_no, odds)

    return out  # {馬番: 単勝}

# ---------- 発走時刻抽出（強化版） ----------
def extract_post_time_from_text(txt: str) -> str | None:
    """テキストから発走時刻(HH:MM)を抽出して「HH:MM発走」に整形。"""
    t = clean_line(txt)
    pats = [
        r"([01]?\d|2[0-3]):([0-5]\d)\s*発走",
        r"発走[：:\s]*([01]?\d|2[0-3]):([0-5]\d)",
        r"発走予定[：:\s]*([01]?\d|2[0-3]):([0-5]\d)",
        r"([01]?\d|2[0-3])時([0-5]\d)分\s*発走",
        r"発走[：:\s]*([01]?\d|2[0-3])時([0-5]\d)分",
    ]
    for p in pats:
        m = re.search(p, t)
        if m:
            hh = int(m.group(1)); mm = int(m.group(2))
            return f"{hh:02d}:{mm:02d}発走"
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t)
    if m:
        hh = int(m.group(1)); mm = int(m.group(2))
        return f"{hh:02d}:{mm:02d}発走"
    return None

def get_post_time(session, rid: str) -> str:
    """yoso → index → shutuba の順で発走時刻を抽出。なければ空文字。"""
    # yoso
    for u in candidate_odds_urls(rid):
        if "/odds/yoso.html" not in u: continue
        html = fetch(session, u)
        if html:
            pt = extract_post_time_from_text(html)
            if pt: return pt
    # index
    for u in candidate_odds_urls(rid):
        if "/odds/index.html" not in u: continue
        html = fetch(session, u)
        if html:
            pt = extract_post_time_from_text(html)
            if pt: return pt
    # shutuba
    for u in candidate_shutuba_urls(rid):
        html = fetch(session, u)
        if html:
            pt = extract_post_time_from_text(html)
            if pt: return pt
    return ""

# ---------- 的中率モード判定 ----------
UPSET_WORDS = [
    "新馬", "未勝利", "2歳", "２歳", "初出走", "初ダ", "初ダート", "初距離",
    "重賞", "混合", "オープン", "ハンデ"
]

def is_low_variance_race(title: str, headcount: int | None, o1, o2) -> bool:
    """“堅そう”＝的中率重視で採用してよいレースか判定。"""
    t = (title or "")
    if any(w in t for w in UPSET_WORDS):
        return False
    try:
        o1f = float(o1) if o1 is not None else None
        o2f = float(o2) if o2 is not None else None
        gap = (o2f - o1f) if (o1f is not None and o2f is not None) else None
        if o1f is None:
            return False                 # 本命オッズが無いレースは見送り
        if o1f > HIT_O1_MAX:
            return False
        if gap is None or gap < HIT_GAP_MIN:
            return False
        if headcount is not None and headcount > HIT_MAX_HEADS:
            return False
        return True
    except Exception:
        return False

# ---------- スコア & 推奨券種 ----------
def score(o1, o2, hc):
    # 的中率重視：本命の強さ・人気差・小頭数を少し強めに
    s = 0.0
    try:
        if o1 is not None:
            s += max(0.0, (6.0 - float(o1))) * 12
        if (o1 is not None) and (o2 is not None):
            s += max(0.0, float(o2) - float(o1)) * 10
        if hc is not None:
            s += max(0.0, (18 - hc)) * 2.0
    except Exception:
        pass
    return round(s, 2)

def pick_primary_bet(o1, o2, hc, rank):
    """
    的中率重視：基本は単勝。状況によりワイド/複勝。
    馬連は“断然＋大きな人気差”のみ。三連複は原則出さない。
    """
    defaults = {1:"単勝（1点）", 2:"ワイド（1点）", 3:"複勝（1点）", 4:"単勝（1点）", 5:"ワイド（1点）"}
    choice = defaults.get(rank, "単勝（1点）")
    try:
        o1f = float(o1) if o1 is not None else None
        o2f = float(o2) if o2 is not None else None
        gap = (o2f - o1f) if (o1f is not None and o2f is not None) else None
        # 断然本命
        if o1f is not None and o1f <= 1.5:
            choice = "単勝（1点）"
            if gap is not None and gap >= 1.2 and (hc is not None and hc <= HIT_MAX_HEADS):
                choice = "馬連（1点）"
        # 本命1.6-2.0＆小頭数 → ワイドで堅く
        elif o1f is not None and o1f <= 2.0 and (hc is not None and hc <= HIT_MAX_HEADS):
            choice = "ワイド（1点）"
        # 多頭数 → 複勝
        if hc is not None and hc >= 12:
            choice = "複勝（1点）"
    except Exception:
        pass
    return choice

# ---------- 通知 ----------
def build_message(items):
    header = f"{now_jst_str()} 中央競馬 “今日の堅そう”ランキング(最大{TOP_K}件)"
    if not items:
        return header + "\n見送り：収集0件 or 解析不可（サイト構造変更の可能性）"

    lines = [header]
    for idx, it in enumerate(items[:TOP_K], 1):
        title = clean_line(it['title'])
        ptime = clean_line(it['post_time'])

        if it.get("no1"):
            left = f"{circled(it['no1'])}" + (f" {it['name1']}" if it.get("name1") else "")
            left += (f"（単勝{it['o1']}）" if it.get("o1") is not None else "")
        else:
            left = "1人気想定" + (f"（単勝{it['o1']}）" if it.get("o1") is not None else "")

        if it.get("no2"):
            right = f"{circled(it['no2'])}" + (f" {it['name2']}" if it.get("name2") else "")
            right += (f"（単勝{it['o2']}）" if it.get("o2") is not None else "")
        else:
            right = "2人気想定" + (f"（単勝{it['o2']}）" if it.get("o2") is not None else "")

        primary_bet = pick_primary_bet(it.get("o1"), it.get("o2"), it.get("headcount"), idx)

        lines.append(f"【{idx}位】")
        lines.append(f"{title} {ptime}".rstrip())
        lines.append(f"◎本命：{left}")
        lines.append(f"○対抗：{right}")
        lines.append(f"推奨券種：{primary_bet}")
        lines.append(f"参考：頭数={it.get('headcount')} / Score={it['score']}")
        if idx < min(len(items), TOP_K):
            lines.append("---")

    return "\n".join(lines)

def send_discord(msg: str, webhook: str):
    webhook=(webhook or "").strip()
    if not webhook:
        print("[Discord] Webhook未設定のためスキップ"); return
    try:
        r=requests.post(webhook, json={"content": f"[ping] {now_jst_str()} - notifier alive"}, timeout=12)
        r.raise_for_status()
    except Exception as e:
        print(f"[Discord] ping失敗: {e}"); return
    for i in range(0,len(msg),DISCORD_CHUNK):
        part=msg[i:i+DISCORD_CHUNK]
        try:
            r=requests.post(webhook, json={"content": part}, timeout=20)
            r.raise_for_status()
        except Exception as e:
            print(f"[Discord] 送信エラー: {e}"); break

def send_line(msg: str, token: str):
    token=(token or "").strip()
    if not token:
        print("[LINE] トークン未設定のためスキップ"); return
    try:
        r=requests.post("https://notify-api.line.me/api/notify",
                        headers={"Authorization": f"Bearer {token}"},
                        data={"message": msg}, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"[LINE] 送信エラー: {e}")

# ---------- 1レース処理（shutuba→yosoの順） ----------
def process_one(session, rid: str):
    try:
        # 1) 馬名を先に確定
        names={}
        sh_html = fetch_shutuba_html(session, rid)
        if sh_html:
            names = parse_shutuba_names(sh_html)

        # 2) yoso から単勝（馬名で突き合わせ）
        yoso_html = fetch_odds_html(session, rid)
        meta = None; odds_map = {}
        if yoso_html:
            meta = parse_meta(yoso_html)
            if names:
                odds_map = parse_yoso_odds_by_name(yoso_html, names)  # {馬番: 単勝}

        # 3) メタがまだないなら shutuba から拾う
        if not meta:
            if not sh_html:
                sh_html = fetch_shutuba_html(session, rid)
            if sh_html:
                meta = parse_meta(sh_html)
            else:
                return None

        # 4) 発走時刻の補完（空なら広域探索）
        if not meta.get("post_time"):
            meta["post_time"] = get_post_time(session, rid) or ""

        # 5) 上位2頭（“馬番ベース”で安全に抽出）
        if odds_map:
            pairs = sorted([(od, no) for no,od in odds_map.items()], key=lambda x: x[0])
            o1,no1 = (pairs[0] if len(pairs)>=1 else (None,None))
            o2,no2 = (pairs[1] if len(pairs)>=2 else (None,None))
            nm1 = names.get(no1); nm2 = names.get(no2)
        else:
            o1=no1=nm1=None; o2=no2=nm2=None

        return {
            "title": meta["title"] if meta else "（開催/レース不明）",
            "post_time": meta.get("post_time") if meta else "",
            "headcount": meta.get("headcount") if meta else None,
            "o1": o1, "o2": o2,
            "no1": no1, "name1": nm1, "no2": no2, "name2": nm2,
            "score": score(o1, o2, meta.get("headcount") if meta else None),
        }
    except Exception as e:
        print(f"[WARN] 解析失敗: {rid} -> {e}")
        return None
    finally:
        time.sleep(SLEEP_SEC)

# ---------- メイン ----------
def main():
    ymd = resolve_target_date()
    print(f"[INFO] 対象日(JRA): {ymd}")
    session = make_session()

    # 1) 一覧収集
    ids = collect_race_ids_from_lists(session, ymd)

    # 2) 少ない/0件ならフォールバック
    if len(ids) < 8:
        add = bruteforce_race_ids(session, ymd)
        print(f"[INFO] フォールバック追加: {len(add)}件")
        ids = list(dict.fromkeys(ids + add))

    if not ids:
        msg = f"{now_jst_str()} 中央競馬 “今日の堅そう”ランキング(最大{TOP_K}件)\n見送り：収集0件 or 解析不可（サイト構造変更の可能性）"
        send_discord(msg, os.getenv("DISCORD_WEBHOOK_URL",""))
        send_line(msg, os.getenv("LINE_NOTIFY_TOKEN",""))
        print("----- 通知本文 -----\n"+msg); return

    # 3) 解析（並列）
    results=[]
    with ThreadPoolExecutor(max_workers=ANALYZE_WORKERS) as ex:
        futs = {ex.submit(process_one, session, rid): rid for rid in ids}
        for fut in as_completed(futs):
            row = fut.result()
            if row: results.append(row)

    # 3.5) 安全レースを優先抽出（的中率モード）
    safe = []
    for it in results:
        # タイトルにJRA場名が含まれない（NARなど混入）は除外
        if not re.search(VENUES, it.get("title","")):
            continue
        if is_low_variance_race(it.get("title",""), it.get("headcount"), it.get("o1"), it.get("o2")):
            safe.append(it)
    if safe:
        results = safe

    # 4) ランキング
    results.sort(key=lambda x: x["score"], reverse=True)
    msg = build_message(results)

    # 5) 通知
    send_discord(msg, os.getenv("DISCORD_WEBHOOK_URL",""))
    send_line(msg, os.getenv("LINE_NOTIFY_TOKEN",""))

    print("----- 通知本文 -----")
    print(msg)

if __name__ == "__main__":
    main()