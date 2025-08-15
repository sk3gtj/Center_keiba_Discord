# -*- coding: utf-8 -*-
"""
中央競馬 予想システム 最小版（モックデータで“堅そう”ランキングを出力）
- そのまま python main.py で動作
- Discord へも投稿可能（環境変数 DISCORD_WEBHOOK_URL を設定すると送信）

次のステップ（本番化）では、ここで使っているモックデータを
実データ取得に差し替えます。
"""
from __future__ import annotations
import os
from datetime import datetime, timezone, timedelta
import textwrap
import requests

# ============== 設定（最小） ==============
MODE = "B"           # "A"=全レース出力 / "B"=堅そうランキングのみ
TOP_N = 5            # ランキング上限件数（Bモード）
USE_CIRCLED = True   # 馬番を①②③…の丸数字にする
TZ = timezone(timedelta(hours=9))  # JST

# “堅い度”の簡易しきい値（あとで調整できます）
FAV_ODDS_MAX = 2.0   # 1番人気の単勝がこの値以下だと堅め
POP_GAP_MIN = 1.8    # 1番人気と2番人気のオッズ差がこの値以上だと堅め
SMALL_FIELD_MAX = 8  # 少頭数ボーナス

# Discord：環境変数にWebhookを入れれば自動投稿
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# ============== モックデータ ==============
# 必要最低限の項目だけ。まずは「雰囲気の正しい出力」を得るのがゴール
MOCK_RACES = [
    # 競馬場, R, 発走時刻, コース, 馬場, 1人気(馬番,馬名,単勝), 2人気(馬番,馬名,単勝), 頭数, 近走安定度(0~1), 騎手厩舎評価(0~1)
    ("札幌", 2, "10:25", "芝1500", "良想定", (1, "サイレントコード", 1.8), (3, "ウインクロッカス", 4.3), 9, 0.78, 0.70),
    ("小倉", 8, "13:45", "ダ1700", "稍想定", (6, "ホープフルスター", 1.9), (4, "ハヤテライン",     5.0), 11, 0.74, 0.62),
    ("新潟", 5, "12:30", "芝1600", "良想定", (1, "スピードスター", 2.0), (3, "グリーンエコー",   3.8), 10, 0.69, 0.66),
    ("新潟", 8, "13:45", "芝1200", "良想定", (6, "ライトニング",   2.0), (3, "フェザー",         5.1), 12, 0.72, 0.60),
    ("札幌", 3, "10:55", "ダ1800", "良想定", (1, "ヴィクトリア",   1.7), (2, "アストライン",     4.6), 9,  0.76, 0.68),
    ("小倉", 2, "10:15", "芝1200", "良想定", (8, "サクラシャイン", 2.8), (5, "ブルースパーク",   4.0), 16, 0.60, 0.58),
]

# ============== ユーティリティ ==============
CIRCLED = {i: ch for i, ch in enumerate("⓪①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳")}
def circled(n: int) -> str:
    if not USE_CIRCLED:
        return str(n)
    # 1~20まで簡単に対応
    return CIRCLED.get(n, str(n))

def jst_now_str() -> str:
    now = datetime.now(TZ)
    return now.strftime("%Y-%m-%d (%a) %H:%M JST")

def score_race(r):
    """
    超簡易スコア：
      - オッズ集中（1人気が低い、1-2人気差が大きい）
      - 近走安定/騎手厩舎評価
      - 少頭数ボーナス
      - 大まかに 0.0 ~ 1.0 ぐらいの値になるように調整
    """
    fav = r[5]  # (num,name,odds)
    sec = r[6]
    headcount = r[7]
    recent = r[8]
    jt = r[9]

    fav_odds = fav[2]
    gap = sec[2] - fav[2]

    # 0~1に寄せる
    odds_term = 0.0
    # 1.2以下→満点に近い、2.5以上→ほぼ0、のような適当な関数（初期）
    odds_term += max(0.0, min(1.0, (2.5 - fav_odds) / (2.5 - 1.0))) * 0.6
    odds_term += max(0.0, min(1.0, (gap) / 3.0)) * 0.4

    size_bonus = 0.15 if headcount <= SMALL_FIELD_MAX else 0.0

    score = (
        0.40 * odds_term +
        0.30 * recent +
        0.20 * jt +
        size_bonus
    )
    # 1を超えないように
    return min(score, 1.0), fav_odds, gap

