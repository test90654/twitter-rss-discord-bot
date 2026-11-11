"""
ムーラン買取表のOCR結果の商品名を、正しい商品名の一覧（参照リスト）と照らし合わせて
読み間違いが無いか調べる。

参照リストに使うもの（どれも「人が書いた・確認した」正しい表記）
  ・入荷カレンダーの「入荷予定」「入荷報告」タブの商品名
  ・ムーランのシートに登録済みの商品名（過去にダッシュボードで確認して登録したもの）
  ・元ポストの本文（【】で囲まれた商品名など）

判定のしかた
  商品名を空白・記号で単語に分け、どの単語も「同じ1つの参照名」の中にそのまま含まれていれば
  綴りに間違いは無いとみなす（verified）。含まれない単語があれば、その単語が読み間違いの疑い。
  参照名の中から似た文字列を探して、置き換え候補（suggestion）を作る。
"""
import re
import unicodedata
from difflib import SequenceMatcher

# 単語の区切りにする記号（長音「ー」は単語の一部なので含めない）
_SEP_RE = re.compile(r"[\s・･/／()（）\[\]［］【】「」『』〈〉<>《》:：,，、。!！?？~〜～\-‐－―_＆&+＋×'\"“”’#＃]+")
# 参照名にあって商品名に無いと、別の商品の可能性がある単語（番号・賞）
_KEY_TOKEN_RE = re.compile(r"^(\d+|[a-zぁ-んァ-ン]?賞|ラストワン賞|ver\.?\d*|vol\.?\d+)$", re.IGNORECASE)
MAX_MISSING_RATIO = 0.5   # 参照名のうち、商品名に無い部分の割合の上限（シリーズ名の省略などを許す）
# 参照名に無くても読み間違いではない単語（買取表だけに付く補足）
_IGNORE_TOKEN_RE = re.compile(r"^(\d{4}年|\d{1,2}月|再販|未開封|新品|開封品|箱なし|箱無し)$")
# 買取表の見出し（カテゴリ・メーカー名）。OCRが商品名の先頭に付けてしまうが、シートには書かない
_HEADING_TOKEN_RE = re.compile(
    r"^(パレード|(その他)?(フリュー|タイトー|セガ|バンダイ|バンプレスト|エイコー|SK|サンリオ)?プライズ(フィギュア|買取強化)?"
    r"|フリュー|タイトー|セガ|エイコー|その他|その他フィギュア|マスコット|ぬいぐるみ)$",
    re.IGNORECASE,
)
# かな・漢字と数字の境目（「マスコット①シーサー」→「マスコット」「1」「シーサー」）
_DIGIT_EDGE_RE = re.compile(r"(?<=[぀-ヿ一-鿿])(?=\d)|(?<=\d)(?=[぀-ヿ一-鿿])")
# 【】や「」で囲まれた商品名（ポスト本文から参照名を拾う）
_BRACKET_RE = re.compile(r"[【「『]([^】」』]{4,})[】」』]")

MIN_VERIFY_CHARS = 8      # 一致した文字数がこれ未満なら、短すぎて判定しない（どの名前にも含まれてしまう）
SUGGEST_MIN_RATIO = 0.6   # 置き換え候補として出す最低の似ている度合い


def _nfkc(s):
    return unicodedata.normalize("NFKC", str(s or ""))


def strip_headings(name):
    """商品名の先頭に付いた買取表の見出し（パレード・その他プライズ など）を取り除く"""
    name = str(name or "").strip()
    while True:
        parts = name.split(None, 1)
        if len(parts) == 2 and _HEADING_TOKEN_RE.match(_nfkc(parts[0])):
            name = parts[1].strip()
        else:
            return name


def tokens(name):
    """商品名を比較用の単語に分ける（数字以外の1文字の単語と補足語は除く）"""
    out = []
    for word in _SEP_RE.split(_nfkc(strip_headings(name))):
        for t in _DIGIT_EDGE_RE.split(word):
            if (len(t) >= 2 or t.isdigit()) and not _IGNORE_TOKEN_RE.match(t.lower()):
                out.append(t)
    return out


