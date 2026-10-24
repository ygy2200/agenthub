# -*- coding: utf-8 -*-
"""unit 层：agent 注册表顺序确定性（R104，2026-10-24）。

守护：AGENT_LEGACY 是 set，core.agent_known_names 曾按 set 遍历序组装 dict——
顺序随进程哈希种子变（实测四进程四序）。凡以注册表为数据源的下拉（playbook/signal
的 agent 选择）默认选中项跨进程随机跳，GUI 结构快照基线跟着随机红。修复后必须
保持 sorted 输出——这里守住「不回归为无序」。
"""


def test_agent_known_names_is_sorted():
    import core
    for root in ("", None):
        names = list(core.agent_known_names(root).keys())
        assert names == sorted(names), names


def test_agent_known_names_sorted_with_root():
    import json
    import tempfile
    from pathlib import Path

    import core
    tmp = Path(tempfile.mkdtemp(prefix="unit_reg_"))
    (tmp / "config.agents.json").write_text(json.dumps({"agents": [
        {"agent": "zeta-agent", "channel": "instr_file",
         "instr_path": str(tmp / "z.md")},
        {"agent": "alpha-agent", "channel": "instr_file",
         "instr_path": str(tmp / "a.md")},
    ]}, ensure_ascii=False), encoding="utf-8")
    names = list(core.agent_known_names(str(tmp)).keys())
    assert names == sorted(names), names
    # 同名归并仍生效：配置名与内置别名共存，规范 id 唯一
    assert len(names) == len(set(names)), names
