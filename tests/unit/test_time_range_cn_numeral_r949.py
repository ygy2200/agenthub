# -*- coding: utf-8 -*-
"""R949：中文数字时间词被静默吞掉——先写会红的用例。

R948 只读探针实锤：`brain._parse_time_range("最近三天的门禁结果")` 返回
**30 天窗口**（2026-10-08~11-07），而正确答案是 3 天。根因在 brain.py:7029
的正则 `(\\d{1,3})?` 只认阿拉伯数字；「三」匹配不到 group(2)，单位 group(3)
又是可选的，于是整句回落成「最近 → 默认 30 天」。

这比「解析失败」更坏：它不报错，而是给一个看似合理的错区间去约束记录检索，
调用方无从察觉（与记忆 #169「入参问题被伪装成服务端故障」同族）。

修法口径：
  · 带数量的中文/阿拉伯数字**必须紧跟时间单位**才生效（前瞻约束），
    避免「最近一起事故」里的「一」被当成数量；
  · 解析不出来时**不许假装是 30 天**——无单位的「最近」保持原有默认 30 天行为，
    其余畸形输入返回 None（fail-open，调用方不触发时间约束）；
  · 中文数字复用「十 / 十一 / 二十 / 二十三」等常见写法，不引入新依赖。

旧码上以下用例全部必红：中文数字会被吞成 30 天。
"""
import datetime as dt


def _expect(days: int):
    today = dt.date.today()
    return ((today - dt.timedelta(days=days)).isoformat(), today.isoformat())


def test_cn_numeral_days_not_swallowed():
    import brain
    assert brain._parse_time_range("最近三天的门禁结果") == _expect(3)
    assert brain._parse_time_range("近五天") == _expect(5)


def test_cn_numeral_weeks_and_liang():
    import brain
    assert brain._parse_time_range("近两周的改动") == _expect(14)
    assert brain._parse_time_range("最近两天") == _expect(2)


def test_cn_compound_tens():
    import brain
    assert brain._parse_time_range("最近三十天") == _expect(30)
    assert brain._parse_time_range("最近十二周") == _expect(84)
    assert brain._parse_time_range("近十天") == _expect(10)


def test_arabic_and_bare_recent_keep_old_behavior():
    """回归守卫：阿拉伯数字与无数字的「最近」不许被改坏。"""
    import brain
    assert brain._parse_time_range("最近3天") == _expect(3)
    assert brain._parse_time_range("最近") == _expect(30)


def test_cn_digit_without_unit_is_not_a_quantity():
    """「最近一起事故」里的「一」后面不是时间单位，不许当成 1 天。"""
    import brain
    assert brain._parse_time_range("最近一起事故") == _expect(30)


def test_month_parsing_still_works():
    """中文月份那条老路径不许被这次改动带偏。"""
    import brain
    today = dt.date.today()
    got = brain._parse_time_range("十月初的部署记录")
    assert got and got[0].endswith("-10-01") and got[1].endswith("-10-10"), got
    assert brain._parse_time_range("FlClash 配置") is None
