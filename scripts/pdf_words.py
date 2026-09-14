"""pdfplumber가 글자를 못 합치는 문서 결함을 우회해서 단어를 뽑는다.

무엇이 문제인가
----------------
일부 문서(제2부 「가.연평균수익률」/「나.연도별 수익률」 상세표에서 특히
많이 보인다)는 글자 하나하나의 변환행렬에 회전이 아주 조금(각도로
약 0.0000016도) 섞여 있다. 실제로는 눈에 안 보이는 반올림 잡음인데,
pdfplumber는 이걸 "세로쓰기 글자"로 보고 `upright=False`를 매긴다.
`extract_words()`는 upright 글자와 아닌 글자를 다른 경로로 묶는데, 이
문서들은 전부 `upright=False`로 찍혀 있어서 한 줄로 나란히 붙어 있는
글자도 단어로 안 합쳐지고 한 자씩 따로 떨어져 나온다.

KR5111450067 56쪽 실측:
    page.chars 1320개 중 1264개가 이 증상 - "최근 1년"이 "최","근","1","년"
    네 단어로, "-25.77" 같은 숫자도 자릿수마다 따로 나온다. 이러면 우리
    좌표 재구성 로직이 뭘 봐도 숫자·라벨을 못 알아본다. `find_tables()`도
    이 표엔 테두리가 없어서 0개를 돌려주므로 셀 방식으로도 못 건진다.

    이 표만 그런 게 아니라 상품 전체 100개 중 상당수의 같은 자리(상세
    수익률표)에서 똑같이 나타난다 - 아마 이 부분을 만드는 하위 시스템이
    공통이라서인 듯하다. 정작 같은 문서의 보수표(예: 41쪽)는 멀쩡하다
    (같은 문서 KR5111450067 41쪽: 글자 1937개 중 이 증상 0개) - 그래서
    페이지 단위로 판단해야 한다.

무엇을 하나
-----------
글자의 회전각을 직접 계산해서, 사실상 0(0.01라디안 ≈ 0.57도 미만)인데
`upright=False`로 잘못 찍힌 글자만 `upright=True`로 고쳐서 pdfplumber의
단어 묶기 로직에 넘긴다. 진짜 세로쓰기(90도 회전)는 이 문턱을 한참
넘으므로 안 건드린다.

기존에 x_tolerance를 5로 올려 두던 임시방편(extract_class_returns.py)은
이 문제를 못 고친다 - 간격을 넓혀도 애초에 다른 경로로 묶이는 걸 못
돌리기 때문이다(실측: 이 페이지에서 x_tolerance=5는 1118자 -> 377단어로
줄이지만 236단어인 정답과는 거리가 멀다 - 여전히 여러 글자가 한
"단어"로 잘못 뭉친다). 회전각을 고치는 것만이 진짜 원인을 없앤다.
"""

import math

import pdfplumber
from pdfplumber.utils.text import WordExtractor

# 이 각(라디안) 미만이면 "회전 없음"으로 본다. 진짜 세로쓰기는 보통
# 90도(1.57rad)나 -90도라서 이 문턱과 두 자릿수 넘게 차이 난다 - 잡음과
# 진짜 회전을 가르는 데 애매함이 없다.
UPRIGHT_ANGLE_TOLERANCE = 0.01


def _fixed_chars(chars):
    fixed = []
    for c in chars:
        if not c["upright"]:
            a, b = c["matrix"][0], c["matrix"][1]
            if abs(math.atan2(b, a)) < UPRIGHT_ANGLE_TOLERANCE:
                c = dict(c, upright=True)
        fixed.append(c)
    return fixed


def extract_words(page, x_tolerance=2, keep_blank_chars=False, **kw):
    """page.extract_words()와 같은 반환 형식이지만, 회전 잡음으로 글자가
    낱개로 흩어지는 문서에서도 제대로 단어를 묶는다.

    항상 이걸로 바꿔 써도 안전하다 - 정상 문서는 고칠 글자가 0개라
    pdfplumber 기본 동작과 완전히 같은 결과가 나온다."""
    return WordExtractor(
        x_tolerance=x_tolerance, keep_blank_chars=keep_blank_chars, **kw
    ).extract_words(_fixed_chars(page.chars))


