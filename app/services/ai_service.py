"""
AI 질문 생성 서비스
- LM Studio (로컬 LLM) 를 사용해 환자 기록 기반 맞춤 질문 생성
- RAG: pgvector로 관련 KDIGO 문단 검색 후 프롬프트에 주입
- LM Studio가 꺼져 있거나 오류 시 빈 리스트 반환 (규칙 기반 질문으로 대체됨)
"""

import json
import logging
from openai import OpenAI

logger = logging.getLogger(__name__)

# LM Studio 기본 주소 (변경 필요 없음)
LM_STUDIO_BASE_URL = "http://localhost:1234/v1"
LM_STUDIO_API_KEY = "lm-studio"  # LM Studio는 아무 값이나 OK


def generate_questions_via_lm_studio(
    record_data: dict,
    rejected_keys: list = None,
    kdigo_context: str = "",
) -> list:
    """
    환자 투석 기록을 LM Studio에 보내서 맞춤 질문을 생성한다.

    Args:
        record_data:    환자의 오늘 투석 기록 (dict)
        rejected_keys:  제외할 질문 패턴 키 목록
        kdigo_context:  RAG로 검색한 KDIGO 관련 문단 (없으면 빈 문자열)

    Returns:
        [{"question_text": "...", "reason": "..."}] 형태의 리스트
        오류 시 빈 리스트 반환
    """
    try:
        client = OpenAI(
            base_url=LM_STUDIO_BASE_URL,
            api_key=LM_STUDIO_API_KEY,
        )

        rejected_str = ", ".join(rejected_keys) if rejected_keys else "없음"

        # KDIGO 컨텍스트 블록 (있을 때만 추가)
        kdigo_block = ""
        if kdigo_context:
            kdigo_block = f"""
[KDIGO 관련 지침 — 아래 내용을 참고하여 질문을 생성하세요]
{kdigo_context}

"""

        prompt = f"""당신은 CAPD(복막투석) 환자를 담당하는 의료 AI 어시스턴트입니다.
아래 오늘의 투석 기록을 분석하고, 의사가 환자에게 추가로 확인해야 할 증상을 묻는 질문 1개를 생성하세요.
{kdigo_block}
[오늘 투석 기록]
{json.dumps(record_data, ensure_ascii=False, indent=2)}

[이미 제외된 패턴]
{rejected_str}

규칙:
- 기록에서 이상 수치나 주의가 필요한 항목에 집중하세요
- KDIGO 지침이 제공된 경우 해당 근거를 바탕으로 질문을 만드세요
- 환자가 직접 대답할 수 있는 구체적인 질문을 만드세요
- 의학 전문용어보다 쉬운 한국어 표현을 사용하세요

아래 JSON 형식으로만 응답하세요 (다른 설명 없이):
{{"question_text": "질문 내용", "reason": "이 질문을 생성한 이유"}}"""

        response = client.chat.completions.create(
            model="qwen2.5-3b-instruct.gguf",  # LM Studio 실제 모델명 (.gguf 포함)
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=300,
        )

        text = response.choices[0].message.content.strip()

        # JSON 파싱 (```json ... ``` 블록 처리)
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        data = json.loads(text)

        if isinstance(data, dict):
            return [data]
        elif isinstance(data, list):
            return data
        return []

    except ConnectionError:
        logger.warning("LM Studio 서버에 연결할 수 없습니다. 서버가 실행 중인지 확인하세요.")
        return []
    except json.JSONDecodeError as e:
        logger.warning(f"LM Studio 응답 JSON 파싱 실패: {e}")
        return []
    except Exception as e:
        logger.warning(f"LM Studio 질문 생성 실패: {e}")
        return []