def _all_words(name):
    """二重登録の判定用の単語（再販・番号なども区別に使うので除かない）"""
    out = []
    for word in _SEP_RE.split(_nfkc(name).lower()):
        out += [t for t in _DIGIT_EDGE_RE.split(word) if t]
    return out


def same_item_name(a, b):
    """
    2つの商品名が表記ゆれ（空白・記号・シリーズ名の書き方）だけの違いで、同じ商品を指しているか。
    互いの単語がすべて相手の名前に含まれているときだけ同じとみなす。
    （「ちいかわ」と「ハチワレ」、「(再販)」の有無、「Vol.3」と「Vol.4」は別の商品）
    """
    a_flat, b_flat = _SEP_RE.sub("", _nfkc(a).lower()), _SEP_RE.sub("", _nfkc(b).lower())
    a_words, b_words = _all_words(a), _all_words(b)
    if not a_words or not b_words:
        return False

    def covered(words, other_flat, other_words):
        # 1文字の単語（「B」「1」など）は別の単語の一部に紛れやすいので、単語として一致するときだけ含まれるとみなす
        return all((w in other_words) if len(w) == 1 else (w in other_flat) for w in words)

    return covered(a_words, b_flat, b_words) and covered(b_words, a_flat, a_words)


class Ref:
    __slots__ = ("name", "source", "cmp", "disp", "toks")

    def __init__(self, name, source):
        self.name = name
        self.source = source
        self.disp = _SEP_RE.sub("", _nfkc(name))
        self.cmp = self.disp.lower()
        self.toks = [t.lower() for t in tokens(name)]


def _missing(ref, name_cmp):
    """参照名の単語のうち、商品名に含まれないもの"""
    return [t for t in ref.toks if t not in name_cmp]


def build_refs(named_sources):
    """[(商品名, 出どころ), ...] から重複を除いた参照リストを作る"""
    seen = set()
    refs = []
    for name, source in named_sources:
        name = str(name or "").strip()
        if not name:
            continue
        r = Ref(name, source)
        if len(r.cmp) < 4 or r.cmp in seen:
            continue
        seen.add(r.cmp)
        refs.append(r)
    return refs


def refs_from_text(text, source="ポスト本文"):
    """ポスト本文から参照名を拾う（【】「」で囲まれた部分と、各行）"""
    text = str(text or "")
    found = [m.group(1) for m in _BRACKET_RE.finditer(text)]
    for line in text.splitlines():
        line = re.sub(r"https?://\S+|#\S+", "", line).strip()
        if len(line) >= 6:
            found.append(line)
    return [(n, source) for n in found]


def _best_window(token_cmp, ref):
    """参照名の中から token に一番似ている部分を探す → (似ている度合い, 部分文字列)"""
    n = len(token_cmp)
    best = (0.0, "")
    # 参照名の単語の中だけを探す（単語の境目をまたいだ部分は候補にしない）
    for word in _SEP_RE.split(_nfkc(ref.name)):
        word_cmp = word.lower()
        if not word or len(word_cmp) != len(word):
            continue
        # 同じ文字数の部分を優先する（同点なら先に見つけたものを残すので、n を最初に調べる）
        for size in (n, n + 1, n - 1):
            if size < 1 or size > len(word):
                continue
            for i in range(0, len(word) - size + 1):
                window = word_cmp[i:i + size]
                same = sum(b.size for b in SequenceMatcher(None, token_cmp, window).get_matching_blocks())
                ratio = same / max(n, size)
                if ratio > best[0]:
                    best = (ratio, word[i:i + size])
    return best


