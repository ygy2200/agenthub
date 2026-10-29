# -*- coding: utf-8 -*-
"""溯源门要说清「脏在哪」（R729）。

病史：v2.41.157 构建后发布检查报「exe 由未提交的工作树构建（清单 dirty=true）——
提交后重建再发布」，但 `git status --short` 只有一个并行 agent 落在本仓库里的私有目录
`.workbuddy/`（未跟踪、不是源码）。门只给了结论没给证据，操作者得自己再去问 git 才知道
该提交什么、该 ignore 什么——一个把活拦住的检查却不说明拦了什么，等于把排查推回给人。
"""
import hashlib
import json
import types


def ops():
    import ops as mod
    return mod


# ---------- _parse_porcelain：git 的行格式 → 人读的路径 ----------

def test_parse_porcelain_strips_status_codes():
    got = ops()._parse_porcelain(" M ops.py\n?? .workbuddy/\nA  new.py\n")
    assert got == ["ops.py", ".workbuddy/", "new.py"], got


def test_parse_porcelain_takes_new_name_of_rename():
    assert ops()._parse_porcelain("R  old.py -> new.py\n") == ["new.py"]


def test_parse_porcelain_unquotes_paths():
    assert ops()._parse_porcelain('?? "带空格 目录/a.py"\n') == ["带空格 目录/a.py"]


def test_parse_porcelain_dedups_and_limits():
    out = "".join(f" M f{i}.py\n" for i in range(20)) + " M f0.py\n"
    got = ops()._parse_porcelain(out)
    assert len(got) == ops().DIRTY_PATHS_MAX
    assert got[0] == "f0.py"
    assert len(set(got)) == len(got), "去重失效：同一文件会重复出现在提示里"


def test_parse_porcelain_ignores_blank_and_short_lines():
    assert ops()._parse_porcelain("\n   \n M x.py\n") == ["x.py"]


def test_git_dirty_paths_without_git_is_silent(monkeypatch):
    """git 不可用不是错误，只是拿不到证据——返回空列表，文案退回原样而不是崩。"""
    def boom(cmd, **kw):
        raise FileNotFoundError("git")
    monkeypatch.setattr(ops().subprocess, "run", boom)
    assert ops()._git_dirty_paths(".") == []


def test_git_dirty_paths_only_asks_status(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout=" M a.py\n", stderr="")
    monkeypatch.setattr(ops().subprocess, "run", run)
    assert ops()._git_dirty_paths(".") == ["a.py"]
    assert all("status" in c for c in calls), "只该问 status，不该重复问 rev-parse"


def test_git_state_contract_unchanged(monkeypatch):
    """_git_state 仍是二元组——它被十几处调用/测试打桩，扩返回值会全线炸。"""
    def run(cmd, **kw):
        if "rev-parse" in cmd:
            return types.SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="?? x/\n", stderr="")
    monkeypatch.setattr(ops().subprocess, "run", run)
    assert ops()._git_state(".") == ("abc1234", True)



# ---------- 构建清单：把那一刻的脏路径一起落盘 ----------

def _stub_build_start(monkeypatch, tmp_path, dirty, paths):
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", dirty))
    monkeypatch.setattr(ops(), "_git_dirty_paths", lambda root: list(paths))
    monkeypatch.setattr(ops(), "_release_sources", lambda root: {"ui.py": "x"})
    monkeypatch.setattr(ops(), "_app_version", lambda root: "2.41.100")
    ops().build_manifest(tmp_path, "start")
    return json.loads((tmp_path / "dist" / "AgentHub.build.json").read_text(encoding="utf-8"))


def test_manifest_records_dirty_paths(monkeypatch, tmp_path):
    data = _stub_build_start(monkeypatch, tmp_path, True, [".workbuddy/", "ops.py"])
    assert data["dirty"] is True
    assert data["dirty_paths"] == [".workbuddy/", "ops.py"]


