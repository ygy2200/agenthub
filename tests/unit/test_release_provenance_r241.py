# -*- coding: utf-8 -*-
"""构建溯源：清单记下「打包时工作树是否脏」，发布前硬闸（R241）。

病史：v2.41.78–v2.41.98 连续 20 个版本都是「先构建、后提交」，清单里的
`git_head` 指向 7a97381——那个提交并不包含 exe 里的源码；而事后拿清单去比
**当前 HEAD** 只会一路绿（HEAD 也是 7a97381，直到最后一次性提交）。可见
「脏不脏」只有在打包那一刻知道，所以必须由构建清单当场记下，发布时再核。
"""
import hashlib
import json
import subprocess
import types


def _md5(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


def _make_target(tmp_path, *, dirty=False, version="2.41.100",
                 git_head="abc1234", with_manifest=True, exe_bytes=b"fake exe"):
    """造一个「可以发布」的源码目录 + 构建清单 + 桌面副本。"""
    target = tmp_path / "src"
    (target / "dist").mkdir(parents=True)
    ui = target / "ui.py"
    ui.write_text("# ui", encoding="utf-8")
    exe = target / "dist" / "AgentHub.exe"
    exe.write_bytes(exe_bytes)
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    (home / "Desktop" / "AgentHub.exe").write_bytes(exe_bytes)
    if with_manifest:
        manifest = {
            "schema": 1, "version": version, "started": "2026-10-28T12:00:00",
            "finished": "2026-10-28T12:02:00", "python": "3.11",
            "source": {"ui.py": _md5(ui)},
            "exe": {"sha256": hashlib.sha256(exe_bytes).hexdigest(),
                    "size": len(exe_bytes), "mtime": 0},
            "git_head": git_head,
        }
        if dirty is not None:
            manifest["dirty"] = dirty
        (target / "dist" / "AgentHub.build.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return target, home, exe


def _stub_release(monkeypatch, ok=True):
    monkeypatch.setattr(ops(), "_release", lambda target: {
        "ok": ok, "summary": "发布一致（桩）", "detail": []})


def ops():
    import ops as mod
    return mod


def _fake_run(head_out="abc1234\n", head_rc=0, status_out="", status_rc=0, calls=None):
    def run(cmd, **kw):
        calls.append(list(cmd))
        assert cmd[0] == "git", cmd
        if "rev-parse" in cmd:
            return types.SimpleNamespace(returncode=head_rc, stdout=head_out, stderr="")
        if "status" in cmd:
            return types.SimpleNamespace(returncode=status_rc, stdout=status_out, stderr="")
        raise AssertionError(f"意外的 git 子命令：{cmd}")
    return run


# ---------- _git_state：git 状态是证据，不是猜测 ----------

def test_git_state_clean_tree(monkeypatch):
    calls = []
    monkeypatch.setattr(ops().subprocess, "run", _fake_run(calls=calls))
    assert ops()._git_state(".") == ("abc1234", False)
    assert len(calls) == 2


def test_git_state_dirty_tree(monkeypatch):
    monkeypatch.setattr(ops().subprocess, "run",
                        _fake_run(status_out=" M ops.py\n?? new.py\n", calls=[]))
    assert ops()._git_state(".") == ("abc1234", True)


def test_git_state_not_a_repo_returns_unknown(monkeypatch):
    """rev-parse 失败＝不在仓库里：脏不脏无从判断，( "", None)，且不再问 status。"""
    calls = []
    monkeypatch.setattr(ops().subprocess, "run", _fake_run(head_rc=128, calls=calls))
    assert ops()._git_state(".") == ("", None)
    assert len(calls) == 1


def test_git_state_status_failure_keeps_head(monkeypatch):
    monkeypatch.setattr(ops().subprocess, "run", _fake_run(status_rc=1, calls=[]))
    assert ops()._git_state(".") == ("abc1234", None)


def test_git_state_git_missing(monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("git")
    monkeypatch.setattr(ops().subprocess, "run", boom)
    assert ops()._git_state(".") == ("", None)


def test_git_state_timeout_is_unknown(monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)
    monkeypatch.setattr(ops().subprocess, "run", boom)
    assert ops()._git_state(".") == ("", None)


# ---------- 构建清单：打包那一刻把 dirty 落盘 ----------

def test_build_manifest_start_records_dirty(monkeypatch, tmp_path):
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", True))
    monkeypatch.setattr(ops(), "_release_sources", lambda root: {"ui.py": "x"})
    monkeypatch.setattr(ops(), "_app_version", lambda root: "2.41.100")
    r = ops().build_manifest(tmp_path, "start")
    assert r["ok"] is True
    data = json.loads((tmp_path / "dist" / "AgentHub.build.json").read_text(encoding="utf-8"))
    assert data["dirty"] is True
    assert data["git_head"] == "abc1234"


def test_build_manifest_start_records_clean_and_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(ops(), "_release_sources", lambda root: {})
    monkeypatch.setattr(ops(), "_app_version", lambda root: "")
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", False))
    ops().build_manifest(tmp_path, "start")
    path = tmp_path / "dist" / "AgentHub.build.json"
    assert json.loads(path.read_text(encoding="utf-8"))["dirty"] is False
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("", None))
    ops().build_manifest(tmp_path, "start")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["dirty"] is None, "没有 git 时也要写明「未知」，不能悄悄写 False"


# ---------- _release：脏构建不许算发布一致 ----------

def test_release_refuses_dirty_build(monkeypatch, tmp_path):
    target, home, _ = _make_target(tmp_path, dirty=True)
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", True))
    r = ops()._release(target, home=home)
    assert r["ok"] is False
    assert "未提交" in r["summary"], r["summary"]
    assert any("工作树脏" in d for d in r["detail"]), r["detail"]


def test_release_passes_clean_build(monkeypatch, tmp_path):
    target, home, _ = _make_target(tmp_path, dirty=False)
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", False))
    r = ops()._release(target, home=home)
    assert r["ok"] is True, r
    assert any("工作树干净" in d for d in r["detail"]), r["detail"]


def test_release_old_manifest_without_dirty_still_passes_but_says_unknown(monkeypatch, tmp_path):
    """旧清单没有 dirty 字段：本地一致性照旧放行（别把历史产物一刀切废），
    但明细必须明说「构建时状态未记录」——对外发布由 publish_provenance 拦。"""
    target, home, _ = _make_target(tmp_path, dirty=None)
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", False))
    r = ops()._release(target, home=home)
    assert r["ok"] is True, r
    assert any("未记录" in d for d in r["detail"]), r["detail"]


# ---------- publish_provenance：对外发布四件硬条件 ----------

def test_provenance_no_manifest(monkeypatch, tmp_path):
    target, _, exe = _make_target(tmp_path, with_manifest=False)
    _stub_release(monkeypatch)
    r = ops().publish_provenance(target, "2.41.100", exe=exe)
    assert r["ok"] is False
    assert any("没有可读的构建清单" in p for p in r["problems"]), r["problems"]


def test_provenance_version_mismatch(monkeypatch, tmp_path):
    target, _, exe = _make_target(tmp_path, version="2.41.99")
    _stub_release(monkeypatch)
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", False))
    r = ops().publish_provenance(target, "2.41.100", exe=exe)
    assert r["ok"] is False
    assert any("≠ 要发布的 v2.41.100" in p for p in r["problems"]), r["problems"]


def test_provenance_refuses_dirty(monkeypatch, tmp_path):
    target, _, exe = _make_target(tmp_path, dirty=True)
    _stub_release(monkeypatch)
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", True))
    r = ops().publish_provenance(target, "2.41.100", exe=exe)
    assert r["ok"] is False
    assert any("dirty=true" in p for p in r["problems"]), r["problems"]


def test_provenance_refuses_old_manifest_without_dirty(monkeypatch, tmp_path):
    target, _, exe = _make_target(tmp_path, dirty=None)
    _stub_release(monkeypatch)
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", False))
    r = ops().publish_provenance(target, "2.41.100", exe=exe)
    assert r["ok"] is False
    assert any("没记构建时是否脏" in p for p in r["problems"]), r["problems"]


def test_provenance_refuses_head_mismatch(monkeypatch, tmp_path):
    """先构建后提交＝清单指向不含自己源码的提交——必须挡在发布前。"""
    target, _, exe = _make_target(tmp_path, git_head="7a97381")
    _stub_release(monkeypatch)
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("9ded518", False))
    r = ops().publish_provenance(target, "2.41.100", exe=exe)
    assert r["ok"] is False
    assert any("先提交、再构建" in p for p in r["problems"]), r["problems"]


def test_provenance_refuses_when_head_unreadable(monkeypatch, tmp_path):
    target, _, exe = _make_target(tmp_path, git_head="abc1234")
    _stub_release(monkeypatch)
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("", None))
    r = ops().publish_provenance(target, "2.41.100", exe=exe)
    assert r["ok"] is False
    assert any("读不到当前 HEAD" in p for p in r["problems"]), r["problems"]


