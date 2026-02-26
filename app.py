"""
HS / HTS 自動判定ツール
========================
外注スタッフがブラウザで簡単に使える HS/HTS コード判定 Streamlit アプリ。

機能:
  - 商品URL スクレイピングによる情報取得
  - キーワードルールベースの HS コード推定（6桁 → HTS 10桁 / 日本HS 9桁）
  - 最大3候補 + 信頼度表示
  - SQLite 履歴保存・検索・CSV エクスポート
  - 管理者モード（履歴削除・コード手動修正）

技術スタック: Python / Streamlit / SQLite / requests / BeautifulSoup
"""

from typing import Dict, List, Optional, Set, Tuple

import streamlit as st
import streamlit.components.v1 as components
try:
    from streamlit_js_eval import streamlit_js_eval
    _HAS_JS_EVAL = True
except ImportError:
    _HAS_JS_EVAL = False
import sqlite3
import csv
import io
import json
import os
import re
import hashlib
import base64
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

# ============================================================
# 定数
# ============================================================

DB_PATH = "hs_hts_history.db"
ADMIN_PASSWORD_HASH = hashlib.sha256("admin1234".encode()).hexdigest()  # デフォルトPW

CONFIDENCE_LABELS = {"high": "High ✅", "medium": "Medium ⚠️", "low": "Low ❓"}

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