def test_manifest_clean_build_skips_extra_git_call(monkeypatch, tmp_path):
    """干净构建是常态：不为它多跑一条 git status。"""
    called = []
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", False))
    monkeypatch.setattr(ops(), "_git_dirty_paths",
                        lambda root: called.append(1) or [])
    monkeypatch.setattr(ops(), "_release_sources", lambda root: {})
    monkeypatch.setattr(ops(), "_app_version", lambda root: "")
    ops().build_manifest(tmp_path, "start")
    data = json.loads((tmp_path / "dist" / "AgentHub.build.json").read_text(encoding="utf-8"))
    assert data["dirty_paths"] == []
    assert called == [], "工作树干净时不该再问 git 要脏路径"


def test_manifest_unknown_dirty_keeps_empty_paths(monkeypatch, tmp_path):
    """没有 git（dirty=None）：路径样本留空，别让「未知」看起来像「没查到」。"""
    data = _stub_build_start(monkeypatch, tmp_path, None, [])
    assert data["dirty"] is None and data["dirty_paths"] == []


# ---------- 判定文案：结论带证据 ----------

def _make_target(tmp_path, *, dirty=True, dirty_paths=None):
    target = tmp_path / "src"
    (target / "dist").mkdir(parents=True)
    ui = target / "ui.py"
    ui.write_text("# ui", encoding="utf-8")
    exe = target / "dist" / "AgentHub.exe"
    exe.write_bytes(b"fake exe")
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    (home / "Desktop" / "AgentHub.exe").write_bytes(b"fake exe")
    manifest = {
        "schema": 1, "version": "2.41.100", "started": "s", "finished": "f",
        "python": "3.11", "source": {"ui.py": hashlib.md5(b"# ui").hexdigest()},
        "exe": {"sha256": hashlib.sha256(b"fake exe").hexdigest(), "size": 9, "mtime": 0},
        "git_head": "abc1234", "dirty": dirty,
    }
    if dirty_paths is not None:
        manifest["dirty_paths"] = dirty_paths
    (target / "dist" / "AgentHub.build.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return target, home, exe


def test_release_names_the_dirty_paths(monkeypatch, tmp_path):
    target, home, _ = _make_target(tmp_path, dirty=True, dirty_paths=[".workbuddy/"])
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", True))
    r = ops()._release(target, home=home)
    assert r["ok"] is False
    assert ".workbuddy/" in r["summary"], r["summary"]
    assert any("工作树脏，未提交：.workbuddy/" in d for d in r["detail"]), r["detail"]


def test_release_falls_back_to_live_probe_for_old_manifest(monkeypatch, tmp_path):
    """旧清单没有 dirty_paths：退回现问一次 git，至少别继续只报结论。"""
    target, home, _ = _make_target(tmp_path, dirty=True, dirty_paths=None)
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", True))
    monkeypatch.setattr(ops(), "_git_dirty_paths", lambda root: ["ops.py"])
    r = ops()._release(target, home=home)
    assert "ops.py" in r["summary"], r["summary"]


def test_release_prefers_build_time_sample_over_live_tree(monkeypatch, tmp_path):
    """污染这次构建的是清单里那批；现在工作树已经变了也不能拿新名单糊弄。"""
    target, home, _ = _make_target(tmp_path, dirty=True, dirty_paths=["brain.py"])
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", True))
    monkeypatch.setattr(ops(), "_git_dirty_paths", lambda root: ["别的.py"])
    r = ops()._release(target, home=home)
    assert "brain.py" in r["summary"] and "别的.py" not in r["summary"], r["summary"]


def test_release_clean_summary_has_no_hint(monkeypatch, tmp_path):
    target, home, _ = _make_target(tmp_path, dirty=False, dirty_paths=[])
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", False))
    r = ops()._release(target, home=home)
    assert r["ok"] is True, r
    assert "未提交" not in " ".join(r["detail"]), r["detail"]


def test_provenance_problem_names_paths_and_offers_ignore(monkeypatch, tmp_path):
    target, _, exe = _make_target(tmp_path, dirty=True, dirty_paths=[".workbuddy/"])
    monkeypatch.setattr(ops(), "_release", lambda t: {"ok": True, "summary": "桩", "detail": []})
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", True))
    r = ops().publish_provenance(target, "2.41.100", exe=exe)
    assert r["ok"] is False
    hit = [p for p in r["problems"] if "dirty=true" in p]
    assert hit and ".workbuddy/" in hit[0], r["problems"]
    assert ".gitignore" in hit[0], "非源码脏最常见出路是 ignore，得把这条指出来"
