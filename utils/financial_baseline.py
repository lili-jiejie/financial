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
        "营业成本": "OPERATE_COST",
        "归母净利润": "PARENT_NETPROFIT",
    },
    "cash_flow_statement": {"经营现金流净额": "NETCASH_OPERATE"},
    "balance_sheet": {
        "总资产": "TOTAL_ASSETS",
        "总负债": "TOTAL_LIABILITIES",
        "货币资金": "MONETARYFUNDS",
        "存货": "INVENTORY",
        "短期借款": "SHORT_LOAN",
        "长期借款": "LONG_LOAN",
    },
}

HK_ITEM_CODES = {
    "income_statement": {
        "营业收入": (4001001, 4001999),
        "营业成本": (4005002,),
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


def _collect_annual_data(
    company: str, files: Iterable[str | Path]
) -> tuple[dict[int, dict[str, float | None]], list[Path]]:
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
    return years, used


def financial_baseline_report(company: str, files: Iterable[str | Path]) -> str:
    """Build a source-linked, annual numeric summary from this run's CSVs."""
    years, used = _collect_annual_data(company, files)
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
    if len(selected) >= 2 and selected[0] - selected[1] == 1:
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


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _growth(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous <= 0:
        return None
    return current / previous - 1


def financial_research_report(company: str, files: Iterable[str | Path]) -> str:
    """Expand the no-model baseline into a transparent, evidence-led report.

    The narrative is limited to formulas and monitoring questions. It cannot
    infer business drivers, produce a price target, or replace the model agent.
    """
    paths = [Path(path) for path in files]
    years, used = _collect_annual_data(company, paths)
    ordered = sorted(years, reverse=True)[:3]
    latest_year = ordered[0]
    latest = years[latest_year]
    prior_year = latest_year - 1
    prior = years.get(prior_year, {})
    revenue = latest.get("营业收入")
    profit = latest.get("归母净利润")
    cash = latest.get("经营现金流净额")
    assets = latest.get("总资产")
    liabilities = latest.get("总负债")
    revenue_growth = _growth(revenue, prior.get("营业收入"))
    profit_growth = _growth(profit, prior.get("归母净利润"))
    gross_margin = (
        _ratio(revenue - latest["营业成本"], revenue)
        if revenue is not None and latest.get("营业成本") is not None else None
    )
    prior_gross_margin = (
        _ratio(prior["营业收入"] - prior["营业成本"], prior["营业收入"])
        if prior.get("营业收入") is not None
        and prior.get("营业成本") is not None else None
    )
    debt_ratio = _ratio(liabilities, assets)
    prior_debt_ratio = _ratio(prior.get("总负债"), prior.get("总资产"))
    cash_conversion = _ratio(cash, profit)
    inventory_growth = _growth(latest.get("存货"), prior.get("存货"))

    lines = [
        "## 报告性质与核心观察",
        "",
        "本节是系统根据本次采集的年度报表独立计算的规则化财务研究稿；"
        "没有调用模型生成商业判断，也没有把卖方预测当作公司事实。"
        "它用于提出可复核的问题，不提供投资评级或目标价。",
        "",
    ]
    if revenue is not None and profit is not None:
        observation = (
            f"{latest_year} 年营业收入 {_money(revenue)} 亿、"
            f"归母净利润 {_money(profit)} 亿"
        )
        if revenue_growth is not None and profit_growth is not None:
            observation += (
                f"；较 {prior_year} 年分别变动 "
                f"{revenue_growth:+.1%}、{profit_growth:+.1%}"
            )
        lines.extend([observation + "。", ""])
    if cash_conversion is not None and debt_ratio is not None:
        lines.extend([
            f"{latest_year} 年经营现金流/归母净利润为 "
            f"{cash_conversion:.2f} 倍，年末资产负债率为 {debt_ratio:.1%}。"
            "这两项只描述现金与资产负债表状况，不能单独推断未来增长或偿债安全。",
            "",
        ])
    lines.extend([
        financial_baseline_report(company, paths),
        "",
        "## 收入、利润与盈利能力",
        "",
        "| 年度 | 营收同比 | 毛利率（收入减成本） | 归母净利润/收入 |",
        "| --- | ---: | ---: | ---: |",
    ])
    for year in ordered:
        row = years[year]
        preceding = years.get(year - 1, {})
        row_revenue = row.get("营业收入")
        row_cost = row.get("营业成本")
        row_profit = row.get("归母净利润")
        margin = (
            _ratio(row_revenue - row_cost, row_revenue)
            if row_revenue is not None and row_cost is not None else None
        )
        lines.append(
            f"| {year} | {_percent(_growth(row_revenue, preceding.get('营业收入')))} | "
            f"{_percent(margin)} | {_percent(_ratio(row_profit, row_revenue))} |"
        )
    lines.append("")
    if revenue_growth is not None:
        lines.append(
            f"{latest_year} 年营收同比 {revenue_growth:+.1%}，"
            "增长能否持续需结合销量、产品售价、业务结构及季度收入核查；"
            "年度总表无法识别具体驱动。"
        )
    else:
        lines.append("缺少相邻两年可比收入，暂无法计算最新年度营收增速。")
    if gross_margin is not None:
        margin_text = f"{latest_year} 年按收入与营业成本计算的毛利率为 {gross_margin:.1%}"
        if prior_gross_margin is not None:
            margin_text += (
                f"，较 {prior_year} 年变动 "
                f"{(gross_margin - prior_gross_margin) * 100:+.1f} 个百分点"
            )
        lines.append(margin_text + "；该变化的原因仍需核对分业务披露和成本说明。")
    lines.extend([
        "",
        "## 现金流、资产结构与营运资金",
        "",
        "| 年度 | 经营现金流/归母净利润 | 资产负债率 | 货币资金（亿） | 存货（亿） |",
        "| --- | ---: | ---: | ---: | ---: |",
    ])
    for year in ordered:
        row = years[year]
        conversion = _ratio(
            row.get("经营现金流净额"), row.get("归母净利润")
        )
        conversion_text = "—" if conversion is None else f"{conversion:.2f}"
        lines.append(
            f"| {year} | {conversion_text} | "
            f"{_percent(_ratio(row.get('总负债'), row.get('总资产')))} | "
            f"{_money(row.get('货币资金'))} | {_money(row.get('存货'))} |"
        )
    lines.append("")
    if cash is not None and profit is not None and profit > 0:
        lines.append(
            f"{latest_year} 年经营现金流 {_money(cash)} 亿，"
            f"为归母净利润的 {cash_conversion:.2f} 倍。"
            "该比值不能替代应收款、预付款、合同负债和资本开支的逐项分析。"
        )
    if debt_ratio is not None:
        sentence = f"{latest_year} 年资产负债率 {debt_ratio:.1%}"
        if prior_debt_ratio is not None:
            sentence += (
                f"，较 {prior_year} 年变动 "
                f"{(debt_ratio - prior_debt_ratio) * 100:+.1f} 个百分点"
            )
        lines.append(sentence + "；负债结构、受限资金与到期债务仍需单独核查。")
    lines.extend([
        "",
        "## 自研风险评估与监测",
        "",
        "下列为财务数据触发的核查问题，并非风险已经发生，也不代表概率或等级。",
        "",
        "| 可核对的财务证据 | 可能影响路径或解释限制 | 后续监测项 |",
        "| --- | --- | --- |",
    ])
    if revenue_growth is not None:
        lines.append(
            f"| {latest_year} 年营收同比 {revenue_growth:+.1%} | "
            "需求或销量变化可能影响收入；不能仅凭年度总表判断持续性 | "
            "季度营收、销量、订单与产品价格 |"
        )
    if gross_margin is not None:
        lines.append(
            f"| {latest_year} 年毛利率 {gross_margin:.1%} | "
            "原材料成本或产品售价变动可能影响单位盈利，具体驱动尚未确认 | "
            "季度毛利率、原材料价格、产品售价 |"
        )
    if inventory_growth is not None:
        lines.append(
            f"| {latest_year} 年末存货 {_money(latest.get('存货'))} 亿，"
            f"同比 {inventory_growth:+.1%} | "
            "存货增长可能占用资金；是否存在跌价仍需核对附注 | "
            "存货周转、跌价准备、经营现金流 |"
        )
    if debt_ratio is not None:
        lines.append(
            f"| {latest_year} 年末资产负债率 {debt_ratio:.1%} | "
            "总负债包含经营性项目，不能直接等同有息债务 | "
            "借款到期结构、受限货币资金、资产负债率 |"
        )
    lines.extend([
        "",
        "## 结论边界与待补资料",
        "",
        "财务总表可以验证规模、利润、现金与杠杆变化，但尚不足以确认竞争优势、"
        "行业份额、盈利预测或合理估值。下一步应核对公司公告中的分业务收入、"
        "销量与订单、成本构成、存货及应收款附注，再与卖方预测使用的年份和"
        "假设逐项比较。未取得股价、股本和经过核验的预测时，不计算目标价或投资评级。",
        "",
        "计算口径：毛利率＝（营业收入－营业成本）/营业收入；"
        "归母净利润/收入为简化盈利指标；现金转化比＝经营现金流净额/归母净利润"
        "（仅在利润为正时计算）；资产负债率＝总负债/总资产。"
        "本节数据来自本次采集的报表 CSV，正式投研使用前须与上市公司原始披露核对。",
        "",
        "使用的数据文件：" + "、".join(f"`{path.name}`" for path in used) + "。",
    ])
    return "\n".join(lines)
