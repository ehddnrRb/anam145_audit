"""scan.py select_files(): the file set both AttackTrail adapters scan (DECISIONS D-16)."""
import os

import pytest

from adapters import attacktrail_cli
from adapters.attacktrail import load_scan_module
from adapters.attacktrail_cli import AttackTrailCLIAgent
from adapters.base import RunContext, Scope

scan = load_scan_module()


def _touch(p, text="x\n"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _rel(root, files):
    return sorted(os.path.relpath(f, root).replace("\\", "/") for f in files)


def test_root_under_a_lib_dir_still_selects(tmp_path):
    # Before D-16 a "/lib/" anywhere in the absolute path dropped every file.
    root = tmp_path / "lib" / "repo"
    _touch(root / "A.sol")
    _touch(root / "lib" / "forge-std" / "Test.sol")
    files, too_big = scan.select_files(str(root))
    assert _rel(root, files) == ["A.sol"] and too_big == []


def test_non_web_rules_unchanged(tmp_path):
    root = tmp_path / "app"
    _touch(root / "src" / "lib" / "auth.ts")   # non-web still drops every lib dir
    _touch(root / "src" / "Login.jsx")         # not a base extension
    _touch(root / "app.js")
    files, too_big = scan.select_files(str(root))
    assert _rel(root, files) == ["app.js"] and too_big == []


BUILD_OUTPUT_DIRS = ["out", "artifacts", "cache", "build", "dist", "typechain", "typechain-types"]   # D-30
TEST_AND_DEPLOY_DIRS = ["test", "tests", "ignition", "script", "scripts", "deploy", "mocks"]


def test_non_web_skip_list_is_pinned():
    # D-26: changing the list changes which files every non-web run sends. Update the D-26 verification table with it.
    assert scan.SKIP_DIRS == {".git", "node_modules", "lib", *TEST_AND_DEPLOY_DIRS, *BUILD_OUTPUT_DIRS}
    assert not set(TEST_AND_DEPLOY_DIRS) & (scan.SKIP_DIRS_WEB | {".git", "node_modules"})   # --web has its own set


@pytest.mark.parametrize("name", TEST_AND_DEPLOY_DIRS)
def test_non_web_skips_test_and_deploy_dirs_at_any_depth_but_web_keeps_them(tmp_path, name):
    # D-26: matched by directory name against the path relative to the root, like lib. contracts/test/ goes too.
    root = tmp_path / "repo"
    _touch(root / "contracts" / "Vault.sol")
    _touch(root / name / "A.ts")                       # top level
    _touch(root / "contracts" / name / "B.sol")        # under another directory
    _touch(root / "src" / name / "deep" / "C.js")      # with subdirectories below it
    files, too_big = scan.select_files(str(root))
    assert _rel(root, files) == ["contracts/Vault.sol"] and too_big == []
    web_files, _ = scan.select_files(str(root), web=True)   # --web does not use SKIP_DIRS
    assert _rel(root, web_files) == sorted(["contracts/Vault.sol", f"{name}/A.ts", f"contracts/{name}/B.sol",
                                            f"src/{name}/deep/C.js"])


@pytest.mark.parametrize("name", BUILD_OUTPUT_DIRS)
def test_build_output_dirs_are_skipped_at_any_depth_in_both_modes(tmp_path, name):
    # D-30: out/ artifacts/ cache/ build/ dist/ typechain*/ are generated. Web already skipped dist build out; the rest were added too.
    root = tmp_path / "repo"
    _touch(root / "contracts" / "Vault.sol")
    _touch(root / name / "A.ts")
    _touch(root / "contracts" / name / "B.sol")
    for web in (False, True):
        files, _ = scan.select_files(str(root), web=web)
        assert _rel(root, files) == ["contracts/Vault.sol"], (name, web)


def test_directory_named_like_a_source_file_is_not_selected(tmp_path):
    # D-30: Foundry writes out/KHYPE.sol/ (a directory). It used to be picked up and failed with [Errno 13] when read.
    root = tmp_path / "repo"
    _touch(root / "src" / "Real.sol")
    (root / "src" / "Dir.sol").mkdir()                       # empty directory ending in .sol
    _touch(root / "src" / "Nested.sol" / "Inner.sol")        # directory whose child is a real file: the child stays
    for web in (False, True):
        files, _ = scan.select_files(str(root), web=web)
        assert _rel(root, files) == ["src/Nested.sol/Inner.sol", "src/Real.sol"], web


def test_non_web_matches_whole_lowercase_directory_names_relative_to_the_root(tmp_path):
    root = tmp_path / "test" / "repo"                  # the root itself sits under a skipped name: only the relative path counts
    kept = ["src/A.sol", "contracts/test.sol",         # a file called test.sol is not a directory
            "contracts/testing/B.sol", "contracts/scripting/C.sol", "Test/D.sol"]   # not the exact lowercase name
    for rel in kept:
        _touch(root / rel)
    _touch(root / "contracts" / "test" / "E.sol")
    files, _ = scan.select_files(str(root))
    assert _rel(root, files) == sorted(kept)


def test_web_extends_extensions_and_skips_generated(tmp_path):
    root = tmp_path / "app"
    _touch(root / "src" / "lib" / "auth.ts")            # web app code under lib: kept
    _touch(root / "src" / "Login.jsx")
    _touch(root / "src" / "Svc.java")
    _touch(root / "dist" / "app.js")
    _touch(root / "public" / "x.min.js")
    _touch(root / "contracts" / "foundry.toml")
    _touch(root / "contracts" / "lib" / "oz" / "ERC20.sol")  # Foundry dependency
    _touch(root / "contracts" / "src" / "Vault.sol")
    _touch(root / "config" / "app.yml")                 # config: judged by a person
    _touch(root / "src" / "Big.ts", "x" * (scan.MAX_BYTES_WEB + 1))
    files, too_big = scan.select_files(str(root), web=True)
    assert _rel(root, files) == ["contracts/src/Vault.sol", "src/Login.jsx",
                                 "src/Svc.java", "src/lib/auth.ts"]
    assert too_big == ["src/Big.ts"]


def test_cli_agent_zero_files_is_no_files_not_ok_and_not_a_crash(tmp_path, monkeypatch):
    # Zero files used to finish as "ok" with no findings, indistinguishable from a clean scan. D-16 made it crashed;
    # D-27: it is no_files, a tool limit, kept apart from real crashes.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("nothing to scan\n", encoding="utf-8")
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    res = AttackTrailCLIAgent(model="m").run(
        Scope("p", repo), RunContext(run_dir=tmp_path, project_dir=tmp_path / "o",
                                     timeout_s=60, env={}))
    assert res.meta.status == "no_files" and "no files selected" in res.meta.error
    assert (res.meta.files_total, res.meta.files_scanned, res.meta.files_failed, res.meta.files_skipped) == (0, 0, 0, 0)


def test_real_scan_py_with_nothing_to_scan_is_no_files_through_the_real_adapter(tmp_path):
    # D-27 pin. The API adapter reads "nothing selected" from scan.py's exit 1 + its message (scan.py is unchanged, so its
    # sha256 is too). This runs the real scan.py in a subprocess on a repo whose only in-scope files are .move (cabal) or
    # sit under skipped folders, so a reworded message or another exit code breaks here and not silently in a real run.
    # No API call: scan.py selects files before it creates a client, and the key below is never used.
    from adapters.attacktrail import NO_FILES_MARK, SCAN_PY, AttackTrailAgent

    repo = tmp_path / "repo"
    _touch(repo / "sources" / "Cabal.move")
    _touch(repo / "test" / "Vault.t.sol")
    _touch(repo / "scripts" / "deploy.ts")
    assert scan.select_files(str(repo))[0] == []
    res = AttackTrailAgent(scan_py=SCAN_PY).run(
        Scope("p", repo), RunContext(run_dir=tmp_path / "run", project_dir=tmp_path / "proj", timeout_s=120,
                                     env={"ANTHROPIC_API_KEY": "never-sent"}))
    assert res.meta.status == "no_files", res.meta.error
    assert NO_FILES_MARK in res.meta.error                      # the real scan.py's own words
    assert NO_FILES_MARK in (tmp_path / "proj" / "raw" / "scan.stderr.txt").read_text(encoding="utf-8")
    assert not (tmp_path / "proj" / "raw" / "scan.jsonl").exists()   # it never started a scan


def test_web_with_every_file_over_the_size_cap_is_no_files_and_says_so_through_the_real_adapter(tmp_path):
    # D-28: with --web, candidate files over MAX_BYTES_WEB are not sent. When that leaves nothing, scan.py exits 1 with the
    # D-27 message plus "크기 상한 초과로 뺀 파일 N개", and the adapter's no_files reason says "크기 상한 초과" instead of the
    # plain "nothing of a supported type". Real scan.py in a subprocess (files are selected before any client, so no API
    # call; the key is never used). The same repo without the big files gets the old reason, so the two read differently.
    from adapters.attacktrail import SCAN_PY, TOO_BIG_RE, AttackTrailAgent

    big = "x" * (scan.MAX_BYTES_WEB + 1)
    repo = tmp_path / "repo"
    _touch(repo / "src" / "Big.ts", big)
    _touch(repo / "src" / "Huge.tsx", big)
    files, too_big = scan.select_files(str(repo), web=True)
    assert files == [] and sorted(too_big) == ["src/Big.ts", "src/Huge.tsx"]

    def run(sub, root):
        return AttackTrailAgent(scan_py=SCAN_PY, web=True).run(
            Scope("p", root), RunContext(run_dir=tmp_path / sub, project_dir=tmp_path / (sub + "-proj"), timeout_s=120,
                                         env={"ANTHROPIC_API_KEY": "never-sent"}))
    res = run("a", repo)
    assert res.meta.status == "no_files", res.meta.error
    assert "no files selected (크기 상한 초과: --web 후보 파일 2개가 모두 MAX_BYTES_WEB 를 넘어 보내지 않음)" in res.meta.error
    stderr = (tmp_path / "a-proj" / "raw" / "scan.stderr.txt").read_text(encoding="utf-8")
    assert TOO_BIG_RE.search(stderr).group(1) == "2" and "스캔할 파일이 0개입니다" in stderr
    assert (res.meta.files_total, res.meta.files_scanned) == (0, 0)

    plain = tmp_path / "plain"
    _touch(plain / "README.md", "nothing\n")
    res = run("b", plain)
    assert res.meta.status == "no_files" and "크기 상한" not in res.meta.error          # D-27 reason, unchanged
    assert res.meta.error.startswith("no files selected: ") and "확장자·제외 규칙 확인" in res.meta.error


def test_cli_agent_names_the_size_cap_when_web_files_were_all_too_big(tmp_path, monkeypatch):
    # D-28: the CLI adapter takes too_big from select_files directly. A real --web CLI run never gets here (the web standards
    # do not fit the Windows command line, D-15), so the argv-size check is stubbed to reach the file selection.
    repo = tmp_path / "repo"
    _touch(repo / "src" / "Big.ts", "x" * (scan.MAX_BYTES_WEB + 1))
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(attacktrail_cli, "cli_argv", lambda *a: ["claude"])
    res = AttackTrailCLIAgent(model="m", web=True).run(
        Scope("p", repo), RunContext(run_dir=tmp_path, project_dir=tmp_path / "o", timeout_s=60, env={}))
    assert res.meta.status == "no_files"
    assert "(크기 상한 초과: --web 후보 파일 1개가 모두 MAX_BYTES_WEB 를 넘어 보내지 않음)" in res.meta.error
    assert (res.meta.files_total, res.meta.files_scanned) == (0, 0)
