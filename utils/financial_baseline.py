"""Small, reproducible financial baseline for the integrated report.

A-share wide CSVs contain CNY amounts; HK long-form CSVs do not identify a
currency, so their currency is left unverified. Missing values remain blank.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd


METRICS = {
    "income_statement": {
        "营业收入": "TOTAL_OPERATE_INCOME",
        "归母净利润": "PARENT_NETPROFIT",
    },
    "cash_flow_statement": {"经营现金流净额": "NETCASH_OPERATE"},
    "balance_sheet": {
        "总资产": "TOTAL_ASSETS",
        "总负债": "TOTAL_LIABILITIES",
    },
}

HK_ITEM_CODES = {
    "income_statement": {
        "营业收入": (4001001, 4001999),
        "归母净利润": (4025002,),
    },
    "cash_flow_statement": {"经营现金流净额": (3999,)},
    "balance_sheet": {
        "总资产": (4009999,),
        "总负债": (4025999,),
    },
}


def _year_end_rows(path: Path) -> dict[int, dict[str, float | None]]:
    frame = pd.read_csv(path, low_memory=False)
    if "REPORT_DATE" not in frame:
        return {}
    dates = pd.to_datetime(frame["REPORT_DATE"], errors="coerce")
    frame = frame.loc[(dates.dt.month == 12) & (dates.dt.day == 31)].copy()
    if frame.empty:
        return {}
    frame["_year"] = dates.loc[frame.index].dt.year
    if {"STD_ITEM_CODE", "AMOUNT"}.issubset(frame.columns):
        item_codes = next(
            (codes for suffix, codes in HK_ITEM_CODES.items() if suffix in path.stem),
            {},
        )
        if not item_codes:
            return {}
        frame["_code"] = pd.to_numeric(
            frame["STD_ITEM_CODE"], errors="coerce"
        ).astype("Int64")
        results = {}
        for year, annual in frame.groupby("_year"):
            values = {}
            for label, alternatives in item_codes.items():
                value = None
                for code in alternatives:
                    matches = pd.to_numeric(
                        annual.loc[annual["_code"] == code, "AMOUNT"],
                        errors="coerce",
                    ).dropna()
                    if not matches.empty:
                        value = float(matches.iloc[0])
                        break
                values[label] = value
            results[int(year)] = values
        return results
    frame = frame.sort_values("_year", ascending=False).drop_duplicates("_year")
    metric_columns = next(
        (columns for suffix, columns in METRICS.items() if suffix in path.stem), {}
    )
    if not metric_columns or not any(
        column in frame.columns for column in metric_columns.values()
    ):
        return {}
    results = {}
    for _, row in frame.iterrows():
        values = {}
        for label, column in metric_columns.items():
            raw = pd.to_numeric(row.get(column), errors="coerce")
            values[label] = float(raw) if pd.notna(raw) else None
        results[int(row["_year"])] = values
    return results


def _money(value: float | None) -> str:
    return "—" if value is None else f"{value / 1e8:,.2f}"


def financial_baseline_report(company: str, files: Iterable[str | Path]) -> str:
    """Build a source-linked, annual numeric summary from this run's CSVs."""
    paths = [Path(path) for path in files]
    years: dict[int, dict[str, float | None]] = {}
    used: list[Path] = []
    for path in paths:
        if not path.is_file() or not any(key in path.stem for key in METRICS):
            continue
        rows = _year_end_rows(path)
        if rows:
            used.append(path)
        for year, values in rows.items():
            years.setdefault(year, {}).update(values)
    if not years or not any(
        value is not None
        for row in years.values()
        for value in row.values()
    ):
        raise ValueError(f"{company} 缺少可读取的年度财务数据")
    selected = sorted(years, reverse=True)[:3]
    is_hk = any("_HK_" in path.name for path in used)
    lines = [
        f"## {company}财务数据基线（程序计算）",
        "",
        "仅使用本次采集的年末财务报表；金额单位为亿"
        + ("（原始报表币种未核验）" if is_hk else "元人民币") + "。"
        "空值保持为空，不作预测。年度数据的披露时间与修订情况仍需回原始公告核对。",
        "",
        "| 年度 | 营业收入 | 归母净利润 | 经营现金流净额 | "
        "总资产 | 总负债 | 资产负债率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for year in selected:
        row = years[year]
        assets, liabilities = row.get("总资产"), row.get("总负债")
        debt_ratio = (
            f"{liabilities / assets:.1%}"
            if assets is not None and assets > 0 and liabilities is not None else "—"
        )
        lines.append(
            f"| {year} | {_money(row.get('营业收入'))} | "
            f"{_money(row.get('归母净利润'))} | "
            f"{_money(row.get('经营现金流净额'))} | "
            f"{_money(assets)} | {_money(liabilities)} | {debt_ratio} |"
        )
    if len(selected) >= 2:
        latest, previous = years[selected[0]], years[selected[1]]
        growth = []
        for label in ("营业收入", "归母净利润"):
            now, prior = latest.get(label), previous.get(label)
            if now is not None and prior is not None and prior > 0:
                growth.append(f"{label}同比 {(now / prior - 1):+.1%}")
        if growth:
            lines.extend([
                "",
                f"{selected[0]} 年相较 {selected[1]} 年："
                + "；".join(growth) + "。",
            ])
    lines.extend([
        "",
        "核查重点：将经营现金流与利润的方向和变化共同检查；"
        "如后续卖方预测依赖持续增长或利润率改善，应复核订单、价格与原材料成本披露。"
        "这些是核查路径，不是对未来业绩或风险等级的判断。",
        "",
        "数据文件：" + "、".join(f"`{path.name}`" for path in used) + "。",
    ])
    return "\n".join(lines)
