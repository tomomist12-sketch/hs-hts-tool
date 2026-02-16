"""
HTS PDF (finalCopy_2026HTSRev3.pdf) から全 Chapter の
サブヘディング（8桁 XXXX.XX.XX 形式）と説明文を抽出して JSON に書き出す。

出力: hts_codes.json
形式: { "Chapter XX": [ {"code": "XXXX.XX.XX", "description": "..."}, ... ] }

Chapter はコードの上位2桁から決定する（ページフッターに依存しない）。
"""

import fitz
import json
import re
from collections import OrderedDict

PDF_PATH = "/Users/sekitomomi/Downloads/finalCopy_2026HTSRev3.pdf"
OUTPUT_PATH = "/Users/sekitomomi/hs-hts-tool/hts_codes.json"

CODE_8_RE = re.compile(r'^(\d{4}\.\d{2}\.\d{2})\b')
SUBHEADING_RE = re.compile(r'^(\d{4}\.\d{2})\b')

# スキップする行パターン
_SKIP_PATTERNS = [
    re.compile(r'^[\d.]+[%¢/]'),          # duty rates
    re.compile(r'^Free\b'),                # Free duty
    re.compile(r'^\d{2}$'),                # stat suffix only
    re.compile(r'^\d+/$'),                 # footnote number
    re.compile(r'^[IVXLC]+$'),             # Roman numerals
    re.compile(r'^\d{1,2}-\d+$'),          # page ref like "85-44"
    re.compile(r'^Harmonized Tariff'),
    re.compile(r'^Annotated'),
    re.compile(r'^Statistical Reporting'),
    re.compile(r'^Revision \d'),
]

_SKIP_EXACT = frozenset([
    '', 'Rates of Duty', 'Unit', 'of', 'Quantity',
    'Article Description', 'Stat.', 'Suf-', 'fix',
    'Heading/', 'Subheading', 'General', 'Special',
    '1', '2', '..................', 'No.', 'No',
    'kg', 'doz.', 'doz', 'lt', 'm2', 'prs', 'gross',
])

# 説明文クリーンアップ: 除去するパターン
_DESC_NOISE = [
    re.compile(r'\bFree\s*\([A-Z+, ]+\)'),                           # Free (A, AU, BH, ...)
    re.compile(r'\b\d+\.?\d*%\s*$'),                                   # trailing duty rate
    re.compile(r'\b(?:Free|No|kg|doz|lt|m2|prs|gross)\s*$'),           # trailing units
    re.compile(r'\b\d+/\d+[%¢]'),                                      # fractional duty
    re.compile(r'\b\d+\.?\d*¢/kg\b'),                                  # specific duty
    re.compile(r'\([A-Z]{1,2}(?:,\s*[A-Z]{1,2})+\)'),                 # trade agreement codes
    re.compile(r'\b(?:AU|BH|CL|CO|D|E|IL|JO|KR|MA|OM|P|PA|PE|S|SG)\b(?:,\s*(?:AU|BH|CL|CO|D|E|IL|JO|KR|MA|OM|P|PA|PE|S|SG)\b)+'),
    re.compile(r'^\s*\d{2}\s+(?:No|kg|doz|lt|m2|prs)'),               # stat suffix + unit
    re.compile(r'\(\d{3}\)'),                                           # category numbers like (369)
]


def _should_skip(line):
    """この行を説明文収集からスキップすべきか。"""
    if line in _SKIP_EXACT:
        return True
    for pat in _SKIP_PATTERNS:
        if pat.match(line):
            return True
    return False


def _clean_description(desc):
    """説明文からノイズを除去する。"""
    for pat in _DESC_NOISE:
        desc = pat.sub('', desc)
    # ドットリーダー除去
    desc = re.sub(r'\.{2,}', '', desc)
    # "(con.)" を除去
    desc = desc.replace('(con.)', '')
    # 連続空白を統一
    desc = re.sub(r'\s+', ' ', desc).strip()
    # 先頭の "No " を除去
    desc = re.sub(r'^No\s+', '', desc)
    # 末尾の "No" を除去
    desc = re.sub(r'\s+No\.?$', '', desc)
    # 先頭末尾のコロン・パイプを除去
    desc = desc.strip(':| ')
    return desc


def extract_hts_codes():
    doc = fitz.open(PDF_PATH)
    total_pages = doc.page_count
    print(f"Total pages: {total_pages}")

    all_codes = {}  # chapter_num -> list of {code, description}

    start_page = 911
    end_page = 4074
    codes_count = 0

    for page_idx in range(start_page, min(end_page + 1, total_pages)):
        text = doc[page_idx].get_text()

        if page_idx % 200 == 0:
            print(f"  Processing page {page_idx + 1}/{end_page + 1} ... ({codes_count} codes so far)")

        if 'Rates of Duty' not in text:
            continue

        lines = text.split('\n')

        i = 0
        while i < len(lines):
            line = lines[i].strip()

            m8 = CODE_8_RE.match(line)
            if m8:
                code = m8.group(1)

                # Chapter をコードの上位2桁から決定
                ch_num = int(code[:2])
                ch_key = ch_num

                if ch_key not in all_codes:
                    all_codes[ch_key] = {}

                # 説明文を直前の行から収集
                desc_parts = []
                for back in range(1, 10):
                    if i - back < 0:
                        break
                    prev = lines[i - back].strip()

                    # 他のコード行に到達したら停止
                    if CODE_8_RE.match(prev) or (
                        SUBHEADING_RE.match(prev) and not prev.endswith('(con.)')
                    ):
                        break

                    if _should_skip(prev):
                        continue

                    prev_clean = re.sub(r'\.{2,}', '', prev).strip()
                    if prev_clean and len(prev_clean) > 1:
                        desc_parts.insert(0, prev_clean)

                desc = _clean_description(' '.join(desc_parts))

                if desc and len(desc) > 3:
                    # 同じコードの初出のみ保存
                    if code not in all_codes[ch_key]:
                        all_codes[ch_key][code] = desc
                        codes_count += 1

            i += 1

    doc.close()

    # OrderedDict に変換・ソート
    sorted_codes = OrderedDict()
    for ch_num in sorted(all_codes.keys()):
        ch_label = f"Chapter {ch_num:02d}"
        entries = []
        for code in sorted(all_codes[ch_num].keys()):
            entries.append({
                "code": code,
                "description": all_codes[ch_num][code],
            })
        if entries:
            sorted_codes[ch_label] = entries

    total = sum(len(v) for v in sorted_codes.values())
    print(f"\nExtraction complete: {total} unique codes across {len(sorted_codes)} chapters")

    for ch, codes in sorted_codes.items():
        print(f"  {ch}: {len(codes)} codes")

    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(sorted_codes, f, ensure_ascii=False, indent=2)

    print(f"\nSaved to {OUTPUT_PATH}")


if __name__ == "__main__":
    extract_hts_codes()