# ============================================================
# 外部コードデータ (JSON) の読み込み
# ============================================================
_DATA_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_json(filename: str) -> dict:
    """データ JSON を読み込む。ファイルが無ければ空 dict を返す。"""
    path = os.path.join(_DATA_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


HTS_CODES = _load_json("hts_codes.json")    # {"Chapter XX": [{"code":..,"description":..}, ...]}
JP_HS_CODES = _load_json("jp_hs_codes.json")  # {"Chapter XX": [{"code":..,"hs6":..,"category":..,"description":..}, ...]}

# ============================================================
# HS コード分類ルールデータベース
# ============================================================
# データソース:
#   HTS: US HTS 2026 Revision 3 (Publication 5705, February 2026)
#   日本HS: 輸出統計品目表 2026年1月版 (税関 customs.go.jp)
# 将来的にベクトル検索や外部 API に差し替え可能な構造
# 各ルール: keywords, category, material, usage, hs6, hts10, jp_hs9, chapter, reason

CLASSIFICATION_RULES: List[Dict] = [
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 61: ニット製衣料品
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["t-shirt", "tシャツ", "ティーシャツ", "tee", "シングレット"],
        "category": "衣料品（ニット）",
        "material": "綿 / ポリエステル",
        "usage": "日常着用",
        "hs6": "6109.10",
        "hts10": "6109.10.0012",
        "jp_hs9": "6109.10.900",
        "chapter": "Chapter 61",
        "reason": "綿製Tシャツ・シングレット類。HTS 6109.10.00 stat.12(男子用綿製)。"
                  "JP: 6109.10-900(その他のもの)。合成繊維製は6109.90。",
    },
    {
        "keywords": ["sweater", "セーター", "ニット", "knit", "pullover", "プルオーバー",
                      "cardigan", "カーディガン", "hoodie", "パーカー", "フーディー", "ベスト", "vest"],
        "category": "衣料品（ニットウェア）",
        "material": "綿 / ウール / アクリル",
        "usage": "日常着用・防寒",
        "hs6": "6110.20",
        "hts10": "6110.20.2075",
        "jp_hs9": "6110.20.000",
        "chapter": "Chapter 61",
        "reason": "ジャージー、プルオーバー、カーディガン等（メリヤス編み）。"
                  "綿製=6110.20、羊毛製=6110.11、合成繊維製=6110.30。",
    },
    {
        "keywords": ["underwear", "下着", "ランジェリー", "lingerie", "ブラ", "bra",
                      "パンティ", "panty", "ボクサー", "boxer", "ブリーフ", "brief"],
        "category": "衣料品（下着）",
        "material": "綿 / ナイロン",
        "usage": "着用",
        "hs6": "6108.21",
        "hts10": "6108.21.0010",
        "jp_hs9": "6108.21.000",
        "chapter": "Chapter 61",
        "reason": "女子用ブリーフ・パンティ（メリヤス編み）。"
                  "綿製=6108.21、人造繊維製=6108.22。男子用は6107。",
    },
    {
        "keywords": ["socks", "靴下", "ソックス", "stocking", "ストッキング", "タイツ", "tights"],
        "category": "衣料品（靴下類）",
        "material": "綿 / ナイロン",
        "usage": "着用",
        "hs6": "6115.95",
        "hts10": "6115.95.9010",
        "jp_hs9": "6115.95.000",
        "chapter": "Chapter 61",
        "reason": "靴下類（メリヤス編み）。綿製=6115.95、合成繊維製=6115.96。"
                  "パンティストッキング=6115.21/22。",
    },
    {
        "keywords": ["swimwear", "水着", "ビキニ", "bikini", "swim"],
        "category": "衣料品（水着）",
        "material": "ナイロン / ポリエステル",
        "usage": "水泳・レジャー",
        "hs6": "6112.41",
        "hts10": "6112.41.0010",
        "jp_hs9": "6112.41.000",
        "chapter": "Chapter 61",
        "reason": "女子用水着（メリヤス編み）。合成繊維製=6112.41。"
                  "男子用=6112.31。トラックスーツ=6112.11/12。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 62: 織物製衣料品
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["shirt", "シャツ", "ブラウス", "blouse", "dress shirt", "ワイシャツ"],
        "category": "衣料品（織物）",
        "material": "綿 / ポリエステル / 混紡",
        "usage": "日常着用・ビジネス",
        "hs6": "6205.20",
        "hts10": "6205.20.2016",
        "jp_hs9": "6205.20.000",
        "chapter": "Chapter 62",
        "reason": "男子用シャツ（織物製）。綿製=6205.20、人造繊維製=6205.30。"
                  "JP: 6205.20-000。",
    },
    {
        "keywords": ["jacket", "ジャケット", "ブレザー", "blazer", "coat", "コート", "アウター",
                      "アノラック", "anorak", "ウインドブレーカー", "windbreaker"],
        "category": "衣料品（アウター）",
        "material": "綿 / ウール / 化繊",
        "usage": "外出・防寒",
        "hs6": "6201.40",
        "hts10": "6201.40.2010",
        "jp_hs9": "6201.40.000",
        "chapter": "Chapter 62",
        "reason": "男子用オーバーコート・アノラック類（織物製）。"
                  "人造繊維製=6201.40、綿製=6201.30、羊毛製=6201.20。",
    },
    {
        "keywords": ["dress", "ドレス", "ワンピース", "one-piece"],
        "category": "衣料品（婦人）",
        "material": "ポリエステル / 綿",
        "usage": "日常着用・フォーマル",
        "hs6": "6204.43",
        "hts10": "6204.43.4020",
        "jp_hs9": "6204.43.000",
        "chapter": "Chapter 62",
        "reason": "女子用ドレス（織物製）。合成繊維製=6204.43、綿製=6204.42。"
                  "スーツ=6204.11-19、スカート=6204.51-59。",
    },
    {
        "keywords": ["pants", "パンツ", "ズボン", "trousers", "jeans", "ジーンズ", "デニム", "denim",
                      "チノパン", "chinos", "スラックス", "slacks"],
        "category": "衣料品（ボトムス）",
        "material": "綿 / デニム",
        "usage": "日常着用",
        "hs6": "6203.42",
        "hts10": "6203.42.4011",
        "jp_hs9": "6203.42.000",
        "chapter": "Chapter 62",
        "reason": "男子用ズボン・ショーツ（織物製）。綿製=6203.42、合成繊維製=6203.43。"
                  "女子用=6204.62/63。",
    },
    {
        "keywords": ["skirt", "スカート", "キュロット"],
        "category": "衣料品（婦人ボトムス）",
        "material": "ポリエステル / 綿",
        "usage": "日常着用",
        "hs6": "6204.52",
        "hts10": "6204.52.2060",
        "jp_hs9": "6204.52.000",
        "chapter": "Chapter 62",
        "reason": "女子用スカート（織物製）。綿製=6204.52、合成繊維製=6204.53。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 42: バッグ・革製品
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["bag", "バッグ", "カバン", "鞄", "handbag", "ハンドバッグ", "tote", "トート",
                      "shoulder bag", "ショルダー"],
        "category": "バッグ・鞄",
        "material": "革 / 合成皮革 / ナイロン",
        "usage": "携行・収納",
        "hs6": "4202.21",
        "hts10": "4202.21.6000",
        "jp_hs9": "4202.21.000",
        "chapter": "Chapter 42",
        "reason": "ハンドバッグ類。外面が革製=4202.21、プラスチックシート製又は紡織用繊維製=4202.22。"
                  "JP: 4202.21-000(外面が革製又はコンポジションレザー製)。",
    },
    {
        "keywords": ["wallet", "財布", "ウォレット", "purse", "長財布", "二つ折り", "コインケース"],
        "category": "革小物",
        "material": "革 / 合成皮革",
        "usage": "金銭・カード収納",
        "hs6": "4202.31",
        "hts10": "4202.31.6000",
        "jp_hs9": "4202.31.000",
        "chapter": "Chapter 42",
        "reason": "ポケット又はハンドバッグに携帯する製品。革製=4202.31、"
                  "プラスチック・繊維製=4202.32。",
    },
    {
        "keywords": ["backpack", "リュック", "バックパック", "rucksack", "デイパック"],
        "category": "バッグ（リュック）",
        "material": "ナイロン / ポリエステル",
        "usage": "携行・通勤通学",
        "hs6": "4202.92",
        "hts10": "4202.92.3031",
        "jp_hs9": "4202.92.000",
        "chapter": "Chapter 42",
        "reason": "外面がプラスチックシート製又は紡織用繊維製のバッグ類=4202.92。"
                  "JP: 4202.92-000。",
    },
    {
        "keywords": ["suitcase", "スーツケース", "キャリー", "luggage", "トランク", "carry-on"],
        "category": "旅行用鞄",
        "material": "プラスチック / 金属",
        "usage": "旅行・出張",
        "hs6": "4202.12",
        "hts10": "4202.12.8070",
        "jp_hs9": "4202.12.000",
        "chapter": "Chapter 42",
        "reason": "トランク・スーツケース類。外面がプラスチック又は紡織用繊維製=4202.12、"
                  "革製=4202.11。",
    },
    {
        "keywords": ["phone case", "スマホケース", "ケース", "case", "カバー", "cover", "iphone case"],
        "category": "ケース・カバー",
        "material": "プラスチック / シリコン / 革",
        "usage": "機器保護",
        "hs6": "4202.99",
        "hts10": "4202.99.9000",
        "jp_hs9": "4202.99.000",
        "chapter": "Chapter 42",
        "reason": "携帯電話ケース等の容器。その他の材料=4202.99。"
                  "革製=4202.91、プラスチック・繊維製=4202.92。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 64: 履物
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["shoes", "靴", "シューズ", "sneakers", "スニーカー", "footwear",
                      "ブーツ", "boots", "サンダル", "sandals", "トレーニングシューズ",
                      "running shoes", "ランニングシューズ", "air max", "ultraboost",
                      "jordan", "yeezy", "dunk", "chuck taylor", "all star",
                      "rs-x", "gel-", "574", "990", "993", "2002r",
                      "air force", "stan smith", "superstar", "old skool",
                      "classic leather", "club c", "suede",
                      "new balance", "asics", "skechers", "crocs",
                      "birkenstock", "timberland", "dr. martens", "ugg"],
        "category": "履物",
        "material": "ゴム / 合成素材 / 紡織用繊維",
        "usage": "着用・歩行",
        "hs6": "6404.11",
        "hts10": "6404.11.9020",
        "jp_hs9": "6404.11.000",
        "chapter": "Chapter 64",
        "reason": "本底がゴム製又はプラスチック製、甲が紡織用繊維のスポーツ用履物=6404.11。"
                  "JP: スポーツ用の履物及びテニスシューズ等。その他=6404.19。",
    },
    {
        "keywords": ["leather shoes", "革靴", "ローファー", "loafer", "オックスフォード", "oxford",
                      "パンプス", "pumps", "ドレスシューズ"],
        "category": "履物（革）",
        "material": "革",
        "usage": "ビジネス・フォーマル",
        "hs6": "6403.59",
        "hts10": "6403.59.9060",
        "jp_hs9": "6403.59.000",
        "chapter": "Chapter 64",
        "reason": "本底がゴム・プラスチック製、甲が革製の靴=6403.59(くるぶしを覆わないもの)。"
                  "くるぶしを覆うもの=6403.51/91。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 84-85: 電子機器・電気機器
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["phone", "smartphone", "スマホ", "スマートフォン", "携帯", "mobile",
                      "iphone", "android", "galaxy", "pixel"],
        "category": "電子機器（通信）",
        "material": "電子部品 / ガラス / 金属",
        "usage": "通信・情報処理",
        "hs6": "8517.13",
        "hts10": "8517.13.0000",
        "jp_hs9": "8517.13.000",
        "chapter": "Chapter 85",
        "reason": "スマートフォン=8517.13。HTS 8517.13.00 stat.00。"
                  "JP: 8517.13-000(スマートフォン)。その他の携帯電話=8517.14。",
    },
    {
        "keywords": ["earphone", "イヤホン", "headphone", "ヘッドホン", "earbuds",
                      "airpods", "ワイヤレスイヤホン"],
        "category": "電子機器（音響）",
        "material": "プラスチック / 金属",
        "usage": "音声再生",
        "hs6": "8518.30",
        "hts10": "8518.30.2000",
        "jp_hs9": "8518.30.900",
        "chapter": "Chapter 85",
        "reason": "ヘッドホン・イヤホン=8518.30。HTS 8518.30.20 stat.00。"
                  "JP: 8518.30-100(ヘッドホン)、8518.30-900(その他=イヤホン等)。",
    },
    {
        "keywords": ["laptop", "ノートパソコン", "notebook pc", "ノートPC", "macbook", "chromebook"],
        "category": "電子機器（PC）",
        "material": "電子部品 / 金属 / プラスチック",
        "usage": "情報処理",
        "hs6": "8471.30",
        "hts10": "8471.30.0100",
        "jp_hs9": "8471.30.000",
        "chapter": "Chapter 84",
        "reason": "携帯用自動データ処理機械（10kg以下、CPU・キーボード・ディスプレイ内蔵）"
                  "=8471.30。HTS 8471.30.01 stat.00。",
    },
    {
        "keywords": ["tablet", "タブレット", "ipad"],
        "category": "電子機器（タブレット）",
        "material": "電子部品 / ガラス / 金属",
        "usage": "情報処理・閲覧",
        "hs6": "8471.30",
        "hts10": "8471.30.0100",
        "jp_hs9": "8471.30.000",
        "chapter": "Chapter 84",
        "reason": "タブレット端末は携帯用自動データ処理機械として8471.30。"
                  "デスクトップPC=8471.41/49。",
    },
    {
        "keywords": ["camera", "カメラ", "デジカメ", "digital camera", "一眼", "ミラーレス"],
        "category": "電子機器（光学）",
        "material": "金属 / ガラス / 電子部品",
        "usage": "撮影",
        "hs6": "8525.81",
        "hts10": "8525.81.0040",
        "jp_hs9": "8525.81.000",
        "chapter": "Chapter 85",
        "reason": "テレビジョンカメラ、デジタルカメラ、ビデオカメラレコーダー=8525.81。",
    },
    {
        "keywords": ["charger", "充電器", "adapter", "アダプター", "power supply", "電源", "ACアダプター"],
        "category": "電子機器（電源）",
        "material": "プラスチック / 金属",
        "usage": "充電・給電",
        "hs6": "8504.40",
        "hts10": "8504.40.8500",
        "jp_hs9": "8504.40.900",
        "chapter": "Chapter 85",
        "reason": "スタティックコンバーター=8504.40。HTS: 8504.40.85(通信機器用)。"
                  "ADP機器用電源=8504.40.60/70。JP: 8504.40-900(その他)。",
    },
    {
        "keywords": ["battery", "バッテリー", "電池", "リチウム", "lithium", "蓄電池",
                      "リチウムイオン", "モバイルバッテリー"],
        "category": "電子機器（蓄電池）",
        "material": "リチウム / 金属",
        "usage": "蓄電・給電",
        "hs6": "8507.60",
        "hts10": "8507.60.0090",
        "jp_hs9": "8507.60.000",
        "chapter": "Chapter 85",
        "reason": "リチウムイオン蓄電池=8507.60。HTS 8507.60.00 stat.90(一般消費者向け)。"
                  "EV用=stat.10。ニッケル水素=8507.50。",
    },
    {
        "keywords": ["speaker", "スピーカー", "bluetooth speaker", "ポータブルスピーカー"],
        "category": "電子機器（音響）",
        "material": "プラスチック / 金属",
        "usage": "音声再生",
        "hs6": "8518.22",
        "hts10": "8518.22.0000",
        "jp_hs9": "8518.22.000",
        "chapter": "Chapter 85",
        "reason": "複数型拡声器（同一エンクロージャー）=8518.22。"
                  "単一型=8518.21。JP: 8518.22-000。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 71: ジュエリー / Chapter 91: 時計
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["jewelry", "ジュエリー", "アクセサリー", "necklace", "ネックレス", "ring", "指輪",
                      "リング", "bracelet", "ブレスレット", "earring", "ピアス", "イヤリング",
                      "pendant", "ペンダント"],
        "category": "宝飾品・アクセサリー",
        "material": "貴金属 / 宝石 / 合金",
        "usage": "装飾・着用",
        "hs6": "7117.19",
        "hts10": "7117.19.9000",
        "jp_hs9": "7117.19.000",
        "chapter": "Chapter 71",
        "reason": "身辺用模造細貨類（卑金属製）=7117.19。カフスボタン=7117.11。"
                  "貴金属製の身辺用細貨類は7113。",
    },
    {
        "keywords": ["watch", "時計", "腕時計", "ウォッチ", "wristwatch", "スマートウォッチ", "smartwatch"],
        "category": "時計",
        "material": "金属 / ガラス / プラスチック",
        "usage": "時刻確認・装飾",
        "hs6": "9102.12",
        "hts10": "9102.12.8040",
        "jp_hs9": "9102.12.000",
        "chapter": "Chapter 91",
        "reason": "電子式腕時計。オプトエレクトロニクス表示部のみ=9102.12(デジタル/スマートウォッチ)、"
                  "機械式表示部のみ=9102.11(アナログ)。非電子式=9102.21/29。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 95: 玩具・スポーツ
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["toy", "おもちゃ", "玩具", "ぬいぐるみ", "stuffed", "plush",
                      "フィギュア", "figure", "人形", "doll", "レゴ", "lego"],
        "category": "玩具",
        "material": "プラスチック / 布 / 金属",
        "usage": "遊戯・収集",
        "hs6": "9503.00",
        "hts10": "9503.00.0080",
        "jp_hs9": "9503.00.000",
        "chapter": "Chapter 95",
        "reason": "三輪車、人形、その他の玩具、縮尺模型及びパズル=9503.00。"
                  "JP: 9503.00-000。",
    },
    {
        "keywords": ["golf", "ゴルフ", "tennis", "テニス", "racket", "ラケット",
                      "sports equipment", "スポーツ用品", "トレーニング", "training",
                      "ダンベル", "dumbbell", "ヨガ", "yoga"],
        "category": "スポーツ用品",
        "material": "金属 / カーボン / ゴム",
        "usage": "スポーツ・運動",
        "hs6": "9506.91",
        "hts10": "9506.91.0030",
        "jp_hs9": "9506.91.000",
        "chapter": "Chapter 95",
        "reason": "身体トレーニング用具、体操用具及び競技用具=9506.91。"
                  "ゴルフクラブ=9506.31、テニスラケット=9506.51。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 94: 家具・照明
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["chair", "椅子", "チェア", "sofa", "ソファ", "stool", "スツール", "座椅子"],
        "category": "家具（座るもの）",
        "material": "木 / 金属 / 布",
        "usage": "着座",
        "hs6": "9401.61",
        "hts10": "9401.61.6011",
        "jp_hs9": "9401.61.000",
        "chapter": "Chapter 94",
        "reason": "木製フレーム・アップホルスターの腰掛け=9401.61。"
                  "金属フレーム=9401.71。JP: 9401.61-000。",
    },
    {
        "keywords": ["table", "テーブル", "desk", "デスク", "机", "ダイニングテーブル"],
        "category": "家具（テーブル）",
        "material": "木 / 金属",
        "usage": "作業・食事",
        "hs6": "9403.60",
        "hts10": "9403.60.8081",
        "jp_hs9": "9403.60.000",
        "chapter": "Chapter 94",
        "reason": "その他の木製家具=9403.60(テーブル・デスク含む)。"
                  "事務所用木製家具=9403.30、寝室用=9403.50。",
    },
    {
        "keywords": ["bed", "ベッド", "mattress", "マットレス", "布団", "futon"],
        "category": "家具（寝具）",
        "material": "木 / 金属 / ウレタン",
        "usage": "睡眠",
        "hs6": "9404.21",
        "hts10": "9404.21.0010",
        "jp_hs9": "9404.21.000",
        "chapter": "Chapter 94",
        "reason": "マットレス（セルラーラバー製又は多泡性プラスチック製）=9404.21。"
                  "その他材料製=9404.29。布団・ベッドスプレッド=9404.40。",
    },
    {
        "keywords": ["lamp", "ランプ", "照明", "light", "ライト", "chandelier", "シャンデリア", "LED"],
        "category": "照明器具",
        "material": "金属 / ガラス / プラスチック",
        "usage": "照明",
        "hs6": "9405.11",
        "hts10": "9405.11.4010",
        "jp_hs9": "9405.11.000",
        "chapter": "Chapter 94",
        "reason": "LED光源用天井・壁掛け照明器具=9405.11。"
                  "卓上・床置き=9405.21/29。非電気式=9405.50。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 33: 化粧品・香水
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["cosmetics", "化粧品", "makeup", "メイク", "ファンデーション", "foundation",
                      "lipstick", "口紅", "リップ", "アイシャドウ", "eyeshadow", "マスカラ", "mascara"],
        "category": "化粧品",
        "material": "化学原料 / 顔料",
        "usage": "美容・メイクアップ",
        "hs6": "3304.10",
        "hts10": "3304.10.0000",
        "jp_hs9": "3304.10.000",
        "chapter": "Chapter 33",
        "reason": "唇のメーキャップ用調製品=3304.10。眼のメーキャップ=3304.20。"
                  "パウダー=3304.91。マニキュア=3304.30。",
    },
    {
        "keywords": ["perfume", "香水", "フレグランス", "fragrance", "cologne", "コロン",
                      "eau de toilette", "eau de parfum", "parfum", "edp", "edt"],
        "category": "香水・フレグランス",
        "material": "アルコール / 香料",
        "usage": "芳香",
        "hs6": "3303.00",
        "hts10": "3303.00.3000",
        "jp_hs9": "3303.00.000",
        "chapter": "Chapter 33",
        "reason": "香水類及びオーデコロン類=3303.00。JP: 3303.00-000。",
    },
    {
        "keywords": ["skincare", "スキンケア", "cream", "クリーム", "lotion", "ローション",
                      "serum", "美容液", "moisturizer", "保湿", "sunscreen", "日焼け止め",
                      "化粧水", "乳液"],
        "category": "スキンケア製品",
        "material": "化学原料 / 植物エキス",
        "usage": "肌の手入れ",
        "hs6": "3304.99",
        "hts10": "3304.99.5000",
        "jp_hs9": "3304.99.900",
        "chapter": "Chapter 33",
        "reason": "皮膚の手入れ用調製品（その他）=3304.99。"
                  "JP: 化粧下=3304.99-100、クリーム=3304.99-200、その他=3304.99-900。",
    },
    {
        "keywords": ["shampoo", "シャンプー", "conditioner", "コンディショナー", "hair care", "ヘアケア",
                      "hair oil", "ヘアオイル", "トリートメント", "treatment"],
        "category": "ヘアケア製品",
        "material": "界面活性剤 / 化学原料",
        "usage": "頭髪の手入れ",
        "hs6": "3305.10",
        "hts10": "3305.10.0000",
        "jp_hs9": "3305.10.000",
        "chapter": "Chapter 33",
        "reason": "シャンプー=3305.10。ヘアラッカー=3305.30。"
                  "その他の頭髪用調製品=3305.90。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 34: 石鹸・洗剤・ろうそく
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["soap", "石鹸", "ソープ", "hand wash", "ハンドソープ", "ボディソープ", "body wash"],
        "category": "石鹸・洗浄剤",
        "material": "界面活性剤 / 油脂",
        "usage": "洗浄",
        "hs6": "3401.30",
        "hts10": "3401.30.5000",
        "jp_hs9": "3401.30.000",
        "chapter": "Chapter 34",
        "reason": "有機界面活性剤及びその調製品（皮膚の洗浄に使用する液状・クリーム状のもの）"
                  "=3401.30。固形石鹸（化粧用）=3401.11。",
    },
    {
        "keywords": ["candle", "キャンドル", "ろうそく", "蝋燭", "アロマキャンドル"],
        "category": "ろうそく・キャンドル",
        "material": "ワックス / パラフィン",
        "usage": "照明・芳香",
        "hs6": "3406.00",
        "hts10": "3406.00.0000",
        "jp_hs9": "3406.00.000",
        "chapter": "Chapter 34",
        "reason": "ろうそく及びこれに類する物品=3406.00。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 39: プラスチック製品
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["plastic container", "プラスチック容器", "タッパー", "保存容器", "food container"],
        "category": "プラスチック製品",
        "material": "プラスチック",
        "usage": "食品保存・収納",
        "hs6": "3924.10",
        "hts10": "3924.10.4000",
        "jp_hs9": "3924.10.000",
        "chapter": "Chapter 39",
        "reason": "プラスチック製の食卓用品及び台所用品=3924.10。"
                  "その他の家庭用品=3924.90。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 73: 鉄鋼製品
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["stainless", "ステンレス", "水筒", "tumbler", "タンブラー", "flask", "魔法瓶", "thermos"],
        "category": "金属製容器",
        "material": "ステンレス鋼",
        "usage": "飲料携行・保温",
        "hs6": "7323.93",
        "hts10": "7323.93.0045",
        "jp_hs9": "7323.93.000",
        "chapter": "Chapter 73",
        "reason": "ステンレス鋼製の食卓用品、台所用品等=7323.93。"
                  "鋳鉄製=7323.91/92。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 63: 繊維製品（その他）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["towel", "タオル", "バスタオル", "ハンドタオル"],
        "category": "繊維製品（タオル）",
        "material": "綿",
        "usage": "清拭・乾燥",
        "hs6": "6302.60",
        "hts10": "6302.60.0020",
        "jp_hs9": "6302.60.000",
        "chapter": "Chapter 63",
        "reason": "トイレットリネン及びキッチンリネン（テリータオル地、綿製）=6302.60。",
    },
    {
        "keywords": ["blanket", "ブランケット", "毛布"],
        "category": "繊維製品（毛布）",
        "material": "ポリエステル / ウール",
        "usage": "防寒・寝具",
        "hs6": "6301.40",
        "hts10": "6301.40.0020",
        "jp_hs9": "6301.40.000",
        "chapter": "Chapter 63",
        "reason": "合成繊維製の毛布=6301.40。綿製=6301.30。羊毛製=6301.20。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 69: 陶磁器
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["ceramic", "陶器", "磁器", "cup", "カップ", "mug", "マグカップ",
                      "pottery", "食器", "plate", "皿", "bowl", "ボウル"],
        "category": "陶磁器",
        "material": "磁器 / 陶器",
        "usage": "食事・装飾",
        "hs6": "6912.00",
        "hts10": "6912.00.4500",
        "jp_hs9": "6912.00.000",
        "chapter": "Chapter 69",
        "reason": "陶磁製の食卓用品、台所用品等（磁器製を除く）=6912.00。"
                  "磁器製=6911.10。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 49: 書籍・印刷物
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["book", "本", "書籍", "マンガ", "manga", "comic", "雑誌", "magazine"],
        "category": "書籍・印刷物",
        "material": "紙",
        "usage": "閲覧・学習",
        "hs6": "4901.99",
        "hts10": "4901.99.0092",
        "jp_hs9": "4901.99.000",
        "chapter": "Chapter 49",
        "reason": "印刷された書籍（その他）=4901.99。辞典・事典=4901.91。"
                  "単一シート=4901.10。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 09: コーヒー・茶 / Chapter 18: チョコレート / Chapter 21: 調製食料品
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["coffee", "コーヒー", "珈琲", "espresso", "カフェ"],
        "category": "飲料（コーヒー）",
        "material": "コーヒー豆",
        "usage": "飲用",
        "hs6": "0901.21",
        "hts10": "0901.21.0045",
        "jp_hs9": "0901.21.000",
        "chapter": "Chapter 09",
        "reason": "焙煎コーヒー（カフェイン除去なし）=0901.21。"
                  "カフェイン除去済み=0901.22。生豆=0901.11。",
    },
    {
        "keywords": ["tea", "茶", "お茶", "green tea", "緑茶", "紅茶", "black tea", "抹茶", "matcha"],
        "category": "飲料（茶）",
        "material": "茶葉",
        "usage": "飲用",
        "hs6": "0902.10",
        "hts10": "0902.10.1010",
        "jp_hs9": "0902.10.900",
        "chapter": "Chapter 09",
        "reason": "緑茶（3kg以下の包装）=0902.10。JP: 粉末状=0902.10-100、"
                  "その他=0902.10-900。紅茶=0902.30/40。",
    },
    {
        "keywords": ["chocolate", "チョコ", "チョコレート", "cacao", "カカオ", "cocoa"],
        "category": "食品（チョコレート）",
        "material": "カカオ / 砂糖",
        "usage": "食用",
        "hs6": "1806.31",
        "hts10": "1806.31.0040",
        "jp_hs9": "1806.31.000",
        "chapter": "Chapter 18",
        "reason": "チョコレート菓子（詰物をしたもの）=1806.31。"
                  "詰物なし=1806.32。その他チョコレート製品=1806.90。",
    },
    {
        "keywords": ["supplement", "サプリ", "サプリメント", "vitamin", "ビタミン",
                      "プロテイン", "protein", "ミネラル", "アミノ酸"],
        "category": "栄養補助食品",
        "material": "各種栄養素",
        "usage": "健康維持",
        "hs6": "2106.90",
        "hts10": "2106.90.9998",
        "jp_hs9": "2106.90.300",
        "chapter": "Chapter 21",
        "reason": "調製食料品（その他）=2106.90。JP: ビタミン・ミネラル・アミノ酸等をもととした"
                  "栄養補助食品=2106.90-300。豆腐=2106.90-200。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 65: 帽子 / Chapter 66: 傘
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["hat", "帽子", "cap", "キャップ", "ベレー帽", "beret", "ニット帽", "beanie"],
        "category": "帽子類",
        "material": "綿 / ポリエステル / ウール",
        "usage": "着用・日除け",
        "hs6": "6505.00",
        "hts10": "6505.00.8015",
        "jp_hs9": "6505.00.000",
        "chapter": "Chapter 65",
        "reason": "メリヤス編み又はクロセ編みの帽子、ヘアネット等=6505.00。"
                  "フェルト帽子=6505に含む。",
    },
    {
        "keywords": ["umbrella", "傘", "日傘", "折り畳み傘", "parasol", "折りたたみ傘"],
        "category": "傘",
        "material": "金属 / ポリエステル",
        "usage": "防雨・日除け",
        "hs6": "6601.99",
        "hts10": "6601.99.0000",
        "jp_hs9": "6601.99.000",
        "chapter": "Chapter 66",
        "reason": "傘（その他）=6601.99。折畳み式=6601.91。ビーチパラソル=6601.10。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 90: 光学機器 / Chapter 87: 自動車部品
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["glasses", "メガネ", "眼鏡", "サングラス", "sunglasses"],
        "category": "光学機器（眼鏡）",
        "material": "プラスチック / ガラス / 金属",
        "usage": "視力矯正・遮光",
        "hs6": "9004.10",
        "hts10": "9004.10.0000",
        "jp_hs9": "9004.10.000",
        "chapter": "Chapter 90",
        "reason": "サングラス=9004.10。その他の眼鏡（矯正用等）=9004.90。",
    },
    {
        "keywords": ["car parts", "自動車部品", "カーパーツ", "タイヤ", "tire", "wheel", "ホイール"],
        "category": "自動車部品",
        "material": "金属 / ゴム",
        "usage": "自動車整備",
        "hs6": "8708.99",
        "hts10": "8708.99.8180",
        "jp_hs9": "8708.99.900",
        "chapter": "Chapter 87",
        "reason": "自動車部品（その他）=8708.99。JP: 車輪式トラクター用=8708.99-100、"
                  "その他=8708.99-900。バンパー=8708.10、ブレーキ=8708.30。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 44: 木製品 / Chapter 48: 紙製品
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["wooden", "木製", "cutting board", "まな板", "木箱", "wood"],
        "category": "木製品",
        "material": "木材",
        "usage": "家庭用・調理",
        "hs6": "4419.19",
        "hts10": "4419.19.0000",
        "jp_hs9": "4419.19.090",
        "chapter": "Chapter 44",
        "reason": "木製の食卓用品及び台所用品=4419。竹製まな板=4419.11。"
                  "JP: 漆塗り=4419.19-010、その他=4419.19-090。",
    },
    {
        "keywords": ["paper", "紙", "ノート", "notebook", "メモ帳", "便箋", "封筒", "envelope",
                      "ティッシュ", "tissue", "トイレットペーパー", "toilet paper"],
        "category": "紙製品",
        "material": "紙・パルプ",
        "usage": "筆記・衛生",
        "hs6": "4820.10",
        "hts10": "4820.10.2020",
        "jp_hs9": "4820.10.000",
        "chapter": "Chapter 48",
        "reason": "帳簿、会計簿、雑記帳、注文帳、便せん、メモ帳等=4820.10。"
                  "練習帳=4820.20。バインダー=4820.30。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 82: 工具・刃物 / Chapter 96: 雑品
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["knife", "ナイフ", "包丁", "kitchen knife", "刃物", "はさみ", "scissors",
                      "工具", "tool", "ドライバー", "screwdriver", "wrench", "レンチ"],
        "category": "工具・刃物",
        "material": "鋼 / ステンレス",
        "usage": "切断・作業",
        "hs6": "8211.91",
        "hts10": "8211.91.8060",
        "jp_hs9": "8211.91.000",
        "chapter": "Chapter 82",
        "reason": "テーブルナイフ（固定刃）=8211.91。その他のナイフ（固定刃）=8211.92。"
                  "折り畳みナイフ=8211.93。詰合せセット=8211.10。",
    },
    {
        "keywords": ["toothbrush", "歯ブラシ", "brush", "ブラシ", "ヘアブラシ"],
        "category": "ブラシ類",
        "material": "プラスチック / ナイロン",
        "usage": "清掃・衛生",
        "hs6": "9603.21",
        "hts10": "9603.21.0000",
        "jp_hs9": "9603.21.000",
        "chapter": "Chapter 96",
        "reason": "歯ブラシ（義歯用ブラシを含む）=9603.21。HTS: 2¢ each + Free(特恵)。"
                  "その他の身体用ブラシ=9603.29。",
    },
    {
        "keywords": ["pen", "ペン", "ボールペン", "ballpoint", "万年筆", "fountain pen",
                      "鉛筆", "pencil", "シャープペンシル", "マーカー", "marker"],
        "category": "筆記具",
        "material": "プラスチック / 金属",
        "usage": "筆記",
        "hs6": "9608.10",
        "hts10": "9608.10.0000",
        "jp_hs9": "9608.10.900",
        "chapter": "Chapter 96",
        "reason": "ボールペン=9608.10。JP: 油性=9608.10-100、その他=9608.10-900。"
                  "フェルトペン=9608.20。万年筆=9608.30。シャープペンシル=9608.40。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 92: 楽器
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["guitar", "ギター", "bass guitar", "ベースギター", "acoustic guitar",
                      "electric guitar", "ukulele", "ウクレレ", "banjo", "mandolin"],
        "category": "弦楽器",
        "material": "木材 / 金属弦",
        "usage": "演奏",
        "hs6": "9202.90",
        "hts10": "9202.90.2000",
        "jp_hs9": "9202.90.000",
        "chapter": "Chapter 92",
        "reason": "その他の弦楽器=9202.90。ギター=9202.90。"
                  "JP: 9202.90-000。バイオリン=9202.10。",
    },
    {
        "keywords": ["piano", "ピアノ", "keyboard", "キーボード", "organ", "オルガン",
                      "synthesizer", "シンセサイザー", "accordion", "アコーディオン"],
        "category": "鍵盤楽器",
        "material": "木材 / 金属 / プラスチック",
        "usage": "演奏",
        "hs6": "9201.10",
        "hts10": "9201.10.0000",
        "jp_hs9": "9201.10.000",
        "chapter": "Chapter 92",
        "reason": "アップライトピアノ=9201.10。グランドピアノ=9201.20。"
                  "電子ピアノ・キーボード=9207。JP: 9201.10-000。",
    },
    {
        "keywords": ["drum", "ドラム", "percussion", "パーカッション", "cymbal", "シンバル",
                      "snare", "tambourine", "タンバリン", "cajon", "カホン"],
        "category": "打楽器",
        "material": "木材 / 金属 / 皮革",
        "usage": "演奏",
        "hs6": "9206.00",
        "hts10": "9206.00.4000",
        "jp_hs9": "9206.00.000",
        "chapter": "Chapter 92",
        "reason": "打楽器=9206.00。ドラムセット、シンバル等。"
                  "JP: 9206.00-000。",
    },
    {
        "keywords": ["trumpet", "トランペット", "saxophone", "サクソフォン", "flute", "フルート",
                      "clarinet", "クラリネット", "trombone", "トロンボーン", "harmonica", "ハーモニカ",
                      "violin", "バイオリン", "cello", "チェロ", "viola"],
        "category": "管楽器・弦楽器",
        "material": "金属 / 木材",
        "usage": "演奏",
        "hs6": "9205.90",
        "hts10": "9205.90.4000",
        "jp_hs9": "9205.90.000",
        "chapter": "Chapter 92",
        "reason": "その他の管楽器=9205.90。トランペット=9205.10。フルート=9205.10。"
                  "バイオリン=9202.10。チェロ=9202.10。JP: 9205.90-000。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 57: じゅうたん
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["rug", "ラグ", "carpet", "カーペット", "じゅうたん", "絨毯",
                      "area rug", "runner", "mat", "マット", "persian rug", "kilim",
                      "tapestry", "タペストリー"],
        "category": "じゅうたん・床敷物",
        "material": "羊毛 / 合成繊維 / 綿",
        "usage": "床敷・装飾",
        "hs6": "5703.30",
        "hts10": "5703.30.2010",
        "jp_hs9": "5703.30.000",
        "chapter": "Chapter 57",
        "reason": "タフテッドじゅうたん・床用敷物（ナイロン製）=5703.20、その他合成繊維=5703.30。"
                  "手織り=5701、結びパイル=5701.10。JP: 5703.30-000。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 88: 航空機・宇宙機 (ドローン含む)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["drone", "ドローン", "quadcopter", "マルチコプター", "UAV",
                      "unmanned aerial", "camera drone", "racing drone", "fpv drone"],
        "category": "無人航空機（ドローン）",
        "material": "プラスチック / カーボン / 金属",
        "usage": "空撮・レース・産業用",
        "hs6": "8806.10",
        "hts10": "8806.10.0000",
        "jp_hs9": "8806.10.000",
        "chapter": "Chapter 88",
        "reason": "無人航空機=8806.10（最大離陸重量250g以下）。250g超2kg以下=8806.21。"
                  "2kg超25kg以下=8806.22。25kg超150kg以下=8806.23。150kg超=8806.24。"
                  "JP: 8806.10-000。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 30: 医薬品
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["medicine", "医薬品", "pharmaceutical", "tablet", "capsule",
                      "supplement", "サプリメント", "vitamin", "ビタミン"],
        "category": "医薬品・サプリメント",
        "material": "化学物質 / 天然成分",
        "usage": "治療・健康維持",
        "hs6": "3004.90",
        "hts10": "3004.90.9290",
        "jp_hs9": "3004.90.000",
        "chapter": "Chapter 30",
        "reason": "投与量にしたもの又は小売用の形状にした医薬品=3004.90。"
                  "ビタミン剤=2106.90（食品扱い）の場合あり。JP: 3004.90-000。",
    },
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Chapter 97: 美術品・収集品
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {
        "keywords": ["painting", "絵画", "oil painting", "watercolor", "art print",
                      "lithograph", "リトグラフ", "sculpture", "彫刻", "antique", "アンティーク",
                      "collectible", "コレクティブル"],
        "category": "美術品・収集品",
        "material": "キャンバス / 木 / 石 / 金属",
        "usage": "鑑賞・収集",
        "hs6": "9701.10",
        "hts10": "9701.10.0000",
        "jp_hs9": "9701.10.000",
        "chapter": "Chapter 97",
        "reason": "絵画（手描き）=9701.10。版画=9702。彫刻=9703。"
                  "収集品=9705。骨とう=9706。JP: 9701.10-000。",
    },
]

