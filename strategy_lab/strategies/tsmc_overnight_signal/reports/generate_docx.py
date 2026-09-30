#!/usr/bin/env python3
"""
將 TSMC 隔夜訊號策略 output/ 之圖表與 CSV 彙整為 Word 量化報告（.docx），支援中文／英文。

使用方式（專案根目錄，**必須用 `-m`**，理由見同主題的 `run.py` 模組說明）：
    .venv/bin/python -m strategy_lab.strategies.tsmc_overnight_signal.reports.generate_docx
    .venv/bin/python -m strategy_lab.strategies.tsmc_overnight_signal.reports.generate_docx --lang en
    .venv/bin/python -m strategy_lab.strategies.tsmc_overnight_signal.reports.generate_docx --lang both

若尚未有圖表／CSV，請先執行：
    .venv/bin/python -m strategy_lab.strategies.tsmc_overnight_signal.run

共用零件（樣式、表格、圖片、CSV 讀取）在 `docx_common.py`，敘事內容在
`docx_append.py`；三者單向相依，不再互相 import。

產出的兩個 `.docx` 是**產出物、不進版控**（`.gitignore` 的
`strategy_lab/**/reports/*.docx`）：`build_report()` 不傳 `output_path` 時
預設就是 `_DOCX_ZH`／`_DOCX_EN`，而 `main()` 正是這樣呼叫——跑一次即產生
約 3.3 MB 的二進位 diff。需要成品請自行執行本檔。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

# 循環解掉之後，這兩個 import 才能放回模組層級——函式內 import 只是把問題藏起來，
# 讓 `scripts/check_layer_deps.py` 看不見，實際的相依環還在
from strategy_lab.strategies.tsmc_overnight_signal.reports.docx_append import (
    append_en_report,
    append_zh_report,
)
from strategy_lab.strategies.tsmc_overnight_signal.reports.docx_common import (
    _DOCX_EN,
    _DOCX_ZH,
    _OUTPUT,
    TITLES_EN,
    TITLES_ZH,
    Cm,
    Document,
    Lang,
    _add_cover,
    _load_csv_kv,
    _set_doc_styles,
)


def build_report(
    lang: Lang,
    output_path: Optional[Path] = None,
) -> Path:
    titles = TITLES_EN if lang == "en" else TITLES_ZH
    out_doc = output_path or (_DOCX_EN if lang == "en" else _DOCX_ZH)
    meta = _load_csv_kv(_OUTPUT / "run_meta.csv")
    metrics_raw = _load_csv_kv(_OUTPUT / "metrics_summary.csv")
    metrics_vec = _load_csv_kv(_OUTPUT / "metrics_vectorized_summary.csv")

    doc = Document()
    section = doc.sections[0]
    if lang == "en":
        # Slightly wider side margins + breathing room for formal English layout
        section.top_margin = Cm(2.4)
        section.bottom_margin = Cm(2.6)
        section.left_margin = Cm(2.85)
        section.right_margin = Cm(2.85)
    else:
        section.top_margin = Cm(2.2)
        section.bottom_margin = Cm(2.2)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2.5)

    _set_doc_styles(doc, lang)
    _add_cover(doc, lang, titles)

    if lang == "zh":
        append_zh_report(doc, meta, metrics_raw, metrics_vec)
    else:
        append_en_report(doc, meta, metrics_raw, metrics_vec)

    doc.save(str(out_doc))
    return out_doc


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate TSMC overnight-signal quant report DOCX."
    )
    parser.add_argument(
        "--lang",
        choices=("zh", "en", "both"),
        default="both",
        help="Output language (default: both Chinese and English files)",
    )
    args = parser.parse_args()

    paths: List[Path] = []
    if args.lang in ("zh", "both"):
        paths.append(build_report("zh"))
    if args.lang in ("en", "both"):
        paths.append(build_report("en"))

    for p in paths:
        print(f"Generated: {p}")


if __name__ == "__main__":
    main()