def test_provenance_refuses_missing_git_head(monkeypatch, tmp_path):
    target, _, exe = _make_target(tmp_path, git_head="")
    _stub_release(monkeypatch)
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", False))
    r = ops().publish_provenance(target, "2.41.100", exe=exe)
    assert r["ok"] is False
    assert any("没记构建提交" in p for p in r["problems"]), r["problems"]


def test_provenance_passes_clean_matching_build(monkeypatch, tmp_path):
    target, _, exe = _make_target(tmp_path, dirty=False)
    _stub_release(monkeypatch)
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", False))
    r = ops().publish_provenance(target, "2.41.100", exe=exe)
    assert r["ok"] is True, r["problems"]
    assert any("abc1234（HEAD abc1234）" in line for line in r["lines"]), r["lines"]


def test_provenance_refuses_foreign_exe(monkeypatch, tmp_path):
    """要上传的 exe 必须就是清单产物——否则「可见产物」变成假证据。"""
    target, _, _ = _make_target(tmp_path, dirty=False, exe_bytes=b"build under test")
    _stub_release(monkeypatch)
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", False))
    old = tmp_path / "old.exe"
    old.write_bytes(b"some older build")
    r = ops().publish_provenance(target, "2.41.100", exe=old)
    assert r["ok"] is False
    assert any("别把旧构建挂到新标签下" in p for p in r["problems"]), r["problems"]


def test_provenance_refuses_missing_exe(monkeypatch, tmp_path):
    target, _, _ = _make_target(tmp_path, dirty=False)
    _stub_release(monkeypatch)
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", False))
    r = ops().publish_provenance(target, "2.41.100", exe=tmp_path / "nope.exe")
    assert r["ok"] is False
    assert any("要上传的 exe 不存在" in p for p in r["problems"]), r["problems"]


def test_provenance_reraises_release_failure(monkeypatch, tmp_path):
    target, _, exe = _make_target(tmp_path, dirty=False)
    _stub_release(monkeypatch, ok=False)
    monkeypatch.setattr(ops(), "_git_state", lambda root: ("abc1234", False))
    r = ops().publish_provenance(target, "2.41.100", exe=exe)
    assert r["ok"] is False
    assert any("发布一致性未过" in p for p in r["problems"]), r["problems"]
