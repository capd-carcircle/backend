#!/usr/bin/env bash
# CAPD - 특정 환자의 '오늘'(또는 지정 날짜) 일일 기록 1건 통째 삭제 헬퍼
#
# Cloud Shell 사용법:
#   source delete_today.sh
#   delete_today                 # 박차원 오늘 기록 삭제
#   delete_today 김환희           # 김환희 오늘 기록 삭제
#   delete_today 박차원 2026-06-14 # 박차원 특정 날짜 삭제
#
# 동작: Cloud Run Job(migrate-report-date) 으로 scripts/delete_today_record.py 실행
#   update(CONFIRM=yes) -> execute(--wait) -> 해당 실행 로그 출력 -> CONFIRM env 자동 정리
#
# 사전 조건: delete_today_record.py 가 배포된 capd-backend 이미지에 포함되어 있어야 함
#           (push 후 GitHub Actions 배포 완료 상태)

delete_today() {
  local NAME="${1:-박차원}"
  local REGION="asia-northeast3"
  local JOB="migrate-report-date"
  local PROJECT="skuniv-training-2"

  local DATE_ENV=""
  [ -n "$2" ] && DATE_ENV=",RECORD_DATE=$2"

  local IMAGE
  IMAGE=$(gcloud run services describe capd-backend --region="$REGION" \
    --format='value(spec.template.spec.containers[0].image)')
  if [ -z "$IMAGE" ]; then
    echo "❌ capd-backend 이미지를 찾지 못했습니다." >&2
    return 1
  fi

  echo "▶ 삭제 실행: $NAME / ${2:-오늘}"
  gcloud run jobs update "$JOB" --image="$IMAGE" --region="$REGION" \
    --command=python --args="scripts/delete_today_record.py" \
    --set-secrets="DATABASE_URL=DATABASE_URL:latest" \
    --update-env-vars="PATIENT_NAME=${NAME},CONFIRM=yes${DATE_ENV}" >/dev/null || return 1

  local EXEC
  EXEC=$(gcloud run jobs execute "$JOB" --region="$REGION" --wait \
    --format='value(metadata.name)')

  sleep 5
  echo "── 실행 로그 ($EXEC) ──"
  gcloud logging read \
    "resource.type=cloud_run_job AND labels.\"run.googleapis.com/execution_name\"=\"$EXEC\"" \
    --project="$PROJECT" --limit=30 --format="value(textPayload)" --order=asc

  # 위험한 CONFIRM 환경변수 자동 정리 (다음 마이그레이션 재사용 안전)
  gcloud run jobs update "$JOB" --region="$REGION" \
    --remove-env-vars="CONFIRM,PATIENT_NAME,RECORD_DATE" >/dev/null 2>&1

  echo "✅ 끝 (CONFIRM env 자동 정리됨)"
}

# 스크립트로 직접 실행 시: ./delete_today.sh [이름] [날짜]
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  delete_today "$@"
fi
