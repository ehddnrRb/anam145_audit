"""datasets/holdout/projects/*.json 을 SCABench 항목 형태로 검증한다.

eval/ 에서: .venv\\Scripts\\python.exe datasets\\holdout\\validate.py
문제가 하나라도 있으면 exit 1.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Codebase(_Strict):
    codebase_id: str = Field(min_length=1)
    repo_url: str = Field(min_length=1)
    commit: str
    tree_url: str
    tarball_url: str

    @field_validator("commit")
    @classmethod
    def _commit(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("commit 이 비어 있다 (pinned_commit_missing). 재현 불가하므로 받지 않는다")
        if not SHA40.match(v):
            raise ValueError("commit 은 40자 소문자 hex 전체 sha 여야 한다")
        return v


class Vulnerability(_Strict):
    finding_id: str = Field(min_length=1)
    severity: Literal["critical", "high", "medium", "low", "informational"]
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)


class Project(_Strict):
    project_id: str
    name: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    codebases: list[Codebase] = Field(min_length=1)
    vulnerabilities: list[Vulnerability] = Field(min_length=1)

    @field_validator("project_id")
    @classmethod
    def _pid(cls, v: str) -> str:
        if not SAFE_ID.match(v) or ".." in v:
            raise ValueError("project_id 는 [A-Za-z0-9._-] 만, '..' 금지")
        return v


def validate_file(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"JSON 읽기 실패: {e}"]
    if not isinstance(data, dict):
        return ["최상위는 객체 하나여야 한다 (SCABench 배열의 항목 1개)"]
    try:
        p = Project.model_validate(data)
    except ValidationError as e:
        return [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
    errs = []
    if p.project_id != path.stem:
        errs.append(f"파일명({path.stem})과 project_id({p.project_id})가 다르다")
    if "REPLACE" in json.dumps(data):
        errs.append("TEMPLATE 의 REPLACE 자리표시자가 남아 있다")
    ids = [v.finding_id for v in p.vulnerabilities]
    if len(ids) != len(set(ids)):
        errs.append("finding_id 가 중복된다")
    return errs


def main(root: Path | None = None) -> int:
    root = root or Path(__file__).resolve().parent / "projects"
    files = sorted(root.glob("*.json"))
    if not files:
        print(f"검증할 파일이 없다: {root}")
        return 0
    bad = 0
    for f in files:
        errs = validate_file(f)
        if errs:
            bad += 1
            print(f"FAIL {f.name}")
            for e in errs:
                print(f"  - {e}")
        else:
            print(f"ok   {f.name}")
    print(f"{len(files)}개 중 실패 {bad}개")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