def check_name(name, refs):
    """
    商品名を参照リストと照らし合わせる。戻り値の dict:
      status     : "verified"（綴りに間違いなし） / "suspect"（読み間違いの疑い） / "unknown"（参照名が見つからない・短すぎる）
      ref        : 一番近い参照名
      source     : その出どころ
      score      : 単語の文字数のうち、参照名に含まれていた割合（0〜1）
      unmatched  : 参照名に含まれなかった単語
      suggestion : 疑わしい単語を参照名の表記に置き換えた商品名（作れたときだけ）
      reason     : 判定の理由（画面表示用）
    """
    toks = tokens(name)
    result = {"status": "unknown", "ref": "", "source": "", "score": 0.0,
              "unmatched": [], "missing": [], "suggestion": "", "reason": ""}
    if not toks:
        result["reason"] = "商品名が空です"
        return result

    # 同じ長い単語が2回出てくるのは、OCRが同じ行を重ねて読んだときの典型的な崩れ
    lowered = [t.lower() for t in toks]
    dup = sorted({t for t in toks if len(t) >= 4 and lowered.count(t.lower()) > 1})

    total = sum(len(t) for t in toks)
    name_cmp = "".join(lowered)
    best = None
    for ref in refs:
        matched = sum(len(t) for t in toks if t.lower() in ref.cmp)
        if matched == 0:
            continue
        missing_chars = sum(len(t) for t in _missing(ref, name_cmp))
        # 商品名の単語が多く含まれ、参照名の余り（商品名に無い部分）が少ないものを選ぶ
        key = (matched - 0.5 * missing_chars, -len(ref.cmp))
        if best is None or key > best[0]:
            best = (key, ref, matched)

    if best is None:
        result["reason"] = "参照リストに近い商品名がありません"
        return result

    _, ref, matched = best
    result.update(ref=ref.name, source=ref.source, score=round(matched / total, 3))
    result["unmatched"] = [t for t in toks if t.lower() not in ref.cmp]
    missing = _missing(ref, name_cmp)
    result["missing"] = missing
    ref_total = sum(len(t) for t in ref.toks) or 1
    missing_ratio = sum(len(t) for t in missing) / ref_total
    missing_key = [t for t in missing if _KEY_TOKEN_RE.match(t)]

    if not result["unmatched"] and not dup:
        if matched < MIN_VERIFY_CHARS:
            result["reason"] = "一致はしましたが、商品名が短すぎて確実とは言えません"
        elif missing_key:
            result["status"] = "suspect"
            result["reason"] = f"参照名「{ref.name}」にある「{'」「'.join(missing_key)}」が商品名にありません（番号・賞の読み落とし？）"
        elif missing_ratio > MAX_MISSING_RATIO:
            result["reason"] = f"参照名「{ref.name}」と比べて商品名に足りない部分が多いので、別の商品の可能性があります"
        else:
            result["status"] = "verified"
            result["reason"] = f"すべての単語が「{ref.name}」（{ref.source}）に含まれています"
        return result

    # 読み間違いの疑いがある単語を、参照名の中の似た表記に置き換えた候補を作る
    suggestion = _nfkc(strip_headings(name))
    fixes = []
    for t in result["unmatched"]:
        ratio, window = _best_window(t.lower(), ref)
        if ratio >= SUGGEST_MIN_RATIO and window:
            suggestion = suggestion.replace(t, window)
            fixes.append(f"{t}→{window}")
    for t in dup:
        # 2回目以降の重複を取り除く
        first = suggestion.find(t)
        if first >= 0:
            head, tail = suggestion[:first + len(t)], suggestion[first + len(t):]
            suggestion = head + tail.replace(t, "")
        fixes.append(f"重複「{t}」を削除")
    suggestion = re.sub(r"\s+", " ", suggestion).strip()
    if suggestion and suggestion != _nfkc(name).strip():
        # 見出しを外しただけの場合も、シートに書く表記として候補に出す
        result["suggestion"] = suggestion

    if result["score"] >= 0.5 or dup:
        result["status"] = "suspect"
        parts = []
        if result["unmatched"]:
            parts.append("「" + "」「".join(result["unmatched"]) + "」が参照名に見当たりません")
        if dup:
            parts.append("同じ単語が重なっています")
        result["reason"] = "、".join(parts) + (f"（候補: {', '.join(fixes)}）" if fixes else "")
    else:
        result["reason"] = "参照リストに近い商品名がありません"
    return result
