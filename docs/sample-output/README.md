# 예시 산출물

이 폴더는 **테스트용 가짜 데이터**로 만든 예시입니다. 실제 뉴스가 아닙니다.

- 기사는 `tests/fixtures/sample_feed.xml` 의 가상 기사(`example.test` 도메인)입니다.
- 요약·대본은 Claude 호출 없이 `tests/test_pipeline.py` 의 샘플 객체로 채웠습니다.

실제로 어떤 파일이 어떤 모양으로 나오는지 미리 보려고 넣어 둔 것이고,
`python -m rebrief run` 을 돌리면 같은 구조에 그날의 진짜 내용이 채워집니다.
