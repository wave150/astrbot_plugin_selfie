"""Chat wording for the request / acknowledgement / result of a photo.

The old flow answered every order with a fixed short line ending in 🤳 and sent
the picture together with a bracket caption (「给你～（在食堂，穿工装）」), which
reads like a bot filling a template.  These helpers keep the same facts but turn
them into chat-model prompts and normalise what comes back, so the whole
exchange sounds like the character talking:

* request  -> a normal reply to what the user said, with the first sentence
              ending in KACHA (（咔嚓）) once she has accepted the order;
* result   -> the picture alone, then one sentence in her own voice about what
              she is doing in it.

Framework-independent: standard library only, no AstrBot and no image code.
"""

import json
import re

# The shutter sound that marks "order accepted" for a photo request.
KACHA = "（咔嚓）"

_ROLE_LABELS = {"user": "对方", "assistant": "你"}
_QUOTES = "“”\"'`「」『』"
_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]+")
# Characters that end the first sentence of a reply (～ is treated as a tail).
_SENTENCE_END = "。！？!?…"
# Words that only belong to the image prompt; a caption mentioning them leaked.
_PROMPT_MARKERS = ("提示词", "构图", "竖屏", "画幅", "分辨率", "9:16", "8:16", "镜头语言")


def _clean_text(text, *, keep_lines=False, limit=None):
    value = str(text or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _EMOJI.sub("", value)
    for token in ("**", "__", "`"):
        value = value.replace(token, "")
    value = value.strip().strip(_QUOTES).strip()
    if keep_lines:
        lines = [" ".join(line.split()) for line in value.split("\n")]
        value = "\n".join(line for line in lines if line)
    else:
        value = " ".join(value.split())
    if limit and len(value) > limit:
        value = value[:limit].rstrip()
    return value


def clean_reply(text, *, limit=120):
    """Single-line, quote-free model reply text (still unvalidated)."""
    return _clean_text(text, limit=limit)


_SHUTTER_TOKENS = ("🤳", "📸", "（咔嚓）", "(咔嚓)", "（喀嚓）", "(喀嚓)", "咔嚓")
_CJK_SPACE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")


def _join_cjk_spaces(text):
    """聊天里中文之间的空格更像断句，换成逗号。"""
    return _CJK_SPACE.sub("，", text)


def with_kacha(text):
    """Put exactly one KACHA at the end of the first sentence, dropping 🤳.

    Models place the marker unreliably (often at the very start) and often write
    the reply as two lines without punctuation, so the position is normalised
    instead of trusted:

    * 「好呀这就拍。今天厂里好闷」 -> 「好呀这就拍（咔嚓）。今天厂里好闷」
    * 「（咔嚓）食堂灯太绿了\\n正端着盘子找座」 ->
      「食堂灯太绿了（咔嚓）。正端着盘子找座」
    """
    value = _clean_text(text, keep_lines=True, limit=160)
    for token in _SHUTTER_TOKENS:
        value = value.replace(token, "")
    lines = [_join_cjk_spaces(line.strip(" ，,、"))
             for line in value.split("\n") if line.strip()]
    if not lines:
        return ""
    first, rest = lines[0], lines[1:]
    cut = -1
    for index, char in enumerate(first):
        if char in _SENTENCE_END:
            cut = index
            break
    mark = ""
    if cut >= 0:
        mark = first[cut]
        leftover = first[cut + 1:].strip(" ，,、")
        head = first[:cut].rstrip(" ，,、")
        # 「嗯……然后呢」这类只剩标点的残余直接丢掉，别拼成「………」
        if leftover.strip("。！？!?…～~ ，,、"):
            rest.insert(0, leftover)
    else:
        head = first
        if head.endswith(("～", "~")):
            mark = head[-1]
            head = head[:-1].rstrip(" ，,、")
    if not head:
        return ""
    result = head + KACHA + mark
    tail = "".join(rest).strip()
    if tail:
        if not result.endswith(("。", "！", "？", "…", "～", "~")):
            result += "。"
        result += tail
    return result


def finalize_reply(text, *, kacha=True):
    """Normalise an acknowledgement, with or without the shutter marker.

    ``kacha=False`` 对应配置里关掉「（咔嚓）」：仍然清掉模型自己加的拟声词、
    保留分句换行，让 ``split_like_astrbot()`` 继续按句分段发送。
    """
    if kacha:
        return with_kacha(text)
    value = _clean_text(text, keep_lines=True, limit=160)
    for token in _SHUTTER_TOKENS:
        value = value.replace(token, "")
    lines = [_join_cjk_spaces(line.strip(" ，,、"))
             for line in value.split("\n") if line.strip()]
    return "\n".join(lines)


def split_reply(text, *, max_segments=4):
    """Split a chat reply into separate messages at 句号 / 换行.

    她不会把一整段带句号的话塞进一条消息里：句号是分段符，不会出现在发出去
    的文本里；换行同样分段。！？… 是语气，不额外拆，保留在句尾。
    最多 ``max_segments`` 条，多出来的用逗号并进最后一条。
    """
    value = _clean_text(text, keep_lines=True)
    segments = []
    for chunk in re.split(r"[。\n]", value):
        piece = chunk.strip(" ，,、\t")
        if piece:
            segments.append(piece)
    if not segments:
        return []
    if len(segments) > max_segments:
        head = segments[:max_segments - 1]
        tail = "，".join(segments[max_segments - 1:])
        segments = head + [tail]
    return segments


# AstrBot「分段回复」的默认正则（platform_settings.segmented_reply.regex）。
_DEFAULT_SEG_REGEX = r".*?[。？！~…]+|.+$"


def _split_by_words(text, words, max_segments):
    """按分段词切开（和 AstrBot 的 words 模式一致：分段词本身不保留）。"""
    words = [str(word) for word in (words or ["。", "？", "！", "~", "…"]) if word]
    if not words:
        return split_reply(text, max_segments=max_segments)
    pattern = re.compile(
        "(" + "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True)) + ")"
    )
    pieces = []
    for chunk in text.split("\n"):
        buffer = ""
        for part in pattern.split(chunk):
            if pattern.fullmatch(part):
                if buffer.strip():
                    pieces.append(buffer)
                buffer = ""
            else:
                buffer += part
        if buffer.strip():
            pieces.append(buffer)
    if len(pieces) > max_segments:
        pieces = pieces[:max_segments - 1] + ["，".join(pieces[max_segments - 1:])]
    return pieces


