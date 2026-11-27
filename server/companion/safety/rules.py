"""사기 감지 1단계: 규칙.

1. **문지기 (is_candidate)**: 돈·개인정보·기관 사칭 낱말이 하나도 없으면 LLM을 부르지 않는다.
   사기는 결국 돈을 보내게 하거나, 정보를 알아내거나, 앱을 깔게 하므로 이 낱말 없이 성립하기 어렵다.
   놓치면 되돌릴 수 없어서 넓게 잡되, 흔한 낱말 안에 들어 있는 짧은 말("기억"의 "억", "건강검사"의 "검사",
   "사진 보내"의 "보내")은 빼거나 좁혔다. 테스트 문장 세트(samples/fraud_cases.json)의 사기 문장은
   모두 통과해야 한다 (tests/test_safety.py)
2. **안전망 (strong_signal)**: LLM 호출이 실패했을 때만 쓴다. **돈·정보를 내놓으라는 요구**와 **압박·사칭**이
   함께 있을 때만 알린다. LLM 없이 보호자에게 긴급 푸시가 가므로 문지기보다 훨씬 좁게 잡는다
   ("딸이 택배 보냈대", "병원 검사 결과 보내 준대"는 걸리지 않음)
"""

from __future__ import annotations

import re

from .models import ABOUT_TO_ACT, ALREADY_ACTED, RECEIVED

# --- 낱말 ---

# 돈 (명사). "억"은 "기억·추억" 안에 있어서 숫자와 붙은 것만 본다 (_AMOUNT)
MONEY_NOUNS = (
    "돈", "송금", "이체", "입금", "계좌", "통장", "만원", "만 원", "천만", "백만", "현금", "대출", "투자", "수익",
    "코인", "비트코인", "주식", "리딩", "원금", "배당", "적금", "예금", "상품권", "기프트카드", "수수료", "보증금",
    "환급", "당첨", "상금", "결제", "인출", "합의금", "병원비", "치료비", "급전", "몸값", "보석금", "벌금", "달러",
    "빌려",
)
_AMOUNT = re.compile(r"\d+ ?(만|억|천)|[일이삼사오육칠팔구십백천몇] ?억|억 ?원")
CREDENTIAL_WORDS = (
    "비밀번호", "비번", "인증번호", "인증 번호", "OTP", "보안카드", "주민번호", "주민등록번호", "신분증",
    "앱을 깔", "앱 깔", "앱을 설치", "앱 설치", "어플", "링크", "원격", "카드 번호", "카드번호", "카드 사진",
    "공인인증", "본인 확인", "본인확인", "개인정보",
)
# 사칭하는 사람. "검사"(건강검사), "택배", "경찰서" 같은 흔한 말은 넣지 않는다
IMPERSONATION_WORDS = (
    "검찰", "검사라", "검사가 전화", "검사님", "수사관", "금감원", "금융감독원", "국세청", "경찰이라", "경찰관이",
    "경찰청", "사이버수사", "은행 직원이", "은행이라고", "은행이라며", "은행에서 전화", "캐피탈",
)
# 앞에서 돈 이야기가 나온 뒤 행동만 말할 때 ("그래서 지금 은행 가는 중이야", "그래서 아까 보냈지")
ACTION_WORDS = ("은행 가", "은행에 가", "가는 중", "가고 있", "보낼게", "보냈", "알려 줬", "알려줬", "깔았",
                "설치했", "눌렀", "찾아왔", "뽑았", "부쳤", "넣었")

PRESSURE_WORDS = (
    "급하", "급히", "빨리", "당장", "오늘까지", "지금 바로", "아무한테도 말하지", "아무에게도 말하지", "비밀로",
    "고장", "액정", "번호가 바뀌", "새 번호", "번호 바꿨", "두 배", "세 배", "원금 보장", "무조건", "확정 수익",
    "수익 보장", "안 그러면", "연루", "범죄", "구속", "체포", "동결", "대포통장",
)

# 돈을 내놓으라는 요구: 돈 명사 + 보내는 동사, 또는 그 자체로 송금인 말
_SEND_VERB = re.compile(r"보내|보낼|보냈|부쳐|부칠|부쳤|넣어|넣으|넣었|옮기|찾아서|찾아 ?놓|내라|내래|입금")
_TRANSFER_ITSELF = re.compile(r"송금|이체|계좌로|인출|현금으로 ?(찾|바꿔|인출|두)|상품권")
_CREDENTIAL = re.compile(r"비밀번호|비번|인증 ?번호|OTP|보안카드|주민(등록)?번호|카드 ?번호|앱을? ?(깔|설치)|원격|신분증")
_ACTED = re.compile(r"보냈|부쳤|이체했|입금했|넣었|알려 ?줬|불러 ?줬|깔았|설치했|눌렀|찾아 ?왔|해 ?줬")
_ABOUT = re.compile(r"보낼게|보내려|보낼 거|부칠게|이체하려|넣어 ?보려|넣으려|가는 중|가고 있|알려 줄게|알려줄게|깔게|설치할게")


def _any(words, text: str) -> bool:
    return any(w in text for w in words)


def _has_money(text: str) -> bool:
    return _any(MONEY_NOUNS, text) or bool(_AMOUNT.search(text))


def _sensitive(text: str) -> bool:
    return _has_money(text) or _any(CREDENTIAL_WORDS, text) or _any(IMPERSONATION_WORDS, text)


def is_candidate(text: str, recent: tuple[str, ...] | list[str] = ()) -> bool:
    """LLM에게 물어볼 만한 발화인지 (문지기)."""
    if _sensitive(text):
        return True
    # 바로 앞 두 마디에 돈·정보 이야기가 있었고 지금 행동을 말함
    return _sensitive(" ".join(recent[-2:])) and _any(ACTION_WORDS, text)


def _asks_money_or_info(text: str) -> bool:
    return bool((_has_money(text) and _SEND_VERB.search(text)) or _TRANSFER_ITSELF.search(text)
                or _CREDENTIAL.search(text))


def strong_signal(text: str, recent: tuple[str, ...] | list[str] = ()) -> tuple[str, str] | None:
    """LLM 없이도 알릴 만큼 분명한 사기 신호면 (유형, 단계), 아니면 None. LLM이 실패했을 때만 쓴다."""
    whole = " ".join((*recent[-2:], text))
    if not (_asks_money_or_info(whole) and (_any(PRESSURE_WORDS, whole) or _any(IMPERSONATION_WORDS, whole))):
        return None
    if _any(IMPERSONATION_WORDS, whole):
        category = "agency_impersonation"
    elif _CREDENTIAL.search(whole):
        category = "remote_control"
    elif re.search(r"두 배|세 배|원금 보장|확정 수익|수익 보장|코인|리딩", whole):
        category = "investment"
    elif re.search(r"고장|액정|번호가 바뀌|새 번호|번호 바꿨|아들|딸|손주|사위|며느리|조카", whole):
        category = "family_impersonation"
    else:
        category = "other"
    if _ACTED.search(text):
        stage = ALREADY_ACTED
    elif _ABOUT.search(text):
        stage = ABOUT_TO_ACT
    else:
        stage = RECEIVED
    return category, stage