# ============================================================
# データベース関連
# ============================================================


def get_db_connection() -> sqlite3.Connection:
    """SQLite データベースへの接続を返す。テーブルが無ければ作成。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            product_name TEXT,
            url         TEXT,
            hs6         TEXT,
            hts         TEXT,
            jp_hs       TEXT,
            confidence  TEXT,
            reason      TEXT,
            category    TEXT,
            material    TEXT,
            usage_      TEXT,
            chapter     TEXT
        )
    """)
    # 修正履歴テーブル
    conn.execute("""
        CREATE TABLE IF NOT EXISTS edit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            history_id  INTEGER NOT NULL,
            edited_at   TEXT NOT NULL,
            field       TEXT NOT NULL,
            old_value   TEXT,
            new_value   TEXT,
            FOREIGN KEY (history_id) REFERENCES history(id)
        )
    """)
    # アプリ設定テーブル
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def get_setting(key: str) -> Optional[str]:
    """設定値を取得する。未登録なら None を返す。"""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    if row:
        return row["value"]
    return None


def save_setting(key: str, value: str) -> None:
    """設定値を保存（上書き）する。"""
    conn = get_db_connection()
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()
    conn.close()


def delete_setting(key: str) -> None:
    """設定値を削除する。"""
    conn = get_db_connection()
    conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    conn.commit()
    conn.close()


# ============================================================
# localStorage ヘルパー（ブラウザ永続保存）
# ============================================================

def _load_local_storage_keys() -> None:
    """ブラウザの localStorage からAPIキーを読み込み、session_state に格納する。"""
    if not _HAS_JS_EVAL:
        return
    if "_ls_keys_loaded" in st.session_state:
        return
    ls_data = streamlit_js_eval(
        js_expressions="""(function(){
            try {
                return JSON.stringify({
                    ak: localStorage.getItem('hts_anthropic_api_key') || '',
                    eci: localStorage.getItem('hts_ebay_client_id') || '',
                    ecs: localStorage.getItem('hts_ebay_client_secret') || '',
                    pin: localStorage.getItem('hts_settings_pin') || ''
                });
            } catch(e) { return '{}'; }
        })()""",
        key="ls_loader",
    )
    if isinstance(ls_data, str):
        try:
            data = json.loads(ls_data)
            if data.get("ak"):
                st.session_state["_ls_anthropic_api_key"] = data["ak"]
            if data.get("eci"):
                st.session_state["_ls_ebay_client_id"] = data["eci"]
            if data.get("ecs"):
                st.session_state["_ls_ebay_client_secret"] = data["ecs"]
            if data.get("pin"):
                st.session_state["_ls_settings_pin"] = data["pin"]
            st.session_state["_ls_keys_loaded"] = True
        except (json.JSONDecodeError, TypeError):
            pass


def _process_local_storage_ops() -> None:
    """Pending な localStorage 書き込み/削除を実行する。"""
    if "_ls_save_pin" in st.session_state:
        val = st.session_state.pop("_ls_save_pin")
        components.html(
            f"<script>try{{localStorage.setItem('hts_settings_pin',{json.dumps(val)})}}catch(e){{}}</script>",
            height=0,
        )
    if "_ls_save_claude" in st.session_state:
        val = st.session_state.pop("_ls_save_claude")
        components.html(
            f"<script>try{{localStorage.setItem('hts_anthropic_api_key',{json.dumps(val)})}}catch(e){{}}</script>",
            height=0,
        )
    if "_ls_save_ebay" in st.session_state:
        pair = st.session_state.pop("_ls_save_ebay")
        components.html(
            "<script>try{"
            f"localStorage.setItem('hts_ebay_client_id',{json.dumps(pair[0])});"
            f"localStorage.setItem('hts_ebay_client_secret',{json.dumps(pair[1])})"
            "}catch(e){}</script>",
            height=0,
        )
    if st.session_state.pop("_ls_remove_claude", False):
        components.html(
            "<script>try{localStorage.removeItem('hts_anthropic_api_key')}catch(e){}</script>",
            height=0,
        )
    if st.session_state.pop("_ls_remove_ebay", False):
        components.html(
            "<script>try{"
            "localStorage.removeItem('hts_ebay_client_id');"
            "localStorage.removeItem('hts_ebay_client_secret')"
            "}catch(e){}</script>",
            height=0,
        )
    if st.session_state.pop("_ls_remove_pin", False):
        components.html(
            "<script>try{localStorage.removeItem('hts_settings_pin')}catch(e){}</script>",
            height=0,
        )


def _generate_share_code() -> str:
    """現在のAPI設定から共有コードを生成する。"""
    payload: dict = {}
    claude_key = _get_anthropic_api_key()
    if claude_key:
        payload["ak"] = claude_key
    ebay_cid, ebay_csec = _get_ebay_client_credentials()
    if ebay_cid and ebay_csec:
        payload["eci"] = ebay_cid
        payload["ecs"] = ebay_csec
    pin = st.session_state.get("_ls_settings_pin", "")
    if pin:
        payload["pin"] = pin
    return "HTS-" + base64.b64encode(json.dumps(payload).encode()).decode()


def _decode_share_code(code: str) -> Optional[dict]:
    """共有コードをデコードする。無効なら None を返す。"""
    try:
        raw = code.strip()
        if raw.startswith("HTS-"):
            raw = raw[4:]
        payload = json.loads(base64.b64decode(raw.encode()).decode())
        if isinstance(payload, dict) and (payload.get("ak") or payload.get("eci")):
            return payload
        return None
    except Exception:
        return None


