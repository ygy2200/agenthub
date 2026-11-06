# -*- coding: utf-8 -*-
r"""R925：指纹 token 卫生——带反引号/中文标点的招牌永远匹配不上（系统性假阴性）。

修之前会怎样失败（2026-11-06 真库取证，`tools/fp_hygiene_probe_r924.py`）：
近 800 笔注入里 120/4041 个 token 含反引号或中文标点，受影响 10 条记忆，**这 10 条
一笔都没被判过「用上」**。原因是 Windows 路径那条正则 `[^\s\"'<>|*?*]+` 会一路吞到
下一个空格，于是从正文里抽出来的是

    c:\users\y\.agents\skills\flclash-proxy\skill.md`（作者
    https://www.google.com/generate_204`
    e:\legionzone；确认健康时

`recall_adherence` 的判据是「这些串原样出现在 agent 输出里」（R827 substring 命中）。
反引号和中文标点不会原样出现在命令或回答里，命中恒为 0——采纳率被**结构性压低**，
而 R923 刚把简报必做规则接进这本账，被压低的正是 required 那一档。

口径（这轮只有这么点自由度，越界就是放宽闸门）：
  · 只改「token 怎么切」：在第一个反引号/引号/中文标点/括号处断开，再走原来的尾点剥离；
  · 四道过滤一条不动——长度≥4、停用词、提问去重、池内 df>2；
  · 命中判据（substring）不动，稀有词闸门不动；
  · 净化后不合格的（太短/撞停用词）就丢掉，不硬凑。
"""
import brain

# 取证样本全部来自真库（memory #205/#476/#259/#387）。
SKILL_MD = "c:\\users\\y\\.agents\\skills\\flclash-proxy\\skill.md"
PROBE_URL = "https://www.google.com/generate_204"


def _fps(text, **kw):
    return brain.recall_fingerprints(text, pool_texts=kw.get("pool_texts"),
                                     prompt=kw.get("prompt", ""))


def test_backtick_and_cjk_tail_is_cut_off():
    got = _fps(f"【团队规程】凡动 FlClash 一律走 `{SKILL_MD}`（作者说明里还提到别的）",
               pool_texts=[])
    assert got, "整条抽不出指纹就别谈判定"
    assert SKILL_MD.lower() in got, got
    for t in got:
        assert "`" not in t and "（" not in t and "作者" not in t, got


def test_url_token_keeps_scheme_and_host_but_drops_backtick():
    got = _fps(f"连通性检查用 `{PROBE_URL}` 返回 204 才算通", pool_texts=[])
    assert PROBE_URL.lower() in got, got


def test_cjk_semicolon_ends_the_path_token():
    got = _fps(f"健康检查脚本在 e:\\legionzone；确认健康时才写心跳", pool_texts=[])
    assert "e:\\legionzone" in got, got
    assert all("；" not in t and "确认" not in t for t in got), got


def test_fullwidth_paren_after_path_is_not_part_of_token():
    got = _fps(f"快捷方式生成看 d:\\build\\mk_lnk.py（支持中文路径，ansi 也行）", pool_texts=[])
    assert "d:\\build\\mk_lnk.py" in got, got
    assert all("支持中文" not in t for t in got), got


def test_gates_still_apply_after_cutting():
    """净化不给新信号：切完撞停用词、已在提问里出现、池内通用——照样进不来。"""
    got = brain.recall_fingerprints("编码统一走 utf-8`（别写错）", pool_texts=[], prompt="")
    assert "utf-8" not in got, got                       # 过滤①停用词
    tok = "z:\\onlydir\\marker.py"
    pool = [f"提到了 {tok} 一次", f"还有 {tok} 两次", f"又是 {tok} 三次"]
    assert brain.recall_fingerprints(f"参考 `{tok}`（就这一条）", pool_texts=pool,
                                     prompt="") == [], "池内通用的招牌不该当指纹"   # 过滤③
    assert brain.recall_fingerprints(f"见 `{SKILL_MD}`（规程）", pool_texts=[],
                                     prompt=f"我要用 {SKILL_MD}") == [], "提问里已有的词不该当指纹"  # ②


def test_clean_tokens_are_untouched():
    """普通招牌词不能因为净化变形（这批形状是真库最常见的四类）。

    取证口径：`recall_fingerprints` 硬上限 6 个 token，所以这里只断言「不变形」，
    不断言「都进来」——`v2.41.244` 在这个句子里排第 7，被上限砍掉是既有行为
    （顺带记下一个遗留：版本号正则会吃 IP 字面量的前段，抽出 `127.0.0` 这种废 token，
    白占一个槽位，留待下一轮）。
    """
    got = _fps("打包用 build.bat，脚本 agenthub_mcp.py，代理 http://127.0.0.1:7890，"
               "版本 v2.41.244，开关 RECALL_CORPUS_RESCUE", pool_texts=[])
    for want in ("build.bat", "agenthub_mcp.py", "http://127.0.0.1:7890",
                 "recall_corpus_rescue"):
        assert want.lower() in got, f"{want} 变形了：{got}"
    for t in got:
        assert "`" not in t and "，" not in t, got


def test_hygiene_is_applied_to_recorded_rows(hub_root):
    """端到端：带中文标点的正文也能被判定「真被用上」（这轮的全部收益点）。"""
    content = f"凡动 FlClash 一律走 `{SKILL_MD}`（不准裸命令行）"
    brain.init_db(hub_root)          # conftest 的 hub_root 只给目录，建库要自己调
    mid = brain.add_memory(hub_root, content, kind="lesson", tags="机制说明", agent="qoder")
    rows = brain._record_adherence(
        hub_root, "s-hyg", "qoder",
        [{"id": mid, "kind": "lesson", "content": content, "tags": "机制说明"}],
        project="", strength="strong", prompt="", pool_texts=[], origin="live")
    assert rows == 1
    with brain.db_conn(hub_root) as conn:
        fp = conn.execute("SELECT fp FROM recall_adherence WHERE session='s-hyg'"
                          " ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "`" not in fp and "（" not in fp, fp
    got = brain.mark_recall_used(hub_root, "s-hyg", f"我按规程跑了 {SKILL_MD} 一遍")
    assert got["matched"] == 1, got