def extract_text(page, **kw):
    """page.extract_text()와 같지만 위 회전 보정을 거친다."""
    from pdfplumber.utils.text import chars_to_textmap
    return chars_to_textmap(_fixed_chars(page.chars), **kw).as_string


def page_needs_fix(page, sample_limit=400):
    """이 문서에 이 결함이 있는지 빠르게 본다(진단·로그용)."""
    chars = page.chars[:sample_limit] if sample_limit else page.chars
    if not chars:
        return False
    bad = sum(1 for c in chars if not c["upright"]
              and abs(math.atan2(c["matrix"][1], c["matrix"][0])) < UPRIGHT_ANGLE_TOLERANCE)
    return bad > len(chars) * 0.3


_PATCHED = False


def patch_pdfplumber():
    """`Page.chars`를 프로세스 전역에서 고쳐서, pdfplumber 안에서 글자를
    쓰는 모든 것(extract_words/extract_text는 물론, page.extract_tables()의
    셀 안 글자 읽기까지)이 이 파일을 한 번도 안 부르고도 자동으로 혜택을
    받게 한다.

    왜 이 방법이 맞나
    ------------------
    `page.extract_tables()`(우리 표 추출의 원천, extractors.py의
    extract_pdf_tables가 이걸 쓴다)는 셀 글자를 읽을 때 내부적으로
    `self.page.chars`를 그대로 가져다 `pdfplumber.utils.extract_text()`에
    넘긴다 - 우리가 만든 extract_words()/extract_text() 함수를 전혀
    거치지 않는다. 그래서 추출기마다 하나씩 고치면 이 원천(chunks/tables
    두 테이블의 근본 자료)은 계속 깨진 채로 남는다.

    `Page.chars`는 원래 `self.objects.get("char", [])`를 그대로 돌려주는
    property다. 이 자리에서 한 번만 고치면 그 아래 있는 모든 pdfplumber
    함수(extract_words, extract_text, find_tables, extract_tables 전부)가
    자동으로 고쳐진 글자를 받는다.

    안전성: `_fixed_chars`는 회전각이 사실상 0인데 upright=False로 잘못
    찍힌 글자만 건드리고, 정상 글자는 원본과 완전히 같은 dict를 그대로
    돌려준다(값 하나 안 바뀜) - 그래서 이 결함이 없는 페이지·문서에는
    아무 영향이 없다. `pdfplumber.open()`을 한 번 이상 부르는 모든
    스크립트 맨 위에서 `import pdf_words; pdf_words.patch_pdfplumber()`
    (또는 그냥 `import pdf_words`만 해도 패치는 모듈을 불러오는 순간
    걸린다)로 걸어 두면 된다. 두 번 불러도 안전하다(이미 패치됐으면
    다시 안 건드림)."""
    global _PATCHED
    if _PATCHED:
        return

    # 원래 pdfplumber의 Page.chars는 캐싱된 property인데, 이 자리를
    # 그냥 property로 통째로 덮어쓰면서 캐싱이 사라졌다 - .chars에
    # 접근할 때마다(문서 전체 페이지를 훑는 보강 로직 등에서) 매번
    # 처음부터 회전각을 다시 계산해, 문서 하나(60여 쪽)를 여러 번
    # 순회하면 수 초~수십 초가 쌓인다(실측: class_returns.py에 새
    # 보강을 하나 추가했더니 전체 재실행이 5분 안팎에서 1시간 가까이로
    # 늘어났다 - 원인이 이 캐싱 부재였다). 페이지 인스턴스 자신에게
    # 결과를 한 번만 저장해 두는 것으로 충분하다 - 입력(원본 글자
    # 목록)이 페이지 생명주기 동안 안 바뀌므로 순수하게 안전하다.
    def patched_chars(self):
        cached = self.__dict__.get("_pdf_words_fixed_chars")
        if cached is None:
            cached = _fixed_chars(self.objects.get("char", []))
            self.__dict__["_pdf_words_fixed_chars"] = cached
        return cached

    pdfplumber.page.Page.chars = property(patched_chars)
    _PATCHED = True


patch_pdfplumber()