def split_like_astrbot(text, settings=None, *, max_segments=4):
    """Split a reply the same way AstrBot's segmented-reply stage would.

    ``settings`` 是 AstrBot 的 ``platform_settings.segmented_reply``：enable /
    split_mode（regex|words）/ regex / split_words / words_count_threshold /
    content_cleanup_rule。无论 AstrBot 怎么配置，这里都不会把句号发进聊天——
    分段后再按句号兜底切一次，保证「句号处必须分开」。
    """
    settings = settings if isinstance(settings, dict) else {}
    value = _clean_text(text, keep_lines=True)
    if not value:
        return []
    if not settings.get("enable", True):
        return split_reply(value, max_segments=max_segments)
    try:
        threshold = int(settings.get("words_count_threshold") or 150)
    except (TypeError, ValueError):
        threshold = 150
    # AstrBot 对超长消息不分段；这里仍然按句号切开，因为句号不能出现在聊天里。
    if threshold > 0 and len(value) > threshold:
        return split_reply(value, max_segments=max_segments)
    if str(settings.get("split_mode") or "regex") == "words":
        raw = _split_by_words(value, settings.get("split_words"), max_segments)
    else:
        regex = str(settings.get("regex") or _DEFAULT_SEG_REGEX)
        try:
            raw = re.findall(regex, value, re.DOTALL | re.MULTILINE)
        except re.error:
            raw = re.findall(_DEFAULT_SEG_REGEX, value, re.DOTALL | re.MULTILINE)
    cleanup = str(settings.get("content_cleanup_rule") or "")
    segments = []
    for part in raw:
        piece = str(part or "")
        if cleanup:
            try:
                piece = re.sub(cleanup, "", piece)
            except re.error:
                pass
        segments.extend(split_reply(piece, max_segments=max_segments))
    if len(segments) > max_segments:
        segments = segments[:max_segments - 1] + ["，".join(segments[max_segments - 1:])]
    return segments


def segmented_interval(settings=None):
    """AstrBot 分段回复的间隔（秒），没开或解析失败返回 ()。

    只在两条之间 sleep，且最多 2 秒，避免接单话等太久。
    """
    settings = settings if isinstance(settings, dict) else {}
    if not settings.get("enable", False):
        return ()
    raw = str(settings.get("interval") or "").replace(" ", "")
    try:
        values = [float(item) for item in raw.split(",") if item]
    except ValueError:
        values = []
    if len(values) >= 2:
        low, high = values[0], values[1]
    elif len(values) == 1:
        low = high = values[0]
    else:
        low, high = 1.5, 3.5
    low, high = max(0.0, min(low, high)), max(0.0, max(low, high))
    return (min(low, 2.0), min(high, 2.0))