def save_result(result: dict) -> int:
    """判定結果を履歴に保存し、挿入された行の id を返す。"""
    conn = get_db_connection()
    cur = conn.execute(
        """INSERT INTO history
           (created_at, product_name, url, hs6, hts, jp_hs,
            confidence, reason, category, material, usage_, chapter)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            result.get("product_name", ""),
            result.get("url", ""),
            result.get("hs6", ""),
            result.get("hts", ""),
            result.get("jp_hs", ""),
            result.get("confidence", ""),
            result.get("reason", ""),
            result.get("category", ""),
            result.get("material", ""),
            result.get("usage", ""),
            result.get("chapter", ""),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def fetch_history(search_query: str = "") -> List[Dict]:
    """履歴を最新順で返す。search_query があれば商品名・URL で部分一致検索。"""
    conn = get_db_connection()
    if search_query:
        rows = conn.execute(
            """SELECT * FROM history
               WHERE product_name LIKE ? OR url LIKE ?
               ORDER BY id DESC""",
            (f"%{search_query}%", f"%{search_query}%"),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM history ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_history_row(row_id: int) -> None:
    """指定された履歴レコードを削除する。"""
    conn = get_db_connection()
    conn.execute("DELETE FROM history WHERE id = ?", (row_id,))
    conn.execute("DELETE FROM edit_log WHERE history_id = ?", (row_id,))
    conn.commit()
    conn.close()


def update_history_field(row_id: int, field: str, old_value: str, new_value: str) -> None:
    """履歴レコードのフィールドを更新し、修正ログを記録する。"""
    allowed = {"hs6", "hts", "jp_hs", "confidence", "reason"}
    if field not in allowed:
        return
    conn = get_db_connection()
    conn.execute(f"UPDATE history SET {field} = ? WHERE id = ?", (new_value, row_id))
    conn.execute(
        "INSERT INTO edit_log (history_id, edited_at, field, old_value, new_value) VALUES (?, ?, ?, ?, ?)",
        (row_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), field, old_value, new_value),
    )
    conn.commit()
    conn.close()


def fetch_edit_log(history_id: int) -> List[Dict]:
    """指定履歴 ID の修正ログを返す。"""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM edit_log WHERE history_id = ? ORDER BY id DESC", (history_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
# eBay Browse API / 商品情報取得
# ============================================================

# eBay API 設定
# - EBAY_API_KEY: OAuth Application Access Token (Bearer token)
#   または
# - EBAY_CLIENT_ID + EBAY_CLIENT_SECRET: 自動でトークン取得
EBAY_API_BASE = "https://api.ebay.com"
EBAY_BROWSE_ENDPOINT = EBAY_API_BASE + "/buy/browse/v1/item"

# eBay のサイトドメインとマーケットプレイスIDの対応
_EBAY_MARKETPLACE_MAP: Dict[str, str] = {
    "www.ebay.com": "EBAY_US",
    "www.ebay.co.uk": "EBAY_GB",
    "www.ebay.de": "EBAY_DE",
    "www.ebay.co.jp": "EBAY_JP",
    "www.ebay.com.au": "EBAY_AU",
    "www.ebay.ca": "EBAY_CA",
    "www.ebay.fr": "EBAY_FR",
    "www.ebay.it": "EBAY_IT",
    "www.ebay.es": "EBAY_ES",
}


def _is_ebay_url(url: str) -> bool:
    """URL が eBay の商品ページかどうかを判定。"""
    try:
        parsed = urlparse(url)
        return parsed.hostname in _EBAY_MARKETPLACE_MAP
    except Exception:
        return False


def _extract_ebay_item_id(url: str) -> Optional[str]:
    """
    eBay URL から商品IDを抽出する。
    対応形式:
      - https://www.ebay.com/itm/314999227653
      - https://www.ebay.com/itm/314999227653?...
      - https://www.ebay.com/itm/Some-Title/314999227653
      - epid パラメータ付き URL
    """
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")

        # /itm/DIGITS or /itm/title/DIGITS
        match = re.search(r'/itm/(?:[^/]+/)?(\d{9,15})', path)
        if match:
            return match.group(1)

        # itm_id パラメータ
        qs = parse_qs(parsed.query)
        for key in ("item", "itm", "itemId"):
            if key in qs:
                val = qs[key][0]
                if val.isdigit():
                    return val

        # hash パラメータ (item49576ac505 形式)
        if "hash" in qs:
            hash_val = qs["hash"][0]
            item_match = re.search(r'item([0-9a-f]{10,})', hash_val)
            if item_match:
                # 16進数のアイテムIDを10進数に変換
                try:
                    return str(int(item_match.group(1), 16))
                except ValueError:
                    pass

    except Exception:
        pass
    return None


def _get_ebay_marketplace_id(url: str) -> str:
    """URL からマーケットプレイスIDを取得。"""
    try:
        parsed = urlparse(url)
        return _EBAY_MARKETPLACE_MAP.get(parsed.hostname, "EBAY_US")
    except Exception:
        return "EBAY_US"


def _get_ebay_client_credentials() -> Tuple[str, str]:
    """
    eBay Client ID / Client Secret を取得する（優先順位）:
    1. Streamlit Secrets
    2. 環境変数
    3. ブラウザ localStorage
    4. SQLite に保存済み（レガシー互換）
    """
    client_id = ""
    client_secret = ""

    # Streamlit Secrets
    try:
        client_id = st.secrets.get("EBAY_CLIENT_ID", "")
        client_secret = st.secrets.get("EBAY_CLIENT_SECRET", "")
        if client_id and client_secret:
            return client_id, client_secret
    except (KeyError, FileNotFoundError):
        pass

    # 環境変数
    client_id = os.environ.get("EBAY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        return client_id, client_secret

    # localStorage（ブラウザ永続保存）
    ls_cid = st.session_state.get("_ls_ebay_client_id", "")
    ls_csec = st.session_state.get("_ls_ebay_client_secret", "")
    if ls_cid and ls_csec:
        return ls_cid, ls_csec

    # DB 保存済み（レガシー互換）
    try:
        client_id = get_setting("ebay_client_id") or ""
        client_secret = get_setting("ebay_client_secret") or ""
        if client_id and client_secret:
            return client_id, client_secret
    except Exception:
        pass

    return "", ""


def _fetch_ebay_app_token(client_id: str, client_secret: str) -> Optional[str]:
    """Client Credentials Grant でアプリケーショントークンを取得する。"""
    try:
        creds = base64.b64encode(
            f"{client_id}:{client_secret}".encode()
        ).decode()
        resp = requests.post(
            EBAY_API_BASE + "/identity/v1/oauth2/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {creds}",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token", "")
        expires_in = data.get("expires_in", 7200)  # デフォルト2時間
        if token:
            # セッションにキャッシュ（有効期限を60秒短縮してマージン確保）
            import time
            st.session_state["_ebay_token"] = token
            st.session_state["_ebay_token_expires"] = time.time() + expires_in - 60
        return token
    except Exception:
        return None


def _get_ebay_oauth_token() -> Optional[str]:
    """
    eBay OAuth アプリケーショントークンを取得する。
    Client ID + Client Secret から Client Credentials Grant で自動取得し、
    セッション内でキャッシュ（有効期限内は再利用）。
    """
    import time

    # キャッシュされたトークンが有効期限内ならそのまま返す
    cached = st.session_state.get("_ebay_token", "")
    expires = st.session_state.get("_ebay_token_expires", 0)
    if cached and time.time() < expires:
        return cached

    # Client Credentials を取得
    client_id, client_secret = _get_ebay_client_credentials()
    if not client_id or not client_secret:
        return None

    # トークンを新規取得
    return _fetch_ebay_app_token(client_id, client_secret)


def _get_anthropic_api_key() -> Optional[str]:
    """
    Anthropic API キーを取得する（優先順位）:
    1. Streamlit Secrets (ANTHROPIC_API_KEY)
    2. 環境変数 ANTHROPIC_API_KEY
    3. ブラウザ localStorage
    4. SQLite に保存済み（レガシー互換）
    """
    # 方法1: Streamlit Secrets
    try:
        key = st.secrets["ANTHROPIC_API_KEY"]
        if key:
            return key
    except (KeyError, FileNotFoundError):
        pass

    # 方法2: 環境変数
    token = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if token:
        return token

    # 方法3: localStorage（ブラウザ永続保存）
    ls_key = st.session_state.get("_ls_anthropic_api_key", "")
    if ls_key:
        return ls_key

    # 方法4: DB 保存済み（レガシー互換）
    try:
        saved = get_setting("anthropic_api_key")
        if saved:
            return saved
    except Exception:
        pass
    return None


def _is_key_preconfigured(key_name: str) -> bool:
    """キーが Secrets または環境変数で事前設定されているかを判定する。"""
    # Streamlit Secrets
    try:
        if st.secrets[key_name]:
            return True
    except (KeyError, FileNotFoundError):
        pass
    # 環境変数
    if os.environ.get(key_name, "").strip():
        return True
    return False


def _is_ebay_preconfigured() -> bool:
    """eBay Client ID/Secret が Secrets または環境変数で事前設定されているかを判定する。"""
    # Streamlit Secrets
    try:
        if st.secrets.get("EBAY_CLIENT_ID") and st.secrets.get("EBAY_CLIENT_SECRET"):
            return True
    except (KeyError, FileNotFoundError):
        pass
    # 環境変数
    if (os.environ.get("EBAY_CLIENT_ID", "").strip()
            and os.environ.get("EBAY_CLIENT_SECRET", "").strip()):
        return True
    return False


def fetch_ebay_item(url: str) -> dict:
    """
    eBay Browse API を使って商品情報を取得する。

    返り値 dict:
      - title: 商品タイトル
      - description: 商品説明テキスト
      - item_specifics: {名前: 値} の dict (Brand, Material, Type 等)
      - category_path: カテゴリパス文字列
      - error: エラーメッセージ（成功時は空文字列）
      - source: "ebay_api"
    """
    result = {
        "title": "",
        "description": "",
        "item_specifics": {},
        "category_path": "",
        "error": "",
        "source": "ebay_api",
    }  # type: Dict[str, object]

    # OAuth トークン取得
    token = _get_ebay_oauth_token()
    if not token:
        result["error"] = "eBay APIキーが未設定です。"
        return result

    # 商品ID抽出
    item_id = _extract_ebay_item_id(url)
    if not item_id:
        result["error"] = "eBay URLから商品IDを抽出できませんでした。"
        return result

    marketplace_id = _get_ebay_marketplace_id(url)

    try:
        # Browse API: getItemByLegacyId
        api_url = (
            EBAY_BROWSE_ENDPOINT
            + "/get_item_by_legacy_id"
            + "?legacy_item_id=" + item_id
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace_id,
        }
        resp = requests.get(api_url, headers=headers, timeout=15)

        if resp.status_code == 401:
            result["error"] = "eBay API認証エラー: トークンが無効または期限切れです。"
            return result
        if resp.status_code == 404:
            result["error"] = f"eBay商品が見つかりません (ID: {item_id})。"
            return result

        resp.raise_for_status()
        data = resp.json()

        # タイトル
        result["title"] = data.get("title", "")

        # Item Specifics (localizedAspects)
        specifics = {}  # type: Dict[str, str]
        for aspect in data.get("localizedAspects", []):
            name = aspect.get("name", "")
            value = aspect.get("value", "")
            if name and value:
                specifics[name] = value
        result["item_specifics"] = specifics

        # カテゴリパス
        cat_path = ""
        for node in data.get("categoryPath", "").split("|"):
            node = node.strip()
            if node:
                cat_path = (cat_path + " > " + node) if cat_path else node
        if not cat_path:
            # categoryId からの取得
            cat_id = data.get("categoryId", "")
            if cat_id:
                cat_path = f"Category ID: {cat_id}"
        result["category_path"] = cat_path

        # 説明文 (shortDescription 優先、なければ description から抽出)
        desc = data.get("shortDescription", "")
        if not desc:
            raw_desc = data.get("description", "")
            if raw_desc:
                # HTML タグを除去
                soup = BeautifulSoup(raw_desc, "html.parser")
                desc = soup.get_text(separator=" ", strip=True)[:500]
        result["description"] = desc

    except requests.exceptions.Timeout:
        result["error"] = "eBay API タイムアウト。"
    except requests.exceptions.HTTPError as e:
        result["error"] = f"eBay API エラー: {e}"
    except Exception as e:
        result["error"] = f"eBay API 取得エラー: {e}"

    return result


def _build_description_from_specifics(specifics: Dict[str, str]) -> str:
    """
    Item Specifics から判定に有用なテキストを構築する。
    Brand, Material, Type 等の情報をキーワードとして追加。
    """
    parts = []
    # 判定に有用なフィールドを優先抽出
    priority_keys = [
        "Brand", "Material", "Type", "Style", "Department", "Product Type",
        "Category", "Sub-Type", "Product Line", "Model", "MPN",
        "Manufacturer Part Number", "Color", "Size", "Pattern",
        "Fabric Type", "Outer Shell Material", "Upper Material",
        "Sole Material", "Features", "Intended Use", "Sport",
        # 日本語キー
        "ブランド", "素材", "タイプ", "カテゴリ",
    ]
    seen = set()  # type: Set[str]
    for key in priority_keys:
        if key in specifics:
            val = specifics[key]
            parts.append(f"{key}: {val}")
            seen.add(key)

    # 残りのキーも追加（ただし冗長を避ける）
    for key, val in specifics.items():
        if key not in seen and val and val.lower() not in ("n/a", "does not apply", "-"):
            parts.append(f"{key}: {val}")

    return " | ".join(parts)


def scrape_product_info(url: str) -> dict:
    """
    非eBay の商品 URL からタイトル・説明文をスクレイピングで取得する。
    取得できなかった場合はエラーメッセージを含む dict を返す。
    """
    result = {
        "title": "",
        "description": "",
        "item_specifics": {},
        "category_path": "",
        "error": "",
        "source": "scraping",
    }  # type: Dict[str, object]
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding  # 日本語サイト対応

        soup = BeautifulSoup(resp.text, "html.parser")

        # タイトル取得
        title_tag = soup.find("title")
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            result["title"] = og_title["content"].strip()
        elif title_tag:
            result["title"] = title_tag.get_text(strip=True)

        # 説明文取得
        og_desc = soup.find("meta", property="og:description")
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if og_desc and og_desc.get("content"):
            result["description"] = og_desc["content"].strip()
        elif meta_desc and meta_desc.get("content"):
            result["description"] = meta_desc["content"].strip()
        else:
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            result["description"] = text[:500]

    except requests.exceptions.Timeout:
        result["error"] = "タイムアウト: サイトの応答がありませんでした。"
    except requests.exceptions.HTTPError as e:
        result["error"] = f"HTTPエラー: {e}"
    except requests.exceptions.ConnectionError:
        result["error"] = "接続エラー: URL に接続できませんでした。"
    except Exception as e:
        result["error"] = f"スクレイピングエラー: {e}"

    return result


def fetch_product_info(url: str) -> dict:
    """
    URL から商品情報を取得する統合関数。
    - eBay URL + APIキー設定済み → eBay Browse API を使用（失敗時はスクレイピングにフォールバック）
    - eBay URL + APIキー未設定 → スクレイピングにフォールバック
    - 非eBay URL → 従来のスクレイピング
    """
    if _is_ebay_url(url):
        token = _get_ebay_oauth_token()
        if token:
            result = fetch_ebay_item(url)
            if not result.get("error"):
                return result
            # eBay API 失敗時はスクレイピングにフォールバック
            api_error = result.get("error", "")
            fallback = scrape_product_info(url)
            if not fallback.get("error") and fallback.get("title"):
                fallback["_api_error"] = api_error
                return fallback
            # スクレイピングも失敗した場合は API のエラーを返す
            result["_api_error"] = api_error
            return result
        else:
            # APIキー未設定 → スクレイピングで試みる
            fallback = scrape_product_info(url)
            if not fallback.get("error") and fallback.get("title"):
                return fallback
            return {
                "title": "",
                "description": "",
                "item_specifics": {},
                "category_path": "",
                "error": "eBay APIキーが未設定のため、商品名を手動入力してください。",
                "source": "none",
            }
    else:
        return scrape_product_info(url)


# ============================================================
# HS コード判定エンジン
# ============================================================

# ── ブランド名辞書（ブランド → 推定カテゴリ chapter） ──

_BRAND_CATEGORY: Dict[str, str] = {}

# 自動車メーカー → Chapter 87
for _b in [
    "honda", "toyota", "nissan", "mazda", "subaru", "mitsubishi", "suzuki",
    "daihatsu", "lexus", "infiniti", "acura", "scion",
    "ford", "chevrolet", "chevy", "gmc", "dodge", "chrysler", "jeep",
    "lincoln", "cadillac", "buick", "ram", "tesla",
    "bmw", "mercedes", "benz", "audi", "volkswagen", "vw", "porsche", "opel",
    "volvo", "saab", "fiat", "alfa romeo", "ferrari", "lamborghini",
    "maserati", "peugeot", "renault", "citroen",
    "hyundai", "kia", "daewoo", "ssangyong",
    "land rover", "jaguar", "bentley", "rolls-royce", "aston martin",
]:
    _BRAND_CATEGORY[_b] = "Chapter 87"

# 電子機器メーカー → Chapter 85 (通信・AV) / Chapter 84 (PC)
for _b in [
    "apple", "samsung", "google", "sony", "lg", "huawei", "xiaomi", "oppo",
    "vivo", "oneplus", "realme", "motorola", "nokia", "asus", "acer",
    "lenovo", "dell", "hp", "microsoft", "toshiba", "sharp", "panasonic",
    "philips", "bose", "jbl", "sennheiser", "anker", "belkin", "logitech",
    "razer", "corsair", "kingston", "sandisk", "western digital",
    "seagate", "nvidia", "intel", "amd", "canon", "nikon", "fujifilm",
    "olympus", "gopro", "dji", "garmin", "fitbit",
]:
    _BRAND_CATEGORY[_b] = "Chapter 85"

# アパレルブランド → Chapter 61/62
for _b in [
    "nike", "adidas", "puma", "reebok", "new balance", "converse", "vans",
    "under armour", "columbia", "the north face", "patagonia", "arc'teryx",
    "uniqlo", "zara", "h&m", "gap", "levi's", "levis", "wrangler",
    "calvin klein", "ralph lauren", "polo", "tommy hilfiger", "lacoste",
    "burberry", "gucci", "prada", "louis vuitton", "chanel", "hermes",
    "dior", "balenciaga", "givenchy", "versace", "armani", "coach",
    "michael kors", "kate spade", "tory burch", "supreme", "stussy",
    "champion", "fila", "asics", "mizuno", "skechers", "crocs",
]:
    _BRAND_CATEGORY[_b] = "Chapter 61"

# 化粧品ブランド → Chapter 33
for _b in [
    "shiseido", "sk-ii", "lancome", "estee lauder", "clinique", "mac",
    "nars", "bobbi brown", "tom ford", "ysl", "maybelline", "loreal",
    "revlon", "covergirl", "neutrogena", "olay", "kiehl's", "lush",
    "the body shop", "innisfree", "etude", "sulwhasoo", "laneige",
]:
    _BRAND_CATEGORY[_b] = "Chapter 33"

# 時計メーカー → Chapter 91
for _b in [
    "citizen", "seiko", "casio", "g-shock", "orient", "grand seiko",
    "rolex", "omega", "tag heuer", "breitling", "longines", "tissot",
    "hamilton", "swatch", "tudor", "cartier", "patek philippe",
    "audemars piguet", "iwc", "panerai", "hublot", "jaeger-lecoultre",
    "vacheron constantin", "zenith", "movado", "bulova", "timex",
    "fossil", "garmin", "fitbit", "suunto", "luminox",
]:
    _BRAND_CATEGORY[_b] = "Chapter 91"

_AUTO_BRANDS: Set[str] = {b for b, c in _BRAND_CATEGORY.items() if c == "Chapter 87"}
_ELECTRONICS_BRANDS: Set[str] = {b for b, c in _BRAND_CATEGORY.items() if c == "Chapter 85"}
_APPAREL_BRANDS: Set[str] = {b for b, c in _BRAND_CATEGORY.items() if c == "Chapter 61"}
_COSMETICS_BRANDS: Set[str] = {b for b, c in _BRAND_CATEGORY.items() if c == "Chapter 33"}
_WATCH_BRANDS: Set[str] = {b for b, c in _BRAND_CATEGORY.items() if c == "Chapter 91"}

# 主に靴メーカーとして知られるブランド（アパレル扱いだが靴ブースト強化）
_SHOE_PRIMARY_BRANDS: Set[str] = {
    "nike", "adidas", "puma", "reebok", "new balance", "converse", "vans",
    "asics", "mizuno", "skechers", "crocs", "under armour",
    "birkenstock", "timberland", "dr. martens", "ugg",
}

# ── 自動車部品の文脈キーワード ──
_AUTO_CONTEXT_WORDS: Set[str] = {
    "oem", "genuine", "jdm", "aftermarket", "replacement", "assembly",
    "automotive", "vehicle", "car", "truck", "sedan", "suv",
    "engine", "motor", "transmission", "exhaust", "intake", "radiator",
    "bumper", "fender", "grille", "hood", "trunk", "tailgate",
    "headlight", "taillight", "brake", "caliper", "rotor", "strut",
    "shock", "suspension", "steering", "axle", "differential",
    "gasket", "bearing", "sensor", "relay", "harness", "wiring",
    "manifold", "carburetor", "alternator", "starter", "compressor",
    "antenna", "block off", "delete", "plug cap", "hose", "clamp",
    "mount", "bracket", "seal", "o-ring", "valve", "piston",
    "crankshaft", "camshaft", "timing", "turbo", "intercooler",
    "muffler", "catalytic", "converter", "fuel pump", "injector",
    "ignition", "coil", "distributor", "flywheel", "clutch",
    "speedometer", "odometer", "tachometer", "gauge",
    "windshield", "wiper", "mirror", "visor", "door handle",
    "window regulator", "latch", "hinge", "weatherstrip",
}

# ── 部品番号パターン ──
# 数字-数字-数字 (例: 82871-671-000), 英数字混合型番 (例: 63217161955)
_PART_NUMBER_RE = re.compile(r'\b\d{3,6}[-]\d{2,4}[-]\d{2,4}\b')
_LONG_PARTNUM_RE = re.compile(r'\b[A-Z]{0,3}\d{8,15}\b', re.IGNORECASE)

# ── 多義語フレーズコンテキストマップ ──
# 多義語が特定の近傍語と共起した場合、正しいカテゴリへ誘導する
# 形式: {多義語: [(共起語セット, 誘導先chapter, ブーストスコア), ...]}
_PHRASE_CONTEXT: Dict[str, List[Tuple[Set[str], str, float]]] = {
    "cap": [
        # 自動車系: cap + (antenna/plug/block/oil/radiator/valve/filler/gas/fuel/distributor)
        ({"antenna", "plug", "block", "oil", "radiator", "valve", "filler",
          "gas", "fuel", "distributor", "rotor", "engine", "delete"}, "Chapter 87", 5.0),
        # ボトルキャップ: cap + (bottle/water/drink)
        ({"bottle", "water", "drink", "lid"}, "Chapter 39", 3.0),
    ],
    "plug": [
        ({"spark", "engine", "ignition", "cylinder", "block", "oil",
          "drain", "antenna", "delete", "freeze"}, "Chapter 87", 5.0),
        ({"electric", "power", "outlet", "adapter", "socket", "usb"}, "Chapter 85", 3.0),
    ],
    "cover": [
        ({"bumper", "fender", "engine", "valve", "timing", "rocker",
          "trunk", "hood", "door"}, "Chapter 87", 5.0),
        ({"phone", "iphone", "tablet", "ipad", "smartphone"}, "Chapter 42", 3.0),
        ({"bed", "sofa", "cushion", "pillow", "seat"}, "Chapter 63", 3.0),
    ],
    "light": [
        ({"brake", "tail", "head", "fog", "turn", "signal", "dashboard",
          "marker", "reverse", "parking", "bumper"}, "Chapter 87", 5.0),
        ({"desk", "table", "floor", "ceiling", "wall", "pendant",
          "chandelier", "照明", "ランプ", "ライト"}, "Chapter 94", 3.0),
    ],
    "band": [
        ({"watch", "wrist", "strap", "apple", "fitbit", "garmin",
          "silicon", "時計"}, "Chapter 91", 4.0),
        ({"rubber", "elastic", "hair", "exercise", "resistance"}, "Chapter 40", 2.0),
    ],
    "case": [
        ({"phone", "iphone", "galaxy", "pixel", "smartphone", "スマホ",
          "tablet", "ipad", "airpods"}, "Chapter 42", 4.0),
        ({"gear", "tool", "gun", "rifle", "ammo", "pelican"}, "Chapter 42", 3.0),
        ({"bumper", "transfer", "transmission", "differential",
          "timing", "chain"}, "Chapter 87", 5.0),
    ],
    "shell": [
        ({"body", "fender", "bumper", "door", "quarter",
          "panel", "trunk"}, "Chapter 87", 5.0),
        ({"phone", "iphone", "laptop", "macbook"}, "Chapter 42", 3.0),
    ],
    "plate": [
        ({"license", "number", "skid", "armor", "mounting",
          "bracket", "pressure", "clutch"}, "Chapter 87", 5.0),
        ({"dinner", "ceramic", "porcelain", "china", "食器",
          "皿", "磁器"}, "Chapter 69", 3.0),
    ],
    "ring": [
        ({"piston", "seal", "o-ring", "snap", "retaining",
          "bearing", "gasket", "synchronizer"}, "Chapter 87", 5.0),
        ({"指輪", "jewelry", "diamond", "gold", "silver",
          "engagement", "wedding", "ジュエリー"}, "Chapter 71", 3.0),
    ],
    "mount": [
        ({"engine", "motor", "transmission", "strut",
          "shock", "exhaust"}, "Chapter 87", 5.0),
        ({"tv", "monitor", "wall", "tripod", "camera"}, "Chapter 85", 2.0),
    ],
    "brush": [
        ({"carbon", "motor", "starter", "alternator",
          "generator", "dynamo"}, "Chapter 87", 5.0),
        ({"tooth", "hair", "paint", "makeup", "歯ブラシ",
          "ヘアブラシ"}, "Chapter 96", 3.0),
    ],
    "mirror": [
        ({"side", "rear", "view", "door", "wing",
          "blind spot", "towing"}, "Chapter 87", 5.0),
        ({"bathroom", "wall", "vanity", "makeup", "compact"}, "Chapter 70", 2.0),
    ],
    "filter": [
        ({"oil", "air", "fuel", "cabin", "transmission",
          "engine", "intake"}, "Chapter 87", 5.0),
        ({"water", "coffee", "hepa", "vacuum"}, "Chapter 84", 2.0),
    ],
    "pump": [
        ({"fuel", "water", "oil", "power steering",
          "brake", "coolant", "washer"}, "Chapter 87", 5.0),
        ({"air", "bicycle", "pool", "aquarium"}, "Chapter 84", 2.0),
    ],
    "pen": [
        ({"ball", "ballpoint", "fountain", "marker", "ボールペン",
          "万年筆", "鉛筆", "ペン"}, "Chapter 96", 3.0),
    ],
}

# ============================================================
# eBay 構造化データ → HS チャプター 判定エンジン
# ============================================================
#
# 判定優先順位:
#   1. 最優先: eBay 最下層カテゴリ名 (Wristwatches, Women's Bags & Handbags 等)
#   2. 次点:   Item Specifics の "Type" / "Category" フィールド
#   3. 補助:   Brand, Material, その他 Item Specifics
#   4. 最後:   商品タイトルのキーワードマッチング (従来ロジック)
#
# カテゴリパスから最下層を抽出し、網羅的マッピングテーブルで HS チャプターを決定する。
# 最下層で決定できない場合のみ上位層にフォールバックする。
# ============================================================

# ── eBay リーフカテゴリ → HS チャプター 網羅的マッピング ──
# キー: eBay カテゴリ名（小文字、完全一致 or 部分一致）
# 値: HS チャプター
# eBay 全トップレベルカテゴリ（約30）をカバー

_EBAY_LEAF_CATEGORY_MAP: Dict[str, str] = {
    # ━━━━━ Jewelry & Watches (トップ) ━━━━━
    # Watches
    "wristwatches": "Chapter 91",
    "pocket watches": "Chapter 91",
    "watch accessories": "Chapter 91",
    "watch bands": "Chapter 91",
    "watch parts": "Chapter 91",
    "watch cases": "Chapter 91",
    "watch tools & repair kits": "Chapter 91",
    "watches": "Chapter 91",
    "clocks": "Chapter 91",
    "clock parts & tools": "Chapter 91",
    "wall clocks": "Chapter 91",
    "alarm clocks & clock radios": "Chapter 91",
    "mantel & shelf clocks": "Chapter 91",
    "grandfather clocks": "Chapter 91",
    "cuckoo clocks": "Chapter 91",
    # Fine Jewelry
    "rings": "Chapter 71",
    "necklaces & pendants": "Chapter 71",
    "earrings": "Chapter 71",
    "bracelets": "Chapter 71",
    "brooches & pins": "Chapter 71",
    "fine jewelry sets": "Chapter 71",
    "fine anklets": "Chapter 71",
    "fine charms & charm bracelets": "Chapter 71",
    # Fashion Jewelry
    "fashion necklaces & pendants": "Chapter 71",
    "fashion earrings": "Chapter 71",
    "fashion bracelets": "Chapter 71",
    "fashion rings": "Chapter 71",
    "fashion brooches & pins": "Chapter 71",
    "fashion jewelry sets": "Chapter 71",
    "body jewelry": "Chapter 71",
    "charms & charm bracelets": "Chapter 71",
    "loose diamonds & gemstones": "Chapter 71",
    "loose beads": "Chapter 71",

    # ━━━━━ Clothing, Shoes & Accessories (トップ) ━━━━━
    # Men's Clothing
    "men's shirts": "Chapter 62",
    "men's t-shirts": "Chapter 61",
    "men's pants": "Chapter 62",
    "men's jeans": "Chapter 62",
    "men's shorts": "Chapter 62",
    "men's coats, jackets & vests": "Chapter 62",
    "men's sweaters": "Chapter 61",
    "men's hoodies & sweatshirts": "Chapter 61",
    "men's suits & suit separates": "Chapter 62",
    "men's activewear": "Chapter 61",
    "men's underwear": "Chapter 61",
    "men's socks": "Chapter 61",
    "men's sleepwear & robes": "Chapter 62",
    "men's swimwear": "Chapter 62",
    "men's uniforms": "Chapter 62",
    "men's costumes": "Chapter 62",
    "men's clothing": "Chapter 62",
    # Women's Clothing
    "women's dresses": "Chapter 62",
    "women's tops": "Chapter 62",
    "women's t-shirts": "Chapter 61",
    "women's pants": "Chapter 62",
    "women's jeans": "Chapter 62",
    "women's shorts": "Chapter 62",
    "women's coats, jackets & vests": "Chapter 62",
    "women's sweaters": "Chapter 61",
    "women's hoodies & sweatshirts": "Chapter 61",
    "women's skirts": "Chapter 62",
    "women's suits & suit separates": "Chapter 62",
    "women's activewear": "Chapter 61",
    "women's intimates & sleepwear": "Chapter 62",
    "women's swimwear": "Chapter 62",
    "women's maternity": "Chapter 62",
    "women's costumes": "Chapter 62",
    "women's clothing": "Chapter 62",
    # Kids' Clothing
    "boys' clothing": "Chapter 62",
    "girls' clothing": "Chapter 62",
    "baby clothing": "Chapter 62",
    "unisex kids' clothing": "Chapter 62",
    # Shoes
    "men's shoes": "Chapter 64",
    "women's shoes": "Chapter 64",
    "boys' shoes": "Chapter 64",
    "girls' shoes": "Chapter 64",
    "baby shoes": "Chapter 64",
    "unisex shoes": "Chapter 64",
    "athletic shoes": "Chapter 64",
    "sneakers": "Chapter 64",
    "boots": "Chapter 64",
    "sandals": "Chapter 64",
    "slippers": "Chapter 64",
    "flats": "Chapter 64",
    "heels": "Chapter 64",
    "loafers & slip ons": "Chapter 64",
    "oxfords & dress shoes": "Chapter 64",
    # Accessories
    "women's bags & handbags": "Chapter 42",
    "men's bags": "Chapter 42",
    "backpacks": "Chapter 42",
    "briefcases & laptop bags": "Chapter 42",
    "wallets": "Chapter 42",
    "coin purses": "Chapter 42",
    "travel luggage": "Chapter 42",
    "luggage": "Chapter 42",
    "suitcases": "Chapter 42",
    "duffel bags": "Chapter 42",
    "tote bags": "Chapter 42",
    "crossbody bags": "Chapter 42",
    "clutch bags": "Chapter 42",
    "messenger bags": "Chapter 42",
    "fanny packs": "Chapter 42",
    "belts": "Chapter 42",
    "key chains, rings & finders": "Chapter 73",
    "sunglasses": "Chapter 90",
    "eyeglass frames": "Chapter 90",
    "hats": "Chapter 65",
    "caps": "Chapter 65",
    "baseball caps": "Chapter 65",
    "beanies": "Chapter 65",
    "sun hats & visors": "Chapter 65",
    "scarves & wraps": "Chapter 62",
    "gloves & mittens": "Chapter 62",
    "ties, bow ties & cravats": "Chapter 62",
    "umbrellas": "Chapter 66",
    "hair accessories": "Chapter 96",
    "watches, parts & accessories": "Chapter 91",

    # ━━━━━ Cell Phones & Accessories (トップ) ━━━━━
    "cell phones & smartphones": "Chapter 85",
    "cell phone cases, covers & skins": "Chapter 42",
    "cell phone screen protectors": "Chapter 39",
    "cell phone chargers & cradles": "Chapter 85",
    "cell phone batteries": "Chapter 85",
    "cell phone cables & adapters": "Chapter 85",
    "headsets": "Chapter 85",
    "cell phone accessories": "Chapter 85",
    "smart watches": "Chapter 91",
    "smart watch accessories": "Chapter 91",
    "smart watch bands": "Chapter 91",

    # ━━━━━ Computers/Tablets & Networking (トップ) ━━━━━
    "laptops & netbooks": "Chapter 84",
    "desktops & all-in-ones": "Chapter 84",
    "tablets & ereaders": "Chapter 84",
    "monitors, projectors & accs": "Chapter 85",
    "monitors": "Chapter 85",
    "projectors": "Chapter 85",
    "printers, scanners & supplies": "Chapter 84",
    "printers": "Chapter 84",
    "scanners": "Chapter 84",
    "keyboards & mice": "Chapter 84",
    "computer components & parts": "Chapter 84",
    "drives, storage & blank media": "Chapter 84",
    "networking": "Chapter 85",
    "routers": "Chapter 85",
    "switches & hubs": "Chapter 85",
    "power protection, distribution": "Chapter 85",
    "computer cables & connectors": "Chapter 85",

    # ━━━━━ Consumer Electronics (トップ) ━━━━━
    "tv, video & home audio": "Chapter 85",
    "televisions": "Chapter 85",
    "home theater projectors": "Chapter 85",
    "home audio": "Chapter 85",
    "speakers": "Chapter 85",
    "headphones": "Chapter 85",
    "earbuds": "Chapter 85",
    "portable audio & headphones": "Chapter 85",
    "mp3 players": "Chapter 85",
    "portable cd players": "Chapter 85",
    "vehicle electronics & gps": "Chapter 85",
    "car audio": "Chapter 85",
    "car video": "Chapter 85",
    "gps units": "Chapter 85",
    "multipurpose batteries & power": "Chapter 85",
    "batteries": "Chapter 85",
    "video game consoles": "Chapter 95",
    "video games": "Chapter 95",

    # ━━━━━ Cameras & Photo (トップ) ━━━━━
    "digital cameras": "Chapter 90",
    "film cameras": "Chapter 90",
    "camcorders": "Chapter 85",
    "camera drones": "Chapter 88",
    "lenses & filters": "Chapter 90",
    "flashes & flash accessories": "Chapter 90",
    "tripods & monopods": "Chapter 90",
    "camera bags & cases": "Chapter 42",
    "binoculars & telescopes": "Chapter 90",
    "binoculars & monoculars": "Chapter 90",
    "telescopes": "Chapter 90",
    "microscopes": "Chapter 90",

    # ━━━━━ eBay Motors > Parts & Accessories (トップ) ━━━━━
    "car & truck parts & accessories": "Chapter 87",
    "car & truck parts": "Chapter 87",
    "motorcycle parts": "Chapter 87",
    "atv, side-by-side & utv parts": "Chapter 87",
    "boat parts": "Chapter 89",
    "automotive tools & supplies": "Chapter 82",
    "automotive paints & supplies": "Chapter 32",
    "oils, fluids, lubricants & sealers": "Chapter 27",
    "tires & wheels": "Chapter 40",
    "brakes & brake parts": "Chapter 87",
    "engine cooling parts": "Chapter 87",
    "engines & components": "Chapter 87",
    "exhaust parts": "Chapter 87",
    "exterior parts & accessories": "Chapter 87",
    "interior parts & accessories": "Chapter 87",
    "lighting & lamps": "Chapter 87",
    "starters, alternators, ecus & wiring": "Chapter 87",
    "steering & suspension": "Chapter 87",
    "transmission & drivetrain": "Chapter 87",
    "air & fuel delivery": "Chapter 87",
    "ignition systems & components": "Chapter 87",
    "filters": "Chapter 87",

    # ━━━━━ Home & Garden (トップ) ━━━━━
    "furniture": "Chapter 94",
    "sofas, armchairs & couches": "Chapter 94",
    "tables": "Chapter 94",
    "chairs": "Chapter 94",
    "beds & mattresses": "Chapter 94",
    "bookcases & shelving": "Chapter 94",
    "desks & tables": "Chapter 94",
    "cabinets & cupboards": "Chapter 94",
    "lamps, lighting & ceiling fans": "Chapter 94",
    "ceiling lights & chandeliers": "Chapter 94",
    "wall fixtures": "Chapter 94",
    "table lamps": "Chapter 94",
    "floor lamps": "Chapter 94",
    "rugs & carpets": "Chapter 57",
    "window treatments & hardware": "Chapter 63",
    "curtains & drapes": "Chapter 63",
    "bedding": "Chapter 63",
    "sheets & pillowcases": "Chapter 63",
    "comforters & sets": "Chapter 63",
    "quilts & bedspreads": "Chapter 63",
    "blankets & throws": "Chapter 63",
    "pillows": "Chapter 63",
    "bath towels": "Chapter 63",
    "bath": "Chapter 63",
    "kitchen, dining & bar": "Chapter 69",
    "dinnerware & serveware": "Chapter 69",
    "cookware": "Chapter 73",
    "bakeware": "Chapter 73",
    "flatware, knives & cutlery": "Chapter 82",
    "small kitchen appliances": "Chapter 85",
    "major appliances": "Chapter 84",
    "refrigerators & freezers": "Chapter 84",
    "washers & dryers": "Chapter 84",
    "dishwashers": "Chapter 84",
    "vacuums": "Chapter 85",
    "candles & home fragrance": "Chapter 34",
    "home décor": "Chapter 94",
    "clocks": "Chapter 91",
    "vases": "Chapter 69",
    "picture frames": "Chapter 44",
    "mirrors": "Chapter 70",
    "yard, garden & outdoor living": "Chapter 94",
    "outdoor furniture": "Chapter 94",
    "garden tools & equipment": "Chapter 82",
    "lawn mowers": "Chapter 84",
    "grills & outdoor cooking": "Chapter 73",
    "storage & organization": "Chapter 39",
    "cleaning supplies": "Chapter 34",

    # ━━━━━ Sporting Goods (トップ) ━━━━━
    "cycling": "Chapter 87",
    "bicycles": "Chapter 87",
    "cycling parts & components": "Chapter 87",
    "golf": "Chapter 95",
    "golf clubs": "Chapter 95",
    "golf bags": "Chapter 42",
    "tennis & racquet sports": "Chapter 95",
    "fishing": "Chapter 95",
    "hunting": "Chapter 93",
    "camping & hiking": "Chapter 63",
    "tents & canopies": "Chapter 63",
    "sleeping bags": "Chapter 94",
    "fitness, running & yoga": "Chapter 95",
    "exercise & fitness equipment": "Chapter 95",
    "team sports": "Chapter 95",
    "water sports": "Chapter 95",
    "winter sports": "Chapter 95",
    "skiing & snowboarding": "Chapter 95",
    "skateboarding & longboarding": "Chapter 95",
    "boxing & martial arts": "Chapter 95",
    "outdoor sports": "Chapter 95",

    # ━━━━━ Toys & Hobbies (トップ) ━━━━━
    "action figures": "Chapter 95",
    "dolls & bears": "Chapter 95",
    "dolls": "Chapter 95",
    "stuffed animals": "Chapter 95",
    "building toys": "Chapter 95",
    "lego sets & packs": "Chapter 95",
    "diecast & toy vehicles": "Chapter 95",
    "model railroads & trains": "Chapter 95",
    "rc model vehicles, toys & control line": "Chapter 95",
    "games": "Chapter 95",
    "board & traditional games": "Chapter 95",
    "card games & poker": "Chapter 95",
    "puzzles": "Chapter 95",
    "outdoor toys & structures": "Chapter 95",
    "educational": "Chapter 95",
    "preschool toys & pretend play": "Chapter 95",
    "electronic, battery & wind-up": "Chapter 95",
    "toy vehicles": "Chapter 95",
    "models & kits": "Chapter 95",
    "hobby rc cars, trucks & motorcycles": "Chapter 95",

    # ━━━━━ Health & Beauty (トップ) ━━━━━
    "fragrances": "Chapter 33",
    "men's fragrances": "Chapter 33",
    "women's fragrances": "Chapter 33",
    "unisex fragrances": "Chapter 33",
    "skin care": "Chapter 33",
    "makeup": "Chapter 33",
    "face makeup": "Chapter 33",
    "eye makeup": "Chapter 33",
    "lip makeup": "Chapter 33",
    "nail care, manicure & pedicure": "Chapter 33",
    "nail polish": "Chapter 33",
    "hair care & styling": "Chapter 33",
    "shampoos & conditioners": "Chapter 34",
    "bath & body": "Chapter 33",
    "oral care": "Chapter 96",
    "toothbrushes": "Chapter 96",
    "shaving & hair removal": "Chapter 82",
    "razors & razor blades": "Chapter 82",
    "health care": "Chapter 30",
    "vitamins & dietary supplements": "Chapter 21",
    "medical & mobility": "Chapter 90",
    "massage equipment": "Chapter 90",
    "vision care": "Chapter 90",

    # ━━━━━ Musical Instruments & Gear (トップ) ━━━━━
    "guitars & basses": "Chapter 92",
    "guitars": "Chapter 92",
    "bass guitars": "Chapter 92",
    "drums & percussion": "Chapter 92",
    "pianos, keyboards & organs": "Chapter 92",
    "brass": "Chapter 92",
    "woodwinds": "Chapter 92",
    "string": "Chapter 92",
    "pro audio equipment": "Chapter 85",
    "microphones": "Chapter 85",
    "amplifiers": "Chapter 85",
    "dj equipment": "Chapter 85",
    "stage lighting & effects": "Chapter 85",

    # ━━━━━ Books, Comics & Magazines (トップ) ━━━━━
    "books": "Chapter 49",
    "fiction books": "Chapter 49",
    "nonfiction books": "Chapter 49",
    "textbooks, education & reference": "Chapter 49",
    "children's & young adults": "Chapter 49",
    "magazines": "Chapter 49",
    "comic books": "Chapter 49",

    # ━━━━━ Movies & TV (トップ) ━━━━━
    "dvds & blu-ray discs": "Chapter 85",
    "vhs tapes": "Chapter 85",

    # ━━━━━ Music (トップ) ━━━━━
    "cds": "Chapter 85",
    "vinyl records": "Chapter 85",
    "cassettes": "Chapter 85",

    # ━━━━━ Collectibles & Art (トップ) ━━━━━
    "art": "Chapter 97",
    "paintings": "Chapter 97",
    "art prints": "Chapter 49",
    "art photographs": "Chapter 49",
    "sculptures & carvings": "Chapter 97",
    "antiques": "Chapter 97",
    "coins & paper money": "Chapter 71",
    "coins": "Chapter 71",
    "paper money": "Chapter 49",
    "stamps": "Chapter 49",
    "sports memorabilia": "Chapter 95",
    "trading cards": "Chapter 49",
    "pottery & glass": "Chapter 69",
    "pottery & china": "Chapter 69",
    "glass": "Chapter 70",
    "decorative collectibles": "Chapter 69",
    "figurines": "Chapter 69",

    # ━━━━━ Business & Industrial (トップ) ━━━━━
    "heavy equipment, parts & attachments": "Chapter 84",
    "heavy equipment": "Chapter 84",
    "cnc, metalworking & manufacturing": "Chapter 84",
    "office": "Chapter 84",
    "printing & graphic arts": "Chapter 84",
    "restaurant & food service": "Chapter 84",
    "test, measurement & inspection": "Chapter 90",
    "electrical equipment & supplies": "Chapter 85",
    "hydraulics, pneumatics, pumps & plumbing": "Chapter 84",
    "industrial automation & motion controls": "Chapter 84",
    "material handling": "Chapter 84",
    "light industrial equipment & tools": "Chapter 84",

    # ━━━━━ Crafts (トップ) ━━━━━
    "sewing": "Chapter 84",
    "sewing machines": "Chapter 84",
    "fabric": "Chapter 50",
    "yarn": "Chapter 56",
    "needlecrafts & yarn": "Chapter 56",
    "beads & jewelry making": "Chapter 71",
    "scrapbooking & paper crafts": "Chapter 48",

    # ━━━━━ Pet Supplies (トップ) ━━━━━
    "dog supplies": "Chapter 42",
    "cat supplies": "Chapter 42",
    "fish & aquariums": "Chapter 84",
    "bird supplies": "Chapter 42",
    "small animal supplies": "Chapter 42",

    # ━━━━━ Baby (トップ) ━━━━━
    "strollers & accessories": "Chapter 87",
    "car safety seats": "Chapter 94",
    "baby feeding": "Chapter 39",
    "baby bottles": "Chapter 39",
    "diapering": "Chapter 96",
    "nursery furniture": "Chapter 94",
    "baby gear": "Chapter 94",
    "baby toys": "Chapter 95",
    "baby clothing": "Chapter 62",

    # ━━━━━ Food & Beverages ━━━━━
    "coffee": "Chapter 09",
    "tea": "Chapter 09",
    "gourmet chocolates": "Chapter 18",
    "candy, gum & chocolate": "Chapter 17",
    "spices, seasonings & extracts": "Chapter 09",

    # ━━━━━ その他 ━━━━━
    "pens & writing instruments": "Chapter 96",
    "lighters": "Chapter 96",
    "knives, swords & blades": "Chapter 82",
    "tool sets & kits": "Chapter 82",
    "hand tools": "Chapter 82",
    "power tools": "Chapter 84",
    "air tools": "Chapter 84",
}

# ── 上位 (親) カテゴリ → HS チャプター フォールバック ──
# リーフカテゴリで判定できない場合のみ使用
_EBAY_PARENT_CATEGORY_MAP: Dict[str, str] = {
    "jewelry & watches": "Chapter 71",
    "watches, parts & accessories": "Chapter 91",
    "fine jewelry": "Chapter 71",
    "fashion jewelry": "Chapter 71",
    "clothing, shoes & accessories": "Chapter 62",
    "men": "Chapter 62",
    "women": "Chapter 62",
    "cell phones & accessories": "Chapter 85",
    "computers/tablets & networking": "Chapter 84",
    "computers, tablets & networking": "Chapter 84",
    "consumer electronics": "Chapter 85",
    "cameras & photo": "Chapter 90",
    "ebay motors": "Chapter 87",
    "parts & accessories": "Chapter 87",
    "home & garden": "Chapter 94",
    "sporting goods": "Chapter 95",
    "toys & hobbies": "Chapter 95",
    "health & beauty": "Chapter 33",
    "musical instruments & gear": "Chapter 92",
    "books, comics & magazines": "Chapter 49",
    "collectibles & art": "Chapter 97",
    "business & industrial": "Chapter 84",
    "crafts": "Chapter 48",
    "pet supplies": "Chapter 42",
    "baby": "Chapter 62",
    "movies & tv": "Chapter 85",
    "music": "Chapter 85",
}

# ── Item Specifics "Type" / "Category" → HS チャプター ──
_ITEM_TYPE_TO_CHAPTER: Dict[str, str] = {
    # 時計
    "wristwatch": "Chapter 91", "wristwatches": "Chapter 91",
    "analog watch": "Chapter 91", "digital watch": "Chapter 91",
    "smartwatch": "Chapter 91", "pocket watch": "Chapter 91",
    "clock": "Chapter 91", "watch": "Chapter 91",
    "dive watch": "Chapter 91", "dress watch": "Chapter 91",
    "sport watch": "Chapter 91", "chronograph": "Chapter 91",
    # 宝飾品
    "necklace": "Chapter 71", "bracelet": "Chapter 71",
    "ring": "Chapter 71", "earring": "Chapter 71", "earrings": "Chapter 71",
    "pendant": "Chapter 71", "brooch": "Chapter 71",
    "anklet": "Chapter 71", "charm": "Chapter 71",
    # 電子機器
    "smartphone": "Chapter 85", "cell phone": "Chapter 85",
    "mobile phone": "Chapter 85",
    "tablet": "Chapter 84", "laptop": "Chapter 84", "desktop": "Chapter 84",
    "headphones": "Chapter 85", "earbuds": "Chapter 85", "speaker": "Chapter 85",
    "camera": "Chapter 90", "digital camera": "Chapter 90",
    "television": "Chapter 85", "monitor": "Chapter 85",
    "printer": "Chapter 84", "scanner": "Chapter 84",
    "router": "Chapter 85", "keyboard": "Chapter 84", "mouse": "Chapter 84",
    # 衣料品
    "shirt": "Chapter 62", "t-shirt": "Chapter 61",
    "jacket": "Chapter 62", "coat": "Chapter 62",
    "pants": "Chapter 62", "jeans": "Chapter 62",
    "dress": "Chapter 62", "skirt": "Chapter 62",
    "sweater": "Chapter 61", "hoodie": "Chapter 61",
    "suit": "Chapter 62", "blazer": "Chapter 62",
    "shorts": "Chapter 62", "vest": "Chapter 62",
    # 靴
    "sneakers": "Chapter 64", "boots": "Chapter 64",
    "sandals": "Chapter 64", "loafers": "Chapter 64",
    "athletic shoes": "Chapter 64", "running shoes": "Chapter 64",
    "heels": "Chapter 64", "flats": "Chapter 64",
    "oxfords": "Chapter 64", "slippers": "Chapter 64",
    # バッグ
    "handbag": "Chapter 42", "backpack": "Chapter 42",
    "wallet": "Chapter 42", "tote bag": "Chapter 42",
    "crossbody bag": "Chapter 42", "clutch": "Chapter 42",
    "briefcase": "Chapter 42", "suitcase": "Chapter 42",
    "duffel bag": "Chapter 42", "messenger bag": "Chapter 42",
    # 玩具
    "action figure": "Chapter 95", "doll": "Chapter 95",
    "board game": "Chapter 95", "puzzle": "Chapter 95",
    "stuffed animal": "Chapter 95", "building set": "Chapter 95",
    # 自動車部品
    "brake pad": "Chapter 87", "air filter": "Chapter 87",
    "headlight": "Chapter 87", "bumper": "Chapter 87",
    "spark plug": "Chapter 87", "alternator": "Chapter 87",
    # 家具
    "sofa": "Chapter 94", "table": "Chapter 94",
    "chair": "Chapter 94", "desk": "Chapter 94",
    "bed": "Chapter 94", "bookcase": "Chapter 94",
    # 化粧品
    "perfume": "Chapter 33", "eau de toilette": "Chapter 33",
    "eau de parfum": "Chapter 33", "cologne": "Chapter 33",
    "lipstick": "Chapter 33", "mascara": "Chapter 33",
    "foundation": "Chapter 33", "concealer": "Chapter 33",
    "moisturizer": "Chapter 33", "serum": "Chapter 33",
    "sunscreen": "Chapter 33",
    # 楽器
    "acoustic guitar": "Chapter 92", "electric guitar": "Chapter 92",
    "bass guitar": "Chapter 92", "keyboard": "Chapter 92",
    "drum set": "Chapter 92", "violin": "Chapter 92",
    "trumpet": "Chapter 92", "flute": "Chapter 92",
    # 帽子
    "hat": "Chapter 65", "cap": "Chapter 65",
    "baseball cap": "Chapter 65", "beanie": "Chapter 65",
}


def _extract_leaf_category(category_path: str) -> str:
    """
    カテゴリパスから最下層（リーフ）カテゴリを抽出する。
    例: "Jewelry & Watches > Watches, Parts & Accessories > Watches > Wristwatches"
        → "wristwatches"
    """
    if not category_path:
        return ""
    parts = [p.strip() for p in category_path.split(">")]
    # 最下層を返す
    leaf = parts[-1] if parts else ""
    return leaf.lower()


def _extract_all_category_levels(category_path: str) -> List[str]:
    """
    カテゴリパスを階層ごとに分解して返す（最下層が先頭）。
    例: "A > B > C > D" → ["d", "c", "b", "a"]
    """
    if not category_path:
        return []
    parts = [p.strip().lower() for p in category_path.split(">") if p.strip()]
    parts.reverse()
    return parts


def _classify_by_ebay_data(
    item_specifics: Dict[str, str],
    category_path: str,
) -> Optional[Tuple[str, float, str]]:
    """
    eBay の構造化データから HS チャプターを判定する。

    判定優先順位:
      1. 最優先: eBay 最下層カテゴリ名
      2. 次点:   Item Specifics の "Type" / "Category"
      3. 補助:   Brand, Material, その他 Item Specifics
      4. (最後のキーワードマッチングは classify_product 側で処理)

    返り値: (chapter, confidence_score, reason_text) or None
    - confidence_score: 判定の確信度 (高いほど確信が強い)
      100: リーフカテゴリ完全一致
       80: Item Specifics Type 一致
       60: 親カテゴリ一致
       40: Brand + 補助情報
    """

    # ── レベル1: 最下層カテゴリでの判定 (最優先) ──
    if category_path:
        levels = _extract_all_category_levels(category_path)
        leaf = levels[0] if levels else ""

        # リーフカテゴリの完全一致
        if leaf and leaf in _EBAY_LEAF_CATEGORY_MAP:
            chapter = _EBAY_LEAF_CATEGORY_MAP[leaf]
            return (
                chapter,
                100.0,
                "eBayカテゴリ(リーフ): {} → {}".format(
                    category_path.split(">")[-1].strip(), chapter
                ),
            )

        # リーフで見つからない場合、部分一致を試す
        if leaf:
            for cat_key, chapter in _EBAY_LEAF_CATEGORY_MAP.items():
                if cat_key in leaf or leaf in cat_key:
                    return (
                        chapter,
                        90.0,
                        "eBayカテゴリ(リーフ部分一致): {} ≈ {} → {}".format(
                            category_path.split(">")[-1].strip(), cat_key, chapter
                        ),
                    )

        # 上位カテゴリにフォールバック（2番目以降の階層）
        for level_name in levels[1:]:
            if level_name in _EBAY_LEAF_CATEGORY_MAP:
                chapter = _EBAY_LEAF_CATEGORY_MAP[level_name]
                return (
                    chapter,
                    70.0,
                    "eBayカテゴリ(上位): {} → {}".format(level_name, chapter),
                )
            if level_name in _EBAY_PARENT_CATEGORY_MAP:
                chapter = _EBAY_PARENT_CATEGORY_MAP[level_name]
                return (
                    chapter,
                    60.0,
                    "eBayカテゴリ(親): {} → {}".format(level_name, chapter),
                )

    # ── レベル2: Item Specifics の Type / Category フィールド (次点) ──
    for field_name in ("Type", "Category", "Product Type", "Sub-Type"):
        val = item_specifics.get(field_name, "").strip().lower()
        if val and val in _ITEM_TYPE_TO_CHAPTER:
            chapter = _ITEM_TYPE_TO_CHAPTER[val]
            return (
                chapter,
                80.0,
                "Item Specifics {}: {} → {}".format(
                    field_name, item_specifics.get(field_name, ""), chapter
                ),
            )

    # ── レベル3: Brand + 補助情報 (補助) ──
    brand = item_specifics.get("Brand", "").strip().lower()
    if brand and brand in _BRAND_CATEGORY:
        brand_ch = _BRAND_CATEGORY[brand]
        # 補助情報で裏付けがあればスコアアップ
        support_score = 40.0
        support_reasons = ["ブランド: {} → {}".format(
            item_specifics.get("Brand", ""), brand_ch
        )]

        # Movement/Display → 時計の裏付け
        movement = item_specifics.get("Movement", "").strip().lower()
        if movement and movement in (
            "quartz", "automatic", "mechanical", "solar", "kinetic", "eco-drive"
        ):
            if brand_ch == "Chapter 91":
                support_score += 10.0
                support_reasons.append("Movement: {}".format(
                    item_specifics.get("Movement", "")
                ))

        # Material → 素材の裏付け
        material = item_specifics.get("Material", "").strip().lower()
        if material:
            support_reasons.append("Material: {}".format(
                item_specifics.get("Material", "")
            ))
            support_score += 3.0

        # Department
        dept = item_specifics.get("Department", "").strip().lower()
        if dept:
            support_reasons.append("Department: {}".format(
                item_specifics.get("Department", "")
            ))
            support_score += 2.0

        return (
            brand_ch,
            support_score,
            " / ".join(support_reasons),
        )

    # eBay データでは判定できなかった
    return None


def _detect_context(text: str) -> Dict[str, object]:
    """
    テキスト全体のコンテキストを解析し、検出結果を dict で返す。
    - brand_chapter: 検出されたブランドの推定 chapter (or None)
    - is_auto: 自動車コンテキストかどうか
    - auto_boost: 自動車部品ルールへのブースト値
    - is_electronics: 電子機器コンテキスト
    - is_apparel: アパレルコンテキスト
    - is_cosmetics: 化粧品コンテキスト
    - has_part_number: 部品番号パターン検出
    """
    text_lower = text.lower()
    ctx = {
        "brand_chapter": None,
        "brand_name": None,
        "is_auto": False,
        "auto_boost": 0.0,
        "is_electronics": False,
        "is_apparel": False,
        "is_cosmetics": False,
        "has_part_number": False,
    }  # type: Dict[str, object]

    # ブランド検出（長い名前から優先マッチ）
    sorted_brands = sorted(_BRAND_CATEGORY.keys(), key=len, reverse=True)
    for brand in sorted_brands:
        if brand in text_lower:
            ctx["brand_chapter"] = _BRAND_CATEGORY[brand]
            ctx["brand_name"] = brand
            break

    # 部品番号パターン検出
    if _PART_NUMBER_RE.search(text) or _LONG_PARTNUM_RE.search(text):
        ctx["has_part_number"] = True

    # 自動車スコア計算
    auto_score = 0.0
    if ctx["brand_chapter"] == "Chapter 87":
        auto_score += 5.0
    if ctx["has_part_number"]:
        auto_score += 4.0
    ctx_count = sum(1 for w in _AUTO_CONTEXT_WORDS if w in text_lower)
    auto_score += ctx_count * 1.5
    ctx["is_auto"] = auto_score >= 3.0
    ctx["auto_boost"] = auto_score

    # 電子機器 / アパレル / 化粧品 / 時計
    ctx["is_electronics"] = ctx["brand_chapter"] == "Chapter 85"
    ctx["is_apparel"] = ctx["brand_chapter"] == "Chapter 61"
    ctx["is_cosmetics"] = ctx["brand_chapter"] == "Chapter 33"
    ctx["is_watch"] = ctx["brand_chapter"] == "Chapter 91"

    return ctx


# ── 短いキーワードの単語境界マッチ ──
_SHORT_KW_BOUNDARY_RE_CACHE: Dict[str, "re.Pattern"] = {}


def _match_keyword(kw_lower: str, text_lower: str) -> bool:
    """キーワードがテキスト内に存在するか判定。短いキーワードは単語境界を考慮。"""
    if len(kw_lower) <= 4 and kw_lower.isascii() and kw_lower.isalpha():
        if kw_lower not in _SHORT_KW_BOUNDARY_RE_CACHE:
            _SHORT_KW_BOUNDARY_RE_CACHE[kw_lower] = re.compile(
                r'(?<![a-z])' + re.escape(kw_lower) + r'(?![a-z])'
            )
        return bool(_SHORT_KW_BOUNDARY_RE_CACHE[kw_lower].search(text_lower))
    return kw_lower in text_lower


def _eval_phrase_context(word: str, text_lower: str) -> List[Tuple[str, float]]:
    """
    多義語の周辺語を調べ、(誘導先chapter, ブーストスコア) のリストを返す。
    マッチした共起語が多いほどスコアが高い。
    """
    if word not in _PHRASE_CONTEXT:
        return []
    results = []
    for co_words, target_chapter, base_boost in _PHRASE_CONTEXT[word]:
        hits = sum(1 for cw in co_words if cw in text_lower)
        if hits > 0:
            results.append((target_chapter, base_boost * min(hits, 3)))
    return results


def _score_rule(rule: dict, text: str, ctx: Dict[str, object]) -> float:
    """
    テキストにマッチするキーワード数をスコアとして返す。
    コンテキスト情報を使って多義語のスコアを調整する。
    """
    score = 0.0
    text_lower = text.lower()
    rule_chapter = rule.get("chapter", "")
    is_auto = ctx["is_auto"]

    for kw in rule["keywords"]:
        kw_lower = kw.lower()
        if not _match_keyword(kw_lower, text_lower):
            continue

        # フレーズコンテキストによる多義語解決
        phrase_results = _eval_phrase_context(kw_lower, text_lower)
        if phrase_results:
            # この多義語に対してフレーズコンテキストが見つかった
            best_match = None
            best_boost = 0.0
            for target_ch, boost in phrase_results:
                if boost > best_boost:
                    best_match = target_ch
                    best_boost = boost
            if best_match == rule_chapter:
                # このルールが正しい誘導先 → ブースト
                score += 1.0 + best_boost
            else:
                # このルールは誘導先と異なる → 大幅減点
                score += 0.05
            continue

        # フレーズコンテキストなしの場合、従来のコンテキスト減点
        if is_auto and kw_lower in _PHRASE_CONTEXT:
            # 自動車コンテキストだが共起語がない多義語
            if rule_chapter != "Chapter 87":
                score += 0.1
                continue

        score += 1.0

    # ブランドコンテキストによるブースト
    base_kw_score = score  # キーワードマッチのみのスコア
    brand_ch = ctx.get("brand_chapter")
    if brand_ch:
        if brand_ch == "Chapter 87" and rule_chapter == "Chapter 87":
            score += ctx["auto_boost"]
        elif brand_ch == "Chapter 91" and rule_chapter == "Chapter 91":
            # 時計ブランド → 時計カテゴリに強ブースト
            score += 5.0
        elif brand_ch == rule_chapter and base_kw_score > 0:
            score += 3.0
        elif brand_ch == "Chapter 61" and rule_chapter in ("Chapter 61", "Chapter 62", "Chapter 64", "Chapter 63", "Chapter 42"):
            brand_name = ctx.get("brand_name", "")
            if brand_name in _SHOE_PRIMARY_BRANDS:
                if rule_chapter == "Chapter 64":
                    # 靴メーカーは靴カテゴリにブースト（キーワードマッチ不要）
                    score += 3.0
                elif base_kw_score > 0:
                    # 靴メーカーでも衣料品キーワードが明示マッチ → ブースト
                    score += 2.0
            elif base_kw_score > 0:
                score += 2.0

    # 部品番号パターンで工業製品ブースト
    if ctx["has_part_number"]:
        if rule_chapter in ("Chapter 87", "Chapter 84", "Chapter 85"):
            score += 3.0

    return score


def _extract_product_keywords(product_name: str, ctx: Dict[str, object]) -> str:
    """
    商品名から有用なキーワードを抽出・強化する。
    ブランド名・型番からコンテキストを補強。
    """
    extra_context = []
    name_lower = product_name.lower()

    brand_ch = ctx.get("brand_chapter")
    if brand_ch == "Chapter 87":
        extra_context.append("car parts 自動車部品 カーパーツ automotive vehicle")
    elif brand_ch == "Chapter 85":
        extra_context.append("電子機器 electronics device gadget")
    elif brand_ch == "Chapter 61":
        extra_context.append("衣料品 apparel clothing ファッション")
    elif brand_ch == "Chapter 33":
        extra_context.append("化粧品 cosmetics beauty スキンケア")
    elif brand_ch == "Chapter 91":
        extra_context.append("時計 watch wristwatch 腕時計")

    if ctx["has_part_number"]:
        if not brand_ch:
            # ブランド不明だが部品番号あり → 工業製品
            extra_context.append("car parts 自動車部品 automotive industrial")
        elif brand_ch == "Chapter 87":
            extra_context.append("car parts 自動車部品")

    if "jdm" in name_lower:
        extra_context.append("car parts 自動車部品 automotive vehicle")

    if extra_context:
        return product_name + " " + " ".join(extra_context)
    return product_name


# ============================================================
# Claude AI 分類エンジン
# ============================================================

def _detect_relevant_chapters(
    product_name: str,
    description: str = "",
    item_specifics: Optional[Dict[str, str]] = None,
    category_path: str = "",
) -> List[str]:
    """
    商品情報から関連する HTS Chapter を特定する。
    CLASSIFICATION_RULES のキーワードマッチ + eBay カテゴリ + ブランド情報を使用。
    最大5章を返す。
    """
    combined = "{} {} {}".format(
        product_name,
        description or "",
        category_path or "",
    ).lower()
    if item_specifics:
        combined += " " + " ".join(
            f"{k} {v}" for k, v in item_specifics.items()
        ).lower()

    chapter_scores = {}  # type: Dict[str, float]

    # 1. CLASSIFICATION_RULES のキーワードマッチ
    for rule in CLASSIFICATION_RULES:
        ch = rule.get("chapter", "")
        if not ch:
            continue
        score = 0.0
        for kw in rule["keywords"]:
            if kw.lower() in combined:
                score += 1.0
        if score > 0:
            chapter_scores[ch] = chapter_scores.get(ch, 0) + score

    # 2. ブランド名からの推定
    for brand, ch in _BRAND_CATEGORY.items():
        if brand in combined:
            full_ch = f"Chapter {int(ch.split()[1]):02d}"
            chapter_scores[full_ch] = chapter_scores.get(full_ch, 0) + 3.0

    # 3. eBay カテゴリパスのキーワード → Chapter マッピング
    _CATEGORY_CHAPTER_MAP = {
        "clothing": ["Chapter 61", "Chapter 62"],
        "apparel": ["Chapter 61", "Chapter 62"],
        "shoes": ["Chapter 64"],
        "footwear": ["Chapter 64"],
        "bags": ["Chapter 42"],
        "handbag": ["Chapter 42"],
        "luggage": ["Chapter 42"],
        "watches": ["Chapter 91"],
        "jewelry": ["Chapter 71"],
        "electronics": ["Chapter 85"],
        "cell phones": ["Chapter 85"],
        "computers": ["Chapter 84"],
        "tablets": ["Chapter 84"],
        "laptops": ["Chapter 84"],
        "cameras": ["Chapter 85"],
        "auto parts": ["Chapter 87"],
        "car parts": ["Chapter 87"],
        "vehicle parts": ["Chapter 87"],
        "motors": ["Chapter 87"],
        "toys": ["Chapter 95"],
        "sporting goods": ["Chapter 95"],
        "cosmetics": ["Chapter 33"],
        "health & beauty": ["Chapter 33"],
        "skin care": ["Chapter 33"],
        "fragrance": ["Chapter 33"],
        "home & garden": ["Chapter 94"],
        "furniture": ["Chapter 94"],
        "kitchen": ["Chapter 73", "Chapter 69"],
        "musical instruments": ["Chapter 92"],
        "books": ["Chapter 49"],
        "pet supplies": ["Chapter 42"],
    }
    cat_lower = (category_path or "").lower()
    for cat_kw, chapters in _CATEGORY_CHAPTER_MAP.items():
        if cat_kw in cat_lower:
            for ch in chapters:
                chapter_scores[ch] = chapter_scores.get(ch, 0) + 5.0

    if not chapter_scores:
        # フォールバック: 最も一般的な chapter を返す
        return ["Chapter 85", "Chapter 61", "Chapter 84", "Chapter 42", "Chapter 87"]

    # スコア順にソート、上位5章
    sorted_chs = sorted(chapter_scores.items(), key=lambda x: x[1], reverse=True)
    return [ch for ch, _ in sorted_chs[:5]]


def _build_hts_reference_for_chapters(chapters: List[str]) -> str:
    """
    指定された Chapter の HTS コード一覧をプロンプト用テキストに変換する。
    HTS_CODES (JSON) から取得。トークン節約のため最大 3 章分を含める。
    """
    lines = []
    included = 0
    for ch in chapters:
        if ch not in HTS_CODES:
            continue
        codes = HTS_CODES[ch]
        lines.append(f"=== {ch} ({len(codes)} codes) ===")
        for entry in codes:
            lines.append(f"  {entry['code']}: {entry['description']}")
        included += 1
        if included >= 3:
            break

    if not lines:
        return "(該当するHTSコード一覧が見つかりません)"
    return "\n".join(lines)


def _build_jp_hs_reference_for_chapters(chapters: List[str]) -> str:
    """
    指定された Chapter の日本 HS コード一覧をプロンプト用テキストに変換する。
    JP_HS_CODES (JSON) から取得。
    """
    lines = []
    for ch in chapters[:3]:
        if ch not in JP_HS_CODES:
            continue
        codes = JP_HS_CODES[ch]
        lines.append(f"=== {ch} ===")
        for entry in codes:
            lines.append(f"  {entry['code']}: {entry['category']} | {entry['description']}")
    return "\n".join(lines) if lines else "(該当する日本HSコード一覧が見つかりません)"


def _build_rules_reference_for_chapters(chapters: List[str]) -> str:
    """
    CLASSIFICATION_RULES から指定 Chapter のルールをプロンプト用テキストに変換する。
    """
    lines = []
    for ch in chapters[:3]:
        ch_rules = [r for r in CLASSIFICATION_RULES if r.get("chapter") == ch]
        if not ch_rules:
            continue
        lines.append(f"=== {ch} (キーワードルール) ===")
        for r in ch_rules:
            kw = ", ".join(r["keywords"][:6])
            lines.append(
                f"  HS6: {r['hs6']} | HTS: {r['hts10']} | JP_HS: {r['jp_hs9']}"
                f" | {r['category']} | {r['material']}"
                f" | keywords: {kw}"
            )
    return "\n".join(lines)


def _call_claude_api(
    product_name: str,
    description: str = "",
    item_specifics: Optional[Dict[str, str]] = None,
    category_path: str = "",
) -> Optional[List[Dict]]:
    """
    Claude API で商品を HS コードに分類する。
    1. 商品情報から関連 Chapter を特定
    2. その Chapter の全 HTS コード一覧をプロンプトに含める
    3. Claude に一覧から最適なコードを選ばせる
    失敗時は None を返し、呼び出し元が従来ロジックにフォールバックする。
    """
    api_key = _get_anthropic_api_key()
    if not api_key:
        return None

    # 関連 Chapter を特定
    relevant_chapters = _detect_relevant_chapters(
        product_name, description, item_specifics, category_path
    )

    # 各 Chapter の HTS コード一覧を構築
    hts_ref = _build_hts_reference_for_chapters(relevant_chapters)
    jp_hs_ref = _build_jp_hs_reference_for_chapters(relevant_chapters)
    rules_ref = _build_rules_reference_for_chapters(relevant_chapters)

    system_prompt = (
        "You are an expert in HS/HTS tariff classification.\n\n"
        "【STRICT RULES】\n"
        "- You MUST only use HTS codes that exist in the 'US HTS Code List' below.\n"
        "- Do NOT generate or guess any HTS code not in the list.\n"
        "- Select the most appropriate HTS code (8-digit XXXX.XX.XX) from the list.\n"
        "- For hs6, use the first 6 digits of the HTS code (XXXX.XX).\n"
        "- For jp_hs, use the 'JP HS Code Reference' if available; otherwise append .000 to hs6.\n"
        "- If no code in the list matches well, pick the closest one and set confidence to \"low\"\n"
        "  with an explanation in reason.\n\n"
        "【LANGUAGE RULES】\n"
        "- category, material, usage, reason: MUST be written in Japanese.\n"
        "- hs6, hts, jp_hs, chapter, confidence: keep alphanumeric / English as-is.\n\n"
        f"【Target Chapters】{', '.join(relevant_chapters)}\n\n"
        "【US HTS Code List (select from this list)】\n"
        f"{hts_ref}\n\n"
        "【JP HS Code Reference】\n"
        f"{jp_hs_ref}\n\n"
        "【Keyword Rule Reference】\n"
        f"{rules_ref}\n\n"
        "【RESPONSE FORMAT】\n"
        "Return ONLY the following JSON. No markdown, no explanation:\n"
        '{"candidates": [\n'
        '  {"hs6": "XXXX.XX", "hts": "XXXX.XX.XX", "jp_hs": "XXXX.XX.XXX", '
        '"category": "日本語カテゴリ名", "material": "日本語素材名", "usage": "日本語用途", '
        '"chapter": "Chapter XX", "reason": "日本語で判定理由", "confidence": "high/medium/low"}\n'
        "]}\n"
        "Return up to 3 candidates. Select the best matching codes from the US HTS Code List above."
    )

    # ユーザーメッセージ構築
    specs_text = ""
    if item_specifics:
        specs_text = " | ".join(f"{k}: {v}" for k, v in item_specifics.items())

    desc_short = (description or "")[:500]
    user_msg = f"商品名: {product_name}"
    if desc_short:
        user_msg += f"\n説明: {desc_short}"
    if specs_text:
        user_msg += f"\nItem Specifics: {specs_text}"
    if category_path:
        user_msg += f"\neBayカテゴリ: {category_path}"

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1024,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_msg}],
    }

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers=headers,
            json=payload,
            timeout=30,
        )

        if resp.status_code != 200:
            # エラー詳細をログ出力して原因特定
            try:
                err_body = resp.json()
                err_msg = err_body.get("error", {}).get("message", resp.text[:200])
            except Exception:
                err_msg = resp.text[:200]

            if resp.status_code == 401:
                st.warning("Claude API 認証エラー: APIキーが無効です。キーワードエンジンで判定します。")
            elif resp.status_code == 429:
                st.warning("Claude API レート制限に達しました。キーワードエンジンで判定します。")
            else:
                st.warning(f"Claude API エラー ({resp.status_code}): {err_msg}。キーワードエンジンで判定します。")
            return None
        data = resp.json()

        # レスポンスからテキスト取得
        text = data["content"][0]["text"]

        # JSON部分を抽出（```json ... ``` ブロックにも対応）
        json_match = re.search(r"\{[\s\S]*\}", text)
        if not json_match:
            st.warning("Claude API: レスポンスの解析に失敗しました。キーワードエンジンで判定します。")
            return None

        result = json.loads(json_match.group())
        raw_candidates = result.get("candidates", [])

        if not raw_candidates:
            return None

        # classify_product と同じ形式に正規化
        candidates = []
        for c in raw_candidates[:3]:
            candidates.append({
                "hs6": c.get("hs6", "-----.--"),
                "hts": c.get("hts", "----------"),
                "jp_hs": c.get("jp_hs", "---------"),
                "category": c.get("category", "不明"),
                "material": c.get("material", "不明"),
                "usage": c.get("usage", "不明"),
                "chapter": c.get("chapter", "N/A"),
                "reason": "[AI] " + c.get("reason", "Claude AI による判定"),
                "confidence": c.get("confidence", "medium"),
                "_score": 0,
            })

        # セッションカウンタをインクリメント
        if "claude_api_calls" in st.session_state:
            st.session_state.claude_api_calls += 1

        return candidates

    except requests.exceptions.Timeout:
        st.warning("Claude API タイムアウト（30秒）。キーワードエンジンで判定します。")
        return None
    except json.JSONDecodeError:
        st.warning("Claude API: JSONパースに失敗しました。キーワードエンジンで判定します。")
        return None
    except Exception as e:
        st.warning(f"Claude API エラー: {e}。キーワードエンジンで判定します。")
        return None


def classify_product(
    product_name: str,
    description: str = "",
    item_specifics: Optional[Dict[str, str]] = None,
    category_path: str = "",
) -> List[Dict]:
    """
    商品を HS コードに分類する。

    判定優先順位（上位が優先、矛盾時は上位に従う）:
      1. eBay 最下層カテゴリ名 (Wristwatches, Women's Bags & Handbags 等)
      2. Item Specifics の Type / Category フィールド
      3. Brand + 補助 Item Specifics (Material, Movement 等)
      4. 商品タイトル・説明文のキーワードマッチング

    返り値: list[dict] — hs6, hts, jp_hs, category, material, usage,
                         chapter, reason, confidence, _score
    """
    if item_specifics is None:
        item_specifics = {}

    # ── Step 0: Claude AI による一次判定 ──
    api_key = _get_anthropic_api_key()
    if api_key:
        ai_result = _call_claude_api(product_name, description, item_specifics, category_path)
        if ai_result:
            return ai_result

    # ── Step 1: eBay 構造化データによる判定 ──
    ebay_result = None  # type: Optional[Tuple[str, float, str]]
    if item_specifics or category_path:
        ebay_result = _classify_by_ebay_data(item_specifics, category_path)

    # ── Step 2: キーワードベースの判定 ──
    pre_text = "{} {}".format(product_name, description)
    ctx = _detect_context(pre_text)
    enhanced_name = _extract_product_keywords(product_name, ctx)
    combined = "{} {}".format(enhanced_name, description)
    ctx = _detect_context(combined)

    ebay_chapter = ebay_result[0] if ebay_result else None

    scored = []  # type: List[Tuple[float, Dict]]
    for rule in CLASSIFICATION_RULES:
        s = _score_rule(rule, combined, ctx)
        if s > 0:
            scored.append((s, rule))
        elif ebay_chapter and rule.get("chapter", "") == ebay_chapter:
            # キーワードマッチなしでも eBay データと一致するルールは候補に含める
            scored.append((0.0, rule))

    # ── Step 3: eBay データとキーワードスコアを統合 ──
    if ebay_result is not None:
        ebay_chapter, ebay_confidence, ebay_reason = ebay_result

        # eBay データの確信度に応じてスコアを調整
        adjusted = []  # type: List[Tuple[float, Dict]]
        for s, rule in scored:
            rule_ch = rule.get("chapter", "")
            if rule_ch == ebay_chapter:
                # eBay データと一致するルール → 大幅ブースト
                adjusted.append((s + ebay_confidence, rule))
            else:
                # eBay データと不一致 → 確信度に応じて抑制
                if ebay_confidence >= 80.0:
                    # リーフカテゴリ or Type 一致: 不一致ルールはほぼ無効
                    adjusted.append((s * 0.05, rule))
                elif ebay_confidence >= 60.0:
                    # 親カテゴリ一致: 不一致ルールを大幅減点
                    adjusted.append((s * 0.2, rule))
                else:
                    # Brand のみ: 不一致ルールを軽度減点
                    adjusted.append((s * 0.5, rule))
        scored = adjusted

    scored.sort(key=lambda x: x[0], reverse=True)

    # ── Step 4: 候補リスト生成 ──
    candidates = []  # type: List[Dict]
    seen_hs6 = set()  # type: Set[str]
    for s, rule in scored:
        if rule["hs6"] in seen_hs6:
            continue
        seen_hs6.add(rule["hs6"])
        rule_ch = rule.get("chapter", "")

        # 信頼度判定
        if ebay_result is not None and rule_ch == ebay_result[0]:
            # eBay データと一致するルール
            ec = ebay_result[1]
            if ec >= 70.0:
                confidence = "high"
            elif ec >= 40.0:
                confidence = "medium"
            else:
                confidence = "low"
        else:
            # キーワードのみの判定
            kw_score = s
            # eBay データで抑制されたスコアの場合は低信頼度
            if ebay_result is not None and ebay_result[1] >= 60.0:
                confidence = "low"
            else:
                brand_ch = ctx.get("brand_chapter")
                if brand_ch and brand_ch == "Chapter 87" and rule_ch == "Chapter 87":
                    confidence = "high" if kw_score >= 5.0 else (
                        "medium" if kw_score >= 3.0 else "low")
                elif brand_ch and brand_ch == rule_ch:
                    confidence = "high" if kw_score >= 4.0 else (
                        "medium" if kw_score >= 2.0 else "low")
                else:
                    confidence = "high" if kw_score >= 3.0 else (
                        "medium" if kw_score >= 2.0 else "low")

        # 判定理由: どの情報を根拠にしたか明示
        reason = rule["reason"]
        if ebay_result is not None and rule_ch == ebay_result[0]:
            reason = "[{}] | {}".format(ebay_result[2], reason)

        candidates.append(
            {
                "hs6": rule["hs6"],
                "hts": rule["hts10"],
                "jp_hs": rule["jp_hs9"],
                "category": rule["category"],
                "material": rule["material"],
                "usage": rule["usage"],
                "chapter": rule["chapter"],
                "reason": reason,
                "confidence": confidence,
                "_score": round(s, 1),
            }
        )
        if len(candidates) >= 3:
            break

    if not candidates:
        candidates.append(
            {
                "hs6": "-----.--",
                "hts": "----------",
                "jp_hs": "---------",
                "category": "不明",
                "material": "不明",
                "usage": "不明",
                "chapter": "N/A",
                "reason": "自動分類できませんでした。商品名や説明文をより具体的に入力してください。",
                "confidence": "low",
                "_score": 0,
            }
        )

    return candidates


# ============================================================
# Streamlit UI
# ============================================================


def render_copy_button(label: str, value: str, key: str) -> None:
    """
    コピーボタン — components.html で JS を実行し、クリップボードにコピーする。
    コード表示を全幅で上に、コピーボタンを全幅で下に縦積み表示。
    """
    st.code(value, language=None)
    escaped_value = value.replace("\\", "\\\\").replace("'", "\\'")
    components.html(
        """
        <button id="btn" style="
            padding:12px 24px; background:#4CAF50; color:#fff;
            border:none; border-radius:4px; cursor:pointer;
            font-size:18px; min-height:48px; width:100%%;
            transition: background 0.2s, transform 0.1s;
            font-family: sans-serif;
        ">%(label)s をコピー</button>
        <script>
        var btn = document.getElementById('btn');
        btn.addEventListener('click', function() {
            var text = '%(value)s';
            function tryClipboardAPI() {
                if (window.parent && window.parent.navigator && window.parent.navigator.clipboard) {
                    return window.parent.navigator.clipboard.writeText(text);
                }
                return Promise.reject();
            }
            function fallbackCopy() {
                var ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.left = '-9999px';
                document.body.appendChild(ta);
                ta.select();
                try {
                    document.execCommand('copy');
                    return true;
                } catch(e) {
                    return false;
                } finally {
                    document.body.removeChild(ta);
                }
            }
            function onSuccess() {
                btn.innerHTML = '&#10003; コピーしました';
                btn.style.background = '#1e7e34';
                btn.style.transform = 'scale(0.95)';
                setTimeout(function(){ btn.style.transform = 'scale(1)'; }, 100);
                setTimeout(function(){
                    btn.innerHTML = '%(label)s をコピー';
                    btn.style.background = '#4CAF50';
                }, 2000);
            }
            function onFail() {
                btn.innerHTML = '&#10007; 失敗';
                btn.style.background = '#dc3545';
                setTimeout(function(){
                    btn.innerHTML = '%(label)s をコピー';
                    btn.style.background = '#4CAF50';
                }, 2000);
            }
            tryClipboardAPI().then(onSuccess).catch(function(){
                if (fallbackCopy()) { onSuccess(); } else { onFail(); }
            });
        });
        </script>
        """ % {"label": label, "value": escaped_value},
        height=55,
    )


# ── ロゴ画像の読み込み (ファビコン & ヘッダー用) ──
_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
with open(_LOGO_PATH, "rb") as _f:
    _LOGO_PNG_BYTES = _f.read()
_LOGO_PNG_B64 = base64.b64encode(_LOGO_PNG_BYTES).decode()

# PIL Image (ファビコン用)
from PIL import Image as _PILImage
_FAVICON = _PILImage.open(io.BytesIO(_LOGO_PNG_BYTES))


def main() -> None:
    """Streamlit アプリのエントリポイント。"""

    # ページ設定
    st.set_page_config(
        page_title="HS/HTS コード判定ツール",
        page_icon=_FAVICON,
        layout="wide",
    )

    # カスタム CSS
    st.markdown(
        """
        <style>
        .main-header {
            text-align: center;
            padding: 1rem 0;
            background: linear-gradient(135deg, #1e3a5f 0%, #2e5984 100%);
            color: #ffffff !important;
            border-radius: 8px;
            margin-bottom: 1.5rem;
        }
        .main-header h1 { margin: 0; font-size: 2rem; color: #ffffff !important; }
        .main-header p  { margin: 0.3rem 0 0 0; color: rgba(255,255,255,0.9) !important; font-size: 0.95rem; }
        .warning-box {
            background: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 12px 16px;
            border-radius: 4px;
            margin-top: 16px;
            color: #856404;
        }
        .result-card {
            background: #f8f9fa;
            border: 1px solid #dee2e6;
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 12px;
        }
        .confidence-high   { color: #28a745; font-weight: bold; }
        .confidence-medium  { color: #ffc107; font-weight: bold; }
        .confidence-low     { color: #dc3545; font-weight: bold; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # メニュー日本語化（components.html で親フレームの DOM を操作）
    components.html(
        """
        <script>
        const T = {
            "Rerun":                "再実行",
            "Settings":             "設定",
            "Print":                "印刷",
            "Record a screencast":  "画面録画",
            "About":                "このアプリについて",
            "Developer options":    "開発者オプション",
            "Clear cache":          "キャッシュクリア",
            "Deploy this app":      "アプリをデプロイ",
            "Report a bug":         "バグを報告",
            "Get help":             "ヘルプ"
        };
        function walk(node) {
            if (node.nodeType === 3) {
                var t = node.textContent.trim();
                if (T[t]) node.textContent = node.textContent.replace(t, T[t]);
                return;
            }
            for (var i = 0; i < node.childNodes.length; i++) walk(node.childNodes[i]);
        }
        function run() {
            var doc = window.parent.document;
            var els = doc.querySelectorAll(
                'ul[data-testid="main-menu-list"] li,' +
                'ul[data-testid="main-menu-list"] span,' +
                '[role="menuitem"],' +
                'header [data-testid] span'
            );
            els.forEach(function(el) { walk(el); });
        }
        var obs = new MutationObserver(function() { run(); });
        obs.observe(window.parent.document.body, {childList: true, subtree: true});
        </script>
        """,
        height=0,
    )

    # ヘッダー
    st.markdown(
        '<div class="main-header">'
        '<h1>HS / HTS コード判定ツール</h1>'
        '<p>商品情報から HS / HTS コードを自動推定します</p>'
        '</div>'.format(b64=_LOGO_PNG_B64),
        unsafe_allow_html=True,
    )

    # ── セッション初期化 ──
    if "admin_mode" not in st.session_state:
        st.session_state.admin_mode = False
    if "results" not in st.session_state:
        st.session_state.results = []
    if "scraped_info" not in st.session_state:
        st.session_state.scraped_info = {}
    if "selected_candidate" not in st.session_state:
        st.session_state.selected_candidate = None
    if "pending_save" not in st.session_state:
        st.session_state.pending_save = {}
    if "claude_api_calls" not in st.session_state:
        st.session_state.claude_api_calls = 0

    # ── localStorage からAPIキーを復元 ──
    _load_local_storage_keys()
    _process_local_storage_ops()

    # ── サイドバー: 管理者モード ──
    with st.sidebar:
        st.header("⚙️ 設定")
        if not st.session_state.admin_mode:
            pw = st.text_input("管理者パスワード", type="password", key="admin_pw")
            if st.button("管理者モードに切替"):
                if hashlib.sha256(pw.encode()).hexdigest() == ADMIN_PASSWORD_HASH:
                    st.session_state.admin_mode = True
                    try:
                        st.rerun()
                    except AttributeError:
                        st.experimental_rerun()
                else:
                    st.error("パスワードが正しくありません。")
        else:
            st.success("管理者モード: ON")
            if st.button("管理者モードを終了"):
                st.session_state.admin_mode = False
                try:
                    st.rerun()
                except AttributeError:
                    st.experimental_rerun()

        st.divider()
        # ── eBay API 設定 ──
        st.subheader("eBay API")
        ebay_cid, ebay_csec = _get_ebay_client_credentials()
        ebay_configured = bool(ebay_cid and ebay_csec)
        if ebay_configured:
            if _is_ebay_preconfigured():
                st.success("接続済み（事前設定）")
            else:
                masked_cid = ebay_cid[:8] + "..." if len(ebay_cid) > 8 else "****"
                st.success(f"接続済み（{masked_cid}）")
                stored_pin = st.session_state.get("_ls_settings_pin", "")
                if stored_pin:
                    pin_ebay_dc = st.text_input(
                        "管理PIN", type="password", key="pin_ebay_dc",
                        placeholder="接続解除にはPINが必要",
                    )
                if st.button("接続解除", key="btn_ebay_disconnect"):
                    if stored_pin and hashlib.sha256(pin_ebay_dc.encode()).hexdigest() != stored_pin:
                        st.error("管理PINが正しくありません。")
                    else:
                        # localStorage から削除
                        st.session_state["_ls_remove_ebay"] = True
                        st.session_state.pop("_ls_ebay_client_id", None)
                        st.session_state.pop("_ls_ebay_client_secret", None)
                        st.session_state.pop("_ls_keys_loaded", None)
                        # DB からも削除（レガシー互換）
                        delete_setting("ebay_client_id")
                        delete_setting("ebay_client_secret")
                        # キャッシュもクリア
                        st.session_state.pop("_ebay_token", None)
                        st.session_state.pop("_ebay_token_expires", None)
                        # 他のキーが残っていなければ PIN も削除
                        if not st.session_state.get("_ls_anthropic_api_key", ""):
                            st.session_state["_ls_remove_pin"] = True
                            st.session_state.pop("_ls_settings_pin", None)
                        st.info("eBay API 設定を削除しました。")
                        try:
                            st.rerun()
                        except AttributeError:
                            st.experimental_rerun()
        else:
            st.caption("未設定（eBay URLの商品取得に必要）")
            with st.expander("API設定", expanded=True):
                st.markdown(
                    "**取得手順:**\n"
                    "1. [developer.ebay.com](https://developer.ebay.com) にログイン\n"
                    "2. **Application Keys** ページを開く\n"
                    "3. Production 環境の **App ID (Client ID)** と "
                    "**Cert ID (Client Secret)** をコピー\n"
                    "4. 下のフィールドに貼り付けて保存\n\n"
                    "*トークンは自動取得・更新されます（有効期限2時間）*"
                )
                ebay_cid_input = st.text_input(
                    "Client ID (App ID)",
                    type="password",
                    key="ebay_client_id_input",
                    placeholder="YourApp-Produc-PRD-xxxxxxxxx-xxxxxxxx",
                )
                ebay_csec_input = st.text_input(
                    "Client Secret (Cert ID)",
                    type="password",
                    key="ebay_client_secret_input",
                    placeholder="PRD-xxxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                )
                _existing_pin = st.session_state.get("_ls_settings_pin", "")
                if not _existing_pin:
                    ebay_pin_input = st.text_input(
                        "管理PIN（4桁以上）",
                        type="password",
                        key="ebay_pin_input",
                        placeholder="接続解除時に必要です",
                        help="このPINを知っている人だけが接続解除・変更できます",
                    )
                if st.button("保存して接続", key="btn_ebay_save"):
                    cid_val = ebay_cid_input.strip()
                    csec_val = ebay_csec_input.strip()
                    pin_val = "" if _existing_pin else ebay_pin_input.strip()
                    if not cid_val or not csec_val:
                        st.warning("Client ID と Client Secret の両方を入力してください。")
                    elif not _existing_pin and len(pin_val) < 4:
                        st.warning("管理PINは4桁以上で入力してください。")
                    else:
                        # 接続テスト
                        test_token = _fetch_ebay_app_token(cid_val, csec_val)
                        if test_token:
                            # localStorage に保存（ブラウザ永続化）
                            st.session_state["_ls_save_ebay"] = (cid_val, csec_val)
                            st.session_state["_ls_ebay_client_id"] = cid_val
                            st.session_state["_ls_ebay_client_secret"] = csec_val
                            # PIN を保存（未設定の場合のみ）
                            if not _existing_pin:
                                pin_hash = hashlib.sha256(pin_val.encode()).hexdigest()
                                st.session_state["_ls_save_pin"] = pin_hash
                                st.session_state["_ls_settings_pin"] = pin_hash
                            st.success("接続成功。ブラウザに保存しました。")
                            try:
                                st.rerun()
                            except AttributeError:
                                st.experimental_rerun()
                        else:
                            st.error("接続失敗: Client ID/Secret を確認してください。")

        st.divider()
        # ── Claude AI 分類設定 ──
        st.subheader("Claude AI 分類")
        claude_key = _get_anthropic_api_key()
        if claude_key:
            if _is_key_preconfigured("ANTHROPIC_API_KEY"):
                st.success("接続済み（事前設定）")
            else:
                masked_ck = claude_key[:8] + "..." if len(claude_key) > 8 else "****"
                st.success(f"接続済み（{masked_ck}）")
                stored_pin = st.session_state.get("_ls_settings_pin", "")
                if stored_pin:
                    pin_claude_dc = st.text_input(
                        "管理PIN", type="password", key="pin_claude_dc",
                        placeholder="接続解除にはPINが必要",
                    )
                if st.button("接続解除", key="btn_claude_disconnect"):
                    if stored_pin and hashlib.sha256(pin_claude_dc.encode()).hexdigest() != stored_pin:
                        st.error("管理PINが正しくありません。")
                    else:
                        # localStorage から削除
                        st.session_state["_ls_remove_claude"] = True
                        st.session_state.pop("_ls_anthropic_api_key", None)
                        st.session_state.pop("_ls_keys_loaded", None)
                        # DB からも削除（レガシー互換）
                        delete_setting("anthropic_api_key")
                        # 他のキーが残っていなければ PIN も削除
                        if not st.session_state.get("_ls_ebay_client_id", ""):
                            st.session_state["_ls_remove_pin"] = True
                            st.session_state.pop("_ls_settings_pin", None)
                        st.info("Claude API キーを削除しました。")
                        try:
                            st.rerun()
                        except AttributeError:
                            st.experimental_rerun()
            used = st.session_state.get("claude_api_calls", 0)
            st.caption(f"利用回数: {used} 回")
        else:
            st.caption("未設定（AI による高精度分類に必要）")
            with st.expander("APIキーを設定", expanded=False):
                st.markdown(
                    "**取得手順:**\n"
                    "1. [console.anthropic.com](https://console.anthropic.com) にログイン\n"
                    "2. **API Keys** ページを開く\n"
                    "3. **Create Key** をクリックしてキーを作成\n"
                    "4. 下のフィールドに貼り付けて保存"
                )
                claude_key_input = st.text_input(
                    "API Key",
                    type="password",
                    key="claude_api_key_input",
                    placeholder="sk-ant-...",
                )
                _existing_pin_c = st.session_state.get("_ls_settings_pin", "")
                if not _existing_pin_c:
                    claude_pin_input = st.text_input(
                        "管理PIN（4桁以上）",
                        type="password",
                        key="claude_pin_input",
                        placeholder="接続解除時に必要です",
                        help="このPINを知っている人だけが接続解除・変更できます",
                    )
                if st.button("保存して接続", key="btn_claude_save"):
                    key_val = claude_key_input.strip()
                    pin_val = "" if _existing_pin_c else claude_pin_input.strip()
                    if not key_val:
                        st.warning("APIキーを入力してください。")
                    elif not _existing_pin_c and len(pin_val) < 4:
                        st.warning("管理PINは4桁以上で入力してください。")
                    else:
                        # localStorage に保存（ブラウザ永続化）
                        st.session_state["_ls_save_claude"] = key_val
                        st.session_state["_ls_anthropic_api_key"] = key_val
                        # PIN を保存（未設定の場合のみ）
                        if not _existing_pin_c:
                            pin_hash = hashlib.sha256(pin_val.encode()).hexdigest()
                            st.session_state["_ls_save_pin"] = pin_hash
                            st.session_state["_ls_settings_pin"] = pin_hash
                        st.success("APIキーを保存しました。ブラウザに記憶されます。")
                        try:
                            st.rerun()
                        except AttributeError:
                            st.experimental_rerun()

        st.divider()
        # ── 共有コード ──
        _any_user_key = bool(
            st.session_state.get("_ls_anthropic_api_key")
            or st.session_state.get("_ls_ebay_client_id")
        )
        _has_pin = bool(st.session_state.get("_ls_settings_pin"))
        _any_key_at_all = _get_anthropic_api_key() is not None or bool(
            _get_ebay_client_credentials()[0]
        )

        if _any_key_at_all and _has_pin:
            # ── 管理者用: 共有コード生成 ──
            with st.expander("外注さんに共有"):
                st.caption(
                    "共有コードを外注さんに渡すと、"
                    "同じAPI設定がそのブラウザに設定されます"
                )
                share_pin = st.text_input(
                    "管理PIN", type="password", key="share_pin_input",
                    placeholder="PINを入力して共有コードを表示",
                )
                if st.button("共有コードを表示", key="btn_show_share"):
                    stored = st.session_state.get("_ls_settings_pin", "")
                    if (
                        stored
                        and hashlib.sha256(share_pin.encode()).hexdigest()
                        == stored
                    ):
                        code = _generate_share_code()
                        st.code(code, language=None)
                        st.caption(
                            "このコードをコピーして外注さんに送ってください"
                        )
                    else:
                        st.error("管理PINが正しくありません。")

        if not _any_key_at_all:
            # ── 外注さん用: 共有コード入力 ──
            st.subheader("共有コードで設定")
            st.caption("管理者から受け取ったコードを貼り付けてください")
            share_code_input = st.text_input(
                "共有コード", key="share_code_input",
                placeholder="HTS-...",
            )
            if st.button("設定する", key="btn_apply_share"):
                data = _decode_share_code(share_code_input)
                if data is None:
                    st.error("共有コードが正しくありません。")
                else:
                    if data.get("ak"):
                        st.session_state["_ls_save_claude"] = data["ak"]
                        st.session_state["_ls_anthropic_api_key"] = data["ak"]
                    if data.get("eci") and data.get("ecs"):
                        st.session_state["_ls_save_ebay"] = (
                            data["eci"], data["ecs"],
                        )
                        st.session_state["_ls_ebay_client_id"] = data["eci"]
                        st.session_state["_ls_ebay_client_secret"] = data["ecs"]
                    if data.get("pin"):
                        st.session_state["_ls_save_pin"] = data["pin"]
                        st.session_state["_ls_settings_pin"] = data["pin"]
                    st.success("API設定を復元しました。")
                    try:
                        st.rerun()
                    except AttributeError:
                        st.experimental_rerun()

        st.divider()
        st.caption("HS / HTS 自動判定ツール v2.0")
        st.caption("ルールベース + AI 分類エンジン")

    # ==========================
    # 使い方ガイド
    # ==========================
    with st.expander("📖 使い方"):
        st.markdown(
            "**Step 1:** 商品URLを貼り付け（またはタイトルを入力）\n\n"
            "**Step 2:**「判定を実行」ボタンをクリック\n\n"
            "**Step 3:** 表示されたHSコードをコピー"
        )

    # ==========================
    # APIキー未設定時の案内表示
    # ==========================
    _has_claude = _get_anthropic_api_key() is not None
    _ebay_cid, _ebay_csec = _get_ebay_client_credentials()
    _has_ebay = bool(_ebay_cid and _ebay_csec)
    if not _has_claude and not _has_ebay:
        st.info("サイドバーからAPIキーを設定するとAI高精度判定とeBay自動取得が使えます")
    elif not _has_claude:
        st.info("Claude APIキーを設定するとAI高精度判定が使えます")
    elif not _has_ebay:
        st.info("eBay APIキーを設定するとURL自動取得が使えます")

    # ==========================
    # 入力エリア
    # ==========================
    st.subheader("🔍 商品情報を入力")

    col_url, col_name = st.columns(2)
    with col_url:
        input_url = st.text_input("商品URL（任意）", placeholder="https://example.com/product/123")
    with col_name:
        input_name = st.text_input("商品名（任意）", placeholder="例: メンズ 綿 Tシャツ")

    input_desc = st.text_area(
        "商品説明（任意）",
        placeholder="商品の素材、用途、特徴などを入力すると判定精度が向上します",
        height=80,
        key="input_description",
    )

    mode = st.radio(
        "判定モード",
        ["HTS（米国）", "HS（日本）", "両方"],
        horizontal=True,
        index=2,
    )

    # 判定ボタン
    if st.button("🚀 判定を実行", type="primary", use_container_width=True):
        if not input_url and not input_name:
            st.warning("商品URLまたは商品名を入力してください。")
        else:
            product_name = input_name
            description = input_desc or ""
            fetch_failed = False
            item_specifics_text = ""
            ebay_item_specifics = {}  # type: Dict[str, str]
            ebay_category_path = ""

            # 商品情報取得（eBay API または スクレイピング）
            if input_url:
                spinner_msg = "eBay API で商品情報を取得中..." if _is_ebay_url(input_url) else "商品ページを取得中..."
                with st.spinner(spinner_msg):
                    info = fetch_product_info(input_url)
                    st.session_state.scraped_info = info

                    if info.get("error"):
                        fetch_failed = True
                        err_detail = info.get("error", "")
                        st.info(
                            "URL からの情報取得ができませんでした。"
                            "商品名と説明文をもとに判定します。"
                        )
                        if err_detail:
                            st.caption(f"詳細: {err_detail}")
                    if info.get("_api_error"):
                        st.caption(f"eBay API: {info['_api_error']}（スクレイピングで取得）")
                    else:
                        # タイトル取得
                        if not product_name and info.get("title"):
                            product_name = info["title"]

                        # 説明文取得
                        if info.get("description"):
                            description = (description + " " + str(info["description"])).strip()

                        # Item Specifics をテキスト化して判定に活用
                        specifics = info.get("item_specifics", {})
                        if specifics:
                            item_specifics_text = _build_description_from_specifics(specifics)
                            ebay_item_specifics = specifics

                        # カテゴリパスを保持
                        if info.get("category_path"):
                            ebay_category_path = str(info["category_path"])

                        # 取得結果を表示
                        source = info.get("source", "")
                        if source == "ebay_api":
                            st.success(f"eBay API で取得: {info.get('title', '')}")
                            # Item Specifics を展開表示
                            if specifics:
                                with st.expander("📋 Item Specifics（商品属性）", expanded=False):
                                    spec_cols = st.columns(2)
                                    items = list(specifics.items())
                                    half = (len(items) + 1) // 2
                                    for i, (k, v) in enumerate(items):
                                        col = spec_cols[0] if i < half else spec_cols[1]
                                        with col:
                                            st.markdown(f"**{k}:** {v}")
                            # カテゴリパス表示
                            cat_path = info.get("category_path", "")
                            if cat_path:
                                st.caption(f"eBay カテゴリ: {cat_path}")
                        else:
                            st.info(f"取得タイトル: {info.get('title', '')}")

            if not product_name:
                st.warning("商品名を特定できませんでした。商品名を手動入力してください。")
            else:
                # Item Specifics テキストを説明文に追加して判定精度を向上
                full_description = description
                if item_specifics_text:
                    full_description = (full_description + " " + item_specifics_text).strip()

                # 分類実行
                _cls_spinner = "AI分析中..." if _get_anthropic_api_key() else "HSコードを判定中..."
                with st.spinner(_cls_spinner):
                    candidates = classify_product(
                        product_name,
                        full_description,
                        item_specifics=ebay_item_specifics,
                        category_path=ebay_category_path,
                    )
                    st.session_state.results = candidates
                    st.session_state.selected_candidate = None

                    if fetch_failed and product_name:
                        st.info("商品名のキーワード解析で判定しました。")

                    # 最有力候補を自動保存（Lowの場合はユーザー選択後に保存）
                    best = candidates[0]
                    if best["confidence"] != "low" or len(candidates) == 1:
                        save_result(
                            {
                                "product_name": product_name,
                                "url": input_url,
                                "hs6": best["hs6"],
                                "hts": best["hts"],
                                "jp_hs": best["jp_hs"],
                                "confidence": best["confidence"],
                                "reason": best["reason"],
                                "category": best["category"],
                                "material": best["material"],
                                "usage": best["usage"],
                                "chapter": best["chapter"],
                            }
                        )
                    else:
                        st.session_state.pending_save = {
                            "product_name": product_name,
                            "url": input_url,
                        }

    # ==========================
    # 結果表示エリア
    # ==========================
    if st.session_state.results:
        st.divider()
        st.subheader("📋 判定結果")

        all_candidates = st.session_state.results
        top_confidence = all_candidates[0]["confidence"] if all_candidates else "low"
        is_low_multi = top_confidence == "low" and len(all_candidates) > 1

        # 信頼度Lowで複数候補 → 手動選択UI
        if is_low_multi:
            st.info(
                "信頼度が低いため複数の候補を表示しています。"
                "正しいカテゴリを選択するか、商品説明を追加して再判定してください。"
            )

        for idx, cand in enumerate(all_candidates):
            rank_label = ["🥇 第1候補", "🥈 第2候補", "🥉 第3候補"][idx] if idx < 3 else f"候補 {idx+1}"
            conf = cand["confidence"]
            conf_class = f"confidence-{conf}"
            conf_label = CONFIDENCE_LABELS.get(conf, conf)

            with st.container():
                st.markdown(f"### {rank_label}")
                st.markdown(f'<div class="result-card">', unsafe_allow_html=True)

                rc1, rc2 = st.columns(2)
                with rc1:
                    st.markdown(f"**カテゴリ:** {cand['category']}")
                    st.markdown(f"**素材:** {cand['material']}")
                    st.markdown(f"**用途:** {cand['usage']}")
                with rc2:
                    st.markdown(
                        f"**信頼度:** <span class='{conf_class}'>{conf_label}</span>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(f"**参照Chapter:** {cand['chapter']}")

                st.markdown(f"**分類理由:** {cand['reason']}")

                # コード表示 — モードに応じて
                st.markdown("---")
                code_cols = st.columns(3)

                with code_cols[0]:
                    st.markdown("**6桁 HS コード**")
                    render_copy_button("HS6", cand["hs6"], f"hs6_{idx}")

                if mode in ["HTS（米国）", "両方"]:
                    with code_cols[1]:
                        st.markdown("**HTS コード（米国10桁）**")
                        render_copy_button("HTS", cand["hts"], f"hts_{idx}")

                if mode in ["HS（日本）", "両方"]:
                    with code_cols[2]:
                        st.markdown("**日本 HS コード（9桁）**")
                        render_copy_button("JP_HS", cand["jp_hs"], f"jphs_{idx}")

                # Low信頼度で複数候補 → 「この候補を採用」ボタン
                if is_low_multi:
                    if st.button(
                        f"✅ この候補を採用（{cand['category']}）",
                        key=f"select_cand_{idx}",
                    ):
                        st.session_state.selected_candidate = idx
                        # 採用された候補を履歴に保存
                        pending = st.session_state.get("pending_save", {})
                        save_result(
                            {
                                "product_name": pending.get("product_name", ""),
                                "url": pending.get("url", ""),
                                "hs6": cand["hs6"],
                                "hts": cand["hts"],
                                "jp_hs": cand["jp_hs"],
                                "confidence": "manual",
                                "reason": cand["reason"],
                                "category": cand["category"],
                                "material": cand["material"],
                                "usage": cand["usage"],
                                "chapter": cand["chapter"],
                            }
                        )
                        st.success(
                            f"「{cand['category']}」(HS: {cand['hs6']}) を採用し、"
                            "履歴に保存しました。"
                        )

                st.markdown("</div>", unsafe_allow_html=True)

        # 注意文
        st.markdown(
            '<div class="warning-box">※最終的な関税分類は通関業者へ確認してください。</div>',
            unsafe_allow_html=True,
        )

    # ==========================
    # 履歴表示エリア
    # ==========================
    st.divider()
    st.subheader("📜 判定履歴")
    st.divider()

    search_q = st.text_input("履歴を検索（商品名・URL）", key="history_search")
    history = fetch_history(search_q)

    if not history:
        st.info("履歴がありません。")
    else:
        # CSV ダウンロードボタン（テーブル上部）
        csv_buffer = io.StringIO()
        writer = csv.DictWriter(
            csv_buffer,
            fieldnames=[
                "id", "created_at", "product_name", "url", "hs6", "hts",
                "jp_hs", "confidence", "reason", "category", "material",
                "usage_", "chapter",
            ],
        )
        writer.writeheader()
        for row in history:
            writer.writerow(row)

        st.download_button(
            label="📥 履歴を CSV でダウンロード",
            data=csv_buffer.getvalue().encode("utf-8-sig"),
            file_name=f"hs_hts_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
        )

        # テーブル表示
        display_data = []
        for row in history:
            display_data.append(
                {
                    "日時": row["created_at"],
                    "商品名": row["product_name"],
                    "URL": row.get("url", ""),
                    "HS6": row["hs6"],
                    "HTS": row["hts"],
                    "日本HS": row["jp_hs"],
                    "信頼度": row["confidence"],
                }
            )
        st.dataframe(display_data, use_container_width=True)

        # ── 管理者モード: 削除・修正 ──
        if st.session_state.admin_mode:
            with st.expander("🔧 管理者操作"):
                # 履歴削除
                st.markdown("**履歴の削除**")
                del_id = st.number_input(
                    "削除する履歴 ID", min_value=1, step=1, key="del_id"
                )
                if st.button("🗑️ この履歴を削除", key="btn_delete"):
                    delete_history_row(int(del_id))
                    st.success(f"ID {int(del_id)} の履歴を削除しました。")
                    try:
                        st.rerun()
                    except AttributeError:
                        st.experimental_rerun()

                # コード修正
                st.markdown("**コードの手動修正**")
                edit_id = st.number_input(
                    "修正する履歴 ID", min_value=1, step=1, key="edit_id"
                )
                edit_field = st.selectbox(
                    "修正フィールド",
                    ["hs6", "hts", "jp_hs", "confidence", "reason"],
                    key="edit_field",
                )

                # 現在の値を表示
                target_rows = [r for r in history if r["id"] == int(edit_id)]
                current_val = ""
                if target_rows:
                    current_val = target_rows[0].get(edit_field, "")
                    st.info(f"現在の値: {current_val}")

                new_val = st.text_input("新しい値", key="edit_new_val")
                if st.button("✏️ 修正を保存", key="btn_edit"):
                    if new_val:
                        update_history_field(int(edit_id), edit_field, current_val, new_val)
                        st.success(f"ID {int(edit_id)} の {edit_field} を更新しました。")
                        try:
                            st.rerun()
                        except AttributeError:
                            st.experimental_rerun()
                    else:
                        st.warning("新しい値を入力してください。")

                # 修正ログ表示
                st.markdown("**修正履歴ログ**")
                log_id = st.number_input(
                    "ログを表示する履歴 ID", min_value=1, step=1, key="log_id"
                )
                if st.button("修正ログを表示", key="btn_log"):
                    logs = fetch_edit_log(int(log_id))
                    if logs:
                        for log in logs:
                            st.markdown(
                                f"- `{log['edited_at']}` — **{log['field']}**: "
                                f"`{log['old_value']}` → `{log['new_value']}`"
                            )
                    else:
                        st.info("修正ログはありません。")

    # フッター注意文（常に表示）
    st.divider()
    st.markdown(
        '<div class="warning-box">※最終的な関税分類は通関業者へ確認してください。</div>',
        unsafe_allow_html=True,
    )


# ============================================================
# エントリポイント
# ============================================================
if __name__ == "__main__":
    main()
