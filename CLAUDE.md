# 작업 규칙

전체 기획은 `PLAN.md`. 작업 전에 읽고 9장 단계 순서대로 진행한다. 단계마다 멈추고 보고한다. `[결정 필요]` 항목은 질문한다.

## 반드시 지킬 것

- 비밀값(`NOTION_TOKEN`, `NOTION_DATABASE_ID`)은 환경변수/`.env`로만 받는다. 코드·설정·테스트·로그·커밋에 넣지 않는다.
- 개인정보 금지: 작성자 정보(SW `nick_name`·`user_id`, 게시판 `bdlUser`)는 읽지도 저장하지도 않는다. fixture에서도 제거한다.
- `verify=False` 금지. SSL 오류는 우회하지 말고 보고한다.
- 크롤링 값은 신뢰하지 않는 입력. 링크는 `https://` + `ulsan.ac.kr` 하위 도메인만 허용한다.
- 노션 쓰기는 행 생성·5-3 갱신·보관 처리만. 6단계 전에는 실제 DB에 쓰지 않는다.
- `git push`, 실제 노션 쓰기, GitHub Secrets 등록은 사용자 승인 후에만.
- 새 라이브러리는 이유를 설명하고 승인받은 뒤 추가한다. 버전은 `==`로 고정한다.

## 명령

- 테스트: `.venv/bin/pytest`
- 실행: `.venv/bin/python -m src.main --dry-run`