def usable_reply(text, *, min_len=4, max_len=90, keep_lines=False):
    """Reject empty / prompt-flavoured model output before it reaches the chat.

    ``keep_lines`` keeps the model's line breaks: the acknowledgement uses the
    first line for the shutter marker, so the split must survive validation.
    """
    value = _clean_text(text, keep_lines=keep_lines, limit=max_len)
    if len(value) < min_len:
        return ""
    if any(marker in value for marker in _PROMPT_MARKERS):
        return ""
    return value


_ACK_CLICHES = ("行行行", "行吧行吧", "好好好", "来了来了", "马上马上", "这就这就")

# 「刚从外面回来」「刚下班」「刚到家」这类交代来路的句子：用户明确不要。
# 处境里没给过的行踪不能编，出现了就退回兜底文案。
_FRESH_CLAIM_PAT = re.compile(
    r"刚(?:才)?(?:从[^，。；！？,.;!?\s]{0,10}?)?"
    r"(?:回来|到家|回家|下班|放学|起床|睡醒|洗完|吃完|忙完|出门)"
)


def usable_ack(text, *, recent=(), max_len=90):
    """接单话的额外校验：敷衍口头禅和跟最近重复的说法一律退回兜底。"""
    value = usable_reply(text, keep_lines=True, max_len=max_len)
    if not value:
        return ""
    first = value.split("\n")[0].replace(KACHA, "").strip(" ，,、")
    if any(first.startswith(word) for word in _ACK_CLICHES):
        return ""
    if _FRESH_CLAIM_PAT.search(value):
        return ""
    tips = [str(item).replace(KACHA, "").strip(" ，,、") for item in recent if str(item).strip()]
    if first and any(first == tip for tip in tips[-6:]):
        return ""
    return value


def usable_caption(text, *, max_len=90):
    """Validate the post-photo sentence; '' means caller falls back."""
    value = usable_reply(text, min_len=4, max_len=max_len, keep_lines=True)
    if not value:
        return ""
    # 模板腔：「给你～」「这是照片」这类还是机械味，直接退回兜底配文。
    if value.startswith(("给你", "这是照片", "照片里")):
        return ""
    if _FRESH_CLAIM_PAT.search(value):
        # 用户明确不要「刚从外面回来」这种交代：把带这句的句子整句删掉，
        # 剩下的照片描述照常发；删空了才退回兜底配文。
        pieces = re.split(r"(?<=[。！？!?；;\n])", value)
        value = "".join(part for part in pieces
                        if part.strip() and not _FRESH_CLAIM_PAT.search(part)).strip()
        if not value or _FRESH_CLAIM_PAT.search(value):
            return ""
    if value.count("（") >= 2 and "咔嚓" not in value:
        # 一串括号补充说明就是旧配文格式，不要。
        return ""
    lines = []
    for part in value.split("\n"):
        # 模型习惯用空格断句（人格要求短句），中文之间的空格换成逗号更像人话。
        part = _join_cjk_spaces(part).strip(" ，,、")
        if part:
            lines.append(part)
    if not lines:
        return ""
    if len(lines) == 1:
        return lines[0][:max_len].rstrip()
    joined = ""
    for part in lines[:-1]:
        if not part.endswith(("。", "！", "？", "…", "～", "~")):
            part += "。"
        joined += part
    joined += lines[-1]
    return joined[:max_len].rstrip()


def _message_text(content):
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                kind = part.get("type")
                if kind in (None, "text"):
                    parts.append(str(part.get("text") or ""))
                elif kind in ("image_url", "image"):
                    parts.append("[图片]")
    elif isinstance(content, dict):
        parts.append(str(content.get("text") or ""))
    return "".join(parts)


def history_lines(history, *, limit=8, width=80):
    """Flatten AstrBot conversation history into short 「对方：…」 lines.

    ``history`` may be the JSON string of a Conversation row or an already
    decoded OpenAI-style message list.  System/tool turns are skipped because
    the caller supplies its own persona prompt.
    """
    if isinstance(history, str):
        try:
            rows = json.loads(history)
        except Exception:
            return []
    else:
        rows = history
    if not isinstance(rows, list):
        return []
    lines = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = _ROLE_LABELS.get(str(row.get("role") or ""))
        if not label:
            continue
        text = clean_reply(_message_text(row.get("content")), limit=width)
        if not text:
            continue
        lines.append(f"{label}：{text}")
    return lines[-limit:]


