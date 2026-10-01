"""Tests for the public sell-side lookup and evidence-bound supplement."""

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import markdown
import pytest
import requests

from utils.sell_side_review import (
    EastmoneySellSideProvider,
    SellSideReport,
    SellSideSearch,
    _focused_excerpt,
    _parse_analysis,
    build_sell_side_review,
    normalize_a_share_code,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.params = None

    def get(self, _url, *, params, timeout):
        self.params = params
        return FakeResponse({"data": self.rows, "TotalPage": 1})


def row(info_code, broker, published_on, stock_code="300750"):
    return {
        "infoCode": info_code,
        "stockCode": stock_code,
        "stockName": "宁德时代",
        "orgSName": broker,
        "title": "公司研报",
        "publishDate": published_on,
    }


def test_code_normalization_and_rejection():
    assert normalize_a_share_code("SZ300750") == "300750"
    assert normalize_a_share_code("600519.SH") == "600519"
    assert normalize_a_share_code("BJ830799") == "830799"
    with pytest.raises(ValueError):
        normalize_a_share_code("00020")


def test_code_only_name_resolution_does_not_require_broker_coverage(monkeypatch):
    import akshare as ak
    import pandas as pd

    from utils.sell_side_review import lookup_a_share_company

    monkeypatch.setattr(
        ak,
        "stock_info_a_code_name",
        lambda: pd.DataFrame({"code": ["300750"], "name": ["宁德时代"]}),
    )
    assert lookup_a_share_company("300750", session=FakeSession([])) == "宁德时代"


def test_standalone_cli_rejects_hk_code_cleanly(monkeypatch, capsys):
    import sys

    from utils.sell_side_review import main

    monkeypatch.setattr(sys, "argv", ["program", "00020"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "仅支持六位 A 股代码" in capsys.readouterr().err


def test_standalone_cli_reports_missing_original_markdown(
    monkeypatch, capsys, tmp_path
):
    import sys

    from utils.sell_side_review import main

    missing = tmp_path / "missing.md"
    monkeypatch.setattr(sys, "argv", ["program", "300750", "--report", str(missing)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "无法读取原研报 Markdown" in capsys.readouterr().err


def test_sources_only_cli_never_calls_model_even_with_api_key(monkeypatch, tmp_path):
    import sys

    import utils.sell_side_review as review

    observed = {}

    def fake_review(**kwargs):
        observed.update(kwargs)
        return "## 公开来源"

    output = tmp_path / "sources.md"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(review, "build_sell_side_review", fake_review)
    monkeypatch.setattr(
        sys,
        "argv",
        ["program", "300750", "--sources-only", "--output", str(output)],
    )
    review.main()
    assert observed["llm"] is None
    assert observed["sources_only"] is True
    assert output.read_text(encoding="utf-8") == "## 公开来源\n"


def test_search_filters_other_companies_future_dates_and_duplicate_brokers():
    session = FakeSession(
        [
            row("AP202609291829995400", "甲证券", "2026-09-29"),
            row("AP202609281829995401", "甲证券", "2026-09-28"),
            row("AP202609271829995402", "乙证券", "2026-09-27"),
            row("AP202610021829995403", "丙证券", "2026-10-02"),
            row("AP202609261829995404", "丁证券", "2026-09-26", "600519"),
        ]
    )
    provider = EastmoneySellSideProvider(session=session)
    result = provider.search(
        "300750", as_of=date(2026, 10, 1), max_reports=3, read_content=False
    )

    assert [report.broker for report in result.reports] == ["甲证券", "乙证券"]
    assert session.params["code"] == "300750"
    assert session.params["beginTime"] == "2026-07-03"
    assert session.params["endTime"] == "2026-10-02"


def test_search_reads_next_page_when_first_has_one_broker():
    class PagedSession:
        def __init__(self):
            self.visited = []

        def get(self, _url, *, params, timeout):
            page = int(params["pageNo"])
            self.visited.append(page)
            rows = (
                [
                    row("AP202609291829995400", "甲证券", "2026-09-29"),
                    row("AP202609281829995401", "无效日期券商", "2026-10-02"),
                    row("invalid", "无效编号券商", "2026-09-28"),
                ]
                if page == 1
                else [row("AP202609271829995402", "乙证券", "2026-09-27")]
            )
            return FakeResponse({"data": rows, "TotalPage": 2})

    session = PagedSession()
    result = EastmoneySellSideProvider(session=session).search(
        "300750", as_of=date(2026, 10, 1), max_reports=2, read_content=False
    )
    assert session.visited == [1, 2]
    assert [report.broker for report in result.reports] == ["甲证券", "乙证券"]


def test_search_retries_one_transient_index_timeout():
    class FlakySession:
        def __init__(self):
            self.calls = 0

        def get(self, _url, *, params, timeout):
            self.calls += 1
            if self.calls == 1:
                raise requests.Timeout("temporary timeout")
            return FakeResponse(
                {
                    "data": [row("AP202609291829995400", "甲证券", "2026-09-29")],
                    "TotalPage": 1,
                }
            )

    session = FlakySession()
    result = EastmoneySellSideProvider(session=session).search(
        "300750", as_of=date(2026, 10, 1), max_reports=1, read_content=False
    )
    assert session.calls == 2
    assert len(result.reports) == 1


def test_unavailable_index_is_reported_without_model_claims():
    class DownSession:
        def get(self, _url, *, params, timeout):
            raise requests.Timeout("public index timeout")

    llm = FixedLLM()
    output = build_sell_side_review(
        code="300750",
        original_report="原研报",
        llm=llm,
        provider=EastmoneySellSideProvider(session=DownSession()),
        as_of=date(2026, 10, 1),
    )
    assert "公开研报索引不可用" in output
    assert "暂不生成观点对照或风险判断" in output
    assert not llm.called


def test_search_keeps_first_page_when_later_page_fails():
    class PartialSession:
        def get(self, _url, *, params, timeout):
            if params["pageNo"] == "2":
                raise requests.Timeout("temporary timeout")
            return FakeResponse(
                {
                    "data": [row("AP202609291829995400", "甲证券", "2026-09-29")],
                    "TotalPage": 2,
                }
            )

    result = EastmoneySellSideProvider(session=PartialSession()).search(
        "300750", as_of=date(2026, 10, 1), max_reports=3, read_content=False
    )
    assert len(result.reports) == 1
    assert "结果可能不完整" in " ".join(result.notices)


def test_search_prefers_readable_brokers_over_unreadable_newer_sources(monkeypatch):
    session = FakeSession(
        [
            row("AP202609291829995400", "甲证券", "2026-09-29"),
            row("AP202609281829995401", "乙证券", "2026-09-28"),
            row("AP202609271829995402", "丙证券", "2026-09-27"),
            row("AP202609261829995403", "丁证券", "2026-09-26"),
        ]
    )
    provider = EastmoneySellSideProvider(session=session)
    checked = []

    def read_content(report):
        checked.append(report.broker)
        if report.broker in ("丙证券", "丁证券"):
            return replace(report, content_level="web_excerpt", content="可核对正文")
        return report

    monkeypatch.setattr(provider, "read_public_content", read_content)
    result = provider.search("300750", as_of=date(2026, 10, 1), max_reports=2)
    assert checked == ["甲证券", "乙证券", "丙证券", "丁证券"]
    assert [report.broker for report in result.reports] == ["丙证券", "丁证券"]


def test_no_recent_reports_is_reported_without_analysis():
    provider = EastmoneySellSideProvider(session=FakeSession([]))
    llm = FixedLLM()
    output = build_sell_side_review(
        code="300750",
        original_report="原研报",
        llm=llm,
        provider=provider,
        as_of=date(2026, 10, 1),
    )
    assert "未找到可核对的公开个股研报" in output
    assert "本次选取 0 篇" in output
    assert not llm.called


def test_analysis_requires_real_quotes_and_valid_source_ids():
    original = "公司营业收入继续增长，产能利用率保持稳定。"
    source = "报告认为储能订单增长较快，但原材料价格波动可能侵蚀利润。"
    source_two = "另一家券商认为储能收入增速可能放缓，需要观察行业竞争。"
    response = json.dumps(
        {
            "agreements": [
                {
                    "point": "需求增长",
                    "original_quote": "公司营业收入继续增长",
                    "source_quote": "储能订单增长较快",
                    "source_ids": ["S1"],
                },
                {
                    "point": "编造",
                    "original_quote": "不存在的原文引述",
                    "source_quote": "储能订单增长较快",
                    "source_ids": ["S1"],
                },
            ],
            "differences": [],
            "assumptions": [
                {
                    "claim": "订单增长可持续",
                    "verification": "跟踪公司公告中的订单披露",
                    "source_quote": "储能订单增长较快",
                    "source_ids": ["S1"],
                },
            ],
            "broker_differences": [
                {
                    "topic": "储能业务增速",
                    "source_a": "S1",
                    "quote_a": "储能订单增长较快",
                    "source_b": "S2",
                    "quote_b": "储能收入增速可能放缓",
                },
                {
                    "topic": "无效比较",
                    "source_a": "S1",
                    "quote_a": "储能订单增长较快",
                    "source_b": "S2",
                    "quote_b": "并不存在的判断依据",
                },
            ],
            "risks": [
                {
                    "risk": "成本风险",
                    "impact_path": "可能压低利润",
                    "monitor": "跟踪原材料价格",
                    "source_quote": "原材料价格波动可能侵蚀利润",
                    "source_ids": ["S1"],
                },
                {
                    "risk": "无依据",
                    "impact_path": "可能压低利润",
                    "monitor": "跟踪原材料价格",
                    "source_quote": "报告没有提过这一内容",
                    "source_ids": ["S1"],
                },
                {
                    "risk": "错误编号",
                    "impact_path": "可能压低利润",
                    "monitor": "跟踪原材料价格",
                    "source_quote": "原材料价格波动可能侵蚀利润",
                    "source_ids": ["S2"],
                },
                {
                    "risk": "缺监测项",
                    "impact_path": "可能压低利润",
                    "source_quote": "原材料价格波动可能侵蚀利润",
                    "source_ids": ["S1"],
                },
            ],
        },
        ensure_ascii=False,
    )
    sources = {"S1": source, "S2": source_two}
    result = _parse_analysis(response, sources, original)
    wrapped = _parse_analysis(
        "分析结果：\n```json\n" + response + "\n```", sources, original
    )

    assert len(result["agreements"]) == 1
    assert wrapped == result
    assert len(result["assumptions"]) == 1
    assert len(result["broker_differences"]) == 1
    assert len(result["risks"]) == 1
    assert result["risks"][0]["source_ids"] == ["S1"]


def test_prompt_excerpt_keeps_late_risk_within_budget():
    source = "业务进展稳定。" * 250 + "风险提示：原材料价格大幅波动可能侵蚀利润。"
    excerpt = _focused_excerpt(source, 800)
    assert len(excerpt) <= 800
    assert "原材料价格大幅波动可能侵蚀利润" in excerpt


def test_generated_report_excerpt_preserves_late_risk_section():
    original = "## 公司概况\n" + "收入持续增长且风险可控。" * 300
    original += "\n## 风险提示\n原料成本增加可能压低毛利率，需跟踪采购成本。"
    excerpt = _focused_excerpt(original, 800)
    assert len(excerpt) <= 800
    assert "## 风险提示" in excerpt
    assert "原料成本增加可能压低毛利率" in excerpt


def test_blocked_pdf_falls_back_to_public_detail_excerpt():
    class ContentResponse:
        encoding = "utf-8"

        def __init__(self, content):
            self.content = content
            self.text = content.decode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            yield self.content

    class ContentSession:
        def get(self, url, **_kwargs):
            if "pdf.dfcfw.com" in url:
                return ContentResponse(b"<script>verification</script>")
            html = (
                '<div class="ctx-content">'
                + "业绩增长具有一定持续性，仍需关注原材料价格波动。" * 4
                + "</div>"
            )
            return ContentResponse(html.encode("utf-8"))

    report = SellSideReport(
        info_code="AP202609291829995400",
        stock_code="300750",
        company="宁德时代",
        broker="甲证券",
        title="公司研报",
        published_on=date(2026, 9, 29),
        detail_url="https://data.eastmoney.com/report/info/AP202609291829995400.html",
        pdf_url="https://pdf.dfcfw.com/pdf/H3_AP202609291829995400_1.pdf",
    )
    reviewed = EastmoneySellSideProvider(session=ContentSession()).read_public_content(
        report
    )
    assert reviewed.content_level == "web_excerpt"
    assert "原材料价格波动" in reviewed.content


class FixedProvider:
    def __init__(self, content):
        self.content = content

    def search(self, _code, **kwargs):
        report = SellSideReport(
            info_code="AP202609291829995400",
            stock_code="300750",
            company="宁德时代",
            broker="甲证券",
            title="宁德时代公司研报",
            published_on=date(2026, 9, 29),
            detail_url="https://data.eastmoney.com/report/info/AP202609291829995400.html",
            pdf_url="https://pdf.dfcfw.com/pdf/H3_AP202609291829995400_1.pdf",
        )
        if self.content:
            report = replace(report, content_level="web_excerpt", content=self.content)
        return SellSideSearch([report], [], date(2026, 10, 1), 90)


class FixedLLM:
    def __init__(self):
        self.called = False
        self.prompt = ""

    def call(self, prompt, **_kwargs):
        self.called = True
        self.prompt = prompt
        return json.dumps(
            {
                "agreements": [],
                "differences": [],
                "risks": [
                    {
                        "risk": "成本波动风险",
                        "impact_path": "可能压低毛利率",
                        "monitor": "跟踪原材料价格",
                        "source_ids": ["S1"],
                        "source_quote": "原材料价格波动可能侵蚀利润",
                    }
                ],
            },
            ensure_ascii=False,
        )


def test_metadata_only_never_calls_model_or_invents_risks():
    llm = FixedLLM()
    output = build_sell_side_review(
        code="300750",
        original_report="原研报",
        llm=llm,
        provider=FixedProvider(""),
    )
    assert not llm.called
    assert "仅标题与元数据" in output
    assert "暂不生成观点对照或风险判断" in output


def test_metadata_label_blocks_analysis_even_if_provider_accidentally_sets_content():
    report = replace(
        FixedProvider("原材料价格波动可能侵蚀利润。").search("300750").reports[0],
        content_level="metadata",
    )

    class InconsistentProvider:
        def search(self, _code, **_kwargs):
            return SellSideSearch([report], [], date(2026, 10, 1), 90)

    llm = FixedLLM()
    output = build_sell_side_review(
        code="300750", original_report="", llm=llm, provider=InconsistentProvider()
    )
    assert not llm.called
    assert "未取得可阅读的正文" in output


def test_sources_only_reason_is_not_mistaken_for_missing_key():
    output = build_sell_side_review(
        code="300750",
        original_report="",
        llm=None,
        provider=FixedProvider("原材料价格波动可能侵蚀利润，需持续关注。"),
        sources_only=True,
    )
    assert "按来源清单模式运行" in output
    assert "未配置模型密钥" not in output


@pytest.mark.parametrize(
    ("answer", "message"),
    [("", "模型未返回分析结果"), ("无法解析的响应", "分析格式无法解析")],
)
def test_model_failure_keeps_sources_and_explains_failure(answer, message):
    class Model:
        def call(self, _prompt, **_kwargs):
            return answer

    output = build_sell_side_review(
        code="300750",
        original_report="公司经营保持稳定。",
        llm=Model(),
        provider=FixedProvider("原材料价格波动可能侵蚀利润，需持续关注。"),
    )
    assert "[宁德时代公司研报]" in output
    assert message in output
    assert "需要核实的风险" not in output


def test_readable_source_adds_auditable_risk():
    llm = FixedLLM()
    output = build_sell_side_review(
        code="300750",
        original_report="公司营业收入继续增长。",
        llm=llm,
        provider=FixedProvider("原材料价格波动可能侵蚀利润，报告提示关注成本趋势。"),
    )
    assert llm.called
    assert "目标股票代码为 300750" in llm.prompt
    assert "同行数据不得写成目标公司的观点" in llm.prompt
    assert "公司：宁德时代" in output
    assert "成本波动风险" in output
    assert (
        "风险点：成本波动风险；影响路径：可能压低毛利率；监测项：跟踪原材料价格"
        in output
    )
    assert "原材料价格波动可能侵蚀利润" in output
    assert "来源：[S1]" in output


def test_authorized_provider_can_supply_full_text_without_changing_review_logic():
    base = FixedProvider("原材料价格波动可能侵蚀利润，需持续关注。")
    licensed = replace(base.search("300750").reports[0], content_level="licensed_full")

    class LicensedProvider:
        def search(self, _code, **_kwargs):
            return SellSideSearch([licensed], [], date(2026, 10, 1), 90)

    output = build_sell_side_review(
        code="300750",
        original_report="公司营业收入继续增长。",
        llm=FixedLLM(),
        provider=LicensedProvider(),
    )
    assert "授权完整正文" in output
    assert "风险点：成本波动风险" in output


def test_long_reports_are_bounded_before_model_call():
    llm = FixedLLM()
    original = "公司收入持续增长。" * 2000 + "风险提示：经营现金流可能承压。"
    source = "业务表现稳健。" * 1500 + "原材料价格波动可能侵蚀利润。"
    output = build_sell_side_review(
        code="300750",
        original_report=original,
        llm=llm,
        provider=FixedProvider(source),
    )
    assert llm.called
    assert len(llm.prompt) < 6500
    assert "仅核对原研报和卖方正文中的部分重点片段" in output


def test_source_context_budget_holds_when_user_requests_twenty_brokers():
    first = (
        FixedProvider("原材料价格波动可能侵蚀利润。" * 60).search("300750").reports[0]
    )

    class ManyBrokers:
        def search(self, _code, **_kwargs):
            reports = [
                replace(first, broker=f"券商{index}", title=f"报告{index}")
                for index in range(20)
            ]
            return SellSideSearch(reports, [], date(2026, 10, 1), 90)

    llm = FixedLLM()
    build_sell_side_review(
        code="300750",
        original_report="公司经营保持稳定。",
        llm=llm,
        provider=ManyBrokers(),
        max_reports=20,
    )
    evidence = json.loads(llm.prompt.split("卖方资料：\n", 1)[1])
    assert len(evidence) == 20
    assert sum(len(item["text"]) for item in evidence) <= 1800


def test_untrusted_titles_and_model_text_cannot_break_markdown_structure():
    source = FixedProvider("原材料价格波动可能侵蚀利润，需持续关注。")
    report = replace(
        source.search("300750").reports[0],
        company="宁德时代<img src=x>",
        broker="甲|乙证券",
        title="[恶意](https://example.com)|<img src=x>",
    )

    class UnsafeProvider:
        def search(self, _code, **_kwargs):
            return SellSideSearch([report], [], date(2026, 10, 1), 90)

    class UnsafeLLM:
        def call(self, _prompt, **_kwargs):
            return json.dumps(
                {
                    "risks": [
                        {
                            "risk": "<img src=x> *成本*",
                            "impact_path": "可能压低毛利率",
                            "monitor": "跟踪原材料价格",
                            "source_quote": "原材料价格波动可能侵蚀利润",
                            "source_ids": ["S1"],
                        }
                    ]
                },
                ensure_ascii=False,
            )

    output = build_sell_side_review(
        code="300750", original_report="", llm=UnsafeLLM(), provider=UnsafeProvider()
    )
    assert "甲\\|乙证券" in output
    assert "\\[恶意\\]" in output
    assert "&lt;img src=x&gt; \\*成本\\*" in output
    assert "公司：宁德时代&lt;img src=x&gt;" in output
    assert "<img src=x>" not in output
    rendered = markdown.markdown(output, extensions=["tables"])
    assert rendered.count("<tr>") == 2
    assert 'href="https://example.com"' not in rendered


def test_broker_difference_renders_both_report_dates():
    first = FixedProvider("报告认为储能订单增长较快，需要持续跟踪销量。")
    first_report = first.search("300750").reports[0]
    second_report = replace(
        first_report,
        info_code="AP202607311827537552",
        broker="乙证券",
        published_on=date(2026, 7, 31),
        detail_url="https://data.eastmoney.com/report/info/AP202607311827537552.html",
        content="另一家券商认为储能收入增速可能放缓，需要观察行业竞争。",
    )

    class TwoProvider:
        def search(self, _code, **_kwargs):
            return SellSideSearch(
                [first_report, second_report], [], date(2026, 10, 1), 90
            )

    class DifferenceLLM:
        def call(self, _prompt, **_kwargs):
            return json.dumps(
                {
                    "agreements": [],
                    "differences": [],
                    "assumptions": [],
                    "risks": [],
                    "broker_differences": [
                        {
                            "topic": "储能增速",
                            "source_a": "S1",
                            "quote_a": "储能订单增长较快",
                            "source_b": "S2",
                            "quote_b": "储能收入增速可能放缓",
                        }
                    ],
                },
                ensure_ascii=False,
            )

    output = build_sell_side_review(
        code="300750", original_report="", llm=DifferenceLLM(), provider=TwoProvider()
    )
    assert "券商之间的差异线索" in output
    assert (
        "[S1](https://data.eastmoney.com/report/info/AP202609291829995400.html)"
        in output
    )
    assert (
        "[S2](https://data.eastmoney.com/report/info/AP202607311827537552.html)"
        in output
    )
    assert "（索引日期：2026-09-29）" in output
    assert "（索引日期：2026-07-31）" in output


def test_integrated_report_appends_supplement_before_saving(monkeypatch, tmp_path):
    import integrated_research_report_generator as integrated

    monkeypatch.chdir(tmp_path)
    generator = object.__new__(integrated.IntegratedResearchReportGenerator)
    generator.target_company = "宁德时代"
    generator.target_company_code = "300750"
    generator.target_company_market = "A"
    generator.run_id = "A_300750_test"
    generator.enable_sell_side_review = True
    generator.sell_side_days = 90
    generator.sell_side_max_reports = 3
    generator.sell_side_as_of = date(2026, 10, 1)
    generator.sell_side_provider = object()
    generator.llm = object()
    generator.extract_images_from_markdown = lambda _src, _dir, dst: Path(
        dst
    ).write_text("基础资料", encoding="utf-8")
    generator.load_report_content = lambda _path: "基础资料"
    generator.get_background = lambda: "公司背景"
    generator.generate_outline = lambda *_args: [{"part_title": "投资分析"}]
    generator.generate_section = lambda *_args: "## 投资分析\n原始观点。"
    steps = []
    generator.format_markdown = lambda _path: steps.append("format")
    generator.convert_to_docx = lambda _path: None
    seen = {}

    def fake_review(**kwargs):
        steps.append("review")
        seen.update(kwargs)
        return "## 卖方研报对照与风险补充\n有依据的风险线索。"

    monkeypatch.setattr(integrated, "build_sell_side_review", fake_review)
    base = tmp_path / "base.md"
    base.write_text("基础资料", encoding="utf-8")
    output = generator.stage2_deep_report_generation(str(base))

    saved = (tmp_path / output).read_text(encoding="utf-8")
    assert "# 宁德时代研究报告" in saved
    assert "原始观点" in saved
    assert "有依据的风险线索" in saved
    assert seen["code"] == "300750"
    assert seen["max_reports"] == 3
    assert seen["provider"] is generator.sell_side_provider
    assert steps == ["format", "review"]


def test_hk_report_explains_current_source_coverage(monkeypatch, tmp_path):
    import integrated_research_report_generator as integrated

    monkeypatch.chdir(tmp_path)
    generator = object.__new__(integrated.IntegratedResearchReportGenerator)
    generator.target_company = "商汤科技"
    generator.target_company_code = "00020"
    generator.target_company_market = "HK"
    generator.run_id = "HK_00020_test"
    generator.enable_sell_side_review = True
    generator.sell_side_provider = None
    generator.llm = object()
    generator.extract_images_from_markdown = lambda _src, _dir, dst: Path(
        dst
    ).write_text("基础资料", encoding="utf-8")
    generator.load_report_content = lambda _path: "基础资料"
    generator.get_background = lambda: "公司背景"
    generator.generate_outline = lambda *_args: [{"part_title": "投资分析"}]
    generator.generate_section = lambda *_args: "## 投资分析\n原始观点。"
    generator.format_markdown = lambda _path: None
    generator.convert_to_docx = lambda _path: None
    monkeypatch.setattr(
        integrated,
        "build_sell_side_review",
        lambda **_kwargs: pytest.fail("HK must not call the A-share source"),
    )
    base = tmp_path / "base.md"
    base.write_text("基础资料", encoding="utf-8")
    output = generator.stage2_deep_report_generation(str(base))

    saved = (tmp_path / output).read_text(encoding="utf-8")
    assert "当前自动检索仅接入 A 股" in saved
    assert "未生成观点或风险判断" in saved


def test_competitor_codes_are_validated_before_collection():
    from integrated_research_report_generator import (
        a_share_financial_code,
        normalize_listed_competitors,
    )

    assert a_share_financial_code("300750") == "SZ300750"
    assert a_share_financial_code("SH600519") == "SH600519"
    assert a_share_financial_code("BJ830799") == "BJ830799"
    assert a_share_financial_code("920071") == "BJ920071"

    peers = normalize_listed_competitors(
        [
            {"name": "甲", "code": "600519", "market": "A股"},
            {"name": "甲重复", "code": "SH600519", "market": "A股"},
            {"name": "乙", "code": "700", "market": "港股"},
            {"name": "不上市", "code": "", "market": "未上市"},
            {"name": "错误代码", "code": "xyz", "market": "A股"},
            {"name": "../错误路径", "code": "300750", "market": "A股"},
        ]
    )
    assert peers == [
        {"name": "甲", "code": "SH600519", "market": "A"},
        {"name": "乙", "code": "00700", "market": "HK"},
    ]


def test_runs_keep_inputs_for_different_stocks_separate(monkeypatch, tmp_path):
    from integrated_research_report_generator import IntegratedResearchReportGenerator

    monkeypatch.chdir(tmp_path)
    first = IntegratedResearchReportGenerator(
        target_company="宁德时代",
        target_company_code="SZ300750",
        target_company_market="A",
    )
    second = IntegratedResearchReportGenerator(
        target_company="贵州茅台",
        target_company_code="600519",
        target_company_market="A",
    )
    assert first.run_id != second.run_id
    assert first.target_company_code == "300750"
    assert Path(first.data_dir).is_dir()
    assert Path(second.data_dir).is_dir()
    assert first.data_dir != second.data_dir


def test_report_images_are_linked_to_this_runs_own_directory(tmp_path):
    from integrated_research_report_generator import IntegratedResearchReportGenerator

    generator = object.__new__(IntegratedResearchReportGenerator)
    image = tmp_path / "figure.png"
    image.write_bytes(b"fixture image bytes")
    source = tmp_path / "source.md"
    source.write_text("![图](figure.png)", encoding="utf-8")
    processed = tmp_path / "processed.md"
    target_dir = tmp_path / "images" / "A_300750_test"
    generator.extract_images_from_markdown(str(source), str(target_dir), str(processed))
    assert (target_dir / "figure.png").read_bytes() == b"fixture image bytes"
    assert "./images/A_300750_test/figure.png" in processed.read_text(encoding="utf-8")
    generator.copy_image = lambda *_args: False
    failed = tmp_path / "failed.md"
    generator.extract_images_from_markdown(
        str(source), str(tmp_path / "images" / "A_600519_test"), str(failed)
    )
    assert "![" not in failed.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "options",
    [{"sell_side_days": 0}, {"sell_side_days": 366}, {"sell_side_max_reports": 21}],
)
def test_full_generator_rejects_invalid_review_options_before_running(options):
    from integrated_research_report_generator import IntegratedResearchReportGenerator

    with pytest.raises(ValueError):
        IntegratedResearchReportGenerator(
            target_company="宁德时代",
            target_company_code="300750",
            target_company_market="A",
            **options,
        )


def test_section_prompt_uses_target_company_not_old_example_urls():
    from integrated_research_report_generator import IntegratedResearchReportGenerator

    generator = object.__new__(IntegratedResearchReportGenerator)
    generator.target_company = "宁德时代"
    generator.target_company_code = "300750"

    class CaptureLLM:
        def call(self, prompt, **_kwargs):
            assert "宁德时代（300750）" in prompt
            assert "000066" not in prompt
            assert "HK0020" not in prompt
            return "## 投资分析\n正文"

    section = generator.generate_section(
        CaptureLLM(), "投资分析", "", "背景", "本次资料", True
    )
    assert section.startswith("## 投资分析")


def test_main_accepts_only_a_share_code(monkeypatch):
    import sys

    import integrated_research_report_generator as integrated

    observed = {}

    class FakeGenerator:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        def run_full_pipeline(self):
            return "base.md", "report.md"

    monkeypatch.setattr(integrated, "lookup_a_share_company", lambda code: "宁德时代")
    monkeypatch.setattr(integrated, "IntegratedResearchReportGenerator", FakeGenerator)
    monkeypatch.setattr(sys, "argv", ["program", "--code", "SZ300750"])
    integrated.main()

    assert observed["target_company"] == "宁德时代"
    assert observed["target_company_code"] == "300750"
    assert observed["target_company_market"] == "A"
    assert observed["enable_sell_side_review"] is True


def test_main_resolves_hk_code_without_reusing_default_company(monkeypatch):
    import sys

    import integrated_research_report_generator as integrated

    observed = {}

    class FakeGenerator:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        def run_full_pipeline(self):
            return "base.md", "report.md"

    monkeypatch.setattr(
        integrated, "lookup_hk_company", lambda code: "腾讯控股有限公司"
    )
    monkeypatch.setattr(integrated, "IntegratedResearchReportGenerator", FakeGenerator)
    monkeypatch.setattr(sys, "argv", ["program", "--code", "00700.HK"])
    integrated.main()

    assert observed["target_company"] == "腾讯控股有限公司"
    assert observed["target_company_code"] == "00700"
    assert observed["target_company_market"] == "HK"


def test_main_keeps_original_default_company_without_lookup(monkeypatch):
    import sys

    import integrated_research_report_generator as integrated

    observed = {}

    class FakeGenerator:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        def run_full_pipeline(self):
            return "base.md", "report.md"

    monkeypatch.setattr(
        integrated,
        "lookup_hk_company",
        lambda _code: pytest.fail("default company must remain available offline"),
    )
    monkeypatch.setattr(integrated, "IntegratedResearchReportGenerator", FakeGenerator)
    monkeypatch.setattr(sys, "argv", ["program"])
    integrated.main()

    assert observed["target_company"] == "商汤科技"
    assert observed["target_company_code"] == "00020"


def test_full_cli_explains_invalid_code_without_traceback(monkeypatch, capsys):
    import sys

    import integrated_research_report_generator as integrated

    monkeypatch.setattr(sys, "argv", ["program", "--code", "invalid"])
    with pytest.raises(SystemExit) as error:
        integrated.main()
    assert error.value.code == 2
    assert "港股股票代码" in capsys.readouterr().err


def test_full_pipeline_keeps_original_stages_and_appends_review(monkeypatch, tmp_path):
    import pandas as pd

    import integrated_research_report_generator as integrated

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(integrated, "identify_competitors_with_ai", lambda **_kw: [])
    monkeypatch.setattr(
        integrated,
        "get_all_financial_statements",
        lambda **_kw: {
            "balance_sheet": pd.DataFrame({"报告期": ["2025"], "资产": [100]}),
            "income_statement": None,
            "cash_flow_statement": None,
        },
    )
    monkeypatch.setattr(integrated, "get_stock_intro", lambda *_a, **_kw: "公司介绍")
    monkeypatch.setattr(
        integrated, "get_shareholder_info", lambda **_kw: {"tables": []}
    )
    monkeypatch.setattr(integrated.time, "sleep", lambda _seconds: None)
    seen = {}

    def fake_review(**kwargs):
        seen.update(kwargs)
        return "## 卖方研报对照与风险补充\n已核对来源。"

    monkeypatch.setattr(integrated, "build_sell_side_review", fake_review)
    generator = integrated.IntegratedResearchReportGenerator(
        target_company="宁德时代",
        target_company_code="300750",
        target_company_market="A",
    )
    generator.search_engine.search = lambda *_args: []
    generator.analyze_companies_in_directory = lambda *_args: {
        "宁德时代": {"final_report": "财务基础分析。"}
    }
    generator.run_comparison_analysis = lambda *_args: {}
    generator.format_markdown = lambda _path: None
    generator.convert_to_docx = lambda _path: None

    class FakeLLM:
        def call(self, prompt, **_kwargs):
            if "分段大纲" in prompt:
                return "```yaml\n- part_title: 投资分析\n  part_desc: 基本面\n```"
            if "直接输出" in prompt:
                return "## 投资分析\n原始报告观点。"
            return "整理后公司信息。"

    generator.llm = FakeLLM()
    base_path, final_path = generator.run_full_pipeline()

    assert Path(base_path).is_file()
    assert "A_300750" in base_path
    assert "A_300750" in final_path
    assert "财务基础分析" in Path(base_path).read_text(encoding="utf-8")
    assert "已核对来源" in Path(final_path).read_text(encoding="utf-8")
    assert "原始报告观点" in seen["original_report"]
