# -*- coding: utf-8 -*-
"""R871：候选池很小时，「稀有词线」不能把唯一命中判成弱相关。

病根：R826 的绝对门槛 raw idf ≥ 2.0 是按 40 条候选标定的（df≈5.5）。候选池只有
1 条时，一个「只在这一条里出现」的词 raw idf 也只有 log(2/1.5)=0.29——门槛在数学
上就够不着，于是整批候选被判弱相关。2026-11-04 实测：问「工作台每组只显示二十条
没有出口」时 rec#3636 明明在记录候选第 1，prompt_recall 的 record_ids 却是空。

边界：放宽只针对小池。池子大时门槛仍≈2.0，泛词命中照样不许冒充依据（R826 底线）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import brain  # noqa: E402


def test_threshold_scales_with_pool():
    assert 1.8 <= brain._recall_rare_threshold(40) < 2.0, "大池应与旧 2.0 同量级"
    assert brain._recall_rare_threshold(1) <= 0.3, "n=1 时必须够得着唯一候选"
    assert brain._recall_rare_threshold(0) <= 0.3, "空池不能除零"
    seq = [brain._recall_rare_threshold(n) for n in (1, 2, 5, 10, 40)]
    assert seq == sorted(seq), seq


def test_narrow_record_pool_still_injects(hub_root, tmp_path):
    """窄池里的唯一记录要能进 record_ids——修前这里必红（best=0.288 < 2.0）。"""
    brain.init_db(hub_root)
    rec = brain.add_record(
        hub_root, "演示-分页", "qoder", "2026-10-30",
        "2026-10-30（qoder）· 工作台每组前 20 条静默截断",
        "工作台每组默认显示 20 条，组头显示总数，新增显示更多，到顶禁用。")
    res = brain.prompt_recall(hub_root, "工作台每组只显示二十条没有出口",
                              agent="qoder", session="narrow",
                              state_path=str(tmp_path / "s.json"),
                              update_state=False, origin="eval")
    assert rec in res["record_ids"], (res["record_ids"], res["terms"], res["text"])


def test_wide_pool_still_requires_rare_word(hub_root, tmp_path):
    """池子大时泛词命中仍旧不注入：12 条各不相同但全含查询词 → df 压零 → 无依据。"""
    brain.init_db(hub_root)
    texts = [
        "工作台显示条数默认二十，改成五十并保留分页出口。",
        "工作台显示条数在深色主题下看不清，字号必须放大。",
        "工作台显示条数统计把已归档项目也算进去了，需要排除。",
        "工作台显示条数上限写死在代码里，配置文件根本改不动。",
        "工作台显示条数变动后导出 CSV 的列顺序错位了。",
        "工作台显示条数减少时底部出现大片空白，布局塌陷。",
        "工作台显示条数手机端只显示第一屏，滚动条消失。",
        "工作台显示条数刷新延迟三秒，用户以为按钮没反应。",
        "工作台显示条数国际化后英文单位缺失，数字挤在一起。",
        "工作台显示条数缓存没失效，切换项目还是旧值。",
        "工作台显示条数权限不同角色看到的不一致，审计要查。",
        "工作台显示条数打印预览被截断，页脚覆盖了最后一行。",
    ]
    for text in texts:
        brain.add_memory(hub_root, text, kind="lesson")
    res = brain.prompt_recall(hub_root, "工作台显示条数怎么调",
                              agent="qoder", session="wide",
                              state_path=str(tmp_path / "w.json"),
                              update_state=False, origin="eval")
    assert res["memory_ids"] == [], (res["memory_ids"], res["terms"], res["text"])
