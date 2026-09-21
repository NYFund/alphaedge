"""
Chinese / English narrative bodies for TSMC overnight quant Word reports.

Called from generate_docx.build_report with document helper callbacks.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import pandas as pd

from strategy_lab.strategies.tsmc_overnight_signal.reports import docx_common as g

Lang = str  # type alias; actual lang literals passed from caller


def _baseline_numeric_rows(lang: Lang) -> Tuple[List[str], List[List[str]]]:
    """Headers and rows for baseline_comparison.csv (test set)."""
    p = g._OUTPUT / "baseline_comparison.csv"
    if not p.exists():
        return [], []

    df = pd.read_csv(p)

    def fmt_cr(v: float) -> str:
        return f"{100.0 * float(v):.2f}%"

    name_map_zh = {
        "Ridge": "Ridge（線性基準）",
        "OLS": "OLS",
        "Logistic (direction)": "Logistic（漲跌方向）",
        "TSM sign only": "僅 TSM 報酬符號",
    }
    rows: List[List[str]] = []
    for _, r in df.iterrows():
        model = str(r["model"])
        if lang == "zh":
            model = name_map_zh.get(model, model)
        rows.append(
            [
                model,
                f"{float(r['test_IC_pearson']):.4f}",
                fmt_cr(float(r["vectorized_cum_return"])),
                fmt_cr(float(r["realistic_cum_return"])),
                f"{float(r['vectorized_sharpe']):.3f}",
                f"{float(r['realistic_sharpe']):.3f}",
            ]
        )
    if lang == "zh":
        hdr = [
            "模型",
            "測試集 IC",
            "向量化累積報酬",
            "實務累積報酬",
            "向量化 Sharpe",
            "實務 Sharpe",
        ]
    else:
        hdr = [
            "Model",
            "Test IC",
            "Vectorized cum. return",
            "Realistic cum. return",
            "Vectorized Sharpe",
            "Realistic Sharpe",
        ]
    return hdr, rows


def _coef_rows() -> List[List[str]]:
    p = g._OUTPUT / "ridge_coefficients.csv"
    if not p.exists():
        return [
            ["(run python -m strategy_lab.strategies.tsmc_overnight_signal.run)", "—"]
        ]
    df = pd.read_csv(p)
    return [
        [g.sanitize_for_word(str(r["feature"])), f"{float(r['coefficient']):.6f}"]
        for _, r in df.iterrows()
    ]


def _ic_test_rows(lang: Lang) -> List[List[str]]:
    p = g._OUTPUT / "signal_ic_test.csv"
    if not p.exists():
        return []
    df = pd.read_csv(p)
    m = {str(r["metric"]): float(r["value"]) for _, r in df.iterrows()}
    ic = m.get("test_IC_pearson", float("nan"))
    t_ = m.get("test_IC_t_stat_approx", float("nan"))
    n = int(m.get("test_n_days", 0))
    if lang == "zh":
        return [
            ["測試集 IC（Pearson，pred vs 實現報酬）", f"{ic:.4f}"],
            ["IC 近似 t 統計量（診斷用）", f"{t_:.3f}"],
            ["測試集交易日數 n", str(n)],
        ]
    return [
        ["Test-set IC (Pearson, pred vs realized return)", f"{ic:.4f}"],
        ["Approximate t-statistic for IC (diagnostic)", f"{t_:.3f}"],
        ["Test-set trading days n", str(n)],
    ]


def _ic_consistency_footnote(lang: Lang) -> str:
    """
    Explain small gap between diagnostic IC (exec_df merge, optional zero-filled preds)
    and placebo baseline IC (strict panel_test). Values are read from output CSVs when present.
    """
    p_ic = g._OUTPUT / "signal_ic_test.csv"
    p_pb = g._OUTPUT / "placebo_ic_alignment.csv"
    if not p_ic.exists() or not p_pb.exists():
        return ""
    df_ic = pd.read_csv(p_ic)
    df_pb = pd.read_csv(p_pb)
    try:
        ic_diag = float(
            df_ic.loc[df_ic["metric"] == "test_IC_pearson", "value"].iloc[0]
        )
        row0 = df_pb.loc[df_pb["variant"] == "baseline_correct_alignment", "IC_pearson"]
        ic_pb = float(row0.iloc[0]) if len(row0) else float("nan")
    except (KeyError, IndexError):
        return ""
    if lang == "zh":
        return (
            f"註：上方測試集 IC（{ic_diag:.4f}）來自 signal_ic_test.csv，係在測試視窗內**所有台股交易日**之執行合併表上計算 "
            "（與回測同日曆；若該日無特徵列合併則 pred 可能填 0）。"
            f"第 9.1 節 placebo 表之 baseline IC（{ic_pb:.4f}）則在嚴格合併特徵之 **panel_test** 上、以相同 Ridge 係數重算預測後計算。"
            "兩者**有效列與缺列處理不同**，故數值可略差，並非兩套公式或不同版本報告混用。"
            "白話總結：兩個 IC 對應的有效交易日集合不同，並非使用了不同預測模型。"
        )
    return (
        f"Note: the headline test-set IC ({ic_diag:.4f}) comes from signal_ic_test.csv—Pearson correlation on the "
        "test-window **full Taiwan-session calendar** used for execution (`exec_df`), where dates without a merged "
        "feature row carry pred filled as zero. "
        f"The Section 9.1 placebo baseline IC ({ic_pb:.4f}) uses the same coefficients but evaluates predictions only "
        "on the **strict merged feature panel** (`panel_test`). "
        "The small gap reflects different effective row sets and missing-row handling, not two IC formulas or mixed report versions. "
        "In plain English: the two IC figures use different valid daily row sets, not different forecasting models."
    )


def _yearly_rows(lang: Lang) -> Tuple[List[str], List[List[str]]]:
    """Returns (headers, rows). Position switches ≈ sum of 0.5 per day the discrete position flips."""
    p = g._OUTPUT / "yearly_diagnostics.csv"
    if not p.exists():
        return [], []

    df = pd.read_csv(p)
    if "approx_round_trips" in df.columns:
        col_sw = "approx_round_trips"
    elif "position_switches_approx" in df.columns:
        col_sw = "position_switches_approx"
    else:
        col_sw = "turnover_round_trips_approx"

    def pct(x: float) -> str:
        return f"{100.0 * float(x):.2f}%"

    rows = []
    for _, r in df.iterrows():
        y = int(r["year"])
        if y != max(df["year"]):
            lab = f"{y}"
        else:
            lab = f"{y} YTD (partial)" if lang == "en" else f"{y} YTD（部分年度）"
        rows.append(
            [
                lab,
                f"{float(r['IC']):.4f}",
                pct(float(r["strategy_cum_return"])),
                pct(float(r["benchmark_cum_return"])),
                f"{float(r[col_sw]):.1f}",
                f"{float(r['strategy_mdd_pct']):.2f}%",
            ]
        )
    if lang == "zh":
        hdr = [
            "年度",
            "IC",
            "策略累積報酬",
            "2330 B&H 累積報酬",
            "約略往返次數",
            "策略 MDD（%）",
        ]
    else:
        hdr = [
            "Year",
            "IC",
            "Strategy cum.",
            "2330 B&H cum.",
            "Approx. round trips",
            "Strategy MDD",
        ]
    return hdr, rows


def _gap_kv(metric: str) -> Optional[float]:
    p = g._OUTPUT / "ic_pnl_gap.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    row = df.loc[df["metric"] == metric, "value"]
    return float(row.iloc[0]) if len(row) else None


def _threshold_rows(lang: Lang) -> Tuple[List[str], List[List[str]]]:
    p = g._OUTPUT / "threshold_robustness_ridge.csv"
    if not p.exists():
        return [], []

    def pct(v: float) -> str:
        return f"{100.0 * float(v):.2f}%"

    df = pd.read_csv(p)
    rows = []
    for _, r in df.iterrows():
        tau = float(r["threshold_tau"])
        bp = tau * 10000.0
        lab = (
            f"{bp:.0f} bp"
            if tau > 0
            else ("0 (baseline)" if lang == "en" else "0（基線）")
        )
        rows.append(
            [
                lab,
                pct(float(r["realistic_cum_return"])),
                f"{float(r['realistic_sharpe']):.3f}",
                f"{float(r['realistic_mdd_pct']):.2f}%",
            ]
        )
    if lang == "zh":
        hdr = [
            "門檻 τ（預測報酬須超過 τ 才做多）",
            "實務累積報酬",
            "實務 Sharpe",
            "實務 MDD",
        ]
    else:
        hdr = [
            "Threshold τ on forecast return",
            "Realistic cum. return",
            "Realistic Sharpe",
            "Realistic MDD",
        ]
    return hdr, rows


def _placebo_ic_rows(lang: Lang) -> Tuple[List[str], List[List[str]]]:
    """placebo_ic_alignment.csv: baseline vs ±1-row feature shifts on TW panel."""
    p = g._OUTPUT / "placebo_ic_alignment.csv"
    if not p.exists():
        return [], []
    df = pd.read_csv(p)
    label_zh = {
        "baseline_correct_alignment": "基準（正確對齊）",
        "placebo_US_features_lagged_1_TW_row": "Placebo：美國特徵沿台股列落後 1 日",
        "placebo_US_features_led_1_TW_row": "Placebo：美國特徵沿台股列超前 1 日",
    }
    label_en = {
        "baseline_correct_alignment": "Baseline (correct alignment)",
        "placebo_US_features_lagged_1_TW_row": "Placebo: U.S. features shifted −1 TW row",
        "placebo_US_features_led_1_TW_row": "Placebo: U.S. features shifted +1 TW row",
    }
    rows = []
    for _, r in df.iterrows():
        var = str(r["variant"])
        ic = float(r["IC_pearson"])
        lab = (label_zh if lang == "zh" else label_en).get(var, var)
        rows.append([lab, f"{ic:.4f}"])
    hdr = (
        ["對齊情境", "Pearson IC"]
        if lang == "zh"
        else ["Alignment variant", "Pearson IC"]
    )
    return hdr, rows


def _capital_sensitivity_rows(lang: Lang) -> Tuple[List[str], List[List[str]]]:
    p = g._OUTPUT / "capital_sensitivity.csv"
    if not p.exists():
        return [], []
    df = pd.read_csv(p)

    def fmt_million(cap: float) -> str:
        m = float(cap) / 1_000_000.0
        if abs(m - round(m)) < 1e-9:
            return f"{int(round(m))}M"
        return f"{m:.1f}M"

    rows = []
    for _, r in df.iterrows():
        cap = float(r["initial_capital_twd"])
        cr = float(r["realistic_cum_return"])
        sh = float(r["realistic_sharpe"])
        mdd = float(r["realistic_mdd_pct"])
        cap_lab = (
            f"{fmt_million(cap)} TWD"
            if lang == "en"
            else f"{fmt_million(cap)} 新台幣（初資）"
        )
        rows.append(
            [
                cap_lab,
                f"{100.0 * cr:.2f}%",
                f"{sh:.3f}",
                f"{mdd:.2f}%",
            ]
        )
    if lang == "zh":
        hdr = ["初始資金（敏感度）", "實務累積報酬", "實務 Sharpe", "實務 MDD（%）"]
    else:
        hdr = [
            "Initial capital (sensitivity)",
            "Realistic cum. return",
            "Realistic Sharpe",
            "Realistic MDD",
        ]
    return hdr, rows


def _passive_benchmark_rows(lang: Lang) -> Tuple[List[str], List[List[str]]]:
    p = g._OUTPUT / "passive_benchmarks.csv"
    if not p.exists():
        return [], []
    df = pd.read_csv(p)

    def fmt(v: float) -> str:
        return f"{100.0 * float(v):.2f}%"

    rows = []
    for _, r in df.iterrows():
        name = str(r["benchmark"])
        if lang == "zh":
            name = name.replace(
                "2330.TW buy-and-hold (test window)", "2330.TW 買入持有（測試集）"
            )
            name = name.replace(
                "Cash (flat, zero daily return)", "現金（空手，日報酬 0）"
            )
        rows.append([name, fmt(float(r["test_cumulative_return"]))])
    hdr = (
        ["基準（測試集）", "累積報酬"]
        if lang == "zh"
        else ["Benchmark (test window)", "Cumulative return"]
    )
    return hdr, rows


def _sanity_check_rows(lang: Lang) -> Tuple[List[str], List[List[str]]]:
    p = g._OUTPUT / "sanity_checks.csv"
    if not p.exists():
        return [], []
    df = pd.read_csv(p)
    if lang == "zh":
        hdr = ["檢查項目", "狀態", "結果摘要", "剩餘風險"]
        rows = [
            [
                str(r["check"]),
                str(r["status"]),
                str(r["result"]),
                str(r["remaining_risk"]),
            ]
            for _, r in df.iterrows()
        ]
    else:
        hdr = ["Check", "Status", "Result", "Remaining risk"]
        rows = [
            [
                str(r["check"]),
                str(r["status"]),
                str(r["result"]),
                str(r["remaining_risk"]),
            ]
            for _, r in df.iterrows()
        ]
    return hdr, rows


def _reconciliation_rows(
    m_vec: Dict[str, str], m_real: Dict[str, str], lang: Lang
) -> List[List[str]]:
    san = g.sanitize_for_word

    def fmt_cr(m: Dict[str, str]) -> str:
        if "策略累積報酬" not in m:
            return "N/A"
        cr = float(m["策略累積報酬"])
        return san(f"{cr:.4f} (wealth ~{1 + cr:.2f}x)")

    def fmt_cagr(m: Dict[str, str]) -> str:
        if "策略年化報酬" not in m:
            return "N/A"
        return f"{100 * float(m['策略年化報酬']):.2f}%"

    def fmt_mdd(m: Dict[str, str]) -> str:
        if "策略最大回撤%" not in m:
            return "N/A"
        return f"{float(m['策略最大回撤%']):.2f}%"

    def fmt_sh(m: Dict[str, str]) -> str:
        if "策略Sharpe" not in m:
            return "N/A"
        return f"{float(m['策略Sharpe']):.3f}"

    if lang == "zh":
        return [
            [
                "成交／報酬口徑",
                "連續複利、全額持有假設；當日訊號 × 當日收對收報酬",
                "整數張數、收盤撮合、現金餘額與手續費／證交稅逐筆扣減",
                "持有期與可投資餘額定義不同會大幅改變複利路徑",
            ],
            [
                "交易成本",
                "僅在部位 0↔1 切換時扣買賣費率",
                "同左，但現金不足以買滿一張時出現「訊號為多卻空手」之執行落差",
                "高換手時摩擦與張數離散效果被放大",
            ],
            [
                "部位路徑",
                "理論上每日二元曝險（向量化的連續資金）",
                "實際持倉受張數、可買進張數限制",
                "多頭報酬無法完全複製「全額貼現」假設",
            ],
            [
                "資本／整張約束",
                "連續資金下名義曝險原則上可隨訊號調整；無「買不起一張」問題",
                "現金不足以買進至少一張時，訊號為多仍可能全日空手",
                "對應 IC–PnL 落差中「欲多未持有」比例，並與初資敏感度呼應",
            ],
            [
                "主要指標（累積／CAGR／Sharpe／MDD）",
                fmt_cr(m_vec)
                + " / "
                + fmt_cagr(m_vec)
                + " / "
                + fmt_sh(m_vec)
                + " / "
                + fmt_mdd(m_vec),
                fmt_cr(m_real)
                + " / "
                + fmt_cagr(m_real)
                + " / "
                + fmt_sh(m_real)
                + " / "
                + fmt_mdd(m_real),
                "兩者方向相反時，應優先採信貼近交易規則之版本並解釋差異來源",
            ],
        ]

    return [
        [
            "Fill / return definition",
            "Continuous compounding; same-day signal times close-to-close return",
            "Integer lots, close prints, cash and fees/taxes booked per trade",
            "Holding-period and investable cash differ materially",
        ],
        [
            "Transaction costs",
            "Fees on discrete 0↔1 position toggles only",
            "Same rule family, but cash constraints may skip desired long exposure",
            "High turnover amplifies friction and lot discreteness",
        ],
        [
            "Position path",
            "Ideal binary daily exposure",
            "Actual holdings constrained by lot size and affordability",
            "Cannot replicate full-notional scaling",
        ],
        [
            "Capital / lot constraint",
            "Full-notional exposure to the signal is always feasible under continuous capital",
            "May stay flat when desired long but one board lot is unaffordable",
            "Explains part of the IC–PnL gap (signal long but flat); ties to capital sensitivity",
        ],
        [
            "Headline metrics (cum. / CAGR / Sharpe / MDD)",
            f"{fmt_cr(m_vec)} / {fmt_cagr(m_vec)} / {fmt_sh(m_vec)} / {fmt_mdd(m_vec)}",
            f"{fmt_cr(m_real)} / {fmt_cagr(m_real)} / {fmt_sh(m_real)} / {fmt_mdd(m_real)}",
            "When signs disagree, favor the execution-faithful path and explain the gap",
        ],
    ]


def append_zh_report(
    document, meta: Dict[str, str], m_real: Dict[str, str], m_vec: Dict[str, str]
) -> None:
    _paragraph = g._paragraph
    _add_table_from_rows = g._add_table_from_rows
    _add_figure = g._add_figure
    _format_metrics_rows = g._format_metrics_rows
    _method_usage_rows = g._method_usage_rows
    sanitize_for_word = g.sanitize_for_word
    _OUT = g._OUTPUT
    _add_figure_with_note = g._add_figure_with_note

    te = meta.get("test_end_effective_last_tw_close", "")
    dr = meta.get("data_end_requested", "")
    cap0 = meta.get("initial_capital_twd", "")
    lotn = meta.get("shares_per_lot_tw_common_stock", "")

    doc = document
    doc.add_heading("摘要", level=1)
    _paragraph(
        doc,
        "本報告顯示 TSM ADR、費城半導體（SOX）與美元／台幣等隔夜領先變數對同日報酬具統計預測力，但在本研究測試之整張制、費後實務執行下，並未呈現穩健可交易 alpha。"
        "向量化全額持有回測僅作診斷對照，不得單獨解讀為獲利主張。"
        "Ridge 為可解釋之線性預測基準；較簡單之方向規則在相同執行假設下亦可能具較佳實現績效。"
        "核心發現為統計可預測性與可執行交易績效之間的落差。",
    )
    note = doc.add_paragraph()
    note.add_run(
        sanitize_for_word(
            "聲明：本文件僅供學術與課程專題展示，不構成投資建議；實務交易含流動性、稅費、法規與執行落差。"
        )
    ).italic = True

    doc.add_heading("一、研究問題與經濟直覺", level=1)
    _paragraph(
        doc,
        "研究問題：在美國隔夜資訊已入價的前提下，TSM／SOX／USD-TWD 是否對台股掛牌之台積電同日報酬具有可預測之領先成分？"
        "經濟直覺：ADR 與半導體板塊反映全球需求與風險偏好，匯率則連結跨市定價；理論上可作為台股開盤前可觀測之資訊集合。"
        "跨市場文獻常討論價格發現與領先—落後關係（price discovery / lead-lag）；本研究將「隔夜可得之美國資訊」視為對台股同日報酬之候選領先集合，並以嚴格日期對齊與 placebo 檢查佐證非單純對齊誤差。",
    )

    doc.add_heading("二、資料、時間對齊與特徵", level=1)
    _paragraph(
        doc,
        f"訓練集：2020 曆年；驗證集：2021（Ridge lambda）；測試集：2022-01-01 至最後一筆台股收盤日 {te}（與 run_meta 之 test_end_effective 一致）。"
        f"資料下載上界可記為 {dr}（下載區間與最後可用交易日可能差一日）。"
        "特徵為嚴格早於台股當日之最後美國交易日報酬，以避免前視。",
    )
    _add_table_from_rows(
        doc,
        ["資料集欄位", "內容"],
        [
            [
                "資料下載／請求上界",
                sanitize_for_word(f"見 run_meta：data_end_requested = {dr}"),
            ],
            ["訓練集", "2020-01-01 ~ 2020-12-31"],
            ["驗證集", "2021-01-01 ~ 2021-12-31"],
            [
                "測試集（OOS）",
                sanitize_for_word(f"2022-01-01 ~ {te}（最後有效台股收盤）"),
            ],
            ["資料來源", "台股：專案 SQLite；美股／匯率：yfinance 日線、auto_adjust"],
            ["基準", "主基準：2330.TW buy-and-hold；0050 為可選大盤代理"],
        ],
        col_widths_cm=[4.5, 12],
    )

    doc.add_heading("三、執行時間點與報酬口徑（限制）", level=1)
    _paragraph(
        doc,
        "美國領先變數在台股開盤前可觀測；若以「可執行持有期」對齊，標的報酬應優先採台股開盤至收盤（open-to-close）。"
        "本報告仍使用 close-to-close 作為日頻持有期 proxy（與常用收盤資料對齊）。"
        "限制：close-to-close 含隔夜缺口，其中一部分在開盤時可能已無法完整交易；故統計 IC 與排序仍可成立，但不保證等同 open-to-close 可實現報酬。"
        "執行摩擦與撮合細節之一般討論可參 Harris（2003）微結構文獻；本報告仍以收對收 proxy 為共同規格。"
        "實務化下一步應改以開收報酬或開盤成交價重估策略。",
    )

    doc.add_heading("四、模型設計", level=1)
    _add_table_from_rows(
        doc,
        ["資料代號（yfinance）", "角色"],
        [
            ["TSM", "台積電 ADR，美國前一日報酬"],
            ["2330.TW", "台股標的與主基準 buy-and-hold"],
            ["^SOX", "費城半導體指數"],
            ["TWD=X", "USD/TWD 即期（簡化代理）"],
        ],
        col_widths_cm=[4.5, 12],
    )
    _paragraph(
        doc,
        "主模型為含截距之 Ridge 迴歸（L2 僅施加於斜率）。lambda 由 2021 驗證集 MSE 選出後，於訓練+驗證重估係數，再於測試集評分。",
    )
    _add_table_from_rows(
        doc,
        ["Ridge 係數（訓練+驗證重估後）", "數值"],
        _coef_rows(),
        col_widths_cm=[6, 9],
    )
    ra = meta.get("ridge_alpha", "")
    _paragraph(
        doc,
        sanitize_for_word(
            f"懲罰係數：驗證集選出之 lambda = {ra}（數值很小，正則化強度有限，Ridge 軌跡接近 OLS）。"
            "仍保留 Ridge 作為主規格，以利與文獻常見之 shrinkage 設定對照，並在共線性下提供數值較穩之係數。"
        ),
    )
    _paragraph(
        doc,
        "係數解讀：TSM 與 USD/TWD 為正，與「美國隔夜資訊偏多時台股跟涨／台幣方向」直覺一致。"
        "SOX 為負可能反映（一）TSM ADR 已吸收板塊 beta，SOX 在迴歸中扮演殘差／控制變數；（二）多重共線性下符號可為條件式解讀，不宜單獨當因果方向。"
        "Logistic 與僅 TSM 符號之基線在「實務口徑」下可能呈現較佳累積報酬，代表連續預測搭配 pred>0 門檻未必最適；方向模型可作為建議實作版本之一。",
    )
    doc.add_page_break()
    doc.add_heading("（一）基線模型實測比較（測試集）", level=2)
    bh, br = _baseline_numeric_rows("zh")
    if br:
        _add_table_from_rows(doc, bh, br, col_widths_cm=[3.4, 2.2, 2.6, 2.6, 2.4, 2.4])
        _paragraph(
            doc,
            "說明：累積報酬為淨值倍數減一。最終可交易解讀以「實務」欄為主；向量化欄僅作對照。"
            "Ridge／OLS 之 IC 為連續預測與實現報酬之相關；Logistic 為 logit 分數；TSM 符號列以 TSM 報酬為分數。"
            "此對照顯示：訊號可能較適合用於方向分類（搭配簡單多／空規則），而不必然優於以連續報酬預測再 pred>0 之映射。",
        )
    else:
        _paragraph(
            doc,
            "（尚無 baseline_comparison.csv，請先執行 python -m strategy_lab.strategies.tsmc_overnight_signal.run）",
        )
    doc.add_heading("（二）被動基準（測試集）", level=2)
    pb_h, pb_r = _passive_benchmark_rows("zh")
    if pb_r:
        _add_table_from_rows(doc, pb_h, pb_r, col_widths_cm=[8, 8])
    doc.add_heading("五、回測實作與交易成本", level=1)
    _paragraph(
        doc,
        sanitize_for_word(
            f"初始資金：{cap0} TWD（見 run_meta）；台股現貨以整張（每張 {lotn} 股）撮合，未模擬零股。"
            "若預測做多但現金不足以買進至少一張，則當日維持空手（訊號與成交不一致之來源）。"
            "此處初資設定刻意貼近小額帳戶之可執行性壓力測試；機構規模若放大資金或採零股／契約化工具，主要限制會改變。"
        ),
    )
    _paragraph(
        doc,
        "貼近實務（主結果）演算法（每日收盤撮合）："
        "（1）依模型得 pred；預設門檻 pred>0 則目標為做多，否則空手。"
        "（2）若目標做多且目前空手：以可用現金買進「最多整張數」，並扣買進手續費。"
        "（3）若目標空手且目前持有：以收盤價賣出全部持股，扣賣出手續費與證交稅。"
        "（4）若目標與昨日持倉相同：不換手、不另扣費。"
        "（5）評價：持股時市值含於淨值；空手時報酬為 0。"
        "收盤價記帳係為與本研究所採「收對收」報酬定義一致，並非主張開盤即可等同收盤成交；更貼近可執行之延伸為開盤成交或開收報酬之重估。",
    )
    _paragraph(
        doc,
        "說明：close-price booking 與 close-to-close 報酬 proxy 對齊，作為可比較之共同規格；並未將其等同為完全可執行之開盤撮合結果。"
        "未來工作應優先改為 open-to-close 回測或以開盤價填單之小型對照實驗。",
    )
    fb = meta.get("fee_buy", "")
    fs = meta.get("fee_sell_plus_tax", "")
    _paragraph(
        doc,
        sanitize_for_word(
            f"費率（與期末報告假設一致）：買進手續費率 {fb}（約 0.1425%）；賣方為手續費加證交稅合計 {fs}（約 0.4425%）。"
            "記帳概念：買進總成本 = 成交價 × 股數 × (1+買進費率)；賣出淨收入 = 成交價 × 股數 × (1−賣出費率)。"
        ),
    )
    _paragraph(
        doc,
        "向量化理想化（診斷）：連續資金、全額做多／空手，當日訊號 × 當日收對收報酬；僅於 0↔1 切換時扣費。與實務路徑對照可凸顯缺口來源。",
    )

    doc.add_heading("六、向量化理想化回測（診斷對照，非主結果）", level=1)
    rows_v = _format_metrics_rows(meta, m_vec, "zh")
    if rows_v:
        _add_table_from_rows(
            doc, ["指標", "向量化（理想化）"], rows_v, col_widths_cm=[5.5, 11]
        )
    else:
        _paragraph(
            doc,
            "（缺少 metrics_vectorized_summary.csv，請先執行 python -m strategy_lab.strategies.tsmc_overnight_signal.run）",
        )

    doc.add_heading("七、貼近實務之回測（整數張數，主結果）", level=1)
    rows_r = _format_metrics_rows(meta, m_real, "zh")
    if rows_r:
        _add_table_from_rows(
            doc, ["指標", "貼近實務／整數張數"], rows_r, col_widths_cm=[5.5, 11]
        )
    else:
        _paragraph(doc, "（缺少 metrics_summary.csv）")

    doc.add_heading("八、向量化與實務回測差異之對帳（核心）", level=1)
    _paragraph(
        doc,
        "下列對帳表說明兩種引擎在成交口徑、成本、部位路徑與主要指標上的結構性差異。"
        "當結果方向相反時，不宜僅以「模型好壞」解釋，而應優先檢視實作假設是否與可交易定義一致。",
    )
    _add_table_from_rows(
        doc,
        ["項目", "向量化理想化", "貼近實務／整數張數", "可能影響"],
        _reconciliation_rows(m_vec, m_real, "zh"),
        col_widths_cm=[3.2, 4.8, 4.8, 4.2],
    )

    doc.add_heading("九、訊號診斷：IC、係數與年度穩定性", level=1)
    _add_table_from_rows(
        doc, ["診斷", "數值"], _ic_test_rows("zh"), col_widths_cm=[6, 9]
    )
    _ic_note_zh = _ic_consistency_footnote("zh")
    if _ic_note_zh:
        _paragraph(doc, sanitize_for_word(_ic_note_zh))
    _paragraph(
        doc,
        "測試集日頻 IC 若顯著偏高，除解讀為「預測力強」外，亦應審慎檢查是否源於曆法對齊、假期或合併列之誤差；"
        "建議後續工作手動抽驗若干組美台對應交易日，並以下表 placebo 與一日平移對照作為快速篩檢。",
    )
    ph_h, ph_r = _placebo_ic_rows("zh")
    if ph_r:
        doc.add_heading("（一）對齊 placebo：特徵沿台股列錯位 ±1 日", level=2)
        _add_table_from_rows(doc, ph_h, ph_r, col_widths_cm=[8.5, 3.5])
        _paragraph(
            doc,
            "解讀：若 baseline IC 在錯位後大幅崩落，支撐「正確對齊下之排序力」敘事；"
            "若錯位後 IC 仍高，則應回頭檢查合併邏輯、假期與報酬定義。"
            "+1 列（特徵超前）placebo 之 IC 仍為正（見上表），可能反映報酬持續性，或美台曆法／列對齊之殘差仍未完全排除。"
            "故 placebo 可減輕對齊疑慮，但無法完全取代人工抽驗配對交易日之必要性。",
        )

    doc.add_heading(
        "（二）IC 與實務損益落差（IC–PnL gap）"
        if ph_r
        else "（一）IC 與實務損益落差（IC–PnL gap）",
        level=2,
    )
    sh_gap = _gap_kv("share_days_signal_long_but_flat_cash_gap")
    cnt_gap = _gap_kv("count_days_signal_long_but_flat")
    d2026 = _gap_kv("y2026_days_in_sample")
    p2026 = _gap_kv("y2026_days_pred_positive")
    h2026 = _gap_kv("y2026_days_held_long")
    _paragraph(
        doc,
        "IC 衡量預測與報酬之排序相關，並不保證門檻 pred>0 後仍能覆蓋交易成本與整張約束。"
        + (
            sanitize_for_word(
                f"樣本中「訊號欲做多卻因空手而未持有」之日約占 {100.0 * float(sh_gap):.1f}%（約 {int(cnt_gap or 0)} 日）。"
            )
            if sh_gap is not None and cnt_gap is not None
            else ""
        )
        + "當現金不足以買進一張時，大涨日可能缺席；頻繁換手亦會讓費用吞噬小幅正預測邊際。"
        + (
            sanitize_for_word(
                f"2026 年樣本內：約 {int(d2026 or 0)} 個交易日，其中 pred>0 約 {int(p2026 or 0)} 日，但實際持倉日為 {int(h2026 or 0)}。"
                "原因為資金不足以買進至少一張時無法建倉，故策略報酬為 0%，並非程式『停止交易』。"
            )
            if d2026 is not None
            else ""
        ),
    )
    cs_h, cs_r = _capital_sensitivity_rows("zh")
    if cs_r:
        _paragraph(
            doc,
            "初資敏感度（與 run_meta 同一費率與規則，僅調整起始現金）：用以檢驗 2026 年『訊號為多卻無法買進一張』是否為結果之主因。"
            "若累積報酬對初資高度敏感，代表解讀應回歸整張與資金規模假設，而非僅歸因於模型訊號本身。",
        )
        _add_table_from_rows(doc, cs_h, cs_r, col_widths_cm=[4.5, 3.5, 3, 3.5])
        _paragraph(
            doc,
            "初資敏感度並未隨起始資金單調改善，顯示「買得起整張」僅解釋 IC–PnL 落差之一部分；"
            "更核心的仍是實現時序路徑，以及收對收統計預測與可執行持股報酬之不一致。"
            "資金提高會放大可買張數與曝險，不利時點之損失亦可能隨資金規模放大；故較多資金並不保證風險調整後績效機械式改善。",
        )
    th_h, th_r = _threshold_rows("zh")
    if th_r:
        doc.add_heading(
            "（三）門檻敏感度（Ridge 預測值）"
            if ph_r
            else "（二）門檻敏感度（Ridge 預測值）",
            level=2,
        )
        _paragraph(
            doc,
            "τ 為預測報酬須超過之最小值才做多（bps 為每日報酬之萬分之一分位，5 bp = 0.05% = 0.0005）。",
        )
        _add_table_from_rows(doc, th_h, th_r, col_widths_cm=[5.5, 3.5, 3, 3.5])
        _paragraph(
            doc,
            "解讀：提高門檻可降低換手與摩擦，使虧損幅度收斂，惟本表中 Ridge 在測試視窗下仍未全面轉正；"
            "顯示問題不僅來自過度交易，亦包含訊號與執行口徑（含收對收 proxy）之落差。",
        )

    _ym = "一二三四五六"
    _yi = 4 if (ph_r and th_r) else (3 if (ph_r or th_r) else 2)
    doc.add_heading(f"（{_ym[_yi - 1]}）年度拆解", level=2)
    hdr_y, yr = _yearly_rows("zh")
    if yr:
        _add_table_from_rows(
            doc, hdr_y, yr, col_widths_cm=[2.2, 2.2, 2.8, 2.8, 2.2, 2.2]
        )
        _paragraph(
            doc,
            "「約略往返次數」定義：隔日持倉由 0→1 或 1→0 各計 0.5；全日連續持有或全日空手則為 0。"
            "年度加總為近似之開平倉往返趟數（round-trip proxy），非委託張數或成交量。",
        )
    _paragraph(
        doc,
        "解讀：係數顯示在正則化後，TSM 與 TWD 方向為正、SOX 為負（樣本內線性投影）；"
        "年度 IC 在測試期多為正，與「貼近實務」淨報酬疲弱並存，代表「排序能力」未必轉成「可交易超額」，"
        "尤其在張數離散、現金約束與高換手摩擦下。",
    )

    doc.add_heading("十、穩健性與合理性檢查", level=1)
    doc.add_heading("（一）穩健性", level=2)
    _add_table_from_rows(
        doc,
        ["方法", "用途說明"],
        _method_usage_rows("zh"),
        col_widths_cm=[6, 10.5],
    )
    _paragraph(
        doc,
        "驗證集挑選 lambda 仍伴隨輕度多重比較／資料探勘風險；Lo & MacKinlay（1990）說明小樣本調參何以可能放大表面可預測性，"
        "故本研究將 Ridge 定位為可複現之規格基準，而非單一「發現 alpha」之宣言。",
    )
    doc.add_heading("（二）合理性檢查（含狀態）", level=2)
    sn_h, sn_r = _sanity_check_rows("zh")
    if sn_r:
        _add_table_from_rows(doc, sn_h, sn_r, col_widths_cm=[3.8, 2.8, 4.2, 4.2])
    _paragraph(
        doc,
        "其餘一般性風險（漲跌停、議價滑價、事件缺口）：本版未逐一數值化檢驗；微結構層面可進一步對照 Harris（2003），並與本報告收對收 proxy 之限制併讀。",
    )

    doc.add_heading("十一、限制", level=1)
    for t in [
        "收對收報酬為持有期 proxy：與「開盤可成交」定義不同，屬本研究可交易性評估之**首要**限制；後續應優先改為開收報酬或開盤價填單對照。",
        "線性規格可能遺漏非線性與結構斷點；未實作滾動視窗重估或 walk-forward，不利於 regime shift。",
        "匯率為即期代理，非完整避險後曝險。僅做多或空手，未涵蓋融券或指數期貨。",
        "驗證集挑選 lambda 具輕度資料探勘風險；未加入多重檢定校正（見 Lo & MacKinlay 1990 對資料探勘偏誤之討論）。",
    ]:
        doc.add_paragraph(sanitize_for_word(t), style="List Bullet")

    doc.add_heading("十二、結論", level=1)
    _paragraph(
        doc,
        "統計上，隔夜領先變數對 2330 同日報酬具排序可預測性（IC）；placebo 錯位檢查有助區分「真訊號」與對齊誤差。"
        "惟最終可交易結論必須以整數張數、現金與費稅後回測為準；向量化全額持有僅供診斷。"
        "本報告刻意並列兩者，凸顯「顯著 IC」未必轉為「正淨報酬」——IC 與可交易損益之差異主要來自執行假設、整張約束與資金規模。"
        "在目前 Ridge 規則與整張制執行假設下，不宜將本策略解讀為具經濟意義之可交易 alpha，儘管統計 IC 偏高。"
        "實務上可優先評估較穩健之方向基線（如 Logistic／TSM 符號），並將標的報酬改為 open-to-close 作為下一步。",
    )

    doc.add_heading("附錄：圖表（測試集）", level=1)
    _paragraph(
        doc, "圖檔檔名區分 realistic（主結果）與 vec（診斷）；下列各圖附一句解讀。"
    )
    figures_zh_notes: List[Tuple[str, str, str]] = [
        (
            "equity_curve_realistic.png",
            "淨值（實務）",
            "實務路徑在 2024–2026 多數時間未能充分參與灰線基準之波段，凸顯空手與整張約束之機會成本。",
        ),
        (
            "mdd_underwater_realistic.png",
            "回撤（實務）",
            "2024 後回撤幅度一度收斂，但相對基準之落後未必同步收斂。",
        ),
        (
            "rolling_sharpe_realistic.png",
            "滾動 Sharpe（實務）",
            "若 2025 之後滾動 Sharpe 再度轉負，表示在實務執行下風險調整報酬未能穩定延續。",
        ),
        (
            "monthly_returns_heatmap_realistic.png",
            "月報酬熱圖（實務）",
            "虧損若集中於 2025 年 M3–M4 等區段，可解釋年度績效下挫之主因。",
        ),
        (
            "equity_curve_vectorized.png",
            "淨值（向量化）",
            "診斷用：全額持有假設；勿與主結果混讀。",
        ),
        ("mdd_underwater_vectorized.png", "回撤（向量化）", "對照用；通常較實務樂觀。"),
        (
            "monthly_returns_heatmap_vectorized.png",
            "月報酬熱圖（向量化）",
            "診斷對照：不宜直接與實務淨值結論混讀。",
        ),
        ("ic_by_year.png", "年度 IC", "IC 為正亦不保證每年實務獲利。"),
        ("rolling_ic.png", "滾動 IC", "可檢視排序力是否隨時間漂移。"),
    ]
    for i, (fname, cap, note) in enumerate(figures_zh_notes, start=1):
        _add_figure_with_note(doc, _OUT / fname, cap, i, "zh", note)

    doc.add_heading("重現方式", level=1)
    _paragraph(
        doc,
        "於專案根目錄執行 .venv/bin/python -m strategy_lab.strategies.tsmc_overnight_signal.run，再執行 .venv/bin/python -m strategy_lab.strategies.tsmc_overnight_signal.reports.generate_docx。"
        "若需列印或繳交 PDF，建議將附錄圖表以約 200–300 dpi 重新匯出後替換 output 內 PNG，以維持字級清晰。",
    )

    doc.add_heading("參考資料", level=1)
    for ref in [
        "Hasbrouck, J. (1995). One security, many markets: Determining the contributions to price discovery. Journal of Finance.",
        "Eun, C. S., & Sabherwal, S. (2003). Cross-border listings and price discovery: Evidence from U.S.-listed Canadian stocks. Journal of Finance.",
        "Harris, L. (2003). Trading and Exchanges: Market Microstructure for Practitioners. Oxford University Press.",
        "Lo, A. W., & MacKinlay, A. C. (1990). Data-snooping biases in tests of financial asset pricing models. Review of Financial Studies.",
        "yfinance（美股／匯率日線資料來源；擷取／查閱：2026 年 5 月）。",
        "AlphaEdge 專案 core/backtest/README.md（回測架構說明）。",
        "課程期末報告原始稿（手續費與樣本假設）。",
    ]:
        doc.add_paragraph(sanitize_for_word(ref), style="List Bullet")


def append_en_report(
    document, meta: Dict[str, str], m_real: Dict[str, str], m_vec: Dict[str, str]
) -> None:
    _paragraph = g._paragraph
    _add_table_from_rows = g._add_table_from_rows
    _format_metrics_rows = g._format_metrics_rows
    _method_usage_rows = g._method_usage_rows
    sanitize_for_word = g.sanitize_for_word
    _OUT = g._OUTPUT
    _add_figure_with_note = g._add_figure_with_note

    te = meta.get("test_end_effective_last_tw_close", "")
    dr = meta.get("data_end_requested", "")
    cap0 = meta.get("initial_capital_twd", "")
    lotn = meta.get("shares_per_lot_tw_common_stock", "")
    ra = meta.get("ridge_alpha", "")

    doc = document
    doc.add_heading("Executive Summary", level=1)
    _paragraph(
        doc,
        "This report finds statistically strong overnight lead information in TSM ADR, SOX, and USD/TWD, but not robust "
        "tradable alpha under the lot-based, fee-realistic execution tested here. "
        "The vectorized full-notional backtest is used only as a diagnostic contrast, not as a standalone profitability claim. "
        "Ridge serves as an interpretable linear forecasting baseline, while simpler directional benchmarks may deliver stronger "
        "realized performance under the same execution assumptions. "
        "Therefore, the core finding is the gap between statistical predictability and implementable trading performance.",
    )
    note = doc.add_paragraph()
    note.add_run(
        "Disclaimer: For academic use only; not investment advice. Live trading involves liquidity, fees, regulation, "
        "and execution gaps."
    ).italic = True

    doc.add_heading("1. Research Question and Economic Rationale", level=1)
    _paragraph(
        doc,
        "Research question: after U.S. overnight information is embedded in prices, do TSM, SOX, and USD/TWD jointly "
        "contain lead information for the Taiwan-session return of TSMC? "
        "Economic rationale: ADR and sector proxies summarize global demand and risk appetite; FX links cross-market valuation. "
        "Cross-market studies emphasize price discovery and lead–lag structure across venues (Hasbrouck 1995); we treat U.S. overnight "
        "information as a candidate lead set for same-day Taiwan returns and corroborate timing with placebo-shift IC checks.",
    )

    doc.add_heading("2. Data, Timing, and Feature Alignment", level=1)
    _paragraph(
        doc,
        sanitize_for_word(
            f"Training: calendar 2020. Validation: 2021 for Ridge lambda. "
            f"Test OOS: 2022-01-01 through the last available TW close ({te}), matching run_meta.test_end_effective_last_tw_close. "
            f"The download upper bound is recorded as data_end_requested = {dr} (may differ by one calendar day from the last bar)."
        ),
    )
    _add_table_from_rows(
        doc,
        ["Dataset Item", "Specification"],
        [
            [
                "Data download request",
                sanitize_for_word(f"See run_meta: data_end_requested = {dr}"),
            ],
            ["Training set", "2020-01-01 to 2020-12-31"],
            ["Validation set", "2021-01-01 to 2021-12-31"],
            [
                "Test set (OOS)",
                sanitize_for_word(f"2022-01-01 to {te} (last TW close in sample)"),
            ],
            [
                "Sources",
                "Taiwan close: project SQLite; U.S./FX: yfinance daily, auto_adjust",
            ],
            [
                "Benchmarks",
                "Primary: 2330.TW buy-and-hold; optional market benchmark: 0050 ETF",
            ],
        ],
        col_widths_cm=[4.5, 12],
    )

    doc.add_heading("3. Execution Timing and Return Definition (Limitation)", level=1)
    _paragraph(
        doc,
        "Lead variables are observable before the Taiwan open. For strict tradability, the natural target is Taiwan "
        "open-to-close return. This report uses close-to-close as a daily proxy aligned with closing prices. "
        "Limitation: close-to-close embeds the overnight gap; part of that move may be difficult to capture with an open fill. "
        "Statistical IC can still hold while open-to-close alpha differs. Execution friction and microstructure considerations "
        "are discussed broadly in Harris (2003); this report nonetheless adopts close-to-close as the common statistical proxy. "
        "Next step: retarget to open-to-close (or open fills).",
    )

    doc.add_heading("4. Model Design", level=1)
    _add_table_from_rows(
        doc,
        ["Ticker (yfinance)", "Role"],
        [
            ["TSM", "TSMC ADR, prior U.S. session return"],
            ["2330.TW", "Target and primary buy-and-hold benchmark"],
            ["^SOX", "Semiconductor sector proxy"],
            ["TWD=X", "USD/TWD spot (simplified)"],
        ],
        col_widths_cm=[4.5, 12],
    )
    _paragraph(
        doc,
        "Ridge with intercept; L2 on slopes only. Lambda from 2021 validation MSE; coefficients re-fit on train+val.",
    )
    _add_table_from_rows(
        doc,
        ["Coefficient (re-fit on train+val)", "Value"],
        _coef_rows(),
        col_widths_cm=[6, 9],
    )
    _paragraph(
        doc,
        sanitize_for_word(
            f"Penalty: validation-selected lambda = {ra} is small, so Ridge is close to OLS in this sample. "
            f"We retain Ridge as the stated specification for shrinkage comparability and mildly stabilized slopes."
        ),
    )
    _paragraph(
        doc,
        "Coefficient narrative: TSM and USD/TWD load positively; SOX loads negatively—consistent with ADR absorbing sector "
        "beta so SOX behaves like a residual control under multicollinearity (signs are conditional, not causal). "
        "Directional baselines may achieve higher realistic cumulative returns under the same execution mapping; logistic or "
        "TSM-sign implementations may be preferred operationally. "
        "This pattern suggests the overnight signal may be more useful for directional classification than for continuous "
        "return forecasting under the pred>0 rule.",
    )
    doc.add_page_break()
    doc.add_heading("4.1 Baseline models (test-set results)", level=2)
    bh, br = _baseline_numeric_rows("en")
    if br:
        _add_table_from_rows(doc, bh, br, col_widths_cm=[3.4, 2.2, 2.6, 2.6, 2.4, 2.4])
        _paragraph(
            doc,
            "Interpret tradable outcomes using the realistic column. Vectorized metrics are diagnostic only. "
            "When logistic or TSM-sign rules outperform Ridge on realistic cum. return, treat it as evidence that the signal "
            "may pack more information in direction than in continuous point forecasts under this mapping.",
        )
    else:
        _paragraph(
            doc,
            "baseline_comparison.csv not found; run python -m strategy_lab.strategies.tsmc_overnight_signal.run.",
        )
    doc.add_heading("4.2 Passive benchmarks (test window)", level=2)
    pb_h, pb_r = _passive_benchmark_rows("en")
    if pb_r:
        _add_table_from_rows(doc, pb_h, pb_r, col_widths_cm=[8, 8])

    doc.add_heading("5. Backtest Implementation and Trading Costs", level=1)
    _paragraph(
        doc,
        sanitize_for_word(
            f"Primary result engine (realistic): initial cash {cap0} TWD; Taiwan common stocks trade in integer lots of "
            f"{lotn} shares; odd lots are not modeled. "
            f"If long is desired but cash cannot afford one lot, the strategy stays flat that day (signal vs execution gap). "
            "The 1M TWD baseline intentionally stresses implementability for a small-account profile; institutional-scale "
            "capital or odd-lot policies would change which constraint binds."
        ),
    )
    _paragraph(
        doc,
        "Daily close-booking pseudo-code: "
        "(1) Compute pred; default rule long if pred > 0 else flat. "
        "(2) If target long and currently flat: buy maximum affordable lots; pay buy-side fee. "
        "(3) If target flat and currently long: sell entire position at close; pay sell fee plus transaction tax. "
        "(4) If target equals yesterday's position: no trade, no fee. "
        "(5) Mark equity with cash plus marked-to-market stock.",
    )
    _paragraph(
        doc,
        "Close-price booking matches the close-to-close return specification used throughout this report; it is not asserted as "
        "an executable open-session fill on the Taiwan cash session. "
        "Future work should prioritize open-to-close targeting or a small open-print AB test against this proxy.",
    )
    fb = meta.get("fee_buy", "")
    fs = meta.get("fee_sell_plus_tax", "")
    _paragraph(
        doc,
        sanitize_for_word(
            f"Fees: buy fee rate {fb} (~0.1425%); combined sell fee plus tax {fs} (~0.4425%). "
            f"Accounting: buy cash need ≈ price × shares × (1 + buy fee); sell proceeds ≈ price × shares × (1 − sell-side fee rate)."
        ),
    )
    _paragraph(
        doc,
        "Vectorized diagnostic: continuous notionals, same-day signal times close-to-close return, fees only on 0↔1 switches.",
    )

    doc.add_heading(
        "6. Vectorized Backtest (Diagnostic Contrast, Not Primary)", level=1
    )
    rows_v = _format_metrics_rows(meta, m_vec, "en")
    if rows_v:
        _add_table_from_rows(
            doc, ["Metric", "Vectorized (idealized)"], rows_v, col_widths_cm=[5.5, 11]
        )
    else:
        _paragraph(
            doc,
            "Missing metrics_vectorized_summary.csv; run python -m strategy_lab.strategies.tsmc_overnight_signal.run.",
        )

    doc.add_heading("7. Realistic Backtest (Integer Lots — Primary)", level=1)
    rows_r = _format_metrics_rows(meta, m_real, "en")
    if rows_r:
        _add_table_from_rows(
            doc, ["Metric", "Realistic / lot-based"], rows_r, col_widths_cm=[5.5, 11]
        )
    else:
        _paragraph(doc, "Missing metrics_summary.csv.")

    doc.add_heading("8. Reconciling Vectorized and Realistic Results", level=1)
    _paragraph(
        doc,
        "The table below decomposes why two engines fed by the same signal can flip sign on cumulative performance. "
        "When the gap is large, the first response should be to audit fills, cost accounting, and the position path—not "
        "to reinterpret the signal in isolation.",
    )
    _add_table_from_rows(
        doc,
        ["Item", "Vectorized backtest", "Realistic backtest", "Potential impact"],
        _reconciliation_rows(m_vec, m_real, "en"),
        col_widths_cm=[3.2, 4.8, 4.8, 4.2],
    )

    doc.add_heading(
        "9. Signal Diagnostics: IC, IC–PnL Gap, Thresholds, Stability", level=1
    )
    _add_table_from_rows(
        doc, ["Diagnostic", "Value"], _ic_test_rows("en"), col_widths_cm=[6, 9]
    )
    _ic_note_en = _ic_consistency_footnote("en")
    if _ic_note_en:
        _paragraph(doc, _ic_note_en)
    _paragraph(
        doc,
        "An unusually high daily IC warrants skepticism beyond favorable interpretation: manually audit matched U.S.–Taiwan calendar "
        "dates in future work. The placebo-shift table below provides a fast screen against alignment artifacts.",
    )
    ph_h, ph_r = _placebo_ic_rows("en")
    _sec9 = 1
    if ph_r:
        doc.add_heading("9.1 Feature-shift placebo (±1 TW panel row)", level=2)
        _add_table_from_rows(doc, ph_h, ph_r, col_widths_cm=[8.5, 3.5])
        _paragraph(
            doc,
            "Interpretation: if IC collapses under misalignment but stays high under correct joins, the ranking signal is more "
            "credible; persistently high placebo ICs would warrant revisiting merge logic and holidays. "
            "The +1 row-shift placebo IC remains positive (see table), which suggests that some return persistence or residual "
            "calendar overlap may remain. Therefore, the placebo test reduces alignment concerns but does not fully eliminate "
            "them; a manual audit of matched U.S.–Taiwan dates remains necessary.",
        )
        _sec9 = 2

    doc.add_heading(f"9.{_sec9} IC versus realistic PnL (gap decomposition)", level=2)
    sh_gap = _gap_kv("share_days_signal_long_but_flat_cash_gap")
    cnt_gap = _gap_kv("count_days_signal_long_but_flat")
    d2026 = _gap_kv("y2026_days_in_sample")
    p2026 = _gap_kv("y2026_days_pred_positive")
    h2026 = _gap_kv("y2026_days_held_long")
    _paragraph(
        doc,
        "IC ranks forecasts; it does not guarantee profitability after fees, discrete lots, or missed strong days when flat. "
        + (
            sanitize_for_word(
                f"Days with desired long signal but flat holdings: ~{100.0 * float(sh_gap):.1f}% of the sample (~{int(cnt_gap or 0)} days)."
            )
            if sh_gap is not None and cnt_gap is not None
            else ""
        )
        + " "
        + (
            sanitize_for_word(
                f"In the 2026 partial sample: {int(d2026 or 0)} sessions, pred>0 on ~{int(p2026 or 0)} days, yet held-long days = {int(h2026 or 0)} "
                f"because cash could not afford one lot—explaining ~0% strategy return despite a rising benchmark."
            )
            if d2026 is not None
            else ""
        ),
    )
    cs_h, cs_r = _capital_sensitivity_rows("en")
    if cs_r:
        _paragraph(
            doc,
            "Capital sensitivity (same fee rules; only initial cash varies) isolates how much headline realistic performance depends "
            "on lot affordability rather than the signal alone.",
        )
        _add_table_from_rows(doc, cs_h, cs_r, col_widths_cm=[4.5, 3.5, 3, 3.5])
        _paragraph(
            doc,
            "Capital sensitivity does not improve monotonically as initial capital increases. This indicates that lot "
            "affordability explains part of the IC–PnL gap, but the larger issue remains the realized timing path and the "
            "mismatch between close-to-close statistical prediction and executable trading returns. "
            "Larger capital increases affordable position size and exposure to adverse timing, so more capital does not "
            "mechanically improve risk-adjusted returns.",
        )
    if ph_r:
        _sec9 = 3
    else:
        _sec9 = 2
    th_h, th_r = _threshold_rows("en")
    if th_r:
        doc.add_heading(
            f"9.{_sec9} Ridge forecast threshold sweep (realistic)", level=2
        )
        _paragraph(
            doc,
            "Tau is the minimum predicted daily return required to go long (5 bp = 0.05% = 0.0005).",
        )
        _add_table_from_rows(doc, th_h, th_r, col_widths_cm=[5.5, 3.5, 3, 3.5])
        _paragraph(
            doc,
            "Raising tau reduces turnover and losses versus the baseline, but Ridge remains negative across this sweep—"
            "suggesting the gap is not only overtrading but also signal-to-execution mismatch (including the close-to-close target).",
        )
        _sec9 += 1

    doc.add_heading(f"9.{_sec9} Year-by-year breakdown", level=2)
    hdr_y, yr = _yearly_rows("en")
    if yr:
        _add_table_from_rows(
            doc, hdr_y, yr, col_widths_cm=[2.2, 2.2, 2.8, 2.8, 2.2, 2.2]
        )
        _paragraph(
            doc,
            "Approx. round trips: add 0.5 on each day the discrete position flips between flat and long; "
            "stable long or stable flat contributes 0. The annual sum is a round-trip proxy, not traded share volume.",
        )

    doc.add_heading("10. Robustness and Sanity Checks", level=1)
    doc.add_heading("10.1 Methods used in this report", level=2)
    _add_table_from_rows(
        doc,
        ["Method", "Purpose"],
        _method_usage_rows("en"),
        col_widths_cm=[6, 10.5],
    )
    _paragraph(
        doc,
        "Validation-driven lambda selection carries mild multiple-testing / data-snooping risk; Lo & MacKinlay (1990) formalize "
        "why small-sample tuning can inflate apparent predictability—consistent with treating Ridge as a disciplined baseline rather "
        "than a standalone discovery claim.",
    )
    doc.add_heading("10.2 Sanity checks (with status)", level=2)
    sn_h, sn_r = _sanity_check_rows("en")
    if sn_r:
        _add_table_from_rows(doc, sn_h, sn_r, col_widths_cm=[3.8, 2.8, 4.2, 4.2])
    _paragraph(
        doc,
        "Other microstructure risks (limits, liquidity, slippage) are listed qualitatively for transparency; see Harris (2003) "
        "for execution-level discussion beyond this close-to-close proxy.",
    )

    doc.add_heading("11. Limitations", level=1)
    for t in [
        "Close-to-close is a holding-period proxy, not a fully executable open-to-close target—the dominant limitation for "
        "tradability in this study; an open-to-close or open-print AB test is the first extension.",
        "No rolling refit or walk-forward design; regime shifts may destabilize fixed 2020–2021 estimation.",
        "FX spot proxy; long-or-flat only; validation-driven lambda implies mild data-mining risk (Lo & MacKinlay 1990).",
    ]:
        doc.add_paragraph(sanitize_for_word(t), style="List Bullet")

    doc.add_heading("12. Conclusion", level=1)
    _paragraph(
        doc,
        "Statistical predictability (IC) does not imply tradable alpha; placebo-shift checks help separate genuine timing structure "
        "from alignment artifacts. "
        "Final interpretation must follow the realistic lot-based path; vectorized results are only a stress illustration. "
        "The gap between IC and tradable PnL is driven mainly by execution assumptions, lot constraints, and capital sizing—not merely "
        "model choice. "
        "Under the current Ridge rule and lot-based execution assumptions, the strategy should not be interpreted as economically "
        "tradable alpha, despite strong statistical IC. "
        "Operational next steps: open-to-close targeting, threshold tuning with costs, and simpler directional models where appropriate.",
    )

    doc.add_heading("Appendix: Charts (test window)", level=1)
    _paragraph(doc, "Each figure includes a one-line interpretation.")
    figures_en_notes: List[Tuple[str, str, str]] = [
        (
            "equity_curve_realistic.png",
            "Equity (realistic)",
            "The realistic strategy fails to participate in much of the 2024–2026 benchmark rally, highlighting the cost of flat exposure and lot constraints.",
        ),
        (
            "mdd_underwater_realistic.png",
            "Drawdown (realistic)",
            "Drawdown moderates after 2024 in places, but benchmark-relative underperformance may not fully recover.",
        ),
        (
            "rolling_sharpe_realistic.png",
            "Rolling Sharpe (realistic)",
            "Rolling Sharpe turning negative again after 2025 indicates unstable risk-adjusted performance under realistic execution.",
        ),
        (
            "monthly_returns_heatmap_realistic.png",
            "Monthly heatmap (realistic)",
            "Losses concentrated around 2025 M3–M4 explain much of the annual decline.",
        ),
        (
            "equity_curve_vectorized.png",
            "Equity (vectorized)",
            "Diagnostic only—full-notional assumption.",
        ),
        (
            "mdd_underwater_vectorized.png",
            "Drawdown (vectorized)",
            "Contrast; often optimistic vs realistic.",
        ),
        (
            "monthly_returns_heatmap_vectorized.png",
            "Monthly heatmap (vectorized)",
            "Diagnostic contrast only—do not mix with realistic conclusions.",
        ),
        (
            "ic_by_year.png",
            "IC by year",
            "Positive IC does not guarantee profitable realistic years.",
        ),
        ("rolling_ic.png", "Rolling IC", "Tracks stability of ranking power."),
    ]
    for i, (fname, cap, note) in enumerate(figures_en_notes, start=1):
        _add_figure_with_note(doc, _OUT / fname, cap, i, "en", note)

    doc.add_heading("Reproducibility", level=1)
    _paragraph(
        doc,
        "From the AlphaEdge repo root: .venv/bin/python -m strategy_lab.strategies.tsmc_overnight_signal.run then "
        ".venv/bin/python -m strategy_lab.strategies.tsmc_overnight_signal.reports.generate_docx [--lang zh|en|both]. "
        "For PDF submission, re-export appendix figures at roughly 200–300 dpi for crisp text.",
    )

    doc.add_heading("References", level=1)
    for ref in [
        "Hasbrouck, J. (1995). One security, many markets: Determining the contributions to price discovery. Journal of Finance.",
        "Eun, C. S., & Sabherwal, S. (2003). Cross-border listings and price discovery: Evidence from U.S.-listed Canadian stocks. Journal of Finance.",
        "Harris, L. (2003). Trading and Exchanges: Market Microstructure for Practitioners. Oxford University Press.",
        "Lo, A. W., & MacKinlay, A. C. (1990). Data-snooping biases in tests of financial asset pricing models. Review of Financial Studies.",
        "yfinance (data vendor for U.S. and FX daily series; accessed May 2026).",
        "AlphaEdge core/backtest/README.md.",
        "Course manuscript (fees and sample assumptions).",
    ]:
        doc.add_paragraph(sanitize_for_word(ref), style="List Bullet")
