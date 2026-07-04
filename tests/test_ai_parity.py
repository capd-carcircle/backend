"""
test_ai_parity.py — ai/tools vs backend/services 정합성 하네스 (DB 불필요)

ai/tools/data_engineering.py + ai/tools/analytics.py(원본)와
backend/app/services/analytics_engine.py(포팅본)가 같은 입력에 같은 출력을 내는지
자동 비교한다. 한쪽만 고쳐서 로직이 어긋나는 걸 즉시 잡기 위한 안전망.

로컬: ai/, backend/가 CAPD/ 아래 형제 폴더이므로 기본 경로(../../ai)로 자동 탐색.
CI: backend 레포만 체크아웃되므로 ai 레포가 없음 -> deploy.yml에서 ai 레포를
    ./ai-repo 로 추가 체크아웃하고 AI_REPO_PATH=./ai-repo 환경변수로 지정한다.
    (ai 레포를 찾지 못하면 이 파일 전체를 skip 처리 — 로컬에서 ai/를 안 받아놓은
    경우에도 다른 테스트가 막히지 않도록.)
"""
import importlib
import json
import os
import sys

import pytest

from synth import gen_synthetic_series

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_AI_PATH = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", "ai"))
AI_REPO_PATH = os.path.abspath(os.environ.get("AI_REPO_PATH", _DEFAULT_AI_PATH))

if not os.path.isdir(os.path.join(AI_REPO_PATH, "tools")):
    pytest.skip(
        f"ai 레포를 찾을 수 없음: {AI_REPO_PATH} "
        "(AI_REPO_PATH 환경변수로 ai 레포 루트 경로를 지정할 것)",
        allow_module_level=True,
    )

if AI_REPO_PATH not in sys.path:
    sys.path.insert(0, AI_REPO_PATH)

ai_data_engineering = importlib.import_module("tools.data_engineering")
ai_analytics = importlib.import_module("tools.analytics")

from app.services import analytics_engine as backend_engine  # noqa: E402  (sys.path 조작 이후 import)


@pytest.fixture(scope="module")
def series():
    """과거->최신 순 (daily_data, exchange_records) 40일치."""
    return gen_synthetic_series(seed=42, n_days=40)


def test_build_daily_model_row_matches(series):
    """Daily Model Row(25개 컬럼) 생성 로직이 완전히 동일한 값을 내는지."""
    for daily_data, exchanges in series:
        ai_row = ai_data_engineering.build_daily_model_row(daily_data, exchanges)
        be_row = backend_engine.build_daily_model_row(daily_data, exchanges)
        assert ai_row == be_row


@pytest.mark.parametrize("window", [7, 30, 90])
def test_run_all_tasks_matches(series, window):
    """Task1~4(run_all_tasks) 출력이 window 7/30/90 전부 완전히 동일한지."""
    rows_oldest_first = [
        backend_engine.build_daily_model_row(daily_data, exchanges)
        for daily_data, exchanges in series
    ]
    rows_newest_first = list(reversed(rows_oldest_first))
    today_row, *historical_rows = rows_newest_first

    ai_result = ai_analytics.run_all_tasks(today_row, historical_rows, window=window)
    be_result = backend_engine.run_all_tasks(today_row, historical_rows, window=window)

    ai_json = json.dumps(ai_result, sort_keys=True, ensure_ascii=False)
    be_json = json.dumps(be_result, sort_keys=True, ensure_ascii=False)
    assert ai_json == be_json