def ack_prompts(*, persona, user_text, scene_line="", action_line="",
                detail="", history=(), mark=True, recent=()):
    """Prompts for the pre-photo reply (order accepted, first sentence 咔嚓)."""
    system = (
        f"{persona}\n"
        "你正在 QQ 上和对方聊天，刚收到一个拍照请求。"
        "像平时聊天那样回他一句：先回应他这句话本身（他在问什么就答什么、在调侃就接住），"
        "再自然地让他知道你已经答应、这就去拍。"
        "只输出你要发出去的文字，不要旁白、不要括号动作描写、不要引号、不要 emoji。"
    )
    blocks = [f"对方刚说：{user_text or '（他没说话，只是想要一张照片）'}"]
    if scene_line:
        blocks.append(f"你现在的处境：{scene_line}（这是你此刻真实的位置和状态，只能按这个说，"
                      "不要另外编行程）")
    if action_line:
        blocks.append(f"接下来你要做的是：{action_line}")
    if detail:
        blocks.append(f"补充情况：{detail}")
    if recent:
        blocks.append(
            "你最近几次接单是这么说的：" + " / ".join(str(line) for line in recent[:6])
            + "\n这次别再用一样的开头和句式，换个说法。"
        )
    if history:
        blocks.append("刚才的聊天（从早到晚）：\n" + "\n".join(history))
    blocks.append(
        "不要用「行行行」「行吧行吧」「好好好」「来了来了」这类敷衍口头禅开头；"
        "不要编造上面处境里没有的行程（比如「刚从外面回来」「刚下班」「刚吃完饭」），"
        "整段都不要用「刚」交代行踪或刚做完的事，"
        "你此刻在哪儿、在做什么以上面的处境为准。"
    )
    if mark:
        blocks.append(
            "要求：写成两行。第一行先接住对方这句话——答应下来，顺便带点你此刻的情绪"
            "（吐槽、撒娇、反问都行，别端着）；"
            f"这一行的结尾就是{KACHA}两个字，不要把它放在开头，行末也别再加别的标点。"
            "第二行再顺口说一句你手上正在忙什么（别交代刚去过哪、刚做完什么）。"
            "两行都是第一人称口语，各 8~25 字，不要引号、不要 emoji、不要解释；"
            "上面没给过的具体菜名、人名、物品不要凭空编。"
        )
    else:
        blocks.append(
            "要求：写成两行。第一行先接住对方这句话——答应下来，顺便带点你此刻的情绪"
            "（吐槽、撒娇、反问都行，别端着）；第二行再顺口说一句你手上正在忙什么"
            "（别交代刚去过哪、刚做完什么）。两行都是第一人称口语，各 8~25 字，"
            "不要拟声词、不要（咔嚓）或🤳之类的符号、不要引号、不要 emoji、不要解释；"
            "上面没给过的具体菜名、人名、物品不要凭空编。"
        )
    return system, "\n".join(blocks)



def caption_prompts(*, persona, kind, scene_line="", doing_line="", look_line="",
                    cos_name="", user_text="", history=()):
    """Prompts for the sentence sent right after the picture itself."""
    system = (
        f"{persona}\n"
        "你刚把一张真实的照片发给对方，照片已经发出去了。"
        "现在用你自己的语气，先用第一人称说说这张照片里的你"
        "（在做什么、在哪儿、什么样子），再顺口回应他刚才说的话，"
        "就像随手发完图补一句那样，不要主持、不要介绍、不要念稿。"
        "只输出你要发出去的文字，不要引号、不要 emoji。"
    )
    blocks = [f"这张照片是：{kind or '自拍'}"]
    if cos_name:
        blocks.append(f"照片里你 cos 的是「{cos_name}」")
    if scene_line:
        # 场景短句常以「在」开头，直接拼会变成「你在：在食堂」。
        place = scene_line[1:] if scene_line.startswith("在") else scene_line
        blocks.append(f"你在{place}")
    if doing_line:
        blocks.append(f"你正在做的：{doing_line}")
    if look_line:
        blocks.append(f"你身上穿的：{look_line}")
    if user_text:
        blocks.append(f"对方刚说：{user_text}")
    if history:
        blocks.append("刚才的聊天：\n" + "\n".join(history))
    blocks.append(
        "要求：先说这张照片本身——第一人称讲照片里的你（在哪个房间或地方、穿什么、"
        "在做什么、什么表情，挑一两个最有画面的细节），最后用一句回应对方刚说的话；"
        "顺序不能反，不要先交代「刚从外面回来」「刚下班」「刚到家」这种来路再讲照片，"
        "整段都不要出现用「刚」交代行踪或刚做完的事的句子；"
        "1~2 句、15~50 个字、第一人称口语，别和上一句答应他的话重复；"
        "不要编造上面没给过的具体菜名、人名或物品，不确定的细节就跳过；"
        "不要用括号解释，不要罗列清单，不要出现照片、图片、构图、比例、镜头语言这类词。"
    )
    return system, "\n".join(blocks)