def pick_recommendation(fav_odds: float, gap: float, recent: float, headcount: int) -> str:
    """推奨券種（最小ルール）"""
    if (fav_odds <= FAV_ODDS_MAX) and (gap >= POP_GAP_MIN):
        return "単勝（1点）"
    if recent >= 0.72 and gap >= 1.2:
        return "馬連（1点）"
    if fav_odds <= 2.2 and headcount <= 10:
        return "三連複1軸（3〜6点）"
    return "ワイド"

def format_line_ranked(rank: int, r, score, fav_odds, gap) -> str:
    place, rn, t, course, going = r[0], r[1], r[2], r[3], r[4]
    fav_num, fav_name, fav_o = r[5]
    sec_num, sec_name, sec_o = r[6]
    headcount, recent, jt = r[7], r[8], r[9]

    body = []
    body.append(f"【{rank}位】{place} {rn}R {t} {course}（{going}）")
    body.append(f"◎{circled(fav_num)} {fav_name}（{fav_o:.1f}） ○{circled(sec_num)} {sec_name}（{sec_o:.1f}）")
    body.append(f"推奨：{pick_recommendation(fav_o, gap, recent, headcount)}")
    reason_bits = []
    if fav_o <= FAV_ODDS_MAX: reason_bits.append("1人気オッズ低め")
    if gap >= POP_GAP_MIN:     reason_bits.append("2人気との差大")
    if headcount <= SMALL_FIELD_MAX: reason_bits.append("少頭数")
    if recent >= 0.72:         reason_bits.append("近走安定")
    if jt >= 0.65:             reason_bits.append("騎手・厩舎◎")
    if reason_bits:
        body.append("理由：" + "、".join(reason_bits))
    body.append(f"（score: {score:.2f}）")
    return "\n".join(body)

def format_header() -> str:
    return f"{jst_now_str()} 中央競馬 “今日の堅そう”ランキング(最大{TOP_N}件)"

def build_output(mode: str = MODE) -> str:
    if mode == "B":
        # スコアリング → 降順ソート → 上位N件
        scored = []
        for r in MOCK_RACES:
            sc, fav_odds, gap = score_race(r)
            scored.append((sc, fav_odds, gap, r))
        scored.sort(key=lambda x: (-x[0], x[1], -x[2], x[3][2]))  # score降順→fav_odds昇順→gap降順→発走時刻
        top = scored[:TOP_N]

        lines = [format_header()]
        if not top:
            lines.append("本日該当なし")
        else:
            for i, (sc, fav_odds, gap, r) in enumerate(top, start=1):
                lines.append(format_line_ranked(i, r, sc, fav_odds, gap))
                lines.append("---")
        return "\n".join(lines).rstrip("-\n")

    # 参考：全レース出力（簡略）。今回はBモードが目的なので最小実装。
    # 必要になったらここを拡充。
    return "（モードA：全レース出力は未実装の簡略版です。必要になれば拡張します）"

def post_discord(message: str) -> None:
    if not DISCORD_WEBHOOK:
        print("※ DISCORD_WEBHOOK_URL が未設定のため、Discord送信はスキップします。")
        return
    # Discordは1メッセージ2000文字制限。長ければ分割
    chunks = []
    current = []
    current_len = 0
    for line in message.splitlines(True):
        if current_len + len(line) > 1900:  # 余裕を見て分割
            chunks.append("".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line)
    if current:
        chunks.append("".join(current))

    for idx, ch in enumerate(chunks, 1):
        payload = {"content": ch}
        resp = requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)
        if resp.status_code >= 300:
            raise RuntimeError(f"Discord送信失敗 {idx}/{len(chunks)}: {resp.status_code} {resp.text}")

def main():
    msg = build_output(MODE)
    print("\n" + msg + "\n")
    if DISCORD_WEBHOOK:
        post_discord(msg)

if __name__ == "__main__":
    main()