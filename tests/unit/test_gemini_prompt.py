from gemini_review.domain import FileDump, FileEntry, PullRequest, RepoRef
from gemini_review.infrastructure.gemini_prompt import build_prompt


def _pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef("octo", "demo"),
        number=7,
        title="제목",
        body="본문",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://github.com/octo/demo.git",
        changed_files=("src/a.py",),
        installation_id=1,
        is_draft=False,
    )


def test_prompt_contains_three_section_schema_and_korean_rule() -> None:
    dump = FileDump(
        entries=(FileEntry(path="src/a.py", content="x=1\ny=2", size_bytes=7, is_changed=True),),
        total_chars=7,
    )
    prompt = build_prompt(_pr(), dump)

    assert "한국어" in prompt
    assert "positives" in prompt
    assert "improvements" in prompt
    assert "comments" in prompt
    assert "--- FILE: src/a.py [CHANGED] ---" in prompt
    assert "    1| x=1" in prompt
    assert "    2| y=2" in prompt


def test_prompt_requires_line_numbers_for_comments() -> None:
    dump = FileDump(entries=(), total_chars=0)
    prompt = build_prompt(_pr(), dump)
    assert "라인 번호" in prompt or "line" in prompt
    assert "반드시" in prompt


def test_prompt_mentions_idiomatic_api_taste() -> None:
    dump = FileDump(entries=(), total_chars=0)
    prompt = build_prompt(_pr(), dump)
    # 기초 타입 수준 팁은 배제한다는 규칙이 프롬프트에 들어 있어야 한다.
    assert "pathlib" in prompt
    assert "useMemo" in prompt or "useCallback" in prompt
    assert "Protocol" in prompt


def test_prompt_mentions_exclusions_when_budget_truncated() -> None:
    dump = FileDump(
        entries=(),
        total_chars=0,
        excluded=("big/foo.py",),
        exceeded_budget=True,
    )
    prompt = build_prompt(_pr(), dump)
    assert "제외된 파일" in prompt
    assert "big/foo.py" in prompt


def test_prompt_contains_priority_list() -> None:
    """지적이 경합할 때 버그 → 예외 → 데이터 → 동시성 → 성능 → 보안 → 테스트 → 설계 순.

    우선순위가 없으면 모델이 "가독성 향상" 같은 낮은 가치 지적을 상위와 섞어 내놓는
    경향이 있어 리뷰 신호 대 잡음 비가 떨어진다. 이 순서를 프롬프트 레벨에서 고정한다.
    """
    dump = FileDump(entries=(), total_chars=0)
    prompt = build_prompt(_pr(), dump)

    # 우선순위 섹션 헤더 존재 — 여기부터 다음 `## ` 헤더 전까지가 검증 대상 슬라이스.
    # "설계" / "보안" 등 일부 키워드는 이 프롬프트의 다른 섹션(역할 설명, 기술 단위 코멘트
    # 취향 등) 에도 등장하므로, 우선순위 블록만 잘라서 그 안에서의 등장 순서를 검증해야
    # "다른 섹션의 동일 단어" 로 인한 오탐을 피할 수 있다.
    start = prompt.index("## 지적 우선순위")
    end_candidate = prompt.find("## ", start + len("## 지적 우선순위"))
    priority_section = prompt[start:end_candidate] if end_candidate != -1 else prompt[start:]

    # 8개 카테고리 키워드 모두 등장해야 — 하나라도 누락되면 경합 규칙이 무너짐.
    # 더 중요한 건 "선언된 순서대로" 등장해야 한다는 것. 우선순위의 가치는 순서에
    # 있으므로, 누가 실수로 순서를 섞어도 테스트가 잡아내도록 인덱스 단조 증가를 확인.
    priority_keywords = [
        "버그 가능성",
        "예외 처리",
        "데이터 손실",
        "동시성",
        "성능",
        "보안",
        "테스트 누락",
        "설계",
    ]
    indices: list[int] = []
    for keyword in priority_keywords:
        position = priority_section.find(keyword)
        assert position != -1, (
            f"우선순위 섹션 안에 '{keyword}' 누락\n--- section ---\n{priority_section}"
        )
        indices.append(position)
    assert indices == sorted(indices), (
        "우선순위 키워드가 선언된 순서대로 우선순위 섹션에 등장해야 함. "
        f"실제 순서의 인덱스: {indices}"
    )


def test_prompt_bans_noise_patterns() -> None:
    """mlx 류의 "가독성이 향상되었습니다" 반복 노이즈와 내용 없는 칭찬을 프롬프트로 차단한다."""
    dump = FileDump(entries=(), total_chars=0)
    prompt = build_prompt(_pr(), dump)

    # 잡음 금지 섹션 자체가 있어야
    assert "잡음 금지" in prompt

    # 실제로 금지하는 대표적 표현들이 구체적 예시로 들어가 있어야 모델이 학습
    assert "가독성이 향상되었습니다" in prompt or "가독성을 높이세요" in prompt
    assert "깔끔합니다" in prompt
    # 변경 안 된 부분 억지 지적 금지 규칙
    assert "억지" in prompt or "지적 수를 채우려" in prompt


def test_prompt_defines_four_line_comment_severity_tiers() -> None:
    """4개 등급이 모두 명시돼 있고, 각 등급의 대표 판정 기준 중 하나 이상이 포함돼야 한다.

    회귀 방지: 등급 정의가 누락되면 모델이 접두사를 생략하거나 임의 등급(예: `[Info]`,
    `[Warning]`) 을 만들어 UI 필터/그레핑 규칙이 깨진다.
    """
    dump = FileDump(entries=(), total_chars=0)
    prompt = build_prompt(_pr(), dump)

    # 4개 영문 태그 모두 대괄호 형태로 등장해야 한다 (외부 스크립트 그레핑 호환).
    for tier in ("[Critical]", "[Major]", "[Minor]", "[Suggestion]"):
        assert tier in prompt, f"등급 태그 {tier} 누락"

    # 각 등급의 대표 판정 기준도 적어도 하나씩 프롬프트에 들어 있어야 한다.
    # 모델이 등급 선택 시 참조할 기준점.
    assert "데이터 손실" in prompt
    assert "버그 가능성" in prompt or "예외 처리 누락" in prompt
    assert "가독성" in prompt or "중복 코드" in prompt or "네이밍" in prompt
    assert "취향 차이" in prompt or "리팩터링 아이디어" in prompt or "선택 제안" in prompt


def test_prompt_schema_shows_grade_prefix_for_comment_body() -> None:
    """스키마 예시의 `body` 필드에 `[등급]` 접두사가 명시돼야 한다.

    모델은 JSON 스키마 예시를 가장 강하게 따라가므로, 이 자리에 접두사가 없으면 본문
    규칙을 아무리 자세히 써도 생략될 확률이 올라간다. 스키마와 규칙 양쪽에 동시에
    박아 두는 것이 실효.
    """
    dump = FileDump(entries=(), total_chars=0)
    prompt = build_prompt(_pr(), dump)

    assert '"[<등급>] <해당 라인에 달릴 기술 단위 코멘트' in prompt

    # event 결정이 등급과 연동된다는 규칙도 들어 있어야 Critical 이 있을 때 모델이
    # COMMENT 로 내보내지 않게 된다.
    assert "REQUEST_CHANGES" in prompt
    assert "Critical" in prompt  # event 규칙 섹션에서 등장
