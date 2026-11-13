name: add-news

on:
  workflow_dispatch:
    inputs:
      url:
        description: "기사 주소"
        required: true
      region:
        description: "구분 (국내/해외/정부)"
        type: choice
        options: ["국내", "해외", "정부"]
        required: false
        default: "국내"
      category:
        description: "카테고리 (비우면 자동 분류)"
        required: false
        default: ""
      title:
        description: "제목 (비우면 자동 추출)"
        required: false
        default: ""

permissions:
  contents: write

concurrency:
  group: news-data
  cancel-in-progress: false

jobs:
  add:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install requests
      - name: 기사 추가
        env:                      # 입력값은 환경변수로만 전달(명령어에 직접 넣지 않음)
          IN_URL: ${{ inputs.url }}
          IN_REGION: ${{ inputs.region }}
          IN_CATEGORY: ${{ inputs.category }}
          IN_TITLE: ${{ inputs.title }}
        run: python add_news.py --url "$IN_URL" --region "$IN_REGION" --category "$IN_CATEGORY" --title "$IN_TITLE"
      - name: Commit
        run: |
          git config user.name "github-actions"
          git config user.email "actions@users.noreply.github.com"
          git add data/weeks
          git diff --staged --quiet || git commit -m "chore: add manual news"
          git pull --rebase
          git push
