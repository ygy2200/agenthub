# -*- coding: utf-8 -*-
"""R835：开工推送（recall_for）的采纳正信号。

为什么改：R830 的采纳回流排序只落在了提问通道（prompt_recall 的 spec 打分），
开工推送（recall_for，hub_heartbeat 消费）的轮换排序里没有采纳档——已曝光的记忆里，
「被指纹证实有用过的」和「从没被证实的」排同一个队列，纯看曝光计数。
R829 的账本都记了，R830 的免疫只做了负向（推了没用 → 冷却），正向那半边一直缺着。

本文件钉住三件事：
  1. 同为已曝光档，采纳过的排没采纳过的前面（项目层 + 置顶层同一处 SQL 形状）；
  2. 零曝光优先的轮换哲学不变——新记忆仍然排在一切已曝光记忆之前；
  3. 跨项目联想层的并列相似度里，采纳过的先出（sim 相等时的次序键）。

「修之前会怎样失败」：第 1 条用例在 HEAD 版上必红——旧 ORDER BY 里两条曝光数相同
的记忆按 inject_count DESC 并列后直接看 maturity/id，与谁被采纳过无关。
"""
import brain


def _seed(root, content, **kw):
    brain.init_db(root)
    return brain.add_memory(root, content, **kw)


def _inject_once(root, mid):
    """造一次被动曝光（与 R830 的 _push_n 同款，走 UPDATE 而非手改语义）。"""
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE memories SET inject_count=inject_count+1, last_inject=? "
                     "WHERE id=?", (brain._now(), mid))


def _adopt(root, mid, n=2):
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE memories SET adopt_count=? WHERE id=?", (n, mid))


def test_adopted_pushed_before_unproven_same_tier(hub_root):
    """项目层：两条都已曝光 1 次，被采纳的那条排前（旧代码此处必红）。"""
    # 先建 used（低 id）后建 plain（高 id）：旧代码 id DESC 恰好把高 id 排前，
    # 顺序侥幸会盖过排序差异——倒过来让 id 与期望序相反，测试才真正区分新旧。
    used = _seed(hub_root, "FlClash 的 TUN 模式接管全局流量前，先在订阅页关掉系统代理，两条链同时开会回环。",
                 kind="lesson", tags="flclash,tun", project="FlClash代理出口-开关控制")
    plain = _seed(hub_root, "FlClash 开机自启要在系统设置里开「登录时启动」，托盘里那个开关只管本次。",
                  kind="fact", tags="flclash,自启", project="FlClash代理出口-开关控制")
    _inject_once(hub_root, plain)
    _inject_once(hub_root, used)
    _adopt(hub_root, used)
    rows = brain.recall_for(hub_root, "FlClash代理出口-开关控制", agent="workbuddy")
    ids = [r["id"] for r in rows]
    assert used in ids and plain in ids, ids
    assert ids.index(used) < ids.index(plain), ids


def test_adopted_pushed_before_unproven_pinned_layer(hub_root):
    """置顶层：同一处 SQL 形状，置顶的采纳条排置顶的未采纳条前。"""
    used_pin = _seed(hub_root, "MATLAB 中文乱码先查 plt 字体：set(groot,'defaultAxesFontName','SimHei')。",
                     kind="lesson", tags="matlab,中文", pinned=True)
    plain_pin = _seed(hub_root, "MATLAB 导出图片用 exportgraphics(gcf,'out.png','Resolution',150)。",
                      kind="fact", tags="matlab,导出", pinned=True)
    _inject_once(hub_root, plain_pin)
    _inject_once(hub_root, used_pin)
    _adopt(hub_root, used_pin)
    rows = brain.recall_for(hub_root, "完全无关的项目名-占位", agent="workbuddy")
    ids = [r["id"] for r in rows]
    assert used_pin in ids and plain_pin in ids, ids
    assert ids.index(used_pin) < ids.index(plain_pin), ids


def test_zero_exposure_still_first(hub_root):
    """轮换哲学不变：零曝光的新记忆仍排在已曝光且被采纳的记忆之前。"""
    fresh = _seed(hub_root, "CS2 着色器缓存损坏的官方清法是删 game/csgo 下的 shadercache 目录。",
                  kind="lesson", tags="cs2,着色器", project="CS2掉帧修复-着色器清理工具")
    used = _seed(hub_root, "CS2 掉帧先查 NVIDIA Instant Replay：它常驻录屏吃帧。",
                 kind="lesson", tags="cs2,录屏", project="CS2掉帧修复-着色器清理工具")
    _inject_once(hub_root, used)
    _adopt(hub_root, used)
    rows = brain.recall_for(hub_root, "CS2掉帧修复-着色器清理工具", agent="workbuddy")
    ids = [r["id"] for r in rows]
    assert fresh in ids and used in ids, ids
    assert ids.index(fresh) < ids.index(used), ids


def test_related_layer_adopt_tiebreak(hub_root):
    """联想层：词集相同 → 相似度并列 → 采纳过的先出。
    词集用 ASCII 词（顺序不敏感）保证两条 sim 严格相等；中文经 bigram 分词后
    语序会改变词集，拿它造并列是测不出来的。"""
    brain.init_db(hub_root)
    brain.add_record(hub_root, "编码转换-中文乱码", "zcode", "2026-11-02",
                     "sim seeds", "alpha beta gamma delta")
    b = _seed(hub_root, "设置 flclash gamma delta beta alpha",
              kind="lesson", project="编码转换-中文乱码C")
    a = _seed(hub_root, "自启 开机 alpha beta gamma delta",
              kind="lesson", project="编码转换-中文乱码B")
    _adopt(hub_root, a)
    rows = brain.recall_for(hub_root, "编码转换-中文乱码", agent="workbuddy")
    ids = [r["id"] for r in rows]
    assert a in ids and b in ids, ids
    assert ids.index(a) < ids.index(b), ids
