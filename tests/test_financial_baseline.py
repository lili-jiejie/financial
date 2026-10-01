"""Hand-checkable checks for the deterministic financial baseline."""

import pandas as pd
import pytest

from utils.financial_baseline import financial_baseline_report


def test_baseline_uses_year_end_values_and_keeps_missing_cells_empty(tmp_path):
    income = tmp_path / "公司_A_300750_income_statement_年度.csv"
    balance = tmp_path / "公司_A_300750_balance_sheet_年度.csv"
    cash = tmp_path / "公司_A_300750_cash_flow_statement_年度.csv"
    pd.DataFrame(
        {
            "REPORT_DATE": ["2025-12-31", "2025-06-30", "2024-12-31"],
            "TOTAL_OPERATE_INCOME": [120e8, 999e8, 100e8],
            "PARENT_NETPROFIT": [20e8, 999e8, 10e8],
        }
    ).to_csv(income, index=False)
    pd.DataFrame(
        {
            "REPORT_DATE": ["2025-12-31", "2024-12-31"],
            "TOTAL_ASSETS": [200e8, 100e8],
            "TOTAL_LIABILITIES": [80e8, 60e8],
        }
    ).to_csv(balance, index=False)
    pd.DataFrame(
        {
            "REPORT_DATE": ["2025-12-31", "2024-12-31"],
            "NETCASH_OPERATE": [None, 5e8],
        }
    ).to_csv(cash, index=False)

    report = financial_baseline_report("测试公司", [income, balance, cash])
    assert "| 2025 | 120.00 | 20.00 | — | 200.00 | 80.00 | 40.0% |" in report
    assert "| 2024 | 100.00 | 10.00 | 5.00 | 100.00 | 60.00 | 60.0% |" in report
    assert "营业收入同比 +20.0%" in report
    assert "归母净利润同比 +100.0%" in report
    assert "999.00" not in report


def test_baseline_rejects_missing_annual_data(tmp_path):
    path = tmp_path / "公司_A_300750_income_statement_年度.csv"
    pd.DataFrame({"REPORT_DATE": ["2025-06-30"]}).to_csv(path, index=False)
    with pytest.raises(ValueError, match="缺少可读取的年度财务数据"):
        financial_baseline_report("测试公司", [path])


def test_hk_long_form_rows_are_mapped_without_claiming_a_currency(tmp_path):
    rows = {
        "income_statement": [
            (4001001, 50e8), (4025002, -10e8),
        ],
        "balance_sheet": [(4009999, 200e8), (4025999, 40e8)],
        "cash_flow_statement": [(3999, -5e8)],
    }
    paths = []
    for statement, items in rows.items():
        path = tmp_path / f"公司_HK_00020_{statement}_年度.csv"
        pd.DataFrame({
            "REPORT_DATE": ["2025-12-31"] * len(items),
            "STD_ITEM_CODE": [code for code, _ in items],
            "AMOUNT": [amount for _, amount in items],
        }).to_csv(path, index=False)
        paths.append(path)

    report = financial_baseline_report("测试港股", paths)
    assert "| 2025 | 50.00 | -10.00 | -5.00 | 200.00 | 40.00 | 20.0% |" in report
    assert "原始报表币种未核验" in report
    assert "亿元人民币" not in report


def test_baseline_rejects_year_end_rows_without_recognized_metrics(tmp_path):
    path = tmp_path / "公司_A_300750_income_statement_年度.csv"
    pd.DataFrame({"REPORT_DATE": ["2025-12-31"], "OTHER": [1]}).to_csv(
        path, index=False
    )
    with pytest.raises(ValueError, match="缺少可读取的年度财务数据"):
        financial_baseline_report("测试公司", [path])
