name: Central Keiba

on:
  schedule:
    - cron: "0 0 * * 6,0"  # JST 9:00 (= UTC 0:00) / 土(6)・日(0)
  workflow_dispatch:

jobs:
  run:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    concurrency:
      group: central-keiba
      cancel-in-progress: true
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install requests beautifulsoup4

      - name: Run script
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          LINE_CHANNEL_ACCESS_TOKEN: ${{ secrets.LINE_CHANNEL_ACCESS_TOKEN }}
          # 可変パラメータ（必要ならSecretsに入れて使ってもOK）
          # O1_MAX: "2.0"
          # GAP_MIN: "0.7"
          # HC_MAX: "12"
        run: python main.py