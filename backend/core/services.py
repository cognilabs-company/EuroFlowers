import json
import re
from html import escape
from datetime import date, timedelta
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.db.models import F, Prefetch, Q
from django.utils import timezone
from openai import OpenAI
from .models import AICatalogItem, AISettings, BusinessSettings, CatalogItem, Conversation, FlowerVariant, IntegrationSettings, Lead, LeadStockUsage, Message, Notification, Packaging, SocialPost, StockBatch
from .platform_services import instagram_send_carousel, instagram_send_image, openai_api_key, telegram_send_image, telegram_send_media_group, telegram_send_rich_message_with, telegram_send_with
from . import vision_services


AI_REPLY_WAIT_SECONDS = 7
INSTAGRAM_AI_REPLY_WAIT_SECONDS = 10
AI_FOLLOW_UP_DELAY_SECONDS = 30 * 60

# Sklad AI ga ko'rsatilmaydi. Bu nomlar tool ro'yxatidan olib tashlangan, model baribir
# chaqirib qolsa execute_ai_tool unga operatorga topshirish yo'riqnomasini qaytaradi.
AI_HIDDEN_STOCK_TOOLS = {"get_stock", "get_flower_variant_info", "calculate_custom_arrangement_price", "send_stock_image", "send_stock_images"}

LEAD_TOPIC_LABELS = {
    "catalog_order": "Katalogdan buyurtma",
    "custom_order": "Yasatma buyurtma",
    "photo_request": "Rasm bo'yicha so'rov",
    "question": "Savol",
    "other": "Boshqa mavzu",
}

ARRANGEMENT_LABELS = [("bouquet", "buket"), ("basket", "savat"), ("stems", "donalab"), ("catalog", "katalog mahsuloti")]

MAX_LEAD_PHOTO_URLS = 5
MAX_CONTEXT_ATTACHMENTS = 6
MAX_OPERATOR_HANDOFF_MEDIA = 10
MAX_AI_CATALOG_MATCH_CANDIDATES = 40
# Instagram bitta karusel xabariga 10 ta rasm sig'adi. Bitta reelga do'kon ettita
# mahsulot qo'yishi mumkin va mijoz ularning hammasini ko'rishi kerak.
MAX_LINK_MATCHES = 10
# Fingerprint bali shundan past bo'lsa mahsulot qisqa ro'yxatga ham tushmaydi —
# modelga umuman aloqasi yo'q rasmni ko'rsatib o'tirishning hojati yo'q.
AI_CATALOG_SHORTLIST_FLOOR = 30


# Mijozga Telegram username berilmaydi. U hech qayerga yozmaydi — operatorning
# o'zi chatga kirib yozadi.
#
# Operatorga o'tkazish ikki qadam: avval TAKLIF qilinadi, mijoz rozilik bergach
# TASDIQ yoziladi va shundagina guruhga xabar ketadi. Rozilik so'ramasdan
# o'tkazib yuborish mijozni suhbatdan uzib qo'yardi.
OPERATOR_OFFER_UZ = "Sizni operatorlarimizga bog'laymizmi? Ular sizga aniq javob berishadi."
OPERATOR_PROMISE_UZ = "Rahmat, operatorlarimiz sizga tez orada yozib yuborishadi."


def operator_telegram_text(handle=""):
    return OPERATOR_OFFER_UZ


# Guruhga faqat g'olib bilan shu qadar yaqin ballar tushadi. Undan pastdagisi
# g'olibga yetmagan mahsulot — uni ko'rsatish mijozni chalg'itadi.
TIED_SCORE_GAP = 6

MEDIA_MATCH_FOUND_INSTRUCTION = (
    "Aynan shu mahsulot topildi. send_catalog_image ni shu catalog_id bilan chaqir. "
    "Javobni \"Bizda hozirda bor, siz ko'rsatganga o'xshagan variant:\" degan mazmundagi "
    "bitta jumla bilan boshla, keyin yangi qatordan mahsulot nomi, narxi va bitta savol. "
    "Mijoz qaysi tilda yozgan bo'lsa shu tilda yoz."
)
MEDIA_MATCH_OWN_STORY_INSTRUCTION = (
    "Mijoz bizning o'z storyimizni yubordi va uning ma'lumoti tizimda saqlangan: "
    "story maydonidagi title va price_text aynan shu mahsulotniki. Mijozga o'sha nomni "
    "va o'sha narxni ayt, keyin bitta savol ber. \"O'xshagan\" yoki \"o'xshash variant\" "
    "DEMA — bu aynan o'sha gulning o'zi. Katalogdan boshqa rasm YUBORMA va boshqa "
    "mahsulot nomini aytma. Mijoz shu storydagi gulning rasmini so'rasa "
    "send_post_image ni social_post_id bilan chaqir."
)
MEDIA_MATCH_OWN_POST_INSTRUCTION = (
    "Mijoz bizning o'z story/postimizni yubordi va undagi mahsulot aniq topildi. "
    "send_catalog_image ni shu catalog_id bilan chaqir. \"O'xshagan\", \"o'xshash\" yoki "
    "\"shunga o'xshagan variant\" DEMA — bu aynan o'sha mahsulotning o'zi. To'g'ridan-to'g'ri "
    "mahsulot nomi, narxi va bitta savol yoz. Mijoz qaysi tilda yozgan bo'lsa shu tilda yoz."
)
MEDIA_MATCH_GROUP_INSTRUCTION = (
    "Katalogda bu rasmga o'xshaydigan bir nechta mahsulot bor, farqi hajmi va gul "
    "sonida — rasmdan qaysi biri ekanini aytib bo'lmaydi. group_matches dagi "
    "catalog_id larni send_catalog_album bilan yubor va mijozdan qaysi biri "
    "kerakligini so'ra. Bittasini tanlab narx aytma."
)
# Mijoz rasm yuborib "shu guldan yasab bering" desa, u katalogdan gul qidirmayapti.
# Bunday paytda katalog albomini yuborish savolga javob bo'lmaydi, shuning uchun
# har ikkala "topilmadi" ko'rsatmasi yasatma javobiga yo'l ochib qo'yadi.
MEDIA_MATCH_CUSTOM_ORDER_NOTE = (
    " LEKIN mijoz shu guldan YASAB BERISHNI so'ragan bo'lsa (\"yasab berolislami\", "
    "\"shunaqasini yasang\", \"shu guldan buket qb bering\") — bu yasatma buyurtma, "
    "katalog qidiruvi emas. Unda albom YUBORMA va 00C bo'limidagi javobni ber: "
    "xohlaganingizdek yasab beramiz, yuborgan rasmingizdagi guldan buket bo'yicha "
    "00H bo'limidagi tartib bo'yicha operatorga bog'lashni TAKLIF qil: \"Sizni operatorlarimizga bog'laymizmi? Ular sizga aniq javob berishadi.\" "
    "Rozilik kelgunicha call_operator CHAQIRILMAYDI."
)
MEDIA_MATCH_NOT_FOUND_INSTRUCTION = (
    "Aynan mos mahsulot topilmadi. Bitta ham katalog rasmini alohida YUBORMA va taxmin "
    "qilib mahsulot nomi yoki narxini aytma. Buning o'rniga send_catalog_album ni "
    "catalog_ids BO'SH massiv bilan chaqirib butun katalogni yubor va shu mazmunda yoz: "
    "hozirda bizda bor gullar shular, shulardan tanlasangiz ham bo'ladi; yoki o'zingiz "
    "yuborgan gul kerak bo'lsa 00H bo'limidagi tartib bo'yicha taklif qil: \"Sizni operatorlarimizga bog'laymizmi? Ular sizga aniq javob berishadi.\" "
    "operatorlarimiz siz yuborgan gul haqida aniq javob berishadi. Telefon "
    "raqami SO'RAMA va lead yaratma — mijoz katalogdan gul tanlasagina buyurtma bo'ladi."
) + MEDIA_MATCH_CUSTOM_ORDER_NOTE
MEDIA_MATCH_LINK_GROUP_INSTRUCTION = (
    "Mijoz yuborgan post/reelga bir nechta katalog mahsuloti qo'yilgan, qaysi birini "
    "so'raganini aytib bo'lmaydi. group_matches dagi catalog_id larni send_catalog_album "
    "bilan yubor va \"siz yuborgan reeldan hozir bizda borlari shular\" degan mazmunda "
    "yozib, qaysi biri kerakligini so'ra."
)
MEDIA_MATCH_LINK_FALLBACK_INSTRUCTION = (
    "Rasmdagi aynan mahsulot topilmadi, lekin mijoz yuborgan post/reelga qo'yilgan "
    "kataloglar ma'lum. group_matches dagi catalog_id larni send_catalog_album bilan "
    "yubor va \"siz yuborgan reeldan hozir bizda borlari shular\" degan mazmunda yozib, "
    "qaysi biri kerakligini so'ra. Aynan rasmdagini topdim dema."
)
MEDIA_MATCH_SIMILAR_INSTRUCTION = (
    "Mijoz yuborgan rasmdagi aynan o'sha mahsulot katalogda yo'q. Bir nechta tanlab "
    "o'tirma — send_catalog_album ni catalog_ids BO'SH massiv bilan chaqirib butun "
    "katalogni yubor. Keyin shu mazmunda yoz: hozirda bizda bor gullar shular, "
    "shulardan tanlasangiz ham bo'ladi; yoki siz yuborgan gul ko'proq qiziq bo'lsa "
    "00H bo'limidagi tartib bo'yicha taklif qil: \"Sizni operatorlarimizga bog'laymizmi? Ular sizga aniq javob berishadi.\" operatorlarimiz "
    "aniq narxini aytishadi. \"Aynan shu\" yoki \"topdim\" dema. Telefon raqami "
    "SO'RAMA va lead yaratma."
) + MEDIA_MATCH_CUSTOM_ORDER_NOTE
MEDIA_MATCH_CLOSE_INSTRUCTION = (
    "Rasmdagi gul katalogimizdagi bir nechta mahsulotga juda yaqin, lekin qaysi biri "
    "ekaniga to'liq ishonch yo'q. group_matches dagi catalog_id larni send_catalog_album "
    "bilan yubor va \"shu rasmga eng mos variantlarimiz shular\" degan mazmunda yozib, "
    "qaysi biri kerakligini so'ra. \"Aynan o'sha gul yo'q\" DEMA — bo'lishi mumkin, "
    "shunchaki qaysi biri ekanini mijozning o'zi tasdiqlashi kerak. Bittasini tanlab "
    "narx aytma. Agar mijoz \"bular emas\", \"o'xshamaydi\" desa, butun katalogni "
    "send_catalog_album bilan yuborib, telefon raqamini so'ra."
)
MEDIA_MATCH_CROP_INSTRUCTION = (
    "Rasmda bir nechta gul bor va mijoz bittasini ko'rsatgan, lekin qaysi biri ekanini "
    "aniq ayta olmadik. Katalogdan hech qanday rasm YUBORMA, narx aytma va hozircha "
    "operatorga ham uzatma. Mijozdan aynan o'sha gulni rasmdan kesib (crop qilib) "
    "qayta yuborishini iltimos qil — kesilgan rasmdan aniq topamiz. Bu iltimosni "
    "faqat bir marta qil."
)

MEDIA_MATCHING_PRIORITY_INSTRUCTION = """
MEDIA MATCHING FIRST:
Before writing a single word of a reply, look at REAL_CONTEXT_JSON.conversation.customer_attachments. If it holds any customer image, story, post or reel whose kind is not "ad", and the customer's latest message is about that media, you MUST call match_ai_catalog_by_media first. Nothing in the backend will call it for you, and no reply you write without it can be trusted: naming a flower or a price you did not get from that tool is the worst mistake you can make here. When in doubt, call it — calling it needlessly costs nothing, skipping it invents a product the shop does not sell.
An attachment whose kind is "ad" is the banner of the Instagram ad this conversation started from. The customer did not send it. Never run the photo matcher because of it.
Whenever a match_ai_catalog_by_media result carries an album field, that tool ALREADY sent the album to the customer. You write the reply and nothing else: do not call send_catalog_album, do not call send_catalog_image. detail "ad_catalog_sent" is exactly this case — the ad banner is linked to no catalog item, so the whole catalog went out with that one call. A second album delivers the very same pictures a second time, which is what happened in real conversations 2581 and 2571.
A question about the address, opening hours, delivery or payment is not a question about the photo. Answer it, and leave the matcher alone even when a photo is sitting in the conversation.
Never skip media matching for "shu nechpul", "shundan bormi", "rasmdagi", "storydagi", "reeldagi", "tepadan 2chisi", "qizili", or circled/marked flower requests.
The tool result field allow_send is the only thing that decides what you may do next.
allow_send true: matches has exactly one item. Call send_catalog_image with that catalog_id, then give the item name, the price, and one next question, in the customer's own language. How you open that reply depends on own_post: when own_post is false, lead with one line saying this is what the shop has that looks like what they showed ("Bizda hozirda bor, siz ko'rsatganga o'xshagan variant:"). When own_post is true the customer sent one of our own stories or reels, so the flower is not a resemblance, it is that exact product — never say "o'xshagan" or "similar", just name it and price it.
Never invent a product name or a price. Every name and every price you write must come from a tool result in this turn. If you have not called a tool, you do not know what the shop sells.
Once a flower has been identified this turn, asking "what else do you have like it" is a request for the catalog, not for that same flower again: call send_catalog_album, do not call match_ai_catalog_by_media a second time on the same picture.
When the flower under discussion came from one of our own stories, "send me the picture" means that story's picture: call send_post_image with its social_post_id, which stays valid for the rest of the conversation. Sending the whole catalog album instead answers a question nobody asked.
detail "own_story_matched": the customer sent one of our own stories and the shop wrote its name and price into the system when the story was posted. That is the answer — give the story.title and the story.price_text, ask one next question, and send no catalog image. Do not say "similar" and do not name any other product. If they ask to see the flower again, call send_post_image with story.social_post_id.
allow_group true: call send_catalog_album with exactly the group_matches catalog_ids, then ask which one the customer means. Do not pick one of them yourself and do not quote a single price. The detail field says what the group is: "several_look_the_same" means these catalog items are indistinguishable in a photo and differ only in size and price; "instagram_link_group" and "instagram_link_fallback" mean these are the items posted on the reel or story the customer shared, so say that these are the ones from their reel that the shop has right now; "similar_only" (with show_whole_catalog) means the exact flower is NOT in the catalog and nothing on the shelf is close enough to offer as a substitute: call send_catalog_album with an empty catalog_ids to send the whole catalog, say these are the flowers available and they are welcome to pick one, and offer to have an operator price the flower they actually sent if they leave a number; "close_matches" means one of these probably IS it but the check was not conclusive, so offer them as the closest matches and let the customer confirm — do not tell them the flower is unavailable.
ask_for_crop true: the photo holds several arrangements and the customer pointed at one, but it could not be told apart. Do not send any catalog image, do not name an item, do not quote a price and do not hand off yet. Ask the customer to crop that one flower out of the photo and send it again, warmly and in one sentence. Ask this only once in a conversation.
allow_send false, allow_group false, ask_for_crop false and no album in the result: you have NOT identified the flower. Do not send a single catalog image on its own, do not name a catalog item, do not quote a price, and do not describe near_matches to the customer — near_matches is internal information for the operator only. Instead call send_catalog_album with an empty catalog_ids so the customer sees everything the shop actually has, say these are the flowers available and they are welcome to pick one, and tell them that for the flower they actually sent an operator will answer them precisely — follow section 00H: first OFFER to connect them ("Sizni operatorlarimizga bog'laymizmi? Ular sizga aniq javob berishadi.") and call call_operator only after they agree. Do not ask for a phone number and do not create a lead: a lead is for an order, and they have not ordered anything yet.
Never send a catalog image and then say the operator will confirm. Those two things contradict each other. Either you identified it, or you hand it over.
"""


def ai_globally_active():
    return AISettings.objects.get_or_create(pk=1)[0].is_active


def conversation_test_account(conversation):
    """Suhbat test uchun ajratilgan Instagram akkauntga kelganmi.

    Test akkauntga kim yozsa ham u test suhbati. Ilgari faqat yozgan mijozning
    username i tekshirilardi va boshqa odam test akkauntga yozganda AI jim
    qolardi — aslida o'sha akkauntdagi hamma yozishma test.
    """
    account_id = str(conversation_instagram_account_id(conversation) or "")
    if not account_id:
        message = conversation.messages.filter(sender="customer").order_by("-created_at", "-id").first()
        account_id = str((message.metadata or {}).get("instagram_recipient_id") or "") if message else ""
    return bool(account_id and account_id in settings.AI_TEST_INSTAGRAM_ACCOUNT_IDS)


def ai_allowed_for_conversation(conversation):
    if ai_globally_active():
        return True
    if conversation_test_account(conversation):
        return True
    customer = conversation.customer
    username = (customer.instagram_username or "").lower().lstrip("@")
    if username and username in settings.AI_TEST_INSTAGRAM_USERNAMES:
        return True
    external_id = str(customer.instagram_user_id or "")
    return bool(external_id and external_id in settings.AI_TEST_INSTAGRAM_USER_IDS)


def parse_lead_date(value):
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def normalize_phone(value):
    """Mijoz yozgan raqamni bitta ko'rinishga keltiradi.

    Mijozlar raqamni juda xilma-xil yozadi: "901234567", "+998 90 123 45 67",
    "998901234567", ruscha odat bilan "8 998 ...", ba'zida oldiga nol qo'yib
    "0901234567". Bularning hammasi bitta raqam. To'qqiz raqamdan kam bo'lsa
    to'ldirib taxmin qilmaymiz — bo'sh qaytaramiz va AI mijozdan qayta so'raydi.
    """
    if "*" in (value or ""):
        return ""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 13 and digits.startswith("8998"):
        digits = digits[1:]
    if len(digits) == 10 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 9:
        digits = "998" + digits
    if len(digits) == 12 and digits.startswith("998"):
        return "+" + digits
    return ""


def valid_customer_name(value):
    text = (value or "").strip()
    return bool(text) and compact_match_text(text) not in {"", "unknown", "nomalum", "noma lum"}


def catalog_composition_summary(item):
    rows = []
    for row in item.composition.select_related("stock_batch__variant__flower"):
        if int(row.quantity_stems or 0) <= 0:
            continue
        batch = row.stock_batch
        name = " ".join(part for part in (batch.variant.flower.name_uz, batch.variant.name_uz, batch.variant.color_uz) if part)
        rows.append({"name_uz": name, "quantity_stems": row.quantity_stems, "quantity_bunches": str(row.quantity_bunches)})
    return rows


def available_catalog_queryset():
    """Sotuvda turgan katalog. Sotilgan, chiqitga chiqarilgan va restavratsiyada
    buzilgan donalar hisobdan chiqariladi."""
    return CatalogItem.objects.filter(status="available").annotate(
        used_quantity=F("quantity_sold") + F("quantity_wasted") + F("quantity_reworked")
    ).filter(used_quantity__lt=F("quantity_total"))


def available_ai_catalog_queryset():
    return AICatalogItem.objects.filter(is_active=True, quantity__gt=0)


def stock_availability(batch):
    if batch.remaining_stems <= 0:
        return "qolmagan"
    if batch.remaining_stems <= batch.minimum_sale_stems:
        return "oz qoldi"
    return "bor"


def flower_variant_display_name(variant, language):
    flower_name = variant.flower.name_uz
    variant_name = variant.name_uz
    color = variant.color_uz
    flower_compact = compact_match_text(flower_name)
    variant_compact = compact_match_text(variant_name)
    if variant_name and flower_compact and variant_compact.startswith(flower_compact):
        parts = [variant_name]
    else:
        parts = [flower_name, variant_name]
    current_compact = compact_match_text(" ".join(part for part in parts if part))
    if color and compact_match_text(color) not in current_compact:
        parts.append(color)
    return " ".join(part for part in parts if part).strip()


# Havola, pochta, Telegram username va brend nomlari yozuv o'girishdan chetda
# qoladi. "@euroflowerspremium" ni himoyalamasak "EuroFlowers" qismi brend
# sifatida saqlanib, qolgan "premium" kirillga o'girilib "@euroflowersпремиум"
# bo'lib chiqadi va mijoz akkauntni topolmaydi.
PROTECTED_RE = re.compile(
    r"https?://\S+|www\.\S+|\bt\.me/\S+|[\w.+-]+@[\w-]+\.[\w.]+|@[A-Za-z0-9_]{2,}"
    r"|EuroFlowers|Next\s+Mall|Instagram|Telegram|\bAI\b",
    re.IGNORECASE,
)

CYRIL_TO_LATIN = {
    "А": "A", "а": "a", "Б": "B", "б": "b", "В": "V", "в": "v", "Г": "G", "г": "g",
    "Д": "D", "д": "d", "Ё": "Yo", "ё": "yo", "Ж": "J", "ж": "j", "З": "Z", "з": "z",
    "И": "I", "и": "i", "Й": "Y", "й": "y", "К": "K", "к": "k", "Л": "L", "л": "l",
    "М": "M", "м": "m", "Н": "N", "н": "n", "О": "O", "о": "o", "П": "P", "п": "p",
    "Р": "R", "р": "r", "С": "S", "с": "s", "Т": "T", "т": "t", "У": "U", "у": "u",
    "Ф": "F", "ф": "f", "Х": "X", "х": "x", "Ц": "Ts", "ц": "ts", "Ч": "Ch", "ч": "ch",
    "Ш": "Sh", "ш": "sh", "Щ": "Shch", "щ": "shch", "Ъ": "’", "ъ": "’", "Ь": "", "ь": "",
    "Э": "E", "э": "e", "Ю": "Yu", "ю": "yu", "Я": "Ya", "я": "ya",
    "Ў": "O‘", "ў": "o‘", "Қ": "Q", "қ": "q", "Ғ": "G‘", "ғ": "g‘", "Ҳ": "H", "ҳ": "h",
    "Ы": "I", "ы": "i", "Ъ".lower(): "’",
}

LATIN_DIGRAPHS = [
    ("Sh", "Ш"), ("SH", "Ш"), ("sh", "ш"),
    ("Ch", "Ч"), ("CH", "Ч"), ("ch", "ч"),
    ("Yo", "Ё"), ("YO", "Ё"), ("yo", "ё"),
    ("Yu", "Ю"), ("YU", "Ю"), ("yu", "ю"),
    ("Ya", "Я"), ("YA", "Я"), ("ya", "я"),
]
APOSTROPHES = "‘’'\u02bb\u02bc\u2018\u2019`\u00b4"
LATIN_APOSTROPHE = (
    [("O" + a, "Ў") for a in APOSTROPHES]
    + [("o" + a, "ў") for a in APOSTROPHES]
    + [("G" + a, "Ғ") for a in APOSTROPHES]
    + [("g" + a, "ғ") for a in APOSTROPHES]
)
LATIN_SINGLE = {
    "a": "а", "b": "б", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "ҳ", "i": "и",
    "j": "ж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "қ",
    "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "x": "х", "y": "й", "z": "з",
    "c": "к", "w": "в",
    "A": "А", "B": "Б", "D": "Д", "E": "Е", "F": "Ф", "G": "Г", "H": "Ҳ", "I": "И",
    "J": "Ж", "K": "К", "L": "Л", "M": "М", "N": "Н", "O": "О", "P": "П", "Q": "Қ",
    "R": "Р", "S": "С", "T": "Т", "U": "У", "V": "В", "X": "Х", "Y": "Й", "Z": "З",
    "C": "К", "W": "В",
    "'": "ъ", "’": "ъ", "‘": "ъ", "\u02bb": "ъ", "\u02bc": "ъ", "\u2018": "ъ", "\u2019": "ъ",
}

WORD_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" + APOSTROPHES


def _split_protected(text):
    parts, last = [], 0
    for m in PROTECTED_RE.finditer(text or ""):
        if m.start() > last:
            parts.append((text[last:m.start()], False))
        parts.append((m.group(0), True))
        last = m.end()
    parts.append((text[last:], False))
    return parts


def _cyril_chunk_to_latin(chunk):
    out, prev = [], ""
    for ch in chunk:
        if ch in ("Е", "е"):
            starts_word = not prev or prev not in WORD_CHARS + "абвгдеёжзийклмнопрстуфхцчшщъьэюяўқғҳАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЬЭЮЯЎҚҒҲ"
            out.append(("Ye" if ch == "Е" else "ye") if starts_word else ("E" if ch == "Е" else "e"))
        else:
            out.append(CYRIL_TO_LATIN.get(ch, ch))
        prev = ch
    return "".join(out)


def cyrillic_to_latin(text):
    return "".join(chunk if keep else _cyril_chunk_to_latin(chunk) for chunk, keep in _split_protected(text or ""))


def _latin_chunk_to_cyril(chunk):
    value = chunk
    for src, dst in LATIN_APOSTROPHE:
        value = value.replace(src, dst)
    value = re.sub(r"\bYe", "Е", value)
    value = re.sub(r"\bye", "е", value)
    value = re.sub(r"\bE(?![" + APOSTROPHES + "])", "Э", value)
    value = re.sub(r"\be(?![" + APOSTROPHES + "])", "э", value)
    for src, dst in LATIN_DIGRAPHS:
        value = value.replace(src, dst)
    return "".join(LATIN_SINGLE.get(ch, ch) for ch in value)


def latin_to_cyrillic(text):
    return "".join(chunk if keep else _latin_chunk_to_cyril(chunk) for chunk, keep in _split_protected(text or ""))


RU_MARKERS = re.compile(r"\b(цветы|цветов|какие|сколько|стоит|есть|адрес|где|здравствуйте|спасибо|доставка|нужен|нужна|хочу|работаете|дорого|букет из|привет|можно|пожалуйста|заказ|цена|день|это|вы|мне|для)\b", re.IGNORECASE)
UZ_CYRIL_LETTERS = re.compile(r"[ўқғҳЎҚҒҲ]")
# O'zbekcha qo'shimchalar. Rus tilida bunday tugaydigan so'z deyarli yo'q, shuning
# uchun bitta so'zdan iborat xabarda ham tilni aniqlashga yetadi: "бермокчиман",
# "келади", "яхшимисиз", "билмайман".
UZ_CYRIL_SUFFIXES = re.compile(r"мокчи|моқчи|япти|вотти|вотки|моқда|мисиз|сизми|сангиз|ганман|гандим|[а-я]йман\b|[а-я]аман\b|лади\b|майди\b", re.IGNORECASE)
# "сум" va "сўм" ataylab bu ro'yxatda YO'Q: valyuta nomi ikki tilda ham bir xil
# yoziladi. Real suhbat 2243 da "мне нужен букет ... за 199.000 сум" degan sof
# ruscha jumla shu so'z sabab o'zbekcha deb hisoblandi va javob o'zbek kirilida
# ketdi. Shu sababdan "дона" ham olib tashlandi.
UZ_CYRIL_MARKERS = re.compile(r"\b(гул|гулла|гуллар|бор|борми|бормиди|булади|бўлади|керак|кере|канака|қанақа|нечпул|неч|манзил|каерда|қаерда|ассалом|ассалому|алекум|алайкум|раҳмат|рахмат|сават|яса|ясаймиз|ясанг|ясаб|олиб|беринг|берин|бервор|сизда|бизда|сиз|биз|ишлайсизми|нархи|киммат|қиммат|арзон|яхши|ҳам|хам|учун|билан|мумкинми|деган|қилиб|килиб|хохлайман|хохласангиз|менга|сизга|уйга|эди|экан|бўлса|булса|нима|нечта|качон|қачон|канча|қанча|кани|қани|нечада|таер|тайёр|обкетаман|олсам|бўлса|бўлсин|булсин)\b", re.IGNORECASE)


def conversation_script(texts):
    """Javob berilayotgan xabarlar qaysi yozuvda kelgan.

    Bitta so'zli xabar ("доставка", "локация", "адрес") o'zbekchada ham, ruschada
    ham aynan bir xil yoziladi. Uni yolg'iz o'qib til tanlash xato: mijoz o'zbek
    kirillida yozib turib oxirida "доставка" desa, javob ruscha ketib qolardi.
    Shuning uchun javob berilayotgan hamma xabar birga qaraladi va bittasi ham
    o'zbek kirilida bo'lsa suhbat o'zbekcha hisoblanadi.
    """
    scripts = [detect_text_script(text) for text in texts if (text or "").strip()]
    if "uz_cyril" in scripts:
        return "uz_cyril"
    if "ru" in scripts:
        return "ru"
    return "latin"


def detect_text_script(text):
    """Matn lotinmi, o'zbek kirillmi yoki ruschami.

    Ikkala til belgisi bir matnda uchrashi oddiy hol: o'zbek mijoz "доставка",
    "адрес", "заказ" deb yozadi, lekin rus mijoz "борми", "каерда", "нечпул"
    demaydi. Shuning uchun rus so'zi topilgani o'zi yetarli emas — o'zbekcha
    belgilar bilan solishtiriladi va teng chiqsa o'zbekcha ustun turadi.
    """
    value = text or ""
    if not re.search(r"[А-Яа-яЁёЎўҚқҒғҲҳ]", value):
        return "latin"
    if UZ_CYRIL_LETTERS.search(value):
        # ў, қ, ғ, ҳ harflari rus alifbosida yo'q. Bittasi ham yetarli.
        return "uz_cyril"
    uz = len(UZ_CYRIL_MARKERS.findall(value)) + len(UZ_CYRIL_SUFFIXES.findall(value))
    ru = len(RU_MARKERS.findall(value))
    if uz and uz >= ru:
        return "uz_cyril"
    if ru:
        return "ru"
    return "uz_cyril"


def uz_latin_to_cyril(text):
    return latin_to_cyrillic(text)


def stock_image_url(batch):
    return batch.image_url or batch.variant.image_url or batch.variant.flower.image_url or ""


def stock_batch_ai_row(batch):
    image_url = stock_image_url(batch)
    display_name_uz = flower_variant_display_name(batch.variant, "uz")
    return {
        "batch_id": batch.id,
        "display_name_uz": display_name_uz,
        "display_name_uz_cyril": uz_latin_to_cyril(display_name_uz),
        "flower_uz": batch.variant.flower.name_uz,
        "flower_uz_cyril": uz_latin_to_cyril(batch.variant.flower.name_uz),
        "variant_uz": batch.variant.name_uz,
        "variant_uz_cyril": uz_latin_to_cyril(batch.variant.name_uz),
        "color_uz": batch.variant.color_uz,
        "color_uz_cyril": uz_latin_to_cyril(batch.variant.color_uz),
        "description_uz": batch.variant.description_uz,
        "description_uz_cyril": uz_latin_to_cyril(batch.variant.description_uz),
        "description_ru": batch.variant.description_ru,
        "height_cm": batch.height_cm,
        "height_from_cm": batch.height_from_cm,
        "height_to_cm": batch.height_to_cm,
        "height_label": batch.height_label,
        "availability": stock_availability(batch),
        "remaining_stems": batch.remaining_stems,
        "stems_per_bunch": batch.stems_per_bunch,
        "price_per_stem": str(batch.sale_price_per_stem),
        "price_per_bunch": str(batch.sale_price_per_bunch),
        "stems_per_pochka": batch.stems_per_bunch,
        "price_per_pochka": str(batch.sale_price_per_bunch),
        "has_image": bool(image_url),
        "image_url": image_url,
    }


def variant_without_stock_ai_row(variant):
    image_url = variant.image_url or variant.flower.image_url or ""
    display_name_uz = flower_variant_display_name(variant, "uz")
    return {
        "batch_id": None,
        "display_name_uz": display_name_uz,
        "display_name_uz_cyril": uz_latin_to_cyril(display_name_uz),
        "flower_uz": variant.flower.name_uz,
        "flower_uz_cyril": uz_latin_to_cyril(variant.flower.name_uz),
        "variant_uz": variant.name_uz,
        "variant_uz_cyril": uz_latin_to_cyril(variant.name_uz),
        "color_uz": variant.color_uz,
        "color_uz_cyril": uz_latin_to_cyril(variant.color_uz),
        "description_uz": variant.description_uz,
        "description_uz_cyril": uz_latin_to_cyril(variant.description_uz),
        "description_ru": variant.description_ru,
        "height_cm": None,
        "height_from_cm": None,
        "height_to_cm": None,
        "height_label": "",
        "availability": "qolmagan",
        "remaining_stems": 0,
        "stems_per_bunch": variant.default_stems_per_bunch,
        "price_per_stem": "",
        "price_per_bunch": "",
        "stems_per_pochka": variant.default_stems_per_bunch,
        "price_per_pochka": "",
        "has_image": bool(image_url),
        "image_url": image_url,
    }


def flower_variant_search_haystack(variant):
    return compact_match_text(" ".join([
        variant.flower.name_uz,
        variant.name_uz,
        variant.color_uz,
        variant.description_uz,
        variant.description_ru,
    ]))


def haystack_has_term(haystack, term):
    """So'z chegarasi bo'yicha moslik. 'all' so'zi 'podgallan' ichiga tushmasligi uchun."""
    if len(term) < 4:
        return any(word == term for word in haystack.split())
    return any(word == term or word.startswith(term) or term.startswith(word) for word in haystack.split())


def ai_search_terms(value):
    text = compact_match_text(value)
    replacements = {
        "кок": "moviy",
        "кўк": "moviy",
        "синий": "moviy",
        "голубой": "moviy",
        "moviy": "blue",
        "kok": "moviy",
        "ko k": "moviy",
        "гортензия": "gortenziya",
        "гортензи": "gortenziya",
        "гидрангея": "gortenziya",
        "атиргул": "atirgul",
        "роза": "atirgul",
        "розы": "atirgul",
        "пион": "pion",
        "пиони": "pion",
        "пушти": "pushti",
        "розовый": "pushti",
        "ок": "oq",
        "оқ": "oq",
        "белый": "oq",
        "қизил": "qizil",
        "кизил": "qizil",
        "красный": "qizil",
    }
    expanded = text
    for source, target in replacements.items():
        expanded = expanded.replace(source, target)
    stopwords = {"narxi", "narx", "nechpul", "qancha", "болади", "боларкан", "нархи", "нечпул", "dona", "tasi", "та", "дона", "buket", "savat", "yasash", "yasalgani", "qilamiz", "bilan", "uchun", "bor", "bormi", "mavjud", "price", "and", "the"}
    return [term for term in dict.fromkeys((text + " " + expanded).split()) if len(term) >= 3 and term not in stopwords]


def ai_color_search_terms(terms):
    return {term for term in terms if term in {"moviy", "blue", "pushti", "qizil", "oq", "yashil", "sariq", "binafsha"}}


def recent_customer_orders(customer):
    orders = []
    for lead in customer.leads.select_related("social_post").prefetch_related("catalog_usage__catalog_item", "stock_usage__stock_batch__variant__flower", "packaging_usage__packaging").order_by("-created_at")[:3]:
        catalog_items = [{"name_uz": row.catalog_item.name_uz, "quantity": row.quantity, "type": row.catalog_item.arrangement_type, "price": str(row.catalog_item.price)} for row in lead.catalog_usage.all()]
        if not catalog_items:
            # AI orqali tushgan leadda sklad katalogi bog'lanmaydi, tanlov `details` da turadi.
            catalog_items = [
                {"name_uz": row.get("catalog_name") or "", "quantity": row.get("quantity") or 1, "type": lead.arrangement_type, "price": row.get("price") or ""}
                for row in (lead.details or {}).get("catalog_items") or []
                if isinstance(row, dict) and row.get("catalog_name")
            ]
        stock_items = [{
            "flower_uz": row.stock_batch.variant.flower.name_uz,
            "variant_uz": row.stock_batch.variant.name_uz,
            "color_uz": row.stock_batch.variant.color_uz,
            "quantity_stems": row.quantity_stems,
            "quantity_bunches": str(row.quantity_bunches),
        } for row in lead.stock_usage.all()]
        packaging_items = [{"name_uz": row.packaging.name_uz, "quantity": row.quantity, "type": row.packaging.packaging_type} for row in lead.packaging_usage.all()]
        orders.append({
            "lead_id": lead.id,
            "created_at": lead.created_at.isoformat(),
            "status": lead.status,
            "arrangement_type": lead.arrangement_type,
            "estimated_price": str(lead.estimated_price or ""),
            "request_uz": lead.request_uz,
            "catalog_items": catalog_items,
            "stock_items": stock_items,
            "packaging_items": packaging_items,
        })
    return orders


def ai_catalog_ranked_matches(queryset, query):
    """Katalogni so'rovdagi so'zlar bo'yicha saralaydi va eng mos qatorlarni qaytaradi.

    Nom qisqa, mijozning jumlasi uzun bo'ladi. Shuning uchun har bir so'zni alohida
    tekshirib, eng ko'p so'zi mos kelgan mahsulotlarni olamiz. Hech biri mos kelmasa
    bo'sh qaytadi — AI shunda buni yasatma buyurtma deb hisoblaydi.
    """
    terms = ai_search_terms(query)
    if not terms:
        return queryset.none()
    ranked = []
    for item in queryset:
        haystack = compact_match_text(" ".join([item.name, item.volume, item.note]))
        score = sum(1 for term in terms if haystack_has_term(haystack, term))
        if score:
            ranked.append((score, item.id))
    if not ranked:
        return queryset.none()
    best = max(score for score, _ in ranked)
    return queryset.filter(id__in=[item_id for score, item_id in ranked if score == best])


def catalog_price_bounds(min_price, max_price):
    """Mijoz aytgan budjetni Decimal chegaraga aylantiradi."""
    bounds = []
    for value in (min_price, max_price):
        try:
            bounds.append(Decimal(str(value)) if value not in (None, "") else None)
        except (TypeError, ValueError, ArithmeticError):
            bounds.append(None)
    return bounds[0], bounds[1]


# Mijoz "350 mingga bormi" desa aynan shu narx katalogda bo'lmasligi mumkin.
# O'shanda butun katalogni 199 000 dan boshlab yuborish javob emas — mijoz shu
# summa atrofidagini so'ragan. Shu radius ichidagilar ko'rsatiladi.
BUDGET_RADIUS = Decimal("100000")

# Mijoz bitta summa aytsa qidiruv avval YUQORIGA ochiladi. "400 000 li gul kere"
# degan mijozga 199 000 lik gulni ko'rsatish savdoni pastga tortadi — u o'sha
# summani ayta olishini aytdi.
#
# Chegara foizda o'lchanadi, qat'iy summada emas. Qat'iy +150 000 katta
# summalarga to'g'ri kelardi, kichiklariga esa yo'q: 250 000 so'ragan mijozga
# 400 000 lik gul chiqib qolardi (+60%), holbuki katalogda 199 000 va 200 000
# turgan edi. Real suhbat 2272 da aynan shunday bo'ldi.
BUDGET_UP_RATIO = Decimal("0.30")

# "200 000 li gul kere" degan mijoz uchun 199 000 ham o'sha narx. Mijoz summani
# yaxlitlab aytadi, katalogda esa 199 000 turadi — bu bittagina ming so'mlik
# farq sabab mahsulot ko'rinmay qolardi.
BUDGET_EXACT_TOLERANCE = Decimal("5000")


def budget_target(low, high):
    """Mijoz aytgan yakka summa. Oraliq yoki bir tomonlama chegara bo'lsa None."""
    if low is not None and high is not None and low == high:
        return low
    return None


def catalog_price_band(low, high):
    """Qidiruv oynasi.

    Pastga faqat BUDGET_EXACT_TOLERANCE qadar — "200 000" deganda 199 000 ham
    o'sha narx. Yuqoriga esa BUDGET_UP_RATIO qadar ochiladi.
    """
    target = budget_target(low, high)
    if target is None:
        return low, high
    lower = max(Decimal("0"), target - BUDGET_EXACT_TOLERANCE)
    return lower, (target * (Decimal("1") + BUDGET_UP_RATIO)).quantize(Decimal("1"))


def catalog_cheapest_price(queryset=None):
    prices = sorted((queryset if queryset is not None else available_ai_catalog_queryset()).values_list("price", flat=True))
    return prices[0] if prices else None


def budget_below_cheapest(low, high, queryset=None):
    """Mijoz aytgan summa butun katalogdan past bo'lsa.

    Bu boshqa javob talab qiladi: eng arzon narxni aytib qo'yish bilan tugatish
    mijozni quruq qaytarish. O'rniga o'sha summaga yasab berishni taklif qilamiz.
    """
    target = budget_target(low, high)
    if target is None:
        target = high if low is None else low
    if target is None:
        return False
    cheapest = catalog_cheapest_price(queryset)
    return cheapest is not None and target < cheapest


def budget_focus(low, high):
    """Mijoz nazarda tutgan summa: bitta narx yoki so'ralgan oraliqning o'rtasi."""
    if low is not None and high is not None:
        return (low + high) / 2
    return low if low is not None else high


def budget_window(low, high):
    """So'ralgan summa atrofidagi ±100 000 chegara."""
    focus = budget_focus(low, high)
    if focus is None:
        return None, None
    return focus - BUDGET_RADIUS, focus + BUDGET_RADIUS


def catalog_below_budget(queryset, target, limit=4):
    """So'ralgan summadan pastdagi eng yaqin mahsulotlar.

    Yuqorida hech narsa bo'lmagandagina ishlaydi. Mijozga uzoq yuqoridagini
    ko'rsatishdan ko'ra yaqin pastdagini ko'rsatish yaxshi: 250 000 so'ragan
    mijoz 400 000 ni emas, 200 000 ni oladi.
    """
    if target is None:
        return queryset.none()
    prices = sorted({price for price in queryset.filter(price__lte=target).values_list("price", flat=True)}, reverse=True)
    if not prices:
        return queryset.none()
    return queryset.filter(price__in=prices[:limit])


def catalog_near_budget(queryset, low, high):
    """So'ralgan narxda mahsulot bo'lmasa radius ichidagilar.

    350 000 so'ralganda 250 000 — 450 000 oynasi ochiladi va 400 000 hamda
    450 000 lik gullar chiqadi.
    """
    window_low, window_high = budget_window(low, high)
    if window_low is None:
        return queryset.none()
    return queryset.filter(price__gte=window_low, price__lte=window_high)


BUDGET_BELOW_CHEAPEST_INSTRUCTION = (
    "Mijoz aytgan summa butun katalogimizdan past. Javob ikki qismdan iborat va "
    "IKKALASI ham bo'lishi SHART:\n"
    "  1. Rostini ayt: bizda eng arzoni {cheapest} so'mdan boshlanadi.\n"
    "  2. Darhol qo'sh: lekin siz aytganingizdek {asked} so'mga mos qilib yasab "
    "beramiz. Ism va telefon raqamini qoldirishini so'ra, operatorlarimiz "
    "bog'lanadi.\n"
    "Ism va telefon kelgach client_lead_create ni topic=\"custom_order\" bilan "
    "chaqir va request_text ga mijoz qancha summaga so'raganini yoz. "
    "Katalog albomini YUBORMA va {cheapest} so'mlik mahsulotni taklif qilma — "
    "mijoz o'sha summani ayta olmadi. Faqat eng arzon narxni aytib javobni "
    "tugatish XATO: mijoz hech narsa olmay ketadi."
)


def catalog_budget_summary(queryset, low, high, exact_match, near_prices=(), below_cheapest=False):
    """Budjet javobi uchun ma'lumot.

    Mos mahsulot topilmasa eng arzonini aytish kerak — "250 mingga bormi" savoliga
    quruq "yo'q" javobi savdoni yopadi. Topilgan bo'lsa esa aksincha: eng arzonini
    eslatish mijozni bekorga pastga tortadi.

    Shuning uchun cheapest_price faqat mos mahsulot bo'lmagandagina beriladi.
    Buni promptda taqiqlash yetarli bo'lmadi — model 77 000 belgilik ko'rsatma
    ichidan o'sha qatorni topa olmay, "1 millionlik savatingiz bormi" savoliga ham
    "eng arzoni 199 000" deb javob berdi. Ko'rmagan raqamini ayta olmaydi.
    """
    summary = {
        "asked_min": str(low) if low is not None else "",
        "asked_max": str(high) if high is not None else "",
    }
    if exact_match:
        return summary
    prices = sorted(queryset.values_list("price", flat=True))
    focus = budget_focus(low, high)
    if below_cheapest and prices:
        asked = budget_target(low, high)
        if asked is None:
            asked = high if low is None else low
        summary["below_cheapest"] = True
        summary["cheapest_price"] = str(prices[0])
        summary["instruction_uz"] = BUDGET_BELOW_CHEAPEST_INSTRUCTION.format(
            cheapest=money_uz(prices[0]), asked=money_uz(asked))
        return summary
    # Mijozning budjeti butun katalogdan past bo'lsa radius emas, rostini aytish
    # kerak: eng arzoni shuncha. Radius esa mijoz katalogdagi narxlar oralig'idagi
    # summani aytganda ishlaydi — 350 ming so'ralsa 400 va 450 minglik chiqadi.
    below_everything = bool(prices) and focus is not None and focus < prices[0]
    if near_prices and not below_everything:
        # Radius ishlagan holat. Bu yerda eng arzon narxni aytish xato bo'ladi:
        # mijoz 350 ming atrofini so'ragan, 199 000 ni eslatish uni pastga tortadi
        # va operator keyin qimmatrog'ini qaytadan taklif qilishga majbur bo'ladi.
        window_low, window_high = budget_window(low, high)
        summary["near_window_min"] = str(window_low)
        summary["near_window_max"] = str(window_high)
        summary["instruction_uz"] = (
            f"Aynan so'ralgan narxda mahsulot yo'q, lekin {money_uz(window_low)} — "
            f"{money_uz(window_high)} so'm oralig'ida bor. Qaytgan qatorlarning "
            "HAMMASINI send_catalog_album bilan yubor va har birining narxini ayt. "
            "Eng arzon mahsulot narxini ESLATMA."
        )
        return summary
    if not prices:
        return summary
    summary["cheapest_price"] = str(prices[0])
    summary["most_expensive_price"] = str(prices[-1])
    summary["instruction_uz"] = (
        f"So'ralgan narxda mahsulot yo'q. Rostini ayt va eng arzoni {money_uz(prices[0])} so'm ekanini "
        "ayt, keyin qaytgan yaqin variantlarni albom qilib yubor."
    )
    return summary


def ai_catalog_rows(query="", limit=24, arrangement_type="", made_from_batch_id=None, min_price=None, max_price=None):
    query = (query or "").strip()
    queryset = available_ai_catalog_queryset().order_by("-created_at", "-id")
    if arrangement_type in ["bouquet", "basket", "box", "other"]:
        queryset = queryset.filter(arrangement_type=arrangement_type)
    if made_from_batch_id:
        queryset = queryset.none()
    low, high = catalog_price_bounds(min_price, max_price)
    band_low, band_high = catalog_price_band(low, high)
    if low is not None or high is not None:
        priced = queryset
        if band_low is not None:
            priced = priced.filter(price__gte=band_low)
        if band_high is not None:
            priced = priced.filter(price__lte=band_high)
        if not priced.exists():
            # Yuqorida hech narsa yo'q ekan — eng yaqin pastdagilar ko'rsatiladi.
            # 250 000 so'ralganda 400 000 emas, 199 000 va 200 000 chiqadi.
            priced = catalog_below_budget(queryset, budget_target(low, high) or high or low)
        if not priced.exists():
            priced = catalog_near_budget(queryset, low, high)
        # Budjet aytilgan bo'lsa arzonidan boshlab ko'rsatamiz — mijoz shu tartibda o'ylaydi.
        queryset = (priced if priced.exists() else queryset).order_by("price", "id")
    generic_query_terms = {"vitrina", "katalog", "catalog", "tayyor", "mahsulot", "gulla", "buketlar", "savatlar"}
    normalized_query = compact_match_text(query)
    is_generic_query = bool(normalized_query) and any(term in normalized_query for term in generic_query_terms)
    if query and not is_generic_query:
        matched = queryset.filter(Q(name__icontains=query) | Q(volume__icontains=query) | Q(note__icontains=query) | Q(instagram_link__icontains=query))
        # `icontains` butun jumlani qidiradi. Mijoz esa "kotta shoxli bambastik gulidan
        # bormi" deb yozadi va hech narsa topilmaydi — o'shanda so'zlar bo'yicha izlaymiz.
        queryset = matched if matched.exists() else ai_catalog_ranked_matches(queryset, query)
    rows = []
    for row in queryset[:limit]:
        rows.append({
            "catalog_id": row.id,
            "name_uz": row.name,
            # Ruscha suhbatda javobga shu nom va shu narx qatori yoziladi.
            "name_ru": catalog_name_ru(row.name),
            "price_text_ru": money_text(row.price, "ru"),
            "type": row.arrangement_type,
            "quantity": row.quantity,
            "volume": row.volume,
            # Operator izohi. Mahsulot tafsiloti ham, kelishilgan narx ham shu yerda
            # bo'ladi — AI o'zi o'qib, qaysi biri mijozga aytilishini hal qiladi.
            "note_uz": row.note,
            "price": str(row.price),
            "has_image": bool(row.image_url),
            "image_url": row.image_url,
            "instagram_link": row.instagram_link,
            "composition": [],
        })
    return rows


def ai_catalog_result(query="", limit=24, arrangement_type="", min_price=None, max_price=None):
    """get_catalog tool natijasi. Budjet so'ralgan bo'lsa chegara ma'lumoti ham keladi."""
    rows = ai_catalog_rows(query, limit=limit, arrangement_type=arrangement_type, min_price=min_price, max_price=max_price)
    low, high = catalog_price_bounds(min_price, max_price)
    result = {"catalog": rows}
    if (query or "").strip() and not rows:
        # Qidiruv so'zi topilmagani "bunday mahsulot yo'q" degani EMAS. Katalog nomlari
        # lotinda yozilgan, mijoz esa "букет" yoki "цветы" deb so'raydi va ro'yxat bo'sh
        # qaytadi. Production'da AI shundan keyin "hozir tayyor buket yo'q" deb yozgan —
        # katalogda o'n to'qqizta mahsulot turgan holda.
        rows = ai_catalog_rows("", limit=limit, arrangement_type=arrangement_type, min_price=min_price, max_price=max_price)
        result["catalog"] = rows
        result["query_matched"] = False
        result["instruction_uz"] = (
            "Qidiruv so'zi katalog nomlariga mos kelmadi, lekin katalog bo'sh emas. "
            "Mijozga tayyor mahsulot yo'q deb AYTMA — quyidagilar hozir sotuvda turibdi."
        )
    if low is None and high is None:
        return result
    # exact_match SO'RALGAN narxga qaraydi, kengaytirilgan oynaga emas. 350 000
    # so'ragan mijozga 400 000 lik gulni "shu summaga bor" deb berish yolg'on
    # bo'lardi — qatorlar o'sha oynadan olinadi, lekin rostini aytamiz.
    within = [row for row in rows if (low is None or Decimal(row["price"]) >= low) and (high is None or Decimal(row["price"]) <= high)]
    below_cheapest = budget_below_cheapest(low, high)
    window_low, window_high = budget_window(low, high)
    near_prices = [] if within or window_low is None else [
        row["price"] for row in rows
        if window_low <= Decimal(row["price"]) <= window_high
    ]
    result["budget"] = dict(
        catalog_budget_summary(available_ai_catalog_queryset(), low, high, bool(within), near_prices, below_cheapest=below_cheapest),
        matched=len(within),
        # Budjetga tushgani yo'q bo'lsa qatorlar eng yaqinlari — "aynan shu narxda bor" dema.
        exact_match=bool(within),
    )
    return result


def ai_stock_rows(query="", limit=24):
    query = (query or "").strip()
    stock_batches = StockBatch.objects.filter(is_active=True, remaining_stems__gt=0).select_related("variant__flower").order_by("received_at", "id")
    queryset = (
        FlowerVariant.objects
        .filter(is_active=True)
        .select_related("flower")
        .prefetch_related(Prefetch("batches", queryset=stock_batches, to_attr="ai_stock_batches"))
        .order_by("flower__name_uz", "color_uz", "name_uz")
    )
    if query:
        terms = ai_search_terms(query)
        color_terms = ai_color_search_terms(terms)
        ranked = []
        for variant in queryset:
            haystack = flower_variant_search_haystack(variant)
            if color_terms and not any(haystack_has_term(haystack, term) for term in color_terms):
                continue
            score = sum(1 for term in terms if haystack_has_term(haystack, term))
            if score:
                ranked.append((score, variant))
        queryset = [variant for _, variant in sorted(ranked, key=lambda row: (-row[0], row[1].flower.name_uz, row[1].color_uz, row[1].name_uz))]
    rows = []
    for variant in queryset:
        for batch in distinct_stock_offers(getattr(variant, "ai_stock_batches", [])):
            rows.append(stock_batch_ai_row(batch))
            if len(rows) >= limit:
                return rows[:limit]
    return rows[:limit]


def distinct_stock_offers(batches):
    """Mijozga aytish uchun bir-biridan farq qiladigan partiyalar.

    Kirimda nav so'ralmaydi, shuning uchun bitta gulda bir nechta partiya
    bo'ladi — bo'yi va narxi har xil. Ularning hammasi kerak, aks holda AI
    guldan faqat bittasini ko'radi. Bo'yi ham, narxi ham bir xil bo'lsa esa
    eng eskisi olinadi: mijozga bir xil taklifni ikki marta aytish shart emas.
    """
    seen = set()
    offers = []
    for batch in batches:
        key = (batch.height_label, str(batch.sale_price_per_stem))
        if key in seen:
            continue
        seen.add(key)
        offers.append(batch)
    return offers


def ai_basket_rows(limit=20):
    return [{
        "id": row.id,
        "name_uz": row.name_uz,
        "min": row.capacity_min_stems,
        "max": row.capacity_max_stems,
        "price": str(row.sale_price),
    } for row in Packaging.objects.filter(packaging_type="basket", is_active=True, quantity__gt=0).order_by("sale_price")[:limit]]


def ai_flower_variant_rows(query="", limit=24):
    queryset = FlowerVariant.objects.filter(is_active=True).select_related("flower").order_by("flower__name_uz", "color_uz", "name_uz")
    if query:
        terms = ai_search_terms(query)
        color_terms = ai_color_search_terms(terms)
        ranked = []
        for variant in queryset:
            haystack = flower_variant_search_haystack(variant)
            if color_terms and not any(haystack_has_term(haystack, term) for term in color_terms):
                continue
            score = sum(1 for term in terms if haystack_has_term(haystack, term))
            if score:
                ranked.append((score, variant))
        queryset = [variant for _, variant in sorted(ranked, key=lambda row: (-row[0], row[1].flower.name_uz, row[1].color_uz))]
    rows = []
    for variant in queryset[:limit]:
        stock_rows = distinct_stock_offers(StockBatch.objects.filter(variant=variant, is_active=True, remaining_stems__gt=0).order_by("received_at", "id"))
        rows.append({
            "variant_id": variant.id,
            "display_name_uz": flower_variant_display_name(variant, "uz"),
            "flower_uz": variant.flower.name_uz,
            "variant_uz": variant.name_uz,
            "color_uz": variant.color_uz,
            "description_uz": variant.description_uz,
            "description_ru": variant.description_ru,
            "active_stock": [{
                "batch_id": batch.id,
                "display_name_uz": flower_variant_display_name(batch.variant, "uz"),
                "display_name_uz_cyril": uz_latin_to_cyril(flower_variant_display_name(batch.variant, "uz")),
                "height_label": batch.height_label,
                "availability": stock_availability(batch),
                "remaining_stems": batch.remaining_stems,
                "stems_per_bunch": batch.stems_per_bunch,
                "price_per_stem": str(batch.sale_price_per_stem),
                "price_per_bunch": str(batch.sale_price_per_bunch),
            } for batch in stock_rows],
        })
    return rows


def is_ad_attachment(url):
    """Bu mijoz yuborgan rasm emas, u bosgan reklamaning banneri.

    Instagram reklama orqali kelgan suhbatda o'sha banner mijozning HAR bir
    xabariga qo'shib yuboriladi va imzosi har safar o'zgaradi. Uni oddiy media
    deb qabul qilsak, mijoz "adres qayerda" deb yozganda ham rasm tahlili
    ishga tushadi va savoliga javob o'rniga katalog albomi ketadi.
    """
    return "facebook.com/ads/image" in (url or "").lower()


def customer_attachment_rows(history_messages):
    """Mijoz yuborgan rasm va media havolalari, eng yangisi oxirida.

    Havolalar Instagram yoki Telegram tomonidan beriladi, biz ularni serverga
    ko'chirmaymiz — leadga aynan shu havola yoziladi.
    """
    rows = []
    ad_seen = False
    for message in history_messages:
        if message.sender != "customer":
            continue
        for attachment in (message.metadata or {}).get("attachments", []) or []:
            url = attachment.get("url")
            if not url or any(row["url"] == url for row in rows):
                continue
            if is_ad_attachment(url):
                # Reklama banneri suhbatda bir marta hisobga olinadi.
                if ad_seen:
                    continue
                ad_seen = True
                rows.append({"kind": "ad", "url": url})
                continue
            row = {"kind": attachment.get("kind") or "media", "url": url}
            # Ulashilgan postning izohi va media raqami — bir xil postni ikki
            # xil ulashuv ko'rinishida bog'laydigan yagona kalitlar.
            if attachment.get("caption"):
                row["caption"] = attachment["caption"]
            if attachment.get("media_id"):
                row["media_id"] = attachment["media_id"]
            rows.append(row)
    return rows[-MAX_CONTEXT_ATTACHMENTS:]


def previous_visit_context(conversation, history_messages):
    """Mijoz avval qachon yozgan va nima so'ragan.

    Ertaga qaytib kelgan mijozni "Sizga qanday gul kerak?" bilan kutib olish uni
    birinchi marta ko'rayotgandek muomala qilish demak. Sanani va o'tgan safargi
    so'rovni bilsak, suhbatni odamdek davom ettirish mumkin.
    """
    latest_customer = next((message for message in reversed(history_messages) if message.sender == "customer"), None)
    previous = None
    for message in reversed(history_messages):
        if latest_customer and message.id == latest_customer.id:
            continue
        if message.sender in {"customer", "ai", "operator"}:
            previous = message
            break
    if not previous or not latest_customer:
        return {"days_since_previous_message": None, "previous_message_date": "", "previous_request": ""}
    gap_days = (timezone.localtime(latest_customer.created_at).date() - timezone.localtime(previous.created_at).date()).days
    lead = conversation.leads.order_by("-created_at", "-id").first()
    return {
        "days_since_previous_message": gap_days,
        "previous_message_date": timezone.localtime(previous.created_at).date().isoformat(),
        "previous_request": (lead.request_uz if lead else "")[:200],
    }


def ai_post_context(conversation):
    if not conversation.social_post_id:
        return None
    post = conversation.social_post
    links = [post.permalink, post.webhook_story_url]
    post_catalog = available_ai_catalog_queryset()
    link_query = Q()
    for link in links:
        if link:
            link_query |= Q(instagram_link__startswith=link) | Q(instagram_link__contains=link)
    post_catalog = post_catalog.filter(link_query) if link_query else post_catalog.none()
    return {
        "type": post.post_type,
        "title_uz": post.title_uz,
        "title_ru": post.title_ru,
        "description_uz": post.description_uz,
        "description_ru": post.description_ru,
        "price": str(post.price or ""),
        "catalog": [{
            "name_uz": row.name,
            "type": row.arrangement_type,
            "note_uz": row.note,
            "height_cm": None,
            "diameter_cm": None,
            "quantity": row.quantity,
            "volume": row.volume,
            "price": str(row.price),
            "has_image": bool(row.image_url),
            "image_url": row.image_url,
            "composition": [],
        } for row in post_catalog],
    }


def mini_app_custom_quote_ai(request_text, arrangement_type):
    business_settings, _ = BusinessSettings.objects.get_or_create(pk=1)
    ai_settings, _ = AISettings.objects.get_or_create(pk=1)
    api_key = openai_api_key()
    florist_fee = business_settings.default_florist_fee
    if not api_key:
        return {
            "lines": [{"type": "custom_text", "request_text": request_text}],
            "packaging": None,
            "florist_fee": str(florist_fee),
            "estimated_price": str(florist_fee),
            "price_is_estimate": True,
            "ai_note": mini_app_quote_note(florist_fee),
        }
    context = {
        "request_text": request_text,
        "arrangement_type": arrangement_type,
        "florist_fee": str(florist_fee),
        "stock": ai_stock_rows("", limit=60),
        "baskets": ai_basket_rows() if arrangement_type == "basket" else [],
        "rule": "Mijozga stock ro‘yxatini ko‘rsatma. Faqat taxminiy umumiy narx qaytar. Florist haqini narxga qo‘sh.",
    }
    schema = {
        "type": "object",
        "properties": {
            "estimated_price": {"type": "number"},
            "ai_note": {"type": "string"},
        },
        "required": ["estimated_price", "ai_note"],
        "additionalProperties": False,
    }
    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=ai_settings.openai_model or settings.OPENAI_MODEL,
        instructions="EuroFlowers mini app uchun custom buket/savat taxminiy narxini hisobla. Narx taxminiy, florist haqi qo‘shilgan bo‘lsin. Javob faqat JSON.",
        input=json.dumps(context, ensure_ascii=False),
        max_output_tokens=700,
        reasoning={"effort": "minimal"},
        text={"format": {"type": "json_schema", "name": "mini_app_quote", "strict": True, "schema": schema}},
    )
    data = json.loads(response.output_text)
    estimated_price = Decimal(str(data["estimated_price"])).quantize(Decimal("1"))
    if estimated_price < florist_fee:
        estimated_price = florist_fee
    return {
        "lines": [{"type": "custom_text", "request_text": request_text}],
        "packaging": None,
        "florist_fee": str(florist_fee),
        "estimated_price": str(estimated_price),
        "price_is_estimate": True,
        "ai_note": mini_app_quote_note(estimated_price),
    }


def mini_app_quote_note(estimated_price):
    return f"Taxminiy narx {money_uz(estimated_price)} so'm. Operatorlarimiz aloqaga chiqib, sizga batafsil ma'lumot berishadi."


def calculate_custom_arrangement_price(stock_items):
    business_settings, _ = BusinessSettings.objects.get_or_create(pk=1)
    florist_fee = Decimal(str(business_settings.default_florist_fee or 0))
    lines = []
    flower_subtotal = Decimal("0")
    errors = []
    for row in stock_items or []:
        batch = StockBatch.objects.filter(id=row.get("batch_id"), is_active=True).select_related("variant__flower").first()
        quantity_stems = int(row.get("quantity_stems") or 0)
        if not batch:
            errors.append({"batch_id": row.get("batch_id"), "detail": "stock_not_found"})
            continue
        if quantity_stems <= 0:
            errors.append({"batch_id": batch.id, "detail": "quantity_stems_required"})
            continue
        if quantity_stems > batch.remaining_stems:
            errors.append({
                "batch_id": batch.id,
                "stock_name": flower_variant_display_name(batch.variant, "uz"),
                "detail": "not_enough_stock",
                "requested_stems": quantity_stems,
                "remaining_stems": batch.remaining_stems,
            })
            continue
        unit_price = Decimal(str(batch.sale_price_per_stem or 0))
        subtotal = (Decimal(quantity_stems) * unit_price).quantize(Decimal("1"))
        flower_subtotal += subtotal
        lines.append({
            "batch_id": batch.id,
            "stock_name": flower_variant_display_name(batch.variant, "uz"),
            "quantity_stems": quantity_stems,
            "price_per_stem": str(unit_price.quantize(Decimal("1"))),
            "subtotal": str(subtotal),
            "display_line_uz": f"{quantity_stems} ta {flower_variant_display_name(batch.variant, 'uz')} {money_uz(subtotal)} so'm",
        })
    total = (flower_subtotal + florist_fee).quantize(Decimal("1"))
    return {
        "ok": not errors and bool(lines),
        "lines": lines,
        "errors": errors,
        "flower_subtotal": str(flower_subtotal.quantize(Decimal("1"))),
        "florist_fee": str(florist_fee.quantize(Decimal("1"))),
        "total": str(total),
        "display_summary_uz": {
            "flower_subtotal": f"Gullar jami {money_uz(flower_subtotal)} so'm",
            "florist_fee": f"Florist haqi taxminan {money_uz(florist_fee)} so'm",
            "total": f"Jami taxminan {money_uz(total)} so'm",
        },
    }


def conversation_state_for_ai(conversation, business_settings, history_messages):
    """AI ga suhbatning haqiqiy holati — nima yuborilgan, nimaga javob berilgan.

    Model qoidani biladi, lekin "buni allaqachon qildimmi" degan savolga
    javobni faqat shu yerdan oladi. Real suhbatlarda aynan shu bilim yetishmadi:
    2276 da butun katalog olti marta, 1831 da yetti marta yuborildi; 2273 da
    "1 yoki 2?" savoli ikki marta berildi.
    """
    now = timezone.now()
    whole_at = None
    last_album = []
    image_ids = []
    albums_sent = 0
    for message in conversation.messages.filter(sender="system").order_by("created_at", "id"):
        album = (message.metadata or {}).get("catalog_album_result") or {}
        rows = [row for row in album.get("items") or [] if row.get("delivered")]
        if not rows:
            continue
        if album.get("whole_catalog"):
            whole_at = message.created_at
        albums_sent += 1
        last_album = [{"position": row.get("position"), "catalog_id": row.get("catalog_id"),
                       "name": row.get("name"), "price": row.get("price")} for row in rows]
        if not album.get("whole_catalog"):
            image_ids.extend(row.get("catalog_id") for row in rows)
    ai_texts = [message.text or "" for message in history_messages if message.sender == "ai"]
    joined = " ".join(ai_texts)
    lowered = joined.lower()

    def said(*values):
        """Shu matnlardan biri javoblarda uchraganmi. Javob kirillda ham bo'lishi
        mumkin, shuning uchun har bir qiymatning kirill ko'rinishi ham qaraladi."""
        for value in values:
            text = (value or "").strip()
            if not text:
                continue
            for variant in {text, uz_latin_to_cyril(text)}:
                if variant and variant[:18].lower() in lowered:
                    return True
        return False

    delivery_fee = money_uz(business_settings.delivery_fee) if business_settings.delivery_fee else ""
    hours = (business_settings.working_hours or {}).get("uz", "")
    answered = {
        "shop_address": said(business_settings.shop_address_uz,
                             business_settings.shop_address_uz_cyril,
                             business_settings.shop_address_ru),
        "delivery_price": bool(delivery_fee and delivery_fee in joined),
        "working_hours": said(hours),
        "payment_types": bool(re.search(r"naqd|нақд|наличн|karta|карта", joined, re.IGNORECASE)),
    }
    album_choice = None
    if last_album:
        maps = album_position_maps(conversation)
        if maps:
            choice = customer_album_choice(conversation, maps[-1])
            if choice is not None:
                chosen = next((row for row in last_album if row.get("position") == choice), None)
                album_choice = {"position": choice, "catalog_id": (chosen or {}).get("catalog_id"),
                                "name": (chosen or {}).get("name"), "price": (chosen or {}).get("price")}
    offered = bool(re.search(OPERATOR_OFFER_PATTERN, joined))
    notified = conversation.messages.filter(sender="system").filter(
        Q(metadata__has_key="operator_needed") | Q(metadata__has_key="operator_lead_notified")).exists()
    return {
        "already_sent": {
            "whole_catalog": bool(whole_at),
            "whole_catalog_minutes_ago": int((now - whole_at).total_seconds() // 60) if whole_at else None,
            "catalog_image_ids": sorted(set(value for value in image_ids if value)),
            # Mijoz raqam yozsa u shu ro'yxatdagi position. Nomi bo'yicha
            # qidirish boshqa mahsulotni topib qo'yishi mumkin.
            "last_album": last_album[-30:],
            # Suhbatda bir necha albom ketgan bo'lsa mijozning ekranida faqat
            # oxirgisi turadi va eskilarining raqamlari bekor. Real suhbat
            # 3051 da AI eski albomning tartibini qaytarib boshqa gulni yozdi.
            "albums_sent": albums_sent,
            "older_album_numbers_void": albums_sent > 1,
        },
        "already_answered": answered,
        # Mijoz albomdan raqam bilan tanlagan bo'lsa aynan shu mahsulot.
        # Oxirgi albomdan olinadi — o'zining eski javobidagi tartib emas.
        "album_choice": album_choice,
        "last_ai_reply": ai_texts[-1][:400] if ai_texts else "",
        "operator": {"offer_made": offered, "group_notified": notified},
    }


def conversation_album_items(conversation):
    """Shu suhbatda yuborilgan albomlardagi mahsulotlar, eng yangisi birinchi.

    Mijoz "26 chi buketdan" deb yozganda raqam aynan shu ro'yxatga tegishli.
    Nom bo'yicha qidirish esa boshqa mahsulotni topib qo'yishi mumkin: real
    suhbat 1976 da "Buket Jumila Va Oq Atir Guldan Yasalgan Kompazitsia"
    (199 000) o'rniga "Savat Jumila Oq Atir Guldan Yasalgan Kompazitsia"
    (1 000 000) yozilib, operatorlar guruhiga boshqa gulning rasmi ketdi.
    """
    if conversation is None:
        return []
    rows = []
    seen = set()
    for message in conversation.messages.filter(sender="system").order_by("-created_at", "-id")[:40]:
        album = (message.metadata or {}).get("catalog_album_result") or {}
        for item in album.get("items") or []:
            catalog_id = item.get("catalog_id")
            if not catalog_id or catalog_id in seen:
                continue
            seen.add(catalog_id)
            rows.append(item)
    return rows


def album_catalog_item(catalog_id):
    """Albomdagi qatorning mahsuloti.

    is_active talab qilinmaydi: albom mijozga NIMA taklif qilinganining yozuvi.
    Mijoz tanlagan gul o'shandan keyin sotilib ketgan bo'lishi mumkin, lekin
    operatorga aynan o'sha gulning nomi va rasmi kerak. Real suhbat 1976 da
    mahsulot sotilgani uchun albomdan topilmadi va nom bo'yicha butunlay
    boshqa gul yozilib qoldi.
    """
    if not catalog_id:
        return None
    return AICatalogItem.objects.filter(id=catalog_id).first()


def album_item_by_name(conversation, name):
    """Albomdagi mahsulotni nomi bo'yicha topadi — faqat aynan mos kelsa."""
    key = compact_match_text(name)
    if not key:
        return None
    for row in conversation_album_items(conversation):
        if compact_match_text(row.get("name")) == key:
            return album_catalog_item(row.get("catalog_id"))
    return None


def album_item_by_price(conversation, price, name=""):
    """Albomdan aynan shu narxdagi mahsulot.

    Shu narxda bir nechta mahsulot bo'lsa nomi bo'yicha ajratiladi; u ham
    ajratmasa hech narsa tanlanmaydi — taxmin qilishdan yozmaslik yaxshi.
    """
    if price is None:
        return None
    try:
        asked = Decimal(str(price))
    except (TypeError, ValueError, ArithmeticError):
        return None
    matched = [row for row in conversation_album_items(conversation)
               if row.get("price") and Decimal(str(row["price"])) == asked]
    if len(matched) == 1:
        return album_catalog_item(matched[0].get("catalog_id"))
    key = compact_match_text(name)
    if key:
        named = [row for row in matched if compact_match_text(row.get("name")) == key]
        if len(named) == 1:
            return album_catalog_item(named[0].get("catalog_id"))
    return None


ALBUM_CHOICE_PATTERN = re.compile(
    r"^(\d{1,2})\s*(?:[-–—]?\s*(?:chi|chisi|chisini|nchi|inchi|raqamli|raqam|ni|si|niki|yi"
    r"|чи|чиси|нчи|инчи|раками|ракамли|ни|си))?\s*[.!]?$",
    re.IGNORECASE)
ALBUM_CHOICE_MAX_LENGTH = 16


def album_position_maps(conversation):
    """Suhbatda yuborilgan har bir albomning raqam -> mahsulot jadvali.

    Eng yangisi oxirida turadi. Ikkinchi albom yuborilishi bilan birinchisining
    raqamlari bekor bo'ladi: mijozning ekranida oxirgi albom turadi.
    """
    maps = []
    if conversation is None:
        return maps
    for message in conversation.messages.filter(sender="system").order_by("created_at", "id"):
        album = (message.metadata or {}).get("catalog_album_result") or {}
        positions = {}
        for row in album.get("items") or []:
            if not row.get("delivered"):
                continue
            position = row.get("position")
            catalog_id = row.get("catalog_id")
            if position and catalog_id:
                positions[int(position)] = catalog_id
        if positions:
            maps.append({"sent_at": message.created_at, "positions": positions})
    return maps


def customer_album_choice(conversation, album):
    """Albom yuborilgandan keyin mijoz yozgan yalang raqam.

    Faqat qisqa xabar qaraladi va raqam albomdagi o'rin bo'lishi shart —
    telefon raqami ham, summa ham bu shartdan o'tmaydi.
    """
    if not album:
        return None
    positions = album.get("positions") or {}
    choice = None
    for message in conversation.messages.filter(sender="customer", created_at__gte=album["sent_at"]).order_by("created_at", "id"):
        text = " ".join((message.text or "").split())
        if not text or len(text) > ALBUM_CHOICE_MAX_LENGTH:
            continue
        matched = ALBUM_CHOICE_PATTERN.match(text)
        if not matched:
            continue
        value = int(matched.group(1))
        if value in positions:
            choice = value
    return choice


def stale_album_position_item(conversation, catalog_id):
    """AI eskirgan albomning raqamlanishi bilan mahsulot tanlab qo'yganmi.

    Real suhbat 3051: reklama rasmiga bitta albom ketdi (1 Alfalob, 2 Jumila),
    o'n sakkiz soniyadan keyin reel bo'yicha to'qqiz rasmli ikkinchi albom
    ketdi (1 Jumila, 2 Alfalob). AI javobida birinchi albomning tartibini
    qaytardi, mijoz esa ekranidagi ikkinchi albomdan "2" deb yozdi — leadga
    Alfalob o'rniga Jumila yozilib operatorga boshqa gulning rasmi ketdi.
    Ikkalasi ham 199 000 bo'lgani uchun narx tuzatgichi buni ushlay olmadi.

    Faqat shu aniq iz bo'yicha tuzatiladi: mijoz yozgan raqam eski albomda
    aynan AI yozgan mahsulotga to'g'ri kelsa. Boshqa hollarda hech narsa
    o'zgartirilmaydi — "1 yoki 2?" degan oddiy savolning javobi albom raqami
    deb o'qilib qolmasligi kerak.
    """
    maps = album_position_maps(conversation)
    if len(maps) < 2:
        return None
    latest = maps[-1]
    choice = customer_album_choice(conversation, latest)
    if choice is None:
        return None
    current_id = (latest.get("positions") or {}).get(choice)
    if not current_id or current_id == catalog_id:
        return None
    for older in maps[:-1]:
        if (older.get("positions") or {}).get(choice) == catalog_id:
            return album_catalog_item(current_id)
    return None


def correct_lead_row_by_album_position(rows, conversation):
    """Eskirgan albom raqami bilan yozilgan qatorni oxirgi albomga qaytaradi."""
    if len(rows) != 1 or conversation is None:
        return rows
    row = rows[0]
    if int(row.get("quantity") or 1) != 1 or not row.get("ai_catalog_item"):
        return rows
    item = stale_album_position_item(conversation, row["ai_catalog_item"])
    if not item:
        return rows
    print("LEAD_CATALOG_CORRECTED_BY_POSITION from=%s to=%s" % (row.get("ai_catalog_item"), item.id), flush=True)
    return [{"catalog_name": item.name, "quantity": 1, "ai_catalog_item": item.id, "price": str(item.price)}]


def ai_catalog_lead_rows(arguments, conversation=None, estimated_price=None):
    """AI katalogidan tanlangan mahsulotlar leadga izoh sifatida yoziladi.

    AI ko'radigan katalog — alohida `AICatalogItem` ro'yxati, sklad katalogi emas.
    Shuning uchun `LeadCatalogUsage` ochilmaydi: u sklad mahsulotiga bog'lanadi va
    haqiqiy mahsulotni operator o'zi tanlaydi.

    Mahsulot uch bosqichda aniqlanadi: aniq catalog_id, shu suhbatda yuborilgan
    albomdagi aynan shu nom, va oxirgi chora sifatida umumiy nom qidiruvi.
    """
    rows = []
    asked_names = []
    for row in arguments.get("catalog_items") or []:
        quantity = int(row.get("quantity") or 1)
        if quantity <= 0:
            continue
        asked_name = str(row.get("catalog_name") or "").strip()
        item = AICatalogItem.objects.filter(id=row.get("catalog_id"), is_active=True).first() if row.get("catalog_id") else None
        if not item:
            item = album_item_by_name(conversation, asked_name)
        if not item and estimated_price not in (None, ""):
            item = album_item_by_price(conversation, estimated_price, asked_name)
        if not item:
            item = _catalog_item_for_ai(asked_name)
        rows.append({
            "catalog_name": item.name if item else asked_name[:180],
            "quantity": quantity,
            "ai_catalog_item": item.id if item else None,
            "price": str(item.price) if item else "",
        })
        asked_names.append(asked_name)
    # Raqam narxdan ishonchli: mijoz albomdagi o'rinni ko'rsatadi, narx esa
    # bir xil bo'lishi mumkin. Shuning uchun avval raqam bo'yicha tuzatiladi.
    corrected = correct_lead_row_by_album_position(rows, conversation)
    if corrected is not rows:
        return corrected
    return correct_lead_row_by_price(rows, conversation, estimated_price, asked_names)


def correct_lead_row_by_price(rows, conversation, estimated_price, asked_names=()):
    """AI aytgan narx bilan yozilgan mahsulot narxi ziddiyatda bo'lsa tuzatadi.

    Mijozga 199 000 deb aytib, leadga 1 000 000 lik qatorni yozib qo'yish
    operatorga boshqa gulning rasmini yuboradi. Narx — eng ishonchli tekshiruv:
    AI javobida aytgan summa albomdagi bitta mahsulotga to'g'ri kelsa, o'sha
    mahsulot olinadi.
    """
    if len(rows) != 1 or estimated_price in (None, ""):
        return rows
    row = rows[0]
    if int(row.get("quantity") or 1) != 1:
        return rows
    try:
        asked = Decimal(str(estimated_price))
    except (TypeError, ValueError, ArithmeticError):
        return rows
    if row.get("price") and Decimal(row["price"]) == asked:
        return rows
    # AI aytgan nom eng ishonchli dalil — hal qilingan qatorning nomi emas.
    asked_name = (list(asked_names) or [""])[0] or row.get("catalog_name") or ""
    item = album_item_by_price(conversation, asked, asked_name)
    if not item or item.id == row.get("ai_catalog_item"):
        return rows
    print("LEAD_CATALOG_CORRECTED_BY_PRICE from=%s to=%s price=%s" % (row.get("ai_catalog_item"), item.id, asked), flush=True)
    return [{"catalog_name": item.name, "quantity": 1, "ai_catalog_item": item.id, "price": str(item.price)}]


def lead_request_details(arguments):
    """Operator ko'radigan qo'shimcha izoh maydonlari. AI tool argumentlaridan olinadi."""
    topic = (arguments.get("topic") or "").strip()
    return {
        "topic": topic if topic in LEAD_TOPIC_LABELS else "",
        "flowers_text": (arguments.get("flowers_text") or "").strip()[:400],
        "size_text": (arguments.get("size_text") or "").strip()[:200],
        "photo_urls": customer_photo_urls(arguments.get("photo_urls")),
    }


def customer_photo_urls(values):
    """Mijoz yuborgan rasm havolalari. Rasm serverga saqlanmaydi, faqat havola yoziladi."""
    urls = []
    for value in values or []:
        url = str(value or "").strip()
        if url.startswith("http") and url not in urls:
            urls.append(url[:500])
    return urls[:MAX_LEAD_PHOTO_URLS]


def operator_chat_url(conversation):
    template = settings.FRONTEND_CHAT_URL or "https://euroflowers.cognilabs.org/chat?conversation_id={conversation_id}"
    if "{conversation_id}" in template:
        return template.format(conversation_id=conversation.id)
    separator = "&" if "?" in template else "?"
    return f"{template}{separator}conversation_id={conversation.id}"


def lead_fulfillment_line(lead):
    if lead.fulfillment == "delivery":
        address = lead.delivery_address or "manzil aytilmagan"
        return f"🚚 Yetkazib berish — {address}"
    if lead.fulfillment == "pickup":
        return "🏬 O'zi kelib olib ketadi"
    return ""


def lead_when_line(lead):
    parts = [value for value in [lead.desired_date.isoformat() if lead.desired_date else "", lead.desired_time] if value]
    return f"📅 {' · '.join(parts)}" if parts else ""


def lead_catalog_lines(lead):
    """Mijoz tanlagan AI katalog mahsulotlari va narxi.

    Katalog izohi bu yerga kirmaydi: u ichki yozuv va operatorlar guruhidagi
    xabarni uzaytirib yuboradi, operator uni CRM da baribir ko'radi.
    """
    rows = []
    for row in (lead.details or {}).get("catalog_items") or []:
        item = AICatalogItem.objects.filter(id=row.get("ai_catalog_item")).first()
        name = row.get("catalog_name") or (item.name if item else "")
        if not name:
            continue
        quantity = int(row.get("quantity") or 1)
        title = f"{name} × {quantity}" if quantity > 1 else name
        rows.append({
            "text": title,
            "image_url": item.image_url if item and item.image_url else "",
        })
    return rows


def lead_operator_media(lead, conversation):
    """Operator ko'radigan rasmlar: tanlangan katalog rasmi va mijoz yuborgan media."""
    urls = []
    for row in lead_catalog_lines(lead):
        if row["image_url"] and row["image_url"] not in urls:
            urls.append(row["image_url"])
    for row in customer_attachment_rows(conversation.messages.order_by("created_at", "id")):
        url = row.get("url")
        if url and url not in urls and row.get("kind") != "ad":
            urls.append(url)
    return [{"kind": "photo", "url": url} for url in urls[:MAX_OPERATOR_HANDOFF_MEDIA]]


def operator_lead_rich_message(lead, conversation):
    """Telegram operatorlar guruhiga ketadigan «Yangi lead» xabari."""
    customer = conversation.customer
    platform = "Telegram" if customer.instagram_user_id.startswith("telegram:") else "Instagram"
    username = f" · @{customer.instagram_username}" if customer.instagram_username else ""
    catalog_rows = lead_catalog_lines(lead)
    details = lead.details or {}
    media_items = []
    blocks = []
    for index, row in enumerate(lead_operator_media(lead, conversation), start=1):
        media_id = f"lead_{index}"
        blocks.append(f'<img src="tg://photo?id={media_id}"/>')
        media_items.append({"id": media_id, "media": {"type": "photo", "media": row["url"]}})
    html = []
    if blocks:
        html.append("<tg-slideshow>")
        html.extend(blocks)
        html.append("</tg-slideshow>")
    html.append(f"<h3>🌸 Yangi lead #{lead.id}</h3>")
    html.append(f"<p>👤 {escape(customer.name or 'Ism yozilmagan')}<br/>📞 {escape(customer.phone or 'raqam berilmagan')}<br/>📍 {escape(platform + username)}</p>")
    if catalog_rows:
        html.append("<p>🛍 Tanlagan mahsuloti</p><ul>")
        for row in catalog_rows:
            html.append(f'<li>{escape(row["text"])}</li>')
        html.append("</ul>")
    if details.get("flowers_text") or details.get("size_text"):
        wanted = " · ".join(value for value in [details.get("flowers_text"), details.get("size_text")] if value)
        html.append(f"<p>🌷 So'ragan guli<br/>{escape(wanted)}</p>")
    extra = [line for line in [lead_fulfillment_line(lead), lead_when_line(lead)] if line]
    if lead.estimated_price is not None:
        extra.append(f"💰 Taxminan {money_uz(lead.estimated_price)} so'm")
    if extra:
        html.append("<p>" + "<br/>".join(escape(line) for line in extra) + "</p>")
    if lead.request_uz:
        html.append(f"<p>🧠 So'rov<br/>{escape(lead.request_uz[:1200])}</p>")
    # Media havolalar ro'yxati yozilmaydi: rasmlar yuqorida slideshow bo'lib
    # ketadi, uzun signed CDN havolalari esa xabarni o'qishga xalaqit qiladi va
    # bir necha soatdan keyin baribir ochilmaydi.
    return {"html": "\n".join(html), "media": media_items}


def operator_lead_plain_message(lead, conversation):
    """Rich xabar o'tmasa yuboriladigan oddiy matn."""
    customer = conversation.customer
    lines = [f"🌸 Yangi lead #{lead.id}", "", f"👤 {customer.name or 'Ism yozilmagan'}", f"📞 {customer.phone or 'raqam berilmagan'}"]
    for row in lead_catalog_lines(lead):
        lines.append(f"🛍 {row['text']}")
    for line in [lead_fulfillment_line(lead), lead_when_line(lead)]:
        if line:
            lines.append(line)
    if lead.request_uz:
        lines.extend(["", lead.request_uz[:1200]])
    return "\n".join(lines)


def notify_operators_about_lead(lead, conversation):
    """Yangi leadni operatorlar Telegram guruhiga yuboradi.

    Bu AI javobiga tegmaydi — lead bazaga yozilgach ishlaydigan yetkazish qadami,
    xuddi ichki Notification kabi. Xatolik bo'lsa lead baribir saqlanib qoladi.
    """
    token = settings.AI_OPERATOR_HANDOFF_BOT_TOKEN
    chat_id = settings.AI_OPERATOR_HANDOFF_GROUP_ID
    if not token or not chat_id:
        return {"ok": False, "detail": "operator_group_not_configured"}
    reply_markup = {"inline_keyboard": [[{"text": "CRM chatni ochish", "url": operator_chat_url(conversation)}]]}
    # To'lov holati keyin shu xabarga qo'shiladi. Asl matn saqlanmasa tahrir
    # lead ma'lumotlarini o'chirib, o'rniga faqat to'lov qatorini yozib qo'yadi.
    plain_body = operator_lead_plain_message(lead, conversation)
    try:
        sent = telegram_send_rich_message_with(token, chat_id, operator_lead_rich_message(lead, conversation), reply_markup=reply_markup, message_thread_id=settings.AI_OPERATOR_HANDOFF_THREAD_ID)
    except Exception as error:
        print(f"AI_LEAD_RICH_NOTIFY_FAILED lead={lead.id} error={error}", flush=True)
        try:
            sent = telegram_send_with(token, chat_id, plain_body, reply_markup=reply_markup, message_thread_id=settings.AI_OPERATOR_HANDOFF_THREAD_ID)
        except Exception as fallback_error:
            print(f"AI_LEAD_NOTIFY_FAILED lead={lead.id} error={fallback_error}", flush=True)
            return {"ok": False, "detail": "telegram_send_failed"}
    Message.objects.create(conversation=conversation, sender="system", text="", metadata={"operator_lead_notified": {"lead_id": lead.id, "telegram_result": sent}})
    # Keyin to'lov holatini shu xabarga qo'shish uchun id si eslab qolinadi.
    from . import payment_services

    payment_services.remember_operator_message(lead, sent, body=plain_body, keyboard=reply_markup)
    return {"ok": True, "lead_id": lead.id}


def operator_needed_message(conversation, reason=""):
    """Operatorlar guruhiga ketadigan qisqa chaqiruv."""
    customer = conversation.customer
    platform = "Telegram" if customer.instagram_user_id.startswith("telegram:") else "Instagram"
    lines = ["🙋 Operator kerak", ""]
    lines.append(f"👤 {customer.name or 'Ism yozilmagan'}")
    if customer.phone:
        lines.append(f"📞 {customer.phone}")
    if customer.instagram_username:
        lines.append(f"📷 {platform} · @{customer.instagram_username}")
    else:
        lines.append(f"📍 {platform}")
    last = conversation.messages.filter(sender="customer").order_by("-created_at", "-id").first()
    if last and (last.text or "").strip():
        lines.append(f"💬 {(last.text or '').strip()[:220]}")
    if reason:
        lines.append(f"🧠 {reason[:220]}")
    return "\n".join(lines)


def notify_operator_needed(conversation, reason=""):
    """AI javob berolmadi — operatorlar guruhiga chat havolasi bilan xabar ketadi.

    Mijozga username berilmaydi, shuning uchun operatorning o'zi chatga kirib
    yozishi kerak. Xabar ostidagi tugma to'g'ri o'sha chatni ochadi.
    """
    token = settings.AI_OPERATOR_HANDOFF_BOT_TOKEN
    chat_id = settings.AI_OPERATOR_HANDOFF_GROUP_ID
    if not token or not chat_id:
        return {"ok": False, "detail": "operator_group_not_configured"}
    keyboard = {"inline_keyboard": [[{"text": "CRM chatni ochish", "url": operator_chat_url(conversation)}]]}
    try:
        sent = telegram_send_with(token, chat_id, operator_needed_message(conversation, reason),
                                  reply_markup=keyboard,
                                  message_thread_id=settings.AI_OPERATOR_HANDOFF_THREAD_ID)
    except Exception as error:
        print(f"OPERATOR_NEEDED_NOTIFY_FAILED conversation={conversation.id} error={error}", flush=True)
        return {"ok": False, "detail": "send_failed"}
    Message.objects.create(conversation=conversation, sender="system", text="",
                           metadata={"operator_needed": {"reason": reason, "telegram_result": sent}})
    return {"ok": bool(sent.get("ok")), "detail": ""}


def latest_customer_media_attachment(conversation, source_url=""):
    attachments = customer_attachment_rows(conversation.messages.order_by("created_at", "id"))
    if source_url:
        for row in reversed(attachments):
            if row.get("url") == source_url:
                return row
        return {"kind": "media", "url": source_url}
    return attachments[-1] if attachments else None


def ai_catalog_match_items(limit=MAX_AI_CATALOG_MATCH_CANDIDATES):
    return list(available_ai_catalog_queryset().exclude(image_url="").order_by("-created_at", "-id")[:limit])


def catalog_match_row(item, score=None, fingerprint=None, verdict="", differences="", reason=""):
    """AI ga qaytariladigan bitta katalog qatori. Narx va izoh ham shu yerda."""
    row = {
        "catalog_id": item.id,
        "name": item.name,
        "type": item.arrangement_type,
        "quantity": item.quantity,
        "volume": item.volume,
        "price": str(item.price),
        "price_text": f"{money_uz(item.price)} so'm",
        "name_ru": catalog_name_ru(item.name),
        "price_text_ru": money_text(item.price, "ru"),
        "note_uz": item.note,
        "has_image": bool(item.image_url),
    }
    if score is not None:
        row["score"] = score
    if fingerprint:
        row["looks_like"] = fingerprint.get("summary", "")
    if verdict:
        row["verdict"] = verdict
    if differences:
        row["differences"] = differences[:400]
    if reason:
        row["reason"] = reason[:400]
    return row


def media_url_match_key(url):
    text = (url or "").strip()
    if not text:
        return ""
    text = text.split("?", 1)[0].rstrip("/")
    return text.lower()


def vision_compatible_media_url(url, kind=""):
    text = f"{url or ''} {kind or ''}".lower()
    direct_extensions = (".jpg", ".jpeg", ".png", ".webp", ".gif")
    clean_url = (url or "").split("?", 1)[0].lower()
    return clean_url.endswith(direct_extensions) or "facebook.com/ads/image" in text or "lookaside.fbsbx.com/ig_messaging_cdn" in text or "photo" in text or "image" in text or "story" in text


def items_matching_link(items, link):
    """Katalogdagi instagram_link shu havola bilan bir xilmi."""
    key = media_url_match_key(link)
    if not key:
        return []
    matched = []
    for item in items:
        catalog_key = media_url_match_key(item.instagram_link)
        if not catalog_key:
            continue
        if catalog_key == key or catalog_key in key or key in catalog_key:
            matched.append(item)
    return matched


def items_matching_ads(items, ad_id="", post_id=""):
    ad_id = str(ad_id or "").strip()
    post_id = str(post_id or "").strip()
    if not ad_id and not post_id:
        return []
    matched = []
    for item in items:
        if ad_id and str(item.instagram_ad_id or "").strip() == ad_id:
            matched.append(item)
            continue
        if post_id and str(item.instagram_ad_post_id or "").strip() == post_id:
            matched.append(item)
    return matched


def ads_context_from_conversation(conversation, source_url=""):
    for message in conversation.messages.filter(sender="customer").order_by("-created_at", "-id")[:20]:
        metadata = message.metadata or {}
        attachments = metadata.get("attachments") or []
        if source_url and not any(row.get("url") == source_url for row in attachments):
            continue
        referral = metadata.get("instagram_referral") or {}
        ads_context = referral.get("ads_context_data") or {}
        ad_id = metadata.get("instagram_ad_id") or referral.get("ad_id") or ""
        post_id = metadata.get("instagram_ad_post_id") or ads_context.get("post_id") or referral.get("post_id") or ""
        if ad_id or post_id:
            return {"ad_id": str(ad_id or ""), "post_id": str(post_id or "")}
    return {"ad_id": "", "post_id": ""}


def customer_shared_our_post(attachment):
    """Mijoz yuborgani bizning o'z story/postimizmi.

    Storyga javob yozgan yoki uni directga tashlagan mijoz o'sha storydagi gulni
    so'rayapti — bu tasodifiy o'xshashlik emas.
    """
    kind = (attachment or {}).get("kind", "").lower()
    if kind in {"story", "reel", "post", "ig_story", "ig_reel", "share", "media_share"}:
        return True
    return bool(social_post_permalink_for_media(attachment))


def shared_link_is_the_media(attachment, media_url):
    """Mijoz yuborgani rasm emas, postning o'zi (reel/story share) bo'lsa.

    Bunday yuborishda tahlil qiladigan rasm yo'q — havolaning o'zi javob beradi.
    """
    kind = (attachment or {}).get("kind", "").lower()
    if kind in {"reel", "video", "post", "share", "ig_reel", "media_share"}:
        return True
    return "instagram.com/" in (media_url or "").lower()


# Ulashilgan post uchun eslab qolinadigan jadval. Kalitlar: media raqami va
# izohning normallashtirilgan ko'rinishi, qiymat — permalink.
SHARED_POST_MEMORY_KEY = "shared_post_permalinks"
SHARED_POST_MEMORY_LIMIT = 400
# O'z postlarimiz ro'yxati shu muddat ichida qayta so'ralmaydi. Har xabarda
# Graph API ga chiqish javobni sekinlashtiradi va kvotani yeydi.
OWN_MEDIA_CACHE_SECONDS = 3600
OWN_MEDIA_CACHE_KEY = "own_media_index"
OWN_MEDIA_LOOKUP_KEY = "own_media_lookups"
OWN_MEDIA_LOOKUP_LIMIT = 2000
OWN_MEDIA_FOREIGN_CACHE_SECONDS = 24 * 3600


def shared_caption_key(caption):
    """Izohni solishtirish uchun normallashtirilgan kalit.

    Bir xil post reel va post ko'rinishida kelganda izoh aynan bir xil bo'ladi,
    faqat bo'shliqlar va registr farq qilishi mumkin.
    """
    text = re.sub(r"\s+", " ", (caption or "")).strip().lower()
    return text[:400]


def integration_extra():
    integration, _ = IntegrationSettings.objects.get_or_create(pk=1)
    return integration, dict(integration.extra or {})


def remember_shared_post_permalink(attachment):
    """Permalink bilan kelgan ulashuvni media raqami va izohi bo'yicha eslab qoladi.

    Real holat: bir xil post ikki xil kelib turadi. `ig_reel` ulashuvida
    permalink bor, `ig_post` ulashuvida esa faqat lookaside CDN havolasi —
    o'shanda katalog mosligi ishlamay butun katalog ketardi. Ikkalasining
    izohi AYNAN bir xil, shuning uchun bir marta ko'rgan postni keyin
    tanib olamiz.
    """
    url = (attachment or {}).get("url") or ""
    if "instagram.com/" not in url:
        return
    remember_shared_post_keys(attachment, url)


def shared_post_memory_keys(attachment):
    keys = []
    media_id = (attachment or {}).get("media_id") or ""
    if media_id:
        keys.append("media:%s" % media_id)
    caption_key = shared_caption_key((attachment or {}).get("caption"))
    if caption_key:
        keys.append("caption:%s" % caption_key)
    return keys


def remembered_shared_post_permalink(attachment):
    keys = shared_post_memory_keys(attachment)
    if not keys:
        return ""
    _, extra = integration_extra()
    table = extra.get(SHARED_POST_MEMORY_KEY) or {}
    for key in keys:
        url = table.get(key)
        if url:
            return url
    return ""


def own_media_index(force=False):
    """O'z postlarimizning media raqami va izohi -> permalink jadvali.

    Graph API faqat o'z akkauntlarimizning postlarini beradi, shuning uchun bu
    jadvalda topilgan post aniq bizning profilimizdan. Jadval bir soat keshda
    turadi.
    """
    integration, extra = integration_extra()
    cache = extra.get(OWN_MEDIA_CACHE_KEY) or {}
    fetched_at = cache.get("fetched_at") or 0
    now = int(timezone.now().timestamp())
    if not force and cache.get("by_media") and now - int(fetched_at) < OWN_MEDIA_CACHE_SECONDS:
        return cache
    from .platform_services import instagram_recent_media

    try:
        rows = instagram_recent_media()
    except Exception as error:
        print(f"OWN_MEDIA_INDEX_FAILED error={error}", flush=True)
        return cache
    if not rows:
        return cache
    by_media = {}
    by_caption = {}
    for row in rows:
        permalink = (row.get("permalink") or "").strip()
        if not permalink:
            continue
        media_id = str(row.get("id") or "").strip()
        if media_id:
            by_media[media_id] = permalink
        caption_key = shared_caption_key(row.get("caption"))
        # Bir xil izohli ikki post bo'lishi mumkin. Katalogga bog'langani
        # afzal — mijoz aynan o'shani so'rayapti.
        if caption_key and (caption_key not in by_caption or ai_catalog_link_count(permalink) > ai_catalog_link_count(by_caption[caption_key])):
            by_caption[caption_key] = permalink
    cache = {"fetched_at": now, "by_media": by_media, "by_caption": by_caption}
    extra[OWN_MEDIA_CACHE_KEY] = cache
    integration.extra = extra
    integration.save(update_fields=["extra", "updated_at"])
    return cache


def own_media_lookup(media_id):
    """Shu media bizning profilimizdanmi — Instagram dan bevosita so'rab.

    `own_media_index()` postlar ro'yxatiga tayanadi, ro'yxat esa to'liq emas.
    Real o'lchov: jim qolgan 69 ta noyob mediadan 67 tasi rostdan begona
    profilniki edi, 2 tasi bizniki — lekin o'sha 2 tasi 158 ta suhbatda
    ulashilgan. Mijozlar eng ko'p yuboradigan reklama postimiz aynan shu.

    Javob keshlanadi: bizniki degan javob abadiy (post egasi o'zgarmaydi),
    begona degani bir kunga. Tarmoq xatosi umuman keshlanmaydi.
    """
    media_id = str(media_id or "").strip()
    if not media_id:
        return {}
    integration, extra = integration_extra()
    cache = dict(extra.get(OWN_MEDIA_LOOKUP_KEY) or {})
    row = cache.get(media_id)
    now = int(timezone.now().timestamp())
    if isinstance(row, dict):
        if row.get("ours"):
            return row
        if now - int(row.get("checked_at") or 0) < OWN_MEDIA_FOREIGN_CACHE_SECONDS:
            return row
    from .platform_services import instagram_media_row

    media, verdict = instagram_media_row(media_id)
    if verdict == "unknown":
        return row if isinstance(row, dict) else {}
    result = {"ours": verdict == "ours", "checked_at": now,
              "permalink": (media or {}).get("permalink") or "",
              "caption": ((media or {}).get("caption") or "")[:400]}
    if result["ours"]:
        print(f"OWN_MEDIA_CONFIRMED media={media_id} permalink={result['permalink']}", flush=True)
    cache[media_id] = result
    if len(cache) > OWN_MEDIA_LOOKUP_LIMIT:
        for key in sorted(cache, key=lambda value: int((cache[value] or {}).get("checked_at") or 0))[:len(cache) - OWN_MEDIA_LOOKUP_LIMIT]:
            cache.pop(key, None)
    extra[OWN_MEDIA_LOOKUP_KEY] = cache
    integration.extra = extra
    integration.save(update_fields=["extra", "updated_at"])
    return result


def ai_catalog_link_count(permalink):
    if not permalink:
        return 0
    return len(items_matching_link(list(available_ai_catalog_queryset()), permalink))


def own_post_permalink_for_shared_media(attachment):
    """Ulashilgan post/reel bizning profilimizdanmi va permalinki nima.

    Tartib qat'iy: permalink -> media raqami -> izoh. Permalink kelgan bo'lsa
    havolaning o'zi bilan solishtiriladi, izoh faqat permalink umuman
    kelmaganda ishlatiladi.
    """
    url = (attachment or {}).get("url") or ""
    if "instagram.com/" in url:
        return url
    remembered = remembered_shared_post_permalink(attachment)
    if remembered:
        return remembered
    media_id = (attachment or {}).get("media_id") or ""
    caption_key = shared_caption_key((attachment or {}).get("caption"))
    if not media_id and not caption_key:
        return ""
    index = own_media_index()
    permalink = (index.get("by_media") or {}).get(media_id) if media_id else ""
    if not permalink and caption_key:
        permalink = (index.get("by_caption") or {}).get(caption_key) or ""
    if not permalink and media_id:
        # Ro'yxatda yo'q post ham bizniki bo'lishi mumkin: media raqamini
        # bevosita so'rasak Instagram permalinkni o'zi qaytaradi.
        permalink = own_media_lookup(media_id).get("permalink") or ""
    if permalink:
        print(f"SHARED_POST_RESOLVED media={media_id or '-'} permalink={permalink}", flush=True)
        # CDN ulashuvining media raqami ham eslab qolinadi: shu postning
        # izohsiz kelgan ko'rinishi keyin faqat shu raqam bilan topiladi.
        remember_shared_post_keys(attachment, permalink)
    return permalink or ""


def remember_shared_post_keys(attachment, permalink):
    keys = shared_post_memory_keys(attachment)
    if not keys or not permalink:
        return
    integration, extra = integration_extra()
    table = dict(extra.get(SHARED_POST_MEMORY_KEY) or {})
    changed = False
    for key in keys:
        if table.get(key) != permalink:
            table[key] = permalink
            changed = True
    if not changed:
        return
    if len(table) > SHARED_POST_MEMORY_LIMIT:
        table = dict(list(table.items())[-SHARED_POST_MEMORY_LIMIT:])
    extra[SHARED_POST_MEMORY_KEY] = table
    integration.extra = extra
    integration.save(update_fields=["extra", "updated_at"])


def resolved_shared_media_attachment(attachment):
    """Ulashuvni permalink bilan boyitilgan ko'rinishda qaytaradi."""
    permalink = own_post_permalink_for_shared_media(attachment)
    if not permalink or permalink == (attachment or {}).get("url"):
        return attachment, ""
    return dict(attachment or {}, resolved_permalink=permalink), permalink


def social_post_for_media(attachment):
    """Mijoz yuborgan story/reel qaysi bizning postimiz ekanini bazadan topadi.

    Instagram direct'da kelgan story rasm CDN havolasi bo'lib keladi, ichida
    faqat asset_id turadi. O'sha asset_id bo'yicha SocialPost topiladi.
    """
    from .webhook_services import social_post_by_media_or_url

    url = (attachment or {}).get("url") or ""
    if not url:
        return None
    try:
        return social_post_by_media_or_url(url=url)
    except Exception as error:
        print(f"AI_CATALOG_SOCIAL_POST_LOOKUP_FAILED url={url[:80]} error={error}", flush=True)
        return None


def social_post_permalink_for_media(attachment):
    """Post topilsa uning permalink'i — katalogdagi instagram_link bilan solishtirish uchun."""
    post = social_post_for_media(attachment)
    return (post.permalink or post.webhook_story_url or "") if post else ""


def social_post_answer(post):
    """Storyning o'zida nomi va narxi yozilgan bo'lsa, javob shu yerda.

    Operator storyni tizimga qo'yganda nomi, narxi va tavsifini yozadi. Mijoz
    o'sha storyni directga tashlaganda taxmin qilishning hojati yo'q — rasmni
    tahlil qilish faqat noaniqlik qo'shadi. Chatda aynan shunday bo'lgan:
    story "Alfalob 200 tali, 1 600 000" edi, rasm tahlili esa 100 talik
    boshqa mahsulotni topib bergan.
    """
    if not post or not post.is_active:
        return None
    title = (post.title_uz or "").strip()
    price = post.price
    if not title or price in (None, ""):
        return None
    return {
        "social_post_id": post.id,
        "title": title,
        "description": (post.description_uz or "").strip()[:400],
        "price": str(price),
        "price_text": f"{money_uz(price)} so'm",
        "title_ru": catalog_name_ru(title),
        "price_text_ru": money_text(price, "ru"),
        "flower_count": post.flower_count or None,
        "has_image": bool(post.image_url),
    }


def conversation_shared_links(conversation, unanswered_only=False):
    """Suhbatda mijoz yuborgan story/reel havolalari, oxirgisi birinchi bo'lib.

    Mijoz avval reel yuborib, keyin o'sha reeldan screenshot tashlashi mumkin.
    Screenshot'ning o'z havolasi yo'q, lekin reel hali ham suhbatda turadi.

    unanswered_only — faqat oxirgi AI javobidan keyin yuborilgan havolalar.
    Biz o'sha reel haqida allaqachon javob bergan bo'lsak, mijozning keyingi
    rasmi yangi savol: uni eski havola bilan javoblash xato bo'ladi.
    """
    queryset = conversation.messages.filter(sender="customer")
    if unanswered_only:
        last_ai = conversation.messages.filter(sender="ai").order_by("-created_at", "-id").first()
        if last_ai:
            queryset = queryset.filter(created_at__gt=last_ai.created_at)
    links = []
    for message in queryset.order_by("-created_at", "-id")[:30]:
        for attachment in (message.metadata or {}).get("attachments") or []:
            url = attachment.get("url") or ""
            if url and url not in links:
                links.append(url)
    return links


def direct_ai_catalog_link_matches(items, source_url, attachment=None, conversation=None):
    """Mijoz yuborgan story/post linki katalogdagi link bilan aynan mos kelsa.

    Bunda rasmni tahlil qilish shart emas — link o'zi aniq javob. Havola uch
    joydan qidiriladi: yuborilgan URL'ning o'zidan, o'sha media bog'langan
    SocialPost'ning permalink'idan, va suhbatda oldinroq yuborilgan story/reel'dan.
    """
    matched = items_matching_link(items, source_url)
    if matched:
        return matched
    permalink = social_post_permalink_for_media(attachment or {"url": source_url})
    if permalink:
        matched = items_matching_link(items, permalink)
        if matched:
            return matched
    if conversation is None:
        return []
    # Mijozning o'z rasmi uchun faqat HALI JAVOB BERILMAGAN havolalar qaraladi.
    # Reel yuborib, darhol o'sha reelning skrinshotini tashlash — bitta savol,
    # havola ishlaydi. Reel haqida javob berib bo'lgach kelgan rasm esa yangi
    # savol: uni eski reel bilan javoblasak "siz yuborgan reeldan borlari
    # shular" deb noto'g'ri javob chiqadi va rasm umuman tahlil qilinmaydi.
    photo = (attachment or {}).get("kind") == "photo"
    for link in conversation_shared_links(conversation, unanswered_only=photo):
        if media_url_match_key(link) == media_url_match_key(source_url):
            continue
        matched = items_matching_link(items, link)
        if matched:
            return matched
        permalink = social_post_permalink_for_media({"url": link})
        if permalink:
            matched = items_matching_link(items, permalink)
            if matched:
                return matched
    return []


# Mijoz Instagram reel/post havolasini directga tashlaganda kelgan URL shu ko'rinishda
# bo'ladi. Mijozning o'z rasmi hech qachon bunday emas — u lookaside.fbsbx.com yoki
# facebook.com/ads/image bo'lib keladi. Shuning uchun bu naqsh faqat havola
# ulashishni ushlaydi va rasm tahlili yo'liga tegmaydi.
SHARED_LINK_PATTERN = re.compile(r"instagram\.com/(reel|reels|p|tv)/", re.IGNORECASE)

# Tizimda yo'q reel kelganda javob berilmaydi. Ketma-ket yozilgan xabar ham shu
# jimlikka kiradi, lekin faqat shu oyna ichida: mijoz bir soatdan keyin "gul kerak"
# deb yozsa javobsiz qolib ketmasligi kerak.
UNLINKED_MEDIA_SILENCE_WINDOW = timedelta(minutes=15)


def shared_media_attachments(message):
    """Xabardagi ulashilgan Instagram post/reel qatorlari."""
    rows = []
    for attachment in (message.metadata or {}).get("attachments", []) or []:
        if not shared_post_attachment(attachment):
            continue
        row = {"kind": attachment.get("kind") or "media", "url": attachment.get("url") or ""}
        for key in ("caption", "media_id"):
            if attachment.get(key):
                row[key] = attachment[key]
        rows.append(row)
    return rows


def shared_post_attachment(attachment):
    """Bu mijozning o'z rasmi emas, ulashilgan post/reel.

    Ikki dalil: havolaning o'zi instagram.com posti, yoki webhook payloadida
    ulashuvga xos media raqami kelgan (mijozning o'z rasmida u bo'lmaydi).
    """
    url = (attachment or {}).get("url") or ""
    if SHARED_LINK_PATTERN.search(url):
        return True
    return bool((attachment or {}).get("media_id"))


def shared_media_verdict(attachment, items=None):
    """Ulashilgan post/reel haqida yakuniy qaror.

    permalink — havolaning o'zi yoki eslab qolingan/Graph API dan topilgan
    permalink. ours — post bizning profilimizdan. catalog — o'sha postga
    bog'langan katalog mahsulotlari.

    Tartib qat'iy: permalink birinchi, keyin media raqami, oxirida izoh.
    """
    url = (attachment or {}).get("url") or ""
    if not url or not shared_post_attachment(attachment):
        return {"shared": False, "permalink": "", "ours": False, "catalog": []}
    catalog_items = ai_catalog_match_items() if items is None else items
    permalink = url if "instagram.com/" in url else own_post_permalink_for_shared_media(attachment)
    ours = bool(social_post_for_media(attachment))
    if permalink and not ours:
        key = media_url_match_key(permalink)
        index = own_media_index()
        ours = any(media_url_match_key(value) == key for value in (index.get("by_media") or {}).values())
    if not ours:
        # Ro'yxat to'liq emas — media raqamini Instagram dan bevosita so'raymiz.
        # Bu profil egaligining yagona ishonchli tekshiruvi.
        lookup = own_media_lookup((attachment or {}).get("media_id"))
        if lookup.get("ours"):
            ours = True
            permalink = permalink or lookup.get("permalink") or ""
    link_attachment = dict(attachment, url=permalink) if permalink and permalink != url else attachment
    # conversation berilmaydi: suhbatdagi boshqa havolalar bu postni tizimda bor
    # qilib ko'rsatmasligi kerak.
    matched = direct_ai_catalog_link_matches(catalog_items, permalink or url, attachment=link_attachment)
    if matched:
        ours = True
    return {"shared": True, "permalink": permalink, "ours": ours, "catalog": matched}


def shared_link_is_in_the_system(attachment, items=None):
    """Ulashilgan post haqida aytadigan gapimiz bormi.

    Bo'lmasa bu havola haqida hech narsa bilmaymiz: video tahlil qilinmaydi,
    nomi ham narxi ham noma'lum. Real suhbatlarda AI o'shanda butun katalogni
    yuborib operatorni chaqirardi — mijozga ham, operatorga ham foydasi yo'q.
    """
    verdict = shared_media_verdict(attachment, items=items)
    if not verdict["shared"]:
        return False
    return bool(verdict["ours"] or verdict["catalog"])


def unlinked_shared_link_in_message(message, items=None):
    for attachment in shared_media_attachments(message):
        if not shared_link_is_in_the_system(attachment, items=items):
            return attachment
    return None


def unlinked_shared_media_silence(conversation):
    """Tizimda yo'q reel/post kelgan bo'lsa AI javob bermaydi.

    Oxirgi AI javobidan keyingi mijoz xabarlari qaraladi — reel bilan birga yoki
    ketma-ket yozilgan matn ham shu jimlikka kiradi. Oyna
    UNLINKED_MEDIA_SILENCE_WINDOW bilan cheklangan, aks holda javob berilmagan
    reel suhbatni butunlay yopib qo'yardi.
    """
    messages = list(conversation.messages.exclude(sender="system").order_by("-created_at", "-id")[:20])
    pending = []
    for message in messages:
        if message.sender != "customer":
            break
        pending.append(message)
    if not pending:
        return None
    newest = pending[0].created_at
    items = None
    for message in pending:
        if newest - message.created_at > UNLINKED_MEDIA_SILENCE_WINDOW:
            break
        if items is None:
            items = ai_catalog_match_items()
        attachment = unlinked_shared_link_in_message(message, items=items)
        if attachment:
            return {"message_id": message.id, "url": attachment["url"], "kind": attachment["kind"]}
    return None


# Aynan mos kelmagan, lekin mijozga ko'rsatishga arziydigan mahsulot uchun eng past
# ball. Rangi butunlay boshqa bo'lgan mahsulot 45 dan oshmaydi (DIFFERENT_COLOUR_CEILING),
# lekin gul turi va idishi bir xil bo'lsa u ham ko'rsatishga arziydi: "binafsha savat
# yo'q, lekin savatlarimiz shular" — bu mijozni quruq qaytarishdan yaxshi.
SIMILAR_ENOUGH_SCORE = 42

# Bundan yuqori ball olgan nomzod haqida "bizda bunday gul yo'q" deb bo'lmaydi —
# u aniq o'sha mahsulot bo'lishi mumkin, faqat tekshiruvdan o'tmagan.
CLOSE_MATCH_SCORE = 70


def similar_enough_rows(rejected, source, limit=3):
    """Aynan o'shasi bo'lmasa, katalogdagi eng yaqin mahsulotlar.

    Model bu mahsulotlarni "different" deb belgilagan bo'lishi mumkin va u haq —
    biz ham mijozga aynan o'shasi emasligini aytamiz. Shuning uchun bu yerda
    modelning hukmi emas, fingerprint o'xshashligi hal qiladi.

    Gul turi va idishi mos kelishi shart: pushti savat so'ralganda qizil quti
    ko'rsatish o'xshashlik emas. Rangi boshqa bo'lishi mumkin — binafsha savat
    yo'q bo'lsa, savatlarimizni ko'rsatish mijozni quruq qaytarishdan yaxshi.
    """
    source_family = vision_services.container_family(source)
    rows = [
        row for row in rejected
        if row["score"] >= SIMILAR_ENOUGH_SCORE
        and vision_services.families_can_match(source_family, row["family"])
        and vision_services.forms_can_match(source.get("flower_form"), row["fingerprint"].get("flower_form"))
    ]
    return sorted(rows, key=lambda row: row["score"], reverse=True)[:limit]


def crop_would_help(conversation, source):
    """Kesilgan rasm so'rashning ma'nosi bormi.

    Faqat mijoz kadrdagi bitta gulni ko'rsatgan va kadrda boshqa gullar ham
    turgan holatda. Bitta gul turgan rasmni kesib berish hech narsani o'zgartirmaydi.
    Bir suhbatda bu iltimos bir marta qilinadi — mijoz kesa olmasa yoki kesilgani
    ham topilmasa, ikkinchi marta so'rash o'rniga operatorga uzatiladi.
    """
    if not source.get("region_requested"):
        return False
    if not source.get("multiple_products_visible") and len(source.get("visible_products") or []) < 2:
        return False
    asked = Message.objects.filter(
        conversation=conversation,
        sender="system",
        metadata__ai_catalog_media_match__detail="ask_for_crop",
    ).exists()
    return not asked


def media_match_result(conversation, payload):
    Message.objects.create(conversation=conversation, sender="system", text="", metadata={"ai_catalog_media_match": payload})
    return payload


def media_match_outcome(tool_results):
    """Shu navbatda media matching ishlaganmi va nima ruxsat etilgani.

    Ishonchli mos kelmasa allowed bo'sh bo'ladi va katalog rasmi yuborilmaydi.
    Production'da aynan shu holat noto'g'ri gul yuborilishiga sabab bo'lgan edi:
    model mos kelmagan bo'lsa ham ikkita katalog rasmini yuborib, ustidan
    "operatorlarimiz aniq javob berishadi" deb yozgan.
    """
    outcome = {"ran": False, "allow_send": False, "allow_group": False, "ask_for_crop": False, "allowed_ids": set(), "group_ids": set(), "candidate_ids": set()}
    for row in tool_results or []:
        if row.get("name") != "match_ai_catalog_by_media":
            continue
        output = row.get("output") or {}
        outcome["ran"] = True
        def ids(key):
            return {match.get("catalog_id") for match in (output.get(key) or []) if match.get("catalog_id")}
        allowed = ids("matches")
        group = ids("group_matches")
        if output.get("allow_send") and allowed:
            outcome["allow_send"] = True
            outcome["allowed_ids"] |= allowed
        if output.get("allow_group") and group:
            outcome["allow_group"] = True
            outcome["group_ids"] |= group
        if output.get("ask_for_crop"):
            outcome["ask_for_crop"] = True
        outcome["candidate_ids"] |= allowed | group | ids("near_matches")
    return outcome


def media_match_send_block(tool_results, name, catalog_ids):
    """Ishonchsiz media matchdan keyin katalog rasmi yuborilishini to'xtatadi.

    Bu AI ning matnini o'zgartirmaydi — faqat tool chaqiruvini rad etadi va nima
    qilish kerakligini javobda yozib beradi.
    """
    outcome = media_match_outcome(tool_results)
    if not outcome["ran"] or outcome["allow_send"]:
        return None
    if outcome["allow_group"]:
        # Bir nechta mahsulot bir xil ko'rinadi: albom bo'lib hammasi ketadi, lekin
        # bittasini ajratib "mana sizniki" deb yuborib bo'lmaydi.
        if name == "send_catalog_album" and set(catalog_ids or []) <= outcome["group_ids"]:
            return None
        return {"ok": False, "detail": "media_match_needs_a_group", "group_ids": sorted(outcome["group_ids"]), "instruction_uz": MEDIA_MATCH_GROUP_INSTRUCTION}
    if name == "send_catalog_image":
        blocked = True
    else:
        blocked = bool(set(catalog_ids or []) & outcome["candidate_ids"])
    if not blocked:
        return None
    if outcome["ask_for_crop"]:
        return {"ok": False, "detail": "media_match_needs_a_crop", "instruction_uz": MEDIA_MATCH_CROP_INSTRUCTION}
    return {
        "ok": False,
        "detail": "media_match_not_confident",
        "instruction_uz": MEDIA_MATCH_NOT_FOUND_INSTRUCTION,
    }


def whole_catalog_already_sent(conversation):
    """Butun katalog hozir, mijoz hech narsa yozmasdan turib yuborilganmi.

    Mahsulotlar sonini sanash bilan aniqlab bo'lmaydi — katalogda uchta mahsulot
    bo'lsa "butun katalog" ham uchta rasm. Shuning uchun albom yuborilganda
    uning butun katalog ekani o'sha yerda belgilab qo'yiladi.

    Ilgari bu tekshiruv butun suhbatga tegishli edi va albom bir marta ketgach
    mijoz "каталогни корсат" deb uch marta so'rasa ham qayta yuborilmasdi.
    Endi to'siq faqat oxirgi albomdan keyin mijoz hech narsa yozmagan holatda
    ishlaydi — ya'ni modelni bir turda ikki marta yuborishdan saqlaydi, mijozning
    o'z so'rovini esa bloklamaydi.
    """
    last_album_at = None
    for message in conversation.messages.filter(sender="system").order_by("-created_at", "-id")[:40]:
        result = (message.metadata or {}).get("catalog_album_result") or {}
        if result.get("whole_catalog") and any(row.get("delivered") for row in result.get("items") or []):
            last_album_at = message.created_at
            break
    if last_album_at is None:
        return False
    return not conversation.messages.filter(sender="customer", created_at__gt=last_album_at).exists()


def catalog_image_already_sent(conversation, catalog_id):
    """Shu katalog rasmi bu suhbatda allaqachon yuborilganmi."""
    if not catalog_id:
        return False
    for message in conversation.messages.filter(sender="system").order_by("-created_at", "-id")[:40]:
        result = (message.metadata or {}).get("image_tool_result") or {}
        if result.get("catalog_id") == catalog_id and result.get("delivered"):
            return True
        for row in ((message.metadata or {}).get("catalog_album_result") or {}).get("items") or []:
            if row.get("catalog_id") == catalog_id and row.get("delivered"):
                return True
    return False


def matched_story_ids(conversation, tool_results):
    """Suhbatda media matching qaysi storylarni topgan bo'lsa, o'shalarning id si.

    Mijoz storyni yuborib narxini bilgach, keyingi xabarda "rasmini ko'rsat"
    deydi — o'shanda shu navbatda match natijasi bo'lmaydi, lekin story hali
    ham suhbatning mavzusi. Shuning uchun oldingi navbatlar ham hisobga olinadi.
    """
    ids = set()
    for row in tool_results or []:
        if row.get("name") != "match_ai_catalog_by_media":
            continue
        story = (row.get("output") or {}).get("story") or {}
        if story.get("social_post_id"):
            ids.add(story["social_post_id"])
    for message in conversation.messages.filter(sender="system").order_by("-created_at", "-id")[:20]:
        story = ((message.metadata or {}).get("ai_catalog_media_match") or {}).get("story") or {}
        if story.get("social_post_id"):
            ids.add(story["social_post_id"])
    return ids


def send_social_post_image(conversation, social_post_id, tool_results=None):
    """Mijoz yuborgan storyning o'z rasmini qayta yuboradi.

    Faqat shu navbatda media matching topgan story uchun. Aks holda AI o'zi
    tanlagan boshqa postning rasmini yuborib qo'yishi mumkin edi.
    """
    allowed = matched_story_ids(conversation, tool_results)
    if not allowed:
        return {"ok": False, "detail": "no_matched_story", "instruction_uz": "Bu tool faqat match_ai_catalog_by_media own_story_matched qaytarganda ishlaydi."}
    if social_post_id not in allowed:
        return {"ok": False, "detail": "story_not_matched", "allowed_ids": sorted(allowed)}
    post = SocialPost.objects.filter(id=social_post_id, is_active=True).first()
    if not post or not post.image_url:
        return {"ok": False, "detail": "post_image_not_found"}
    delivered, detail, sent = send_image_to_customer(conversation.customer, post.image_url, conversation)
    if not delivered:
        return {"ok": False, "detail": detail or "send_failed"}
    Message.objects.create(conversation=conversation, sender="system", text="", metadata={"post_image_result": {"social_post_id": post.id, "image_url": post.image_url, "detail": detail, "sent": sent}})
    return {"ok": True, "image_sent": True, "social_post_id": post.id, "title": post.title_uz, "price_text": f"{money_uz(post.price)} so'm" if post.price else ""}


def first_confident_media_match(tool_results):
    for row in reversed(tool_results or []):
        if row.get("name") != "match_ai_catalog_by_media":
            continue
        output = row.get("output") or {}
        if not output.get("allow_send"):
            continue
        matches = output.get("matches") or []
        if len(matches) == 1:
            return matches[0]
    return None


def tool_results_sent_catalog(tool_results, catalog_id):
    for row in tool_results or []:
        if row.get("name") == "send_catalog_image" and (row.get("output") or {}).get("catalog_id") == catalog_id and (row.get("output") or {}).get("ok"):
            return True
    return False


# "Albomni yubordim", "rasmini yubordim" — javob rasm yuborilgan deb turgan
# so'zlar. Mijozdan yuborishni SO'RAGAN gaplar ("yuborsangiz", "yuboring") bu
# yerga tushmaydi: fe'l birinchi shaxsda bo'lishi shart.
PHOTO_CLAIM_WORDS = re.compile(
    r"albom|альбом|rasm|расм|surat|сурат|фото|katalog|каталог", re.IGNORECASE)
# Kelasi zamon ham hisobga olinadi. Real suhbat 2141: "Hozir katalogni qayta
# yuboraman" deb yozdi va hech narsa yubormadi — o'sha va'da ham da'vo.
PHOTO_CLAIM_VERBS = re.compile(
    r"yubord(im|ik)|yubor(aman|amiz)|jo['‘’ʻ]?natd(im|ik)|jo['‘’ʻ]?nat(aman|amiz)"
    r"|юбордим|юбордик|юбораман|юборамиз|ташаб"
    r"|otpravil|отправил|отправила|отправлю|отправим|скинул|скину|прислал|пришлю",
    re.IGNORECASE)
# "Shular bor", "shulardan tanlang", "hozir bizda borlari shular" — javob
# rasmlarga barmoq bilan ko'rsatib turadi. Rasm yuborilmasa bu gap yolg'on.
# Real suhbat 2141: model uch marta "400 mingga shular bor" deb yozdi, hech
# qanday tool chaqirmadi — o'zining kechagi javobini tarixdan ko'chirib oldi.
PHOTO_POINTER_WORDS = re.compile(
    r"\bshular|\bшулар|\bborlari\b|\bборлари\b|\bquyidagilar|\bвот эти\b|\bэти вариант",
    re.IGNORECASE)
PHOTO_SENDING_TOOLS = {"send_catalog_album", "send_catalog_image", "send_post_image"}


def reply_claims_a_photo_was_sent(text):
    value = text or ""
    if PHOTO_POINTER_WORDS.search(value):
        return True
    return bool(PHOTO_CLAIM_WORDS.search(value) and PHOTO_CLAIM_VERBS.search(value))


def complete_budget_album_ids(catalog_ids, tool_results):
    """Budjet qidiruvi qaytargan qatorlarning hammasini albomga qo'shadi.

    Faqat shu navbatda get_catalog budjet bilan chaqirilgan va model uning
    qatorlaridan tanlab olgan bo'lsa ishlaydi. Boshqa holatlarda ro'yxat
    o'zgarmaydi — masalan rasmga mos kelgan ikkita mahsulot yuborilayotganda.
    """
    if not catalog_ids:
        return catalog_ids
    budget_ids = budget_catalog_ids_from_tool_results(tool_results)
    if not budget_ids:
        return catalog_ids
    chosen = [value for value in catalog_ids if value in budget_ids]
    if not chosen or len(chosen) != len(catalog_ids):
        return catalog_ids
    if set(chosen) == set(budget_ids):
        return catalog_ids
    return budget_ids


def budget_catalog_ids_from_tool_results(tool_results):
    for row in reversed(tool_results or []):
        if row.get("name") != "get_catalog":
            continue
        output = row.get("output") or {}
        if not output.get("budget"):
            continue
        return [item.get("catalog_id") for item in output.get("catalog") or [] if item.get("catalog_id")]
    return []


def catalog_ids_from_tool_results(tool_results):
    """Shu navbatda get_catalog qaytargan mahsulot raqamlari.

    Mijoz "400 mingga qanaqa gullar bor" deb so'ragan bo'lsa butun katalogni
    yuborish javob emas — o'sha budjetga chiqqan qatorlar yuboriladi.
    """
    for row in reversed(tool_results or []):
        if row.get("name") != "get_catalog":
            continue
        rows = (row.get("output") or {}).get("catalog") or []
        ids = [item.get("catalog_id") for item in rows if item.get("catalog_id")]
        if ids:
            return ids
    return []


def budget_ids_from_reply(text):
    """Javobda aytilgan summaga mos katalog mahsulotlari."""
    for price in prices_from_caption(text or ""):
        rows = ai_catalog_rows(min_price=price, max_price=price)
        ids = [row["catalog_id"] for row in rows]
        if ids:
            return ids
    return []


def tool_results_sent_any_photo(tool_results):
    for row in tool_results or []:
        output = row.get("output") or {}
        if not isinstance(output, dict):
            continue
        if row.get("name") in PHOTO_SENDING_TOOLS and output.get("ok"):
            return True
        # match_ai_catalog_by_media rasmni O'ZI yuboradi: reklama katalogga
        # ulanmagan bo'lsa yoki mahsulot rasmi allaqachon ketgan bo'lsa butun
        # katalog albomi shu chaqiruvning ichida yuboriladi va natijaga album
        # bo'lib yoziladi. Buni hisobga olmasak "yubordim" qorovuli o'sha
        # albomni ikkinchi marta yuborib qo'yadi — real suhbat 2581 va 2571,
        # uch kunda o'n uch marta.
        album = output.get("album")
        if isinstance(album, dict) and album.get("ok"):
            return True
    return False


def apply_sent_photo_claim_safeguard(conversation, result, tool_results):
    """"Albomni yubordim" deb yozib, hech narsa yubormaslikning oldini oladi.

    Real suhbat (1966): mijoz 199 000 so'mligini so'radi, AI ikki marta
    "albomda yubordim" deb yozdi, albom esa yuborilmadi. Mijoz "Yuqku",
    "Albom kurinmayapti" deb ketdi va buyurtma operatorga qolib ketdi.

    Javob matniga tegilmaydi — matn tizim promptining ishi. Gap yolg'on
    bo'lib qolmasligi uchun katalog albomi shu yerda yuboriladi.
    """
    if not reply_claims_a_photo_was_sent(result.get("reply")):
        return result
    if tool_results_sent_any_photo(tool_results):
        return result
    # Bu yerda "allaqachon yuborilgan" to'sig'i qo'yilmaydi: javob rasm
    # yuborilgan deb turibdi, demak rasm ketishi SHART. Aks holda mijoz
    # bo'sh gapni oladi — real suhbat 2141 da xuddi shunday bo'ldi.
    ids = catalog_ids_from_tool_results(tool_results)
    if not ids:
        # Model qidiruvni umuman chaqirmagan bo'lsa ham javobda summa turgan
        # bo'ladi ("Aksiyadagi 199 000 so'mlik variantlarimiz shular"). Butun
        # katalogni yuborish o'sha gapni yolg'on qiladi — summani o'zimiz
        # o'qib, aynan o'sha narxdagilarni yuboramiz.
        ids = budget_ids_from_reply(result.get("reply"))
    items = catalog_album_items(ids) if ids else catalog_album_items([])
    if not items:
        items = catalog_album_items([])
        ids = []
    album = send_catalog_album(conversation, items, whole_catalog=not ids)
    tool_results.append({"name": "send_catalog_album",
                         "arguments": {"catalog_ids": ids, "safeguard": True},
                         "output": album})
    result["tool_results"] = tool_results
    return result


# "Operatorlarimiz sizga tez orada yozib yuborishadi" — bu va'da. Operatorlar
# guruhiga xabar ketmasa mijoz javob kutib qoladi va hech kim bilmaydi.
# Ish vaqti haqidagi gap ("operatorlarimiz 08:00 dan 00:00 gacha aloqada")
# va'da emas, shuning uchun fe'l ham talab qilinadi.
# "Operatorlarimizga bog'laymizmi?" — bu taklif, va'da emas. Mijoz rozilik
# bermaguncha operatorlar guruhiga hech narsa ketmaydi.
OPERATOR_OFFER_PATTERN = (r"operator\w*[^.?!]*bog['‘’ʻ]?la[^.?!]*\?"
                          r"|оператор\w*[^.?!]*боғла[^.?!]*\?"
                          r"|оператор\w*[^.?!]*(соедин|связа|переда|подключ)[^.?!]*\?"
                          r"|(соедин|связа|переда|подключ)\w*[^.?!]*оператор[^.?!]*\?")

OPERATOR_PROMISE_WORDS = re.compile(r"operator|оператор", re.IGNORECASE)
OPERATOR_PROMISE_VERBS = re.compile(
    r"yozib\s*yubor|yozishadi|bog['‘’ʻ]?lan|javob\s*ber|ma['‘’ʻ]?lumot\s*ber|aniqlash|aniq\s*ayt"
    r"|ёзиб\s*юбор|ёзишади|боғлан|жавоб\s*бер|маълумот\s*бер"
    r"|напиш|свяж|уточн|ответ|сообщ",
    re.IGNORECASE)


def reply_offers_an_operator(text):
    """Javob operatorga bog'lashni TAKLIF qilyaptimi (hali va'da emas)."""
    return bool(re.search(OPERATOR_OFFER_PATTERN, text or "", re.IGNORECASE))


def reply_promises_an_operator(text):
    """Javob operator yozishini VA'DA qilyaptimi.

    Taklif savoli va'da emas: mijoz hali rozilik bermagan, operatorlar guruhiga
    hech narsa ketmasligi kerak. Ism va telefon so'ralayotgan bo'lsa ham bu
    handoff emas — buyurtma yozilyapti va operatorlar lead kartasini oladi.
    """
    value = text or ""
    if reply_offers_an_operator(value):
        return False
    if re.search(r"telefon\s*raqam|телефон\s*рақам|номер телефона|ism va telefon|исм ва телефон",
                 value, re.IGNORECASE):
        return False
    return bool(OPERATOR_PROMISE_WORDS.search(value) and OPERATOR_PROMISE_VERBS.search(value))


# Operatorlar guruhiga shu muddat ichida ikkinchi chaqiruv ketmaydi. Operator
# birinchi xabarni ko'rib chatga kiradi — orada har javob uchun yangi karta
# yuborish uni chalg'itadi.
OPERATOR_CALL_QUIET_MINUTES = 30


def operator_already_told_recently(conversation):
    """Yaqinda operatorlar guruhiga shu suhbat haqida xabar ketganmi.

    Lead kartasi ham hisobga olinadi: buyurtma ochilgan bo'lsa operatorlar
    mijozni allaqachon ko'rgan.

    Ilgari bu tekshiruv faqat oxirgi AI javobidan keyingi xabarlarni qarardi.
    Real suhbat 2251: buyurtma yopilib bo'lgan, lead kartasi guruhga ketgan,
    lekin AI keyingi har javobini "Operatorlarimiz tez orada bog'lanishadi"
    bilan yakunladi — bu shunchaki xushmuomalalik edi, qorovul esa uni har
    safar bajarilmagan va'da deb hisoblab, guruhga to'rtta chaqiruv yubordi.
    """
    since = timezone.now() - timedelta(minutes=OPERATOR_CALL_QUIET_MINUTES)
    for message in conversation.messages.filter(sender="system", created_at__gte=since):
        metadata = message.metadata or {}
        if "operator_needed" in metadata or "operator_lead_notified" in metadata:
            return True
    return False


def conversation_started_from_an_ad(conversation):
    """Suhbat Instagram reklamasidan boshlanganmi."""
    ads = ads_context_from_conversation(conversation)
    return bool(ads.get("ad_id") or ads.get("post_id"))


def catalog_ever_sent(conversation):
    """Bu suhbatda katalog albomi biror marta yuborilganmi."""
    return conversation.messages.filter(sender="system", metadata__has_key="catalog_album_result").exists()


AD_OPENING_INSTRUCTION = (
    "Mijoz reklama orqali yozdi va bu birinchi javob. Butun katalog albom qilib "
    "ALLAQACHON yuborildi — sen faqat yozasan, boshqa albom yuborma. "
    "Reklamadagi gulning nomini ham, narxini ham AYTMA: qaysi biri ekanini "
    "bilmaysan. Qisqa ayt: hozirda bizda borlari shular, qaysi biri kerakligini "
    "raqami bilan yozsin."
)


def apply_ad_opening_safeguard(conversation, result, tool_results):
    """Reklamadan kelgan mijoz birinchi javobda katalogni ko'radi.

    Real suhbat 2243: mijoz reklamadagi tugmani bosdi, Instagram "Можно
    заказать?" degan auto-matnni yubordi va suhbatga reklama banneri qo'shildi.
    Prompt modelga kind="ad" uchun rasm moslashtiruvchisini chaqirishni
    taqiqlaydi, shuning uchun katalog yo'li umuman ishga tushmadi va mijozga
    faqat "operatorlarimiz bog'lanadi" degan javob bordi.

    Bannerdan gul nomini taxmin qilmaymiz — mijoz o'zi tanlaydi.

    Faqat suhbatning ochilish xabariga javob berilayotganda ishlaydi. Real
    suhbat 2556 va 954: reklamadan kelgan mijozga operator qo'lda javob berdi,
    mijoz keyinroq "Tumanlarga dastafka bormii" va "Dastafka 7 ciga" deb
    yozdi — hali bitta ham AI xabari bo'lmagani uchun qorovul o'sha savolga
    butun katalogni yubordi.
    """
    if conversation.messages.filter(sender__in=("ai", "operator")).exists():
        return result
    if conversation.messages.filter(sender="customer").count() > 1:
        return result
    if not conversation_started_from_an_ad(conversation):
        return result
    if tool_results_sent_any_photo(tool_results) or catalog_ever_sent(conversation):
        return result
    album = send_catalog_album(conversation, catalog_album_items([]), whole_catalog=True)
    if not album.get("ok"):
        return result
    tool_results.append({"name": "send_catalog_album",
                         "arguments": {"catalog_ids": [], "ad_opening": True},
                         "output": dict(album, instruction_uz=AD_OPENING_INSTRUCTION)})
    result["tool_results"] = tool_results
    return result


def apply_operator_promise_safeguard(conversation, result, tool_results):
    """"Operatorlarimiz yozib yuborishadi" deb yozib, guruhga xabar bermaslikni to'xtatadi.

    Real suhbatlar 2182, 2218, 2230: uchalasida ham shu jumla yozilgan, lekin
    call_operator chaqirilmagan. Operatorlar mijozning kutib turganini bilmadi
    va yetti soatdan keyin qo'lda yozishdi.

    Javob matniga tegilmaydi — gap yolg'on bo'lib qolmasligi uchun chaqiruv shu
    yerda yuboriladi.
    """
    if not reply_promises_an_operator(result.get("reply")):
        return result
    for row in tool_results or []:
        if row.get("name") in {"call_operator", "client_lead_create"} and (row.get("output") or {}).get("ok"):
            return result
    if unlinked_shared_media_silence(conversation):
        # Tizimda yo'q reel operatorni chaqirish sababi emas.
        return result
    if operator_already_told_recently(conversation):
        return result
    reason = "AI javobida operator va'da qilindi: " + (result.get("reply") or "")[:160]
    output = notify_operator_needed(conversation, reason)
    tool_results.append({"name": "call_operator",
                         "arguments": {"reason": reason, "safeguard": True},
                         "output": output})
    result["tool_results"] = tool_results
    return result


def apply_media_match_safeguard(conversation, result, tool_results):
    """Aniq mos kelgan mahsulot rasmi yuborilmay qolgan bo'lsa, o'zi yuboradi.

    Javob matniga tegilmaydi — matn tizim promptining ishi.
    """
    match = first_confident_media_match(tool_results)
    if not match:
        return result
    item = catalog_album_queryset().filter(id=match.get("catalog_id")).first()
    if not item or tool_results_sent_catalog(tool_results, item.id):
        return result
    if catalog_image_already_sent(conversation, item.id):
        # Mijoz bu rasmni oldingi javobda ko'rgan. Uni yana yuborish "yana
        # qanaqalari bor" degan savolga o'sha gulni uchinchi marta ko'rsatish bo'lardi.
        return result
    output = send_catalog_item_image(conversation, item)
    tool_results.append({"name": "send_catalog_image", "arguments": {"query": "", "catalog_id": item.id, "safeguard": True}, "output": output})
    return result


# Reel izohida narx ko'pincha shu ko'rinishda yoziladi: "199 000 so'm",
# "1.600.000", "800 ming". Uchalasi ham bitta raqamga keltiriladi.
CAPTION_PRICE_RE = re.compile(
    r"(\d[\d\s.,\u00a0]{2,})\s*(?:ming|минг|mln|млн|so\u2018m|so'm|som|сум|сўм|sum)?",
    re.IGNORECASE,
)
# Izohdagi narx katalogdagi narxdan ozgina farq qilishi mumkin, shuning uchun
# aynan tenglik emas, shu darajadagi yaqinlik qabul qilinadi.
CAPTION_PRICE_TOLERANCE = Decimal("0.12")


def prices_from_caption(caption):
    """Izohdagi pul summalarini topadi, eng kattasidan boshlab."""
    found = []
    for raw, unit in re.findall(r"(\d[\d\s.,\u00a0]{2,})\s*(ming|минг|mln|млн|so\u2018m|so'm|som|сум|сўм|sum)?",
                                caption or "", re.IGNORECASE):
        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            continue
        value = Decimal(digits)
        unit = (unit or "").lower()
        if unit in {"ming", "минг"} and value < 10000:
            value *= 1000
        elif unit in {"mln", "млн"} and value < 1000:
            value *= 1000000
        if value < 50000 or value > 100000000:
            continue
        if value not in found:
            found.append(value)
    return sorted(found, reverse=True)


def media_caption_for_attachment(attachment):
    """Mijoz yuborgan reel/post bizning postimiz bo'lsa uning izohi.

    Boshqa akkauntning reeli bo'lsa izoh o'qib bo'lmaydi — Graph API faqat o'z
    postlarimizni beradi. Bunday holatda bo'sh qaytadi va rasm tahlili ishlaydi.
    """
    from .platform_services import find_media_by_permalink

    # Instagram ulashilgan postning izohini webhook payloadida o'zi beradi.
    # Bu Graph API so'rovidan ham aniqroq: CDN havolasi bilan kelgan ulashuvda
    # permalink umuman yo'q va API dan qidirib bo'lmaydi.
    own_caption = (attachment or {}).get("caption") or ""
    if own_caption.strip():
        return own_caption
    url = (attachment or {}).get("url") or ""
    if "instagram.com/" not in url:
        return ""
    try:
        media = find_media_by_permalink(url)
    except Exception as error:
        print(f"CAPTION_LOOKUP_FAILED url={url[:70]} error={error}", flush=True)
        return ""
    return (media or {}).get("caption") or ""


def catalog_items_near_price(items, price):
    """Izohdagi narxga yaqin katalog gullari."""
    window = price * CAPTION_PRICE_TOLERANCE
    rows = [item for item in items if item.price is not None and abs(Decimal(item.price) - price) <= window]
    return sorted(rows, key=lambda item: abs(Decimal(item.price) - price))


def caption_price_matches(items, attachment):
    """Reel izohidagi narx bo'yicha katalogdan tanlangan gullar.

    Mijoz reel yuboradi, o'sha reel tizimga ulanmagan, lekin izohida narxi
    yozilgan bo'ladi — o'sha narxdagi gullarimizni ko'rsatish rasmni tahlil
    qilishdan ham aniqroq javob beradi.
    """
    caption = media_caption_for_attachment(attachment)
    if not caption:
        return [], None
    for price in prices_from_caption(caption):
        rows = catalog_items_near_price(items, price)
        if rows:
            return rows[:MAX_LINK_MATCHES], price
    return [], None


MEDIA_MATCH_AD_CATALOG_INSTRUCTION = (
    "Mijoz reklama orqali yozdi va reklamadagi mahsulot katalogga ulanmagan. "
    "Butun katalog albom qilib ALLAQACHON yuborildi — sen faqat yozasan. "
    "Reklamadagi gulning nomini ham, narxini ham AYTMA va taxmin qilma: qaysi "
    "biri ekanini bilmaysan. Mijozga qisqa ayt: hozirda bor gullarimiz shular, "
    "qaysi biri kerakligini raqami bilan yozsin. Boshqa albom yuborma."
)

MEDIA_MATCH_UNLINKED_SHARED_MEDIA_INSTRUCTION = (
    "Mijoz ulashgan reel/post tizimda YO'Q — u haqida aytadigan hech narsamiz yo'q. "
    "Reelni umuman tilga OLMA, \"yuborgan reelingiz\" deb yozma, katalog albomini "
    "YUBORMA, gul nomi va narx AYTMA, call_operator ni CHAQIRMA va operatorga "
    "yo'naltirma. Mijozning o'zi alohida savol yozgan bo'lsa faqat o'sha savolga "
    "javob ber. Savoli bo'lmasa hech narsa yozmaslik ham to'g'ri javob."
)

MEDIA_MATCH_OWN_POST_NO_CATALOG_INSTRUCTION = (
    "Mijoz ulashgan post bizning profilimizdan, lekin unga bitta ham katalog "
    "mahsuloti bog'lanmagan — qaysi gul ekanini bilmaymiz. Gul nomini va narxni "
    "AYTMA, taxmin qilma, katalog albomini YUBORMA. Mijozga qisqa ayt: o'sha "
    "post bo'yicha aniq javobni operatorlarimiz beradi, keyin 00H bo'limi "
    "bo'yicha bog'lashni TAKLIF qil. call_operator faqat mijoz rozilik "
    "bergach chaqiriladi."
)

MEDIA_MATCH_CAPTION_PRICE_INSTRUCTION = (
    "Mijoz yuborgan reel tizimga ulanmagan, lekin izohida narxi yozilgan. group_matches "
    "dagi catalog_id larni send_catalog_album bilan yubor va shu mazmunda yoz: yuborgan "
    "reelingizdagi narxda bizda shu gullar bor, qaysi biri kerak? Bitta gulni tanlab "
    "\"aynan shu\" dema — mijozning o'zi tanlaydi."
)


def match_ai_catalog_by_media(conversation, source_url="", user_text="", limit=MAX_AI_CATALOG_MATCH_CANDIDATES):
    """Mijoz yuborgan rasmni AI katalogdagi mahsulot bilan solishtiradi.

    Qaror uch bosqichda chiqadi: link tekshiruvi -> fingerprint bo'yicha qisqa ro'yxat ->
    faqat shu qisqa ro'yxatni mijoz rasmi bilan yonma-yon ko'rsatib tasdiqlatish.
    Yakuniy qarorni model emas, shu funksiya chiqaradi. Ishonch bo'lmasa allow_send
    false bo'ladi va AI hech qanday katalog rasmini yubora olmaydi.
    """
    attachment = latest_customer_media_attachment(conversation, source_url)
    if not attachment or not attachment.get("url"):
        return {"ok": False, "allow_send": False, "allow_group": False, "detail": "no_customer_media", "matches": [], "group_matches": [], "near_matches": []}
    items = ai_catalog_match_items(max(1, min(int(limit or MAX_AI_CATALOG_MATCH_CANDIDATES), MAX_AI_CATALOG_MATCH_CANDIDATES)))
    if not items:
        return {"ok": False, "allow_send": False, "allow_group": False, "detail": "ai_catalog_empty_or_no_images", "source": attachment, "matches": [], "group_matches": [], "near_matches": []}
    media_url = attachment["url"]

    ads = ads_context_from_conversation(conversation, media_url)
    ads_linked = items_matching_ads(items, ads.get("ad_id"), ads.get("post_id"))
    if len(ads_linked) == 1:
        return media_match_result(conversation, {
            "ok": True,
            "allow_send": True,
            "allow_group": False,
            "detail": "instagram_ad_matched",
            "source": attachment,
            "source_description": "Mijoz reklama orqali yozdi, ad_id katalogdagi mahsulot bilan aynan mos keldi.",
            "matches": [catalog_match_row(ads_linked[0], reason="instagram ad matched")],
            "group_matches": [],
            "near_matches": [],
            "no_match_reason": "",
            "own_post": True,
            "instruction_uz": MEDIA_MATCH_OWN_POST_INSTRUCTION,
        })
    if ads_linked:
        return media_match_result(conversation, {
            "ok": True,
            "allow_send": False,
            "allow_group": True,
            "detail": "instagram_ad_group",
            "source": attachment,
            "source_description": "Mijoz reklama orqali yozdi, bu ad_id bir nechta katalog mahsulotiga bog'langan.",
            "matches": [],
            "group_matches": [catalog_match_row(item, reason="instagram ad matched") for item in ads_linked[:MAX_LINK_MATCHES]],
            "near_matches": [],
            "no_match_reason": "",
            "instruction_uz": MEDIA_MATCH_LINK_GROUP_INSTRUCTION,
        })

    if attachment.get("kind") == "ad":
        # Reklama bannerini ko'rish orqali topishga urinish real suhbatda
        # (1966) 199 000 so'mlik e'lonni 1 000 000 va 400 000 so'mlik
        # mahsulotlarga bog'lab qo'ydi va mijoz ketib qoldi. ad_id katalogga
        # ulanmagan bo'lsa taxmin qilmaymiz — reel topilmagandagi kabi butun
        # katalogni yuboramiz, mijoz o'zi tanlaydi.
        album = send_catalog_album(conversation, catalog_album_items([]), whole_catalog=True)
        return media_match_result(conversation, {
            "ok": True,
            "allow_send": False,
            "allow_group": False,
            "detail": "ad_catalog_sent",
            "source": attachment,
            "source_description": "Mijoz reklama orqali yozdi, reklama katalogdagi mahsulotga ulanmagan.",
            "matches": [],
            "group_matches": [],
            "near_matches": [],
            "album": album,
            "instruction_uz": MEDIA_MATCH_AD_CATALOG_INSTRUCTION,
        })

    # Permalink bilan kelgan ulashuv eslab qolinadi: bir xil post keyin CDN
    # havolasi bilan kelganda faqat shu jadval uni tanib oladi.
    remember_shared_post_permalink(attachment)
    shared = shared_media_verdict(attachment, items=items)
    if shared["shared"] and not shared["ours"] and not shared["catalog"]:
        # Tizimda yo'q reel. Odatda bu yerga umuman kelinmaydi — javob yo'li
        # unlinked_shared_media_silence da to'xtaydi. Mijoz keyin alohida savol
        # yozgan bo'lsa esa shu natija reelni javobdan chetda qoldiradi.
        return media_match_result(conversation, {
            "ok": False,
            "allow_send": False,
            "allow_group": False,
            "detail": "unlinked_shared_media",
            "source": attachment,
            "source_description": "Mijoz ulashgan reel/post tizimda topilmadi.",
            "matches": [],
            "group_matches": [],
            "near_matches": [],
            "no_match_reason": "Ulashilgan havola bazadagi story/post yoki katalog mahsulotiga bog'lanmagan.",
            "instruction_uz": MEDIA_MATCH_UNLINKED_SHARED_MEDIA_INSTRUCTION,
        })

    # Ulashuv CDN havolasi bo'lib kelgan bo'lsa, endi uning permalinki ma'lum —
    # katalog mosligi aynan shu havola bo'yicha qidiriladi. Rasm tahlili uchun
    # esa asl media_url qoladi: permalinkni ko'rish so'rovi o'qiy olmaydi.
    link_url = shared["permalink"] or media_url
    link_attachment = dict(attachment, url=link_url) if link_url != media_url else attachment

    own_post = social_post_for_media(link_attachment)
    linked = shared["catalog"] or direct_ai_catalog_link_matches(items, link_url, attachment=link_attachment, conversation=conversation)

    # Reel tizimga ulanmagan, lekin izohida narxi yozilgan bo'lsa — o'sha narxdagi
    # gullarimizni ko'rsatish rasm tahlilidan ham aniqroq javob beradi.
    if not linked and not own_post and (shared["shared"] or attachment.get("kind") in {"reel", "post", "story"}):
        priced, caption_price = caption_price_matches(items, attachment)
        if priced:
            return media_match_result(conversation, {
                "ok": True,
                "allow_send": False,
                "allow_group": True,
                "detail": "caption_price_group",
                "source": attachment,
                "caption_price": str(caption_price),
                "matches": [],
                "group_matches": [catalog_match_row(item, reason="caption price matched") for item in priced],
                "near_matches": [],
                "instruction_uz": MEDIA_MATCH_CAPTION_PRICE_INSTRUCTION,
            })

    # Post bizning profilimizdan, lekin unga bitta ham katalog mahsuloti
    # bog'lanmagan. Taxmin qilib gul nomi aytish yolg'on bo'ladi, jim qolish
    # esa mijozni javobsiz qoldiradi — bu operatorning ishi.
    if shared["shared"] and shared["ours"] and not linked and not own_post:
        return media_match_result(conversation, {
            "ok": True,
            "allow_send": False,
            "allow_group": False,
            "detail": "own_post_without_catalog",
            "source": attachment,
            "source_description": "Ulashilgan post bizning profilimizdan, lekin katalogga bog'lanmagan.",
            "own_post": True,
            "permalink": shared["permalink"],
            "matches": [],
            "group_matches": [],
            "near_matches": [],
            "instruction_uz": MEDIA_MATCH_OWN_POST_NO_CATALOG_INSTRUCTION,
        })

    story = social_post_answer(own_post) if not linked else None
    if story:
        # Storyning o'zida nomi va narxi turibdi — bu eng aniq manba.
        return media_match_result(conversation, {
            "ok": True,
            "allow_send": False,
            "allow_group": False,
            "detail": "own_story_matched",
            "source": attachment,
            "source_description": "Mijoz bizning storyimizni yubordi, uning ma'lumoti tizimda saqlangan.",
            "own_post": True,
            "story": story,
            "matches": [],
            "group_matches": [],
            "near_matches": [],
            "no_match_reason": "",
            "instruction_uz": MEDIA_MATCH_OWN_STORY_INSTRUCTION,
        })
    if len(linked) == 1:
        return media_match_result(conversation, {
            "ok": True,
            "allow_send": True,
            "allow_group": False,
            "detail": "instagram_link_matched",
            "source": attachment,
            "source_description": "Mijoz yuborgan Instagram linki katalogdagi mahsulot bilan aynan mos keldi.",
            "matches": [catalog_match_row(item, reason="instagram_link matched") for item in linked],
            "group_matches": [],
            "near_matches": [],
            "no_match_reason": "",
            "own_post": True,
            "instruction_uz": MEDIA_MATCH_OWN_POST_INSTRUCTION,
        })
    if linked and (not vision_compatible_media_url(media_url, attachment.get("kind")) or shared_link_is_the_media(attachment, media_url)):
        # Reel video: tahlil qiladigan rasm yo'q, lekin o'sha reeldagi mahsulotlar ma'lum.
        return media_match_result(conversation, {
            "ok": True,
            "allow_send": False,
            "allow_group": True,
            "detail": "instagram_link_group",
            "source": attachment,
            "source_description": "Mijoz yuborgan Instagram postiga bir nechta katalog mahsuloti qo'yilgan.",
            "matches": [],
            "group_matches": [catalog_match_row(item, reason="instagram_link matched") for item in linked[:MAX_LINK_MATCHES]],
            "near_matches": [],
            "no_match_reason": "",
            "instruction_uz": MEDIA_MATCH_LINK_GROUP_INSTRUCTION,
        })

    if not vision_compatible_media_url(media_url, attachment.get("kind")):
        return {"ok": False, "allow_send": False, "allow_group": False, "detail": "media_url_not_image", "source": attachment, "matches": [], "group_matches": [], "near_matches": []}
    api_key = openai_api_key()
    if not api_key:
        return {"ok": False, "allow_send": False, "allow_group": False, "detail": "openai_api_key_missing", "source": attachment, "matches": [], "group_matches": [], "near_matches": []}

    text = (user_text or "").strip()
    if not text:
        latest_customer = conversation.messages.filter(sender="customer").order_by("-created_at", "-id").first()
        text = latest_customer.text if latest_customer else ""

    # Mijoz to'lov chekini ham shu yerga yuboradi. Chekni katalogdan qidirish
    # bema'ni javob beradi, shuning uchun gul tahlilidan oldin rasm saralanadi.
    # Bu tekshiruv link, reklama va story yo'llaridan KEYIN turadi: o'sha
    # yo'llarda javob allaqachon aniq va ko'rish so'rovi umuman kerak emas.
    # Suhbatda buyurtma bo'lmasa chek ham bo'lmaydi — bekorga so'rov yubormaymiz.
    if attachment.get("kind") == "photo" and conversation.leads.exists():
        kind = vision_services.classify_customer_image(media_url, api_key=api_key)
        if kind.get("kind") == "payment_receipt" and kind.get("confidence") in {"medium", "high"}:
            return media_match_result(conversation, {
                "ok": True,
                "allow_send": False,
                "allow_group": False,
                "detail": "payment_receipt",
                "source": attachment,
                "source_description": kind.get("summary", ""),
                "matches": [],
                "group_matches": [],
                "near_matches": [],
                "instruction_uz": (
                    "Bu gul rasmi emas, to'lov cheki. Katalog yubormaslik kerak va gul nomi "
                    "aytilmaydi. client_payment_update ni receipt_url ga shu rasm havolasini "
                    "yozib chaqir, keyin natijadagi instruction_uz bo'yicha javob ber."
                ),
            })
    try:
        source = vision_services.analyze_image(media_url, context_text=text, with_region=True, api_key=api_key)
    except Exception as error:
        print(f"AI_CATALOG_MEDIA_SOURCE_FAILED conversation={conversation.id} error={error}", flush=True)
        return {"ok": False, "allow_send": False, "allow_group": False, "detail": "source_analysis_failed", "source": attachment, "error": str(error)[:400], "matches": [], "group_matches": [], "near_matches": []}
    if not source:
        return {"ok": False, "allow_send": False, "allow_group": False, "detail": "source_analysis_empty", "source": attachment, "matches": [], "group_matches": [], "near_matches": []}

    scored = vision_services.shortlist_candidates(source, items, api_key=api_key)
    shortlist = [row for row in scored[:vision_services.shortlist_size()] if row["score"] >= AI_CATALOG_SHORTLIST_FLOOR]
    if not shortlist and linked:
        return media_match_result(conversation, {
            "ok": True,
            "allow_send": False,
            "allow_group": True,
            "detail": "instagram_link_fallback",
            "source": attachment,
            "source_description": source.get("summary", ""),
            "region_description": source.get("region_description", ""),
            "matches": [],
            "group_matches": [catalog_match_row(item, reason="instagram_link matched") for item in linked[:MAX_LINK_MATCHES]],
            "near_matches": [],
            "no_match_reason": "Rasmdagi aynan mahsulot topilmadi, post havolasidagilar ko'rsatilyapti.",
            "instruction_uz": MEDIA_MATCH_LINK_FALLBACK_INSTRUCTION,
        })
    if not shortlist:
        return media_match_result(conversation, {
            "ok": False,
            "allow_send": False,
            "allow_group": False,
            "detail": "no_similar_catalog_item",
            "show_whole_catalog": True,
            "source": attachment,
            "source_description": source.get("summary", ""),
            "region_description": source.get("region_description", ""),
            "instruction_uz": MEDIA_MATCH_NOT_FOUND_INSTRUCTION,
            "matches": [],
            "group_matches": [],
            "near_matches": [],
            "no_match_reason": "Katalogda bu rasmga o'xshash mahsulot topilmadi.",
        })

    verdicts = vision_services.verify_candidates(media_url, source, shortlist, customer_text=text, api_key=api_key)

    # Mijoz rasmidagi idish savatmi, buketmi, quticha yoki vazami. Model bir savatni
    # qo'ldagi buketga "same_product" deb qo'yishi mumkin, shu yerda to'xtatiladi.
    source_family = vision_services.container_family(source)
    required = vision_services.required_score(source)

    passed = []
    rejected = []
    for row in shortlist:
        item = row["item"]
        judgement = verdicts.get(item.id) or {}
        # Yakuniy shart backendda: model "same_product" desa ham gul shakli, rangi va
        # idishi mos kelmasa yoki fingerprint bali chegaradan past bo'lsa o'tmaydi.
        row["verdict"] = judgement.get("verdict") or "different"
        row["differences"] = judgement.get("differences") or ""
        row["family"] = vision_services.container_family(row["fingerprint"], item.arrangement_type)
        row["passed"] = (
            row["verdict"] == "same_product"
            and bool(judgement.get("flower_form_match"))
            and bool(judgement.get("color_match"))
            and bool(judgement.get("container_match"))
            and row["score"] >= required
            and vision_services.families_can_match(source_family, row["family"])
            and vision_services.sizes_can_match(source, row["fingerprint"])
            and vision_services.forms_can_match(source.get("flower_form"), row["fingerprint"].get("flower_form"))
        )
        (passed if row["passed"] else rejected).append(row)

    def as_row(row):
        return catalog_match_row(row["item"], score=row["score"], fingerprint=row["fingerprint"], verdict=row["verdict"], differences=row["differences"])

    common = {
        "source": attachment,
        "source_description": source.get("summary", ""),
        "region_requested": bool(source.get("region_requested")),
        "region_description": source.get("region_description", ""),
        "multiple_products_visible": bool(source.get("multiple_products_visible")),
        "visible_products": source.get("visible_products") or [],
        "chosen_position": source.get("chosen_position") or 0,
    }
    near = [as_row(row) for row in rejected if row["verdict"] in {"same_product", "similar_only"}]
    winner = max(passed, key=lambda row: row["score"], default=None)
    if not winner:
        # Mijoz ko'p gulli rasmda bittasini chizib ko'rsatgan bo'lsa, aybdor ko'pincha
        # rasmning o'zi: qolgan gullar ham kadrda turadi va tahlilga aralashadi.
        # Operatorga uzatishdan oldin o'sha gulni kesib yuborishini so'raymiz.
        if crop_would_help(conversation, source):
            return media_match_result(conversation, dict(common, **{
                "ok": True,
                "allow_send": False,
                "allow_group": False,
                "ask_for_crop": True,
                "detail": "ask_for_crop",
                "matches": [],
                "group_matches": [],
                "near_matches": near[:3],
                "no_match_reason": "Rasmda bir nechta gul bor, ko'rsatilganini aniq ajratib bo'lmadi.",
                "instruction_uz": MEDIA_MATCH_CROP_INSTRUCTION,
            }))
        # Mijoz reel yuborgan bo'lsa, o'sha reelga qo'yilgan kataloglar aniq ma'lum.
        # Rasmdagi aynan gulni topolmasak ham, "shu reeldan hozir borlari shular"
        # deyish operatorga uzatishdan ko'ra ancha foydali.
        if linked:
            return media_match_result(conversation, dict(common, **{
                "ok": True,
                "allow_send": False,
                "allow_group": True,
                "detail": "instagram_link_fallback",
                "matches": [],
                "group_matches": [catalog_match_row(item, reason="instagram_link matched") for item in linked[:MAX_LINK_MATCHES]],
                "near_matches": near[:3],
                "no_match_reason": "Rasmdagi aynan mahsulot topilmadi, post havolasidagilar ko'rsatilyapti.",
                "instruction_uz": MEDIA_MATCH_LINK_FALLBACK_INSTRUCTION,
            }))
        # Aynan o'shasi yo'q bo'lsa ham, mijoz quruq ketmasin: katalogdagi eng
        # o'xshashlarini ko'rsatamiz. Bu "topdim" degani emas va shuni aytish shart.
        similar = similar_enough_rows(rejected, source)
        if similar:
            # Eng yaqin nomzod baland ball olgan bo'lsa, bu "bizda bunday gul yo'q"
            # degani emas — model o'z javobida ziddiyatga tushgan bo'lishi mumkin
            # (mahsulotni "same_product" deb, ayni paytda rangini mos emas deb).
            # Bunday holatda mijozga "aynan yo'q" deyish yolg'on bo'lardi.
            close = similar[0]["score"] >= CLOSE_MATCH_SCORE
            if close:
                return media_match_result(conversation, dict(common, **{
                    "ok": True,
                    "allow_send": False,
                    "allow_group": True,
                    "detail": "close_matches",
                    "matches": [],
                    "group_matches": [as_row(row) for row in similar],
                    "near_matches": [],
                    "no_match_reason": "",
                    "instruction_uz": MEDIA_MATCH_CLOSE_INSTRUCTION,
                }))
            # Uzoqdan o'xshagan ikki-uchta mahsulotni ko'rsatish mijozni ishontirmaydi.
            # Butun katalogni ko'rsatib, tanlash imkonini berish va aynan o'sha gul
            # kerak bo'lsa operatorga uzatish ancha halol va foydali.
            return media_match_result(conversation, dict(common, **{
                "ok": True,
                "allow_send": False,
                "allow_group": True,
                "show_whole_catalog": True,
                "detail": "similar_only",
                "matches": [],
                "group_matches": [],
                "near_matches": [as_row(row) for row in similar],
                "no_match_reason": "Aynan shu mahsulot yo'q, butun katalog ko'rsatilyapti.",
                "instruction_uz": MEDIA_MATCH_SIMILAR_INSTRUCTION,
            }))
        return media_match_result(conversation, dict(common, **{
            "ok": False,
            "allow_send": False,
            "allow_group": False,
            "detail": "not_confident",
            "show_whole_catalog": True,
            "matches": [],
            "group_matches": [],
            "near_matches": near[:3],
            "no_match_reason": "Katalogdagi mahsulotlar bilan aynan mos kelmadi.",
            "instruction_uz": MEDIA_MATCH_NOT_FOUND_INSTRUCTION,
        }))

    # G'olibdan rasmda ajratib bo'lmaydigan mahsulotlar bormi. Bo'lsa bittasini tanlab
    # narx aytish xato bo'ladi — hammasini ko'rsatib mijozning o'zidan so'raymiz.
    #
    # Guruh tor bo'lishi kerak. Model "similar_only" degani — "bu boshqa mahsulot";
    # uni mijozga ko'rsatish "shulardan qaysi biri" degan keraksiz savol tug'diradi.
    # Shuning uchun guruhga faqat modelning o'zi ham aynan shu mahsulot deganlari,
    # va ballari g'olibga juda yaqinlari kiradi.
    twins = [
        row for row in vision_services.indistinguishable_items(winner, shortlist)
        if row["verdict"] == "same_product"
    ]
    twins += [
        row for row in passed
        if row is not winner and row not in twins and winner["score"] - row["score"] <= TIED_SCORE_GAP
    ]
    # Savat bilan buketni yonma-yon qo'yib "qaysi biri" deb so'rash ma'nosiz — ular
    # rasmda aniq farq qiladi, mijoz allaqachon birini ko'rsatgan.
    twins = [row for row in twins if row["family"] == winner["family"]]
    twins = [row for row in twins if vision_services.sizes_can_match(winner["fingerprint"], row["fingerprint"])]
    twins = [row for row in twins if vision_services.forms_can_match(winner["fingerprint"].get("flower_form"), row["fingerprint"].get("flower_form"))]
    # Narxi g'olib bilan bir xil bo'lgan egizakni so'rashning ma'nosi yo'q — mijoz
    # qaysi birini tanlasa ham javob o'zgarmaydi.
    twins = [row for row in twins if row["item"].price != winner["item"].price]
    if twins:
        group = [as_row(row) for row in [winner] + sorted(twins, key=lambda row: row["score"], reverse=True)][:4]
        group_ids = {row["catalog_id"] for row in group}
        return media_match_result(conversation, dict(common, **{
            "ok": True,
            "allow_send": False,
            "allow_group": True,
            "detail": "several_look_the_same",
            "matches": [],
            "group_matches": group,
            "near_matches": [row for row in near if row["catalog_id"] not in group_ids][:3],
            "no_match_reason": "",
            "instruction_uz": MEDIA_MATCH_GROUP_INSTRUCTION,
        }))

    # Mijoz bizning o'z story yoki reelimizni yuborgan bo'lsa, undagi gul aniq —
    # unga "o'xshagan variant" deb javob berish mijozni shubhaga soladi.
    own_post = customer_shared_our_post(attachment)
    return media_match_result(conversation, dict(common, **{
        "ok": True,
        "allow_send": True,
        "allow_group": False,
        "detail": "matched",
        "matches": [as_row(winner)],
        "group_matches": [],
        "near_matches": near[:3],
        "no_match_reason": "",
        "own_post": own_post,
        "instruction_uz": MEDIA_MATCH_OWN_POST_INSTRUCTION if own_post else MEDIA_MATCH_FOUND_INSTRUCTION,
    }))


def lead_summary_text(lead):
    """Operator suhbat ustida ko'radigan bir qatorlik xulosa."""
    details = lead.details or {}
    parts = [LEAD_TOPIC_LABELS.get(details.get("topic"), "So'rov")]
    if lead.arrangement_type:
        parts.append(dict(ARRANGEMENT_LABELS).get(lead.arrangement_type, lead.arrangement_type))
    if details.get("flowers_text"):
        parts.append(f"gul {details['flowers_text']}")
    if details.get("size_text"):
        parts.append(f"hajmi {details['size_text']}")
    if details.get("photo_urls"):
        parts.append(f"{len(details['photo_urls'])} ta rasm havolasi")
    if lead.request_uz:
        parts.append(lead.request_uz)
    return " · ".join(parts)[:2000]


def save_conversation_ai_summary(conversation, lead):
    """Lead yaratilgach yoki yangilangach suhbat xulosasini yozib qo'yadi."""
    summary = lead_summary_text(lead)
    if summary == conversation.ai_summary:
        return
    conversation.ai_summary = summary
    conversation.save(update_fields=["ai_summary", "updated_at"])


def stock_hidden_result(name):
    """AI dan olib tashlangan sklad tool'lari chaqirilsa qaytariladigan yo'riqnoma."""
    return {
        "ok": False,
        "detail": "stock_not_available_to_ai",
        "tool": name,
        "instruction": "Sklad ma'lumoti AI ga berilmaydi. Mijozga gul ro'yxati, dona narxi yoki sklad rasmi ko'rsatilmaydi. Ism va telefonni ol, qaysi guldan, qanday hajmda va buketmi yoki savatmi ekanini so'ra, keyin client_lead_create chaqir va aniq narxni operator aytishini ayt.",
    }


def ai_tool_definitions():
    lead_catalog_item_schema = {
        "type": "object",
        "properties": {
            "catalog_id": {"type": ["integer", "null"], "description": "get_catalog, match_ai_catalog_by_media yoki send_catalog_album natijasidagi catalog_id. Ma'lum bo'lsa majburiy yubor."},
            "catalog_name": {"type": "string"},
            "quantity": {"type": "integer"},
        },
        "required": ["catalog_id", "catalog_name", "quantity"],
        "additionalProperties": False,
    }
    lead_request_properties = {
        "topic": {"type": ["string", "null"], "enum": list(LEAD_TOPIC_LABELS) + [None], "description": "So'rov turi. custom_order — mijoz o'zi yasatmoqchi, photo_request — mijoz rasm yubordi, question — AI javob berolmaydigan savol."},
        "flowers_text": {"type": ["string", "null"], "description": "Faqat gul nomlari va ranglari, mijozning so'zi bilan. Masalan \"jumila pushti\". Butun jumlani ko'chirma. Mijoz gul aytmagan bo'lsa null."},
        "size_text": {"type": ["string", "null"], "description": "Faqat hajm yoki dona soni. Masalan \"51 dona\", \"katta\". Bilmasa null."},
        "photo_urls": {"type": "array", "items": {"type": "string"}, "description": "Mijoz yuborgan rasm havolalari. Suhbatdagi havolani o'zgartirmasdan ko'chir."},
    }
    lead_request_keys = list(lead_request_properties)
    return [
        {
            "type": "function",
            "name": "client_leads_get",
            "description": "Shu mijozning avvalgi leadlarini olish.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
                "required": ["limit"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "client_lead_create",
            "description": "Ism va telefon olingach so'rovni operatorga topshirish. Katalog buyurtmasi ham, yasatma buyurtma ham, rasm bo'yicha so'rov ham, AI javob berolmagan savol ham shu tool orqali yoziladi. request_text faqat o'zbekcha, mijoz so'ragan narsani aniq yoz.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": ["string", "null"]},
                    "phone": {"type": ["string", "null"]},
                    "request_text": {"type": "string"},
                    "arrangement_type": {"type": ["string", "null"], "enum": ["bouquet", "basket", "catalog", None]},
                    "estimated_price": {"type": ["number", "null"], "description": "Faqat katalog mahsulotining aniq narxi. Yasatma buyurtmada null."},
                    "fulfillment": {"type": ["string", "null"], "enum": ["delivery", "pickup", None]},
                    "delivery_address": {"type": ["string", "null"]},
                    "desired_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                    "desired_time": {"type": ["string", "null"], "description": "HH:MM"},
                    "catalog_items": {"type": "array", "items": lead_catalog_item_schema},
                    "note": {"type": ["string", "null"]},
                    **lead_request_properties,
                },
                "required": ["customer_name", "phone", "request_text", "arrangement_type", "estimated_price", "fulfillment", "delivery_address", "desired_date", "desired_time", "catalog_items", "note"] + lead_request_keys,
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "client_lead_edit",
            "description": "Shu mijozning mavjud leadini tahrirlash. Mijoz yetkazib berish yoki kelib olishni tanlasa, manzil, sana yoki vaqt aytsa darhol shu tool bilan leadni yangila.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lead_id": {"type": "integer"},
                    "customer_name": {"type": ["string", "null"]},
                    "phone": {"type": ["string", "null"]},
                    "request_text": {"type": ["string", "null"]},
                    "status": {"type": ["string", "null"]},
                    "arrangement_type": {"type": ["string", "null"], "enum": ["bouquet", "basket", "catalog", None]},
                    "estimated_price": {"type": ["number", "null"]},
                    "fulfillment": {"type": ["string", "null"], "enum": ["delivery", "pickup", None]},
                    "delivery_address": {"type": ["string", "null"]},
                    "desired_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                    "desired_time": {"type": ["string", "null"], "description": "HH:MM"},
                    "catalog_items": {"type": ["array", "null"], "items": lead_catalog_item_schema},
                    "note": {"type": ["string", "null"]},
                    **lead_request_properties,
                },
                "required": ["lead_id", "customer_name", "phone", "request_text", "status", "arrangement_type", "estimated_price", "fulfillment", "delivery_address", "desired_date", "desired_time", "catalog_items", "note"] + lead_request_keys,
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "match_ai_catalog_by_media",
            "description": "MAJBURIY. Conversation.customer_attachments ichida kind ad BO'LMAGAN media bo'lsa va mijozning oxirgi xabari o'sha media haqida bo'lsa, javob yozishdan OLDIN shu toolni chaqirishing SHART. Chaqirmasdan gul nomi yoki narx yozish eng og'ir xato — katalogda yo'q gulni o'ylab topib yuborasan. Shubhalansang chaqir: bekorga chaqirish zarar qilmaydi, chaqirmaslik esa yolg'on javob beradi. Mijozni Telegram akkauntga yo'naltirishdan oldin ham doim shu tool. Mijoz 'shu nechpul', 'shundan bormi', 'tepadan 2chisi', 'qizili', 'chizilgan joydagi' kabi yozsa shu tool shart. source_url bo'sh bo'lsa oxirgi customer media olinadi. Natijadagi allow_send=true bo'lsagina matches ichidagi mahsulot mijozniki: send_catalog_image chaqir. allow_group=true bo'lsa bir nechta mahsulot rasmda bir xil ko'rinadi: group_matches dagi catalog_id larni send_catalog_album bilan yubor va qaysi biri kerakligini so'ra. allow_group=true bo'lgan holatlar detail bilan farqlanadi: several_look_the_same — bir xil ko'rinadigan mahsulotlar; instagram_link_group va instagram_link_fallback — mijoz yuborgan reel/storyga qo'yilgan mahsulotlar, siz yuborgan reeldan hozir borlari shular deb ayt; similar_only — aynan o'sha gul katalogda yo'q, bular faqat o'xshaydiganlari, shuni rostini ayt. ask_for_crop=true bo'lsa rasmda bir nechta gul bor va mijoz bittasini ko'rsatgan, lekin qaysi biri ekanini ajratib bo'lmadi: rasm yuborma, narx aytma, handoff ham qilma — mijozdan o'sha gulni rasmdan kesib qayta yuborishini iltimos qil. Uchalasi ham false bo'lsa gul aniqlanmagan — katalogdan alohida rasm yuborilmaydi, nom va narx aytilmaydi, near_matches mijozga ko'rsatilmaydi (u faqat operator uchun). Butun katalog albom qilib yuboriladi va 00H bo'limi bo'yicha operatorga bog'lash TAKLIF qilinadi, rozilik kelgach call_operator chaqiriladi. Telefon so'ralmaydi.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_url": {"type": ["string", "null"], "description": "Suhbatdagi aniq media URL. Bo'sh yoki null bo'lsa oxirgi customer media olinadi."},
                    "user_text": {"type": "string", "description": "Mijozning media bilan bog'liq oxirgi savoli yoki ko'rsatmasi."},
                },
                "required": ["source_url", "user_text"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "get_catalog",
            "description": "AI katalogdagi mijozga ko'rsatiladigan tayyor buket/savat/kompozitsiyalarni olish. Har qatordagi note_uz — operator izohi: mahsulot tafsiloti va ba'zan kelishilgan narx shu yerda turadi. Mijoz budjet aytsa (\"250 mingga bormi\", \"200 mingdan 500 minggacha\", \"1 millionlik\", \"arzonrog'i\") min_price va max_price ber. Mijoz BITTA summa aytsa (\"350 mingga\", \"за 350\", \"1 millionlik\") min_price va max_price ga O'SHA summani ikkalasiga ham yoz — shunda o'sha narx atrofidagilar chiqadi. Ikki xil qiymatni faqat mijoz oraliq aytganda yoz (\"200 dan 500 gacha\"). \"350 minggacha\" degan yuqori chegara bo'lsa max_price ga yoz, min_price ni null qoldir. Natijadagi budget.exact_match false bo'lsa o'sha narxda mahsulot yo'q, qatorlar eng yaqinlari — budget.cheapest_price eng arzon mahsulot narxi. query_matched false bo'lsa qidiruv so'zi nomlarga mos kelmagan, lekin qaytgan qatorlar sotuvdagi haqiqiy mahsulotlar — mijozga tayyor mahsulot yo'q deb aytma.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "arrangement_type": {"type": ["string", "null"], "enum": ["bouquet", "basket", "box", None]},
                    "min_price": {"type": ["number", "null"], "description": "Budjetning pastki chegarasi so'mda. Aytilmasa null."},
                    "max_price": {"type": ["number", "null"], "description": "Budjetning yuqori chegarasi so'mda. Mijoz bitta summa aytsa (\"250 mingga\") shu yerga yoz."},
                },
                "required": ["query", "arrangement_type", "min_price", "max_price"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "send_catalog_image",
            "description": "AI katalogdagi bitta aniq buket/savat rasmini mijozga yuborish. Butun katalog kerak bo'lsa send_catalog_album ishlat. catalog_id ma'lum bo'lsa uni yubor, aks holda query ga nomini yoz.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "catalog_id": {"type": ["integer", "null"]},
                },
                "required": ["query", "catalog_id"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "send_post_image",
            "description": "Mijoz yuborgan bizning story/postimizning o'z rasmini qayta yuborish. Faqat match_ai_catalog_by_media detail=own_story_matched qaytarganda va mijoz o'sha gulning rasmini so'raganda ishlatiladi. social_post_id o'sha tool natijasidagi story.social_post_id dan olinadi. Katalog mahsuloti uchun bu tool emas, send_catalog_image ishlatiladi.",
            "parameters": {
                "type": "object",
                "properties": {
                    "social_post_id": {"type": "integer"},
                },
                "required": ["social_post_id"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "send_catalog_album",
            "description": "AI katalogni mijozga rasm albomi qilib yuborish. Mijoz katalogni, vitrinani, tayyor buketlarni yoki nima borligini so'rasa shu tool chaqiriladi va katalog matn ro'yxati qilib yozilmaydi. catalog_ids bo'sh bo'lsa AI katalogdagi faol mahsulotlar yuboriladi. Rasmlar bitta xabarda albom bo'lib boradi, har rasm ostida tartib raqami, nomi va narxi ko'rinadi. Natijadagi position mijoz ko'rgan raqam bilan bir xil, mijoz keyin birinchisi, 2-chisi desa o'sha position dagi catalog_id olinadi. Har qatorda note_uz — o'sha mahsulotning operator izohi ham keladi.",
            "parameters": {
                "type": "object",
                "properties": {
                    "catalog_ids": {"type": "array", "items": {"type": "integer"}, "description": "Bo'sh massiv = butun katalog. Aks holda get_catalog qaytargan catalog_id lar."},
                },
                "required": ["catalog_ids"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "delivery_location_link",
            "description": (
                "Yetkazib berish manzilini xaritada belgilash havolasini beradi. Mijoz "
                "yetkazib berishni tanlagach va suhbatda lead bor bo'lgach chaqiriladi. "
                "Natijadagi link ni mijozga AYNAN o'sha ko'rinishda yoz, o'zgartirma va "
                "qisqartirma. Havola bo'lmasa natijada bo'sh keladi — unda mijozdan manzilni "
                "matn bilan yozishini so'ra. Mijoz manzilni matn bilan yozgan bo'lsa ham "
                "havola berish zarar qilmaydi: xaritadagi nuqta kuryerga aniqroq."
            ),
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            "strict": True,
        },
        {
            "type": "function",
            "name": "call_operator",
            "description": (
                "Operatorlarni chaqiradi. Mijozga \"operatorlarimiz sizga tez orada yozib "
                "yuborishadi\" deb javob beradigan HAR QANDAY holatda shu tool chaqiriladi: "
                "javobi senda bo'lmagan savol, rasmdagi gulni topolmaganing, shikoyat, "
                "kelin buketi, to'y va tadbir bezash, hamkorlik, karta rekvizitlari yo'qligi. "
                "Tool operatorlar guruhiga mijozning ismi, raqami, username i va oxirgi xabarini "
                "yuboradi va ostiga shu chatni ochadigan tugma qo'yadi — operator o'zi kirib yozadi. "
                "Mijozdan telefon SO'RAMA va lead YARATMA. reason ga bir og'iz o'zbekcha yoz: "
                "operator nima uchun kerak."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Operator nima uchun kerak, qisqa o'zbekcha."},
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "client_payment_update",
            "description": (
                "Mijozning to'lov turi yoki to'lov cheki. Ikki holatda chaqiriladi. "
                "BIRINCHI: mijoz to'lov turini aytdi — payment_type ga \"cash\" yoki \"card\" yoz. "
                "Karta bo'lsa natijada karta raqami va egasi keladi, ularni mijozga aytasan va "
                "to'lov chekining rasmini so'raysan. "
                "IKKINCHI: mijoz to'lov chekining rasmini yubordi — receipt_url ga o'sha rasm havolasini yoz. "
                "Chek ekanini match_ai_catalog_by_media natijasi aytadi (detail = payment_receipt); "
                "gul rasmini chek deb yuborma. "
                "Bu tool faqat shu suhbatda lead bor bo'lsa ishlaydi. Natijadagi instruction_uz "
                "nima deyish kerakligini aytadi."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "payment_type": {"type": ["string", "null"], "enum": ["cash", "card", None], "description": "Mijoz aytgan to'lov turi. Aytmagan bo'lsa null."},
                    "receipt_url": {"type": ["string", "null"], "description": "Mijoz yuborgan chek rasmining havolasi. Chek bo'lmasa null."},
                },
                "required": ["payment_type", "receipt_url"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    ]


def conversation_instagram_account_id(conversation):
    message = conversation.messages.filter(sender="customer").order_by("-created_at", "-id").first()
    return (message.metadata or {}).get("instagram_account_id") if message else None


def send_image_to_customer(customer, image_url, conversation=None):
    try:
        if customer.instagram_user_id.startswith("telegram:"):
            result = telegram_send_image(customer.instagram_user_id.split(":", 1)[1], image_url)
        elif customer.instagram_user_id:
            result = instagram_send_image(customer.instagram_user_id, image_url, conversation_instagram_account_id(conversation) if conversation else None)
        else:
            return False, "no_platform_id", None
    except Exception as error:
        print(f"IMAGE_SEND_FAILED customer={customer.id} url={image_url} error={error}", flush=True)
        return False, "send_failed", None
    if isinstance(result, dict) and result.get("mocked"):
        return True, "mocked", result
    remember_sent_instagram_message(result)
    return True, "sent", result


def send_catalog_item_image(conversation, item):
    customer = conversation.customer
    item_name = getattr(item, "name", getattr(item, "name_uz", ""))
    image_url = catalog_item_image_url(item)
    price_value = getattr(item, "price", "")
    price_text = f"{money_uz(price_value)} so'm" if price_value != "" else ""
    if not image_url:
        return {"ok": False, "detail": "image_not_found", "catalog_id": item.id, "catalog_name": item_name}
    delivered, detail, sent = send_image_to_customer(customer, image_url, conversation)
    Message.objects.create(conversation=conversation, sender="system", text="", metadata={"image_tool_result": {"catalog_id": item.id, "catalog_name": item_name, "image_url": image_url, "delivered": delivered, "detail": detail, "sent": sent}})
    if not delivered:
        return {"ok": False, "image_sent": False, "detail": detail, "catalog_id": item.id, "catalog_name": item_name}
    name_ru = catalog_name_ru(item_name)
    price_text_ru = money_text(price_value, "ru") if price_value != "" else ""
    return {"ok": True, "image_sent": True, "catalog_id": item.id, "catalog_name": item_name, "price": str(price_value), "price_text": price_text, "note_uz": getattr(item, "note", ""), "image_url": image_url, "reply_instruction": f"{item_name}\nNarxi {price_text}\nSizga qachonga kerak edi?", "catalog_name_ru": name_ru, "price_text_ru": price_text_ru, "reply_instruction_ru": f"{name_ru}\n{price_text_ru}\nНа когда вам нужно?"}


CATALOG_ALBUM_MAX_PER_MESSAGE = 10


def catalog_item_image_url(item):
    return item.image_url or (item.social_post.image_url if getattr(item, "social_post_id", None) else "")


def catalog_album_queryset():
    return available_ai_catalog_queryset().order_by("-created_at", "-id")


def catalog_album_items(catalog_ids=None, limit=60):
    queryset = catalog_album_queryset()
    if catalog_ids:
        items = {item.id: item for item in queryset.filter(id__in=catalog_ids)}
        return [items[value] for value in catalog_ids if value in items][:limit]
    return [item for item in queryset[:limit]]


def send_catalog_album(conversation, items, whole_catalog=False):
    """Katalog rasmlarini albom qilib yuboradi. Bitta xabarga 10 tadan rasm sig'adi, bu platformaning chegarasi.

    Har rasm ostida tartib raqami, nomi va narxi ko'rinadi. Natijadagi position mijoz
    ko'rgan raqam bilan bir xil, keyin mijoz shu raqamni aytsa AI qaysi mahsulot ekanini biladi.
    """
    customer = conversation.customer
    rows = []
    not_sent = []
    for item in items:
        image_url = catalog_item_image_url(item)
        item_name = getattr(item, "name", getattr(item, "name_uz", ""))
        if not image_url:
            not_sent.append({"catalog_id": item.id, "name": item_name, "detail": "image_not_found"})
            continue
        rows.append({"item": item, "image_url": image_url})
    platform = "telegram" if customer.instagram_user_id.startswith("telegram:") else "instagram"
    chat_id = customer.instagram_user_id.split(":", 1)[1] if platform == "telegram" else customer.instagram_user_id
    if not rows:
        return {"ok": False, "detail": "image_not_found", "items": [], "not_sent": not_sent}
    if not chat_id:
        return {"ok": False, "detail": "no_platform_id", "items": [], "not_sent": not_sent}
    # Sarlavhani kod yuboradi, model orqali o'tmaydi — shuning uchun tili shu
    # yerda tanlanadi. Ruscha mijoz o'zbekcha nom va "so'm" ni ko'rmaydi.
    script = conversation_reply_script(conversation)
    position = 0
    for row in rows:
        position += 1
        row["position"] = position
        title = catalog_name_ru(row["item"].name) if script == "ru" else row["item"].name
        row["title"] = f"{row['position']}. {title}"
        row["subtitle"] = money_text(row["item"].price, script)
        row["caption"] = f"{row['title']} — {row['subtitle']}"
    sent_items = []
    sent_message_ids = []
    sent_groups = []
    messages_sent = 0
    album_chunks = 0
    fallback_chunks = 0
    for start in range(0, len(rows), CATALOG_ALBUM_MAX_PER_MESSAGE):
        chunk = rows[start:start + CATALOG_ALBUM_MAX_PER_MESSAGE]
        delivered, detail, sent = send_catalog_album_chunk(customer, platform, chat_id, chunk, conversation=conversation)
        if isinstance(sent, dict) and sent.get("message_id"):
            sent_message_ids.append(sent["message_id"])
            # Mijoz albomga reply qilsa qaysi mahsulot haqida yozganini shu
            # bog'lanish aytadi. Bir albom bir necha xabarga bo'linishi mumkin,
            # shuning uchun har xabar o'z mahsulotlari bilan yoziladi.
            sent_groups.append({"message_id": sent["message_id"],
                                "catalog_ids": [row["item"].id for row in chunk]})
            # Darhol yozamiz. Albom yozuvi butun sikl tugagach saqlanadi, Instagram
            # echo'si esa undan oldin yetib keladi va o'z albomimiz "operator yozdi"
            # bo'lib tushadi. Poyga oynasi shu bilan millisekundgacha qisqaradi.
            record_outbound_platform_message(conversation, sent["message_id"])
        if delivered:
            messages_sent += 1
            album_chunks += 1
            for row in chunk:
                sent_items.append(catalog_album_row(row, True, detail))
            continue
        fallback_chunks += 1
        for row in chunk:
            single = send_catalog_item_image(conversation, row["item"])
            if single.get("ok"):
                messages_sent += 1
                sent_items.append(catalog_album_row(row, True, "one_by_one"))
            else:
                not_sent.append({"catalog_id": row["item"].id, "name": row["item"].name, "detail": single.get("detail") or "send_failed"})
    if album_chunks and fallback_chunks:
        sent_as = "mixed"
    elif album_chunks:
        sent_as = "album"
    else:
        sent_as = "one_by_one"
    result = {
        "ok": bool(sent_items),
        "sent_as": sent_as,
        "messages_sent": messages_sent,
        "album_max_per_message": CATALOG_ALBUM_MAX_PER_MESSAGE,
        "numbering_visible": bool(sent_items) and fallback_chunks == 0,
        "whole_catalog": bool(whole_catalog),
        "items": sent_items,
        "not_sent": not_sent,
        # Instagram yuborgan albomimizni webhook orqali qaytaradi. Id lar bazada tursa
        # echo tanilib, o'z albomimiz "operator javob yozdi" deb hisoblanmaydi.
        "sent_message_ids": sent_message_ids,
        "sent_groups": sent_groups,
    }
    Message.objects.create(conversation=conversation, sender="system", text="", metadata={"catalog_album_result": result})
    return ai_catalog_album_result(result)


def replied_to_catalog_items(conversation, reply_mid):
    """Mijoz javob qilgan xabarimizda qaysi katalog mahsuloti borligi.

    Mijoz albomdagi rasmga reply qilib "nechpul qberasla" deb yozadi. Reply
    bo'lmasa bu savol havoda qoladi: AI qaysi mahsulot haqida gap ketayotganini
    bilmaydi va umumiy javob beradi. Instagram esa javob qilingan xabarning
    mid ini yuboradi — o'sha mid bo'yicha mahsulotni topamiz.
    """
    reply_mid = (reply_mid or "").strip()
    if not reply_mid:
        return []
    for message in conversation.messages.filter(sender="system").order_by("-created_at", "-id")[:80]:
        metadata = message.metadata or {}
        album = metadata.get("catalog_album_result") or {}
        items = album.get("items") or []
        for group in album.get("sent_groups") or []:
            if group.get("message_id") == reply_mid:
                wanted = set(group.get("catalog_ids") or [])
                return [row for row in items if row.get("catalog_id") in wanted]
        sent_ids = album.get("sent_message_ids") or []
        # sent_groups yozilishidan oldingi albomlar: bitta xabar bo'lsa mahsulotlar aniq.
        if reply_mid in sent_ids and len(sent_ids) == 1:
            return list(items)
        image = metadata.get("image_tool_result") or {}
        if image.get("catalog_id") and (image.get("sent") or {}).get("message_id") == reply_mid:
            return [{"catalog_id": image["catalog_id"], "name": image.get("catalog_name") or ""}]
    return []


REPLIED_TO_TEXT_LIMIT = 220


def replied_to_message(conversation, reply_mid):
    """Mijoz javob qilgan xabarning o'zi.

    Har bir yuborilgan va kelgan xabar Instagram id si bilan yoziladi:
    AI javobi `deliver_ai_reply` da, operator xabari va mijoz xabari esa
    webhookda. Shuning uchun reply qilingan xabar matnli bo'lsa ham topiladi.
    """
    reply_mid = (reply_mid or "").strip()
    if not reply_mid:
        return None
    return Message.objects.filter(conversation=conversation, instagram_message_id=reply_mid).order_by("-id").first()


def replied_to_text_note(conversation, reply_mid):
    """Matnli xabarga qilingan reply uchun izoh.

    Real o'lchov: bazadagi 350 ta reply xabaridan atigi 3 tasi katalog albomiga
    edi — 142 tasi operator matniga, 107 tasi AI matniga, 93 tasi mijozning o'z
    xabariga. Ularning hammasi AI ga izohsiz borar edi va savol havoda qolardi:
    suhbat 2993 da "Kami borm8" aynan 800 000 lik Jumiliaga javob edi, 2878 da
    "Bula hammasi 200 mingku" katalog albomi haqidagi javobga.
    """
    message = replied_to_message(conversation, reply_mid)
    if not message:
        return ""
    text = " ".join((message.text or "").split())
    if not text:
        return ""
    text = text[:REPLIED_TO_TEXT_LIMIT]
    if message.sender == "customer":
        return (f"Tizim izohi: mijoz o'zining oldingi xabariga javob qildi — «{text}». "
                "Yangi xabari o'sha xabarga tegishli.")
    who = "javobimizga" if message.sender == "ai" else "operator xabariga"
    return (f"Tizim izohi: mijoz aynan shu {who} javob qildi — «{text}». "
            "Yangi xabarini shu gapning davomi deb tushun.")


def replied_to_note(conversation, reply_mid):
    """Reply qilingan mahsulotni suhbat matniga qo'shiladigan qator qilib beradi.

    Avval katalog albomi va rasmi qaraladi — u yerda mahsulot nomi va narxi
    aniq. Topilmasa reply qilingan xabarning o'z matni beriladi.
    """
    rows = replied_to_catalog_items(conversation, reply_mid)
    if not rows:
        return replied_to_text_note(conversation, reply_mid)
    names = []
    for row in rows[:10]:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        price = row.get("price")
        names.append(f"{name} — {money_uz(price)} so'm" if price else name)
    if not names:
        return replied_to_text_note(conversation, reply_mid)
    if len(names) == 1:
        return f"Tizim izohi: mijoz shu mahsulot rasmiga javob qildi — {names[0]}."
    return ("Tizim izohi: mijoz shu albomga javob qildi — " + "; ".join(names) +
            ". Qaysi biri ekanini so'ra.")


def catalog_album_row(row, delivered, detail):
    item = row["item"]
    return {
        "position": row["position"],
        "catalog_id": item.id,
        "name": item.name,
        "price": str(item.price),
        "type": item.arrangement_type,
        # Albom ketgach mijoz raqam bilan tanlaydi va shu mahsulot haqida so'raydi.
        # Izoh shu yerda tursa AI get_catalog ni qaytadan chaqirmasdan javob bera oladi.
        "note_uz": getattr(item, "note", ""),
        "image_url": row["image_url"],
        "delivered": delivered,
        "detail": detail,
    }


ALBUM_WHOLE_CATALOG_INSTRUCTION = (
    "Butun katalog yuborildi — ichida har xil narxdagi mahsulotlar bor. "
    "\"Shu summaga shular bor\" deb YOZMA: albomdagilarning narxi bir xil emas. "
    "Shu mazmunda yoz: hozirda bizda borlari shular, hammasi shu. Mijoz "
    "\"faqat shularmi\", \"yana bormi\", \"Только эти варианты?\" deb so'ragan "
    "bo'lsa rostini ayt — bu bizdagi butun ro'yxat. Keyin qaysi biri "
    "kerakligini raqami bilan so'ra."
)

ALBUM_PRICE_GROUP_INSTRUCTION = (
    "Faqat tanlangan mahsulotlar yuborildi. Ularning narxi mijoz so'ragan "
    "summaga mos, shuning uchun \"shu summaga shular bor\" deb yozish to'g'ri. "
    "Albomda {count} ta mahsulot bor{prices} — javobingda shu sonni nazarda tut. "
    "Bittasini tanlab \"bitta variantimiz bor\" deb yozish XATO: mijoz "
    "{count} ta rasmni ko'rib turibdi."
)


ALBUM_NUMBERING_INSTRUCTION = (
    "Raqamlar faqat SHU albomga tegishli. Bundan oldin albom yuborilgan bo'lsa "
    "uning raqamlari endi bekor — mijozning ekranida shu albom turadi. "
    "Mahsulotlarni sanasang yoki mijoz raqam yozsa, aynan shu albomning "
    "items ro'yxatidagi position dan foydalan; o'zingning oldingi javobidagi "
    "tartibni takrorlama."
)


def ai_catalog_album_result(result):
    """AI ga rasm havolasi berilmaydi, u URL ni matn qilib yuborib qo'ymasligi uchun.

    Havolalar suhbat xabarining metadata sida qoladi va API orqali CRM chatiga chiqadi.

    Javob matnining mazmuni ham shu yerda aytiladi. Real suhbat 2243: mijoz
    "Только эти варианты?" deb so'radi, AI butun katalogni yubordi — harakat
    to'g'ri edi — lekin matnda "Shu summaga shular bor" deb yozdi. Albomda esa
    199 000 dan 1 000 000 gacha mahsulot bor edi.
    """
    trimmed = dict(result)
    trimmed["items"] = [{key: value for key, value in row.items() if key != "image_url"} for row in result.get("items", [])]
    if result.get("ok"):
        rows = trimmed["items"]
        if result.get("whole_catalog"):
            trimmed["instruction_uz"] = ALBUM_WHOLE_CATALOG_INSTRUCTION
        else:
            prices = sorted({row.get("price") for row in rows if row.get("price")})
            span = ""
            if len(prices) > 1:
                span = ", narxlari %s dan %s gacha" % (money_uz(prices[0]), money_uz(prices[-1]))
            trimmed["instruction_uz"] = ALBUM_PRICE_GROUP_INSTRUCTION.format(
                count=len(rows), prices=span)
        trimmed["instruction_uz"] += " " + ALBUM_NUMBERING_INSTRUCTION
    return trimmed


SENT_INSTAGRAM_MESSAGE_IDS = []


def remember_sent_instagram_message(result):
    """Biz yuborgan Instagram xabar id sini eslab qolamiz.

    Faqat bitta so'rov davomida kerak: albom yuborilgach uning echo'si darhol
    keladi va o'sha paytda bazada hali hech narsa yozilmagan bo'ladi.
    """
    if not isinstance(result, dict):
        return
    message_id = result.get("message_id") or ""
    if not message_id:
        return
    SENT_INSTAGRAM_MESSAGE_IDS.append(message_id)
    del SENT_INSTAGRAM_MESSAGE_IDS[:-200]


def record_outbound_platform_message(conversation, message_id):
    """Biz yuborgan platforma xabarining id sini darhol bazaga yozadi.

    Xotiradagi ro'yxat faqat bitta celery jarayonida yashaydi, echo esa boshqasiga
    tushishi mumkin. Baza ikkalasi uchun ham umumiy.
    """
    if not conversation or not message_id:
        return
    remember_sent_instagram_message({"message_id": message_id})
    Message.objects.create(conversation=conversation, sender="system", text="", metadata={"outbound_platform_message": {"message_id": message_id}})


def send_catalog_album_chunk(customer, platform, chat_id, chunk, conversation=None):
    """Bitta albom xabarini yuboradi. (delivered, detail, result) qaytaradi, exception ko'tarmaydi.

    Do'konning bir nechta Instagram akkaunti bor. Mijoz qaysi akkauntga yozgan bo'lsa,
    javob ham o'sha akkauntdan ketishi kerak — bo'lmasa Instagram 400 qaytaradi.
    """
    account_id = conversation_instagram_account_id(conversation) if conversation else None
    try:
        if platform == "telegram":
            if len(chunk) == 1:
                result = telegram_send_image(chat_id, chunk[0]["image_url"], caption=chunk[0]["caption"])
            else:
                result = telegram_send_media_group(chat_id, [{"image_url": row["image_url"], "caption": row["caption"]} for row in chunk])
        else:
            result = instagram_send_carousel(chat_id, [{"title": row["title"], "subtitle": row["subtitle"], "image_url": row["image_url"]} for row in chunk], account_id)
    except Exception as error:
        print(f"CATALOG_ALBUM_FAILED customer={customer.id} platform={platform} count={len(chunk)} error={error}", flush=True)
        return False, "album_failed", None
    if isinstance(result, dict) and result.get("mocked"):
        return True, "mocked", result
    if isinstance(result, dict) and result.get("ok") is False:
        print(f"CATALOG_ALBUM_REJECTED customer={customer.id} platform={platform} result={result}", flush=True)
        return False, "album_rejected", result
    # Instagram yuborilgan xabarni webhook orqali bizga qaytaradi. Uning id sini
    # eslab qolmasak, o'z albomimiz "operator javob yozdi" deb hisoblanadi va AI
    # o'zini o'n besh daqiqaga to'xtatib qo'yadi.
    remember_sent_instagram_message(result)
    return True, "album", result


def _stock_batch_for_ai(query="", batch_id=None):
    queryset = StockBatch.objects.filter(is_active=True, remaining_stems__gt=0).select_related("variant__flower").order_by("received_at", "id")
    if batch_id:
        return queryset.filter(id=batch_id).first()
    query = (query or "").strip()
    if not query:
        return queryset.first()
    rows = ai_stock_rows(query, limit=20)
    ids = [row["batch_id"] for row in rows if row.get("batch_id") and row.get("has_image")]
    if ids:
        return queryset.filter(id=ids[0]).first()
    ids = [row["batch_id"] for row in rows if row.get("batch_id")]
    if ids:
        return queryset.filter(id=ids[0]).first()
    terms = ai_search_terms(query)
    ranked = []
    for batch in queryset:
        haystack = flower_variant_search_haystack(batch.variant)
        score = sum(1 for term in terms if haystack_has_term(haystack, term))
        if score:
            ranked.append((score, batch))
    if ranked:
        return sorted(ranked, key=lambda row: (-row[0], row[1].received_at, row[1].id))[0][1]
    return None


def send_stock_batch_image(conversation, batch):
    customer = conversation.customer
    image_url = stock_image_url(batch)
    if not image_url:
        return {"ok": False, "detail": "image_not_found", "batch_id": batch.id, "stock_name": flower_variant_display_name(batch.variant, "uz")}
    delivered, detail, sent = send_image_to_customer(customer, image_url, conversation)
    Message.objects.create(conversation=conversation, sender="system", text="", metadata={"image_tool_result": {"stock_batch_id": batch.id, "stock_name": flower_variant_display_name(batch.variant, "uz"), "image_url": image_url, "delivered": delivered, "detail": detail, "sent": sent}})
    if not delivered:
        return {"ok": False, "image_sent": False, "detail": detail, "batch_id": batch.id, "stock_name": flower_variant_display_name(batch.variant, "uz")}
    return {"ok": True, "image_sent": True, "batch_id": batch.id, "stock_name": flower_variant_display_name(batch.variant, "uz"), "image_url": image_url}


def execute_ai_tool(name, arguments, conversation, tool_results=None):
    customer = conversation.customer
    if name in AI_HIDDEN_STOCK_TOOLS:
        return stock_hidden_result(name)
    if name == "client_leads_get":
        limit = max(1, min(int(arguments.get("limit") or 5), 20))
        return {"leads": recent_customer_orders(customer)[:limit]}
    if name == "get_catalog":
        return ai_catalog_result(
            arguments.get("query") or "",
            limit=80,
            arrangement_type=arguments.get("arrangement_type") or "",
            min_price=arguments.get("min_price"),
            max_price=arguments.get("max_price"),
        )
    if name == "send_catalog_album":
        catalog_ids = [int(value) for value in (arguments.get("catalog_ids") or []) if str(value).isdigit() or isinstance(value, int)]
        blocked = media_match_send_block(tool_results, name, catalog_ids)
        if blocked:
            return blocked
        # Mijoz summa aytgan bo'lsa o'sha summaga chiqqan mahsulotlarning
        # HAMMASI ko'rsatiladi. Model ba'zan to'rttadan uchtasini tanlab
        # qolganini tashlab ketardi va mijoz mavjud gulni ko'rmasdi.
        catalog_ids = complete_budget_album_ids(catalog_ids, tool_results)
        items = catalog_album_items(catalog_ids)
        if not items:
            return {"ok": False, "detail": "catalog_empty", "items": [], "not_sent": []}
        if not catalog_ids and whole_catalog_already_sent(conversation):
            # Butun katalogni ikkinchi marta yuborish mijozga yangi hech narsa
            # bermaydi — u allaqachon hammasini ko'rgan.
            return {
                "ok": False,
                "detail": "catalog_already_sent",
                "instruction_uz": (
                    "Butun katalog bu suhbatda allaqachon yuborilgan, qayta yuborma. "
                    "Mijoz katalogdan hech narsa tanlamayotgan bo'lsa 00H bo'limi bo'yicha "
                    "operatorga bog'lashni taklif qil. "
                    "Telefon raqami so'rama."
                ),
            }
        return send_catalog_album(conversation, items, whole_catalog=not catalog_ids)
    if name == "send_catalog_image":
        query = arguments.get("query") or ""
        catalog_id = arguments.get("catalog_id")
        blocked = media_match_send_block(tool_results, name, [catalog_id] if catalog_id else [])
        if blocked:
            return blocked
        item = catalog_album_queryset().filter(id=catalog_id).first() if catalog_id else None
        if not item:
            item = _catalog_item_for_ai(query)
        if not item:
            return {"ok": False, "detail": "catalog_not_found"}
        if catalog_image_already_sent(conversation, item.id):
            # Mijoz bu rasmni allaqachon ko'rgan. Uni qayta yuborish "yana qanaqalari
            # bor" degan savolga javob emas — o'sha savolni yana bir marta bermoqda.
            # Rad etib qo'yish ham yetarli emas: model o'shanda ba'zan hech narsa
            # yubormay javobini takrorlab qo'yardi. Shuning uchun o'rniga butun
            # katalogni yuboramiz — mijoz aynan shuni so'ragan.
            if whole_catalog_already_sent(conversation):
                return {
                    "ok": False,
                    "detail": "catalog_image_already_sent",
                    "catalog_id": item.id,
                    "instruction_uz": (
                        f"{item.name} rasmi ham, butun katalog ham bu suhbatda allaqachon "
                        "yuborilgan. Hech narsa yuborma. 00H bo'limi bo'yicha operatorga "
                        "bog'lashni taklif qil."
                    ),
                }
            album = send_catalog_album(conversation, catalog_album_items([]), whole_catalog=True)
            return {
                "ok": True,
                "detail": "catalog_sent_instead",
                "catalog_id": item.id,
                "album": album,
                "instruction_uz": (
                    f"{item.name} rasmi allaqachon yuborilgani uchun uning o'rniga butun "
                    "katalog albom qilib yuborildi. Mijozga hozirda bor gullar shu ekanini "
                    "ayt va qaysi biri yoqishini so'ra. O'sha mahsulotni qayta taklif qilma."
                ),
            }
        return send_catalog_item_image(conversation, item)
    if name == "send_post_image":
        return send_social_post_image(conversation, arguments.get("social_post_id"), tool_results=tool_results)
    if name == "match_ai_catalog_by_media":
        return match_ai_catalog_by_media(
            conversation,
            source_url=arguments.get("source_url") or "",
            user_text=arguments.get("user_text") or "",
        )
    if name == "delivery_location_link":
        from .location_services import location_link

        lead = conversation.leads.order_by("-created_at", "-id").first()
        if not lead:
            return {"ok": False, "detail": "no_lead_yet",
                    "instruction_uz": "Buyurtma hali ochilmagan. Avval mijoz gulni tanlashi kerak."}
        link = location_link(lead)
        if not link:
            return {"ok": False, "detail": "link_not_configured", "link": "",
                    "instruction_uz": "Xarita havolasi sozlanmagan. Mijozdan manzilni matn bilan "
                                      "yozib yuborishini so'ra."}
        return {"ok": True, "link": link, "lead_id": lead.id,
                "instruction_uz": "Havolani mijozga aynan shu ko'rinishda yoz va xaritada "
                                  "manzilini belgilab tanlash tugmasini bosishini so'ra. "
                                  "Bitta qator yetarli, boshqa savol qo'shma."}
    if name == "call_operator":
        if (not conversation.messages.filter(sender="ai").exists()
                and conversation_started_from_an_ad(conversation)
                and not conversation.leads.exists()):
            # Reklamadagi tugma bosilganda Instagram auto-matn yuboradi
            # ("Можно заказать?", "Buyurtma bermoqchi edim"). Bu operator
            # uchun hech qanday ma'lumot bermaydi — mijoz avval katalogni
            # ko'rsin. Haqiqiy savol bo'lsa keyingi navbatda chaqiriladi.
            return {"ok": False, "detail": "ad_opening_message",
                    "instruction_uz": AD_OPENING_INSTRUCTION}
        if unlinked_shared_media_silence(conversation):
            # Tizimda yo'q reel operatorni chaqirish sababi emas. Operatorlar
            # guruhiga ketgan 77 xabarning ko'pi aynan shundan kelib chiqqan va
            # ularning hech biri bilan operator biror ish qila olmaydi.
            return {"ok": False, "detail": "unlinked_shared_media",
                    "instruction_uz": MEDIA_MATCH_UNLINKED_SHARED_MEDIA_INSTRUCTION}
        if operator_already_told_recently(conversation):
            # Operatorlar bu suhbat haqida yaqinda xabar olgan. Ikkinchi karta
            # yangi ma'lumot bermaydi — real suhbat 2221 da model o'zi bir
            # daqiqa ichida ikki marta chaqirgan edi.
            return {"ok": True, "detail": "operator_already_notified",
                    "instruction_uz": ("Operatorlar bu suhbatdan allaqachon xabardor. "
                                       "Mijozga odatdagidek javob ber, qayta chaqirma.")}
        return dict(notify_operator_needed(conversation, (arguments.get("reason") or "").strip()),
                    instruction_uz=("Mijoz rozilik berdi — javobda shu mazmunni yoz: rahmat, operatorlarimiz "
                                    "sizga tez orada yozib yuborishadi. Telegram username BERMA, "
                                    "telefon so'rama, lead yaratma."))
    if name == "client_payment_update":
        from . import payment_services

        lead = conversation.leads.order_by("-created_at", "-id").first()
        if not lead:
            return {"ok": False, "detail": "no_lead_yet",
                    "instruction_uz": "Bu suhbatda buyurtma hali yo'q. To'lov haqida gaplashishdan "
                                      "oldin mijoz gulni tanlashi kerak."}
        receipt_url = (arguments.get("receipt_url") or "").strip()
        payment_type = (arguments.get("payment_type") or "").strip()
        if receipt_url:
            return dict(payment_services.register_receipt(lead, receipt_url), lead_id=lead.id)
        if payment_type:
            return dict(payment_services.set_payment_type(lead, payment_type), lead_id=lead.id)
        return {"ok": False, "detail": "nothing_to_update"}
    if name not in {"client_lead_create", "client_lead_edit"}:
        return {"ok": False, "detail": "unknown_tool"}
    name_value = (arguments.get("customer_name") or "").strip()
    phone_value = normalize_phone(arguments.get("phone") or "")
    customer_changed = []
    if valid_customer_name(name_value) and not valid_customer_name(customer.name):
        customer.name = name_value[:160]
        customer_changed.append("name")
    if phone_value:
        customer.phone = phone_value
        customer_changed.append("phone")
    if customer_changed:
        customer.save(update_fields=list(set(customer_changed)) + ["updated_at"])
    request_text = (arguments.get("request_text") or "").strip()
    estimated_price = arguments.get("estimated_price")
    arrangement_type = arguments.get("arrangement_type") or ""
    fulfillment = arguments.get("fulfillment") or ""
    delivery_address = (arguments.get("delivery_address") or "").strip()
    desired_date = parse_lead_date(arguments.get("desired_date"))
    desired_time = (arguments.get("desired_time") or "").strip()
    details = {
        "catalog_items": ai_catalog_lead_rows(arguments, conversation, estimated_price),
        "stock_items": arguments.get("stock_items") or [],
        "note": arguments.get("note") or "",
        "created_by": "ai_tool",
        **lead_request_details(arguments),
    }
    if name == "client_lead_edit":
        lead = Lead.objects.filter(id=arguments.get("lead_id"), customer=customer).first()
        if not lead:
            return {"ok": False, "detail": "lead_not_found"}
        fields = []
        if request_text:
            lead.request_uz = request_text
            fields.append("request_uz")
        if arguments.get("status"):
            lead.status = arguments["status"][:40]
            fields.append("status")
        if arrangement_type:
            lead.arrangement_type = arrangement_type
            fields.append("arrangement_type")
        if estimated_price is not None:
            lead.estimated_price = Decimal(str(estimated_price))
            fields.append("estimated_price")
        if fulfillment in {"delivery", "pickup"}:
            lead.fulfillment = fulfillment
            fields.append("fulfillment")
        if delivery_address:
            lead.delivery_address = delivery_address[:255]
            fields.append("delivery_address")
        if desired_date:
            lead.desired_date = desired_date
            fields.append("desired_date")
        if desired_time:
            lead.desired_time = desired_time[:20]
            fields.append("desired_time")
        # Tahrirda faqat yuborilgan maydon yangilanadi. Butun details ni almashtirsak
        # avvalgi izoh, gul turi va rasm havolasi yo'qolib ketardi.
        changed_details = {key: value for key, value in details.items() if value not in (None, "", [], {})}
        changed_details.pop("created_by", None)
        if changed_details:
            lead.details = {**(lead.details or {}), **changed_details}
            fields.append("details")
        if fields:
            lead.save(update_fields=list(set(fields)) + ["updated_at"])
        if arguments.get("stock_items") is not None:
            lead.stock_usage.all().delete()
            for row in arguments.get("stock_items") or []:
                batch = StockBatch.objects.filter(id=row.get("batch_id"), is_active=True).first()
                quantity_stems = int(row.get("quantity_stems") or 0)
                if batch and quantity_stems > 0:
                    LeadStockUsage.objects.create(lead=lead, stock_batch=batch, quantity_stems=quantity_stems, quantity_bunches=Decimal(str(row.get("quantity_bunches") or 0)))
        save_conversation_ai_summary(conversation, lead)
        # Sana keyin aytilgan bo'lsa eslatma shu yerda qo'yiladi.
        from .recall_services import schedule_from_desired_date

        schedule_from_desired_date(lead)
        return {"ok": True, "lead_id": lead.id}
    if not request_text:
        return {"ok": False, "detail": "request_text_required"}
    if not valid_customer_name(customer.name):
        return {"ok": False, "detail": "customer_name_required"}
    if not customer.phone:
        return {"ok": False, "detail": "phone_required"}
    lead = Lead.objects.create(
        customer=customer,
        conversation=conversation,
        social_post=conversation.social_post,
        request_uz=request_text,
        arrangement_type=arrangement_type,
        estimated_price=Decimal(str(estimated_price)) if estimated_price is not None else None,
        # Florist haqini operator CRM da belgilaydi. AI uni bilmaydi va yozmaydi.
        florist_fee=Decimal("0"),
        fulfillment=fulfillment if fulfillment in {"delivery", "pickup"} else "",
        delivery_address=delivery_address[:255],
        desired_date=desired_date,
        desired_time=desired_time[:20],
        details=details,
        source="telegram" if customer.instagram_user_id.startswith("telegram:") else "instagram",
    )
    for row in arguments.get("stock_items") or []:
        batch = StockBatch.objects.filter(id=row.get("batch_id"), is_active=True).first()
        quantity_stems = int(row.get("quantity_stems") or 0)
        if batch and quantity_stems > 0:
            LeadStockUsage.objects.create(lead=lead, stock_batch=batch, quantity_stems=quantity_stems, quantity_bunches=Decimal(str(row.get("quantity_bunches") or 0)))
    Notification.objects.create(notification_type="lead", title_uz=f"Yangi lead: {customer}", title_ru=f"Новый лид: {customer}", body_uz=request_text, body_ru=request_text, reference_type="lead", reference_id=lead.id)
    save_conversation_ai_summary(conversation, lead)
    # Mijoz sanani aytgan bo'lsa o'sha kun ertalab 9:00 ga eslatma qo'yiladi.
    from .recall_services import schedule_from_desired_date

    schedule_from_desired_date(lead)
    # Operatorlar leadni CRM ni ochib emas, Telegram guruhida ko'radi. Yuborilmasa
    # buyurtma bazada yotib qoladi va hech kim mijozga qo'ng'iroq qilmaydi.
    notified = notify_operators_about_lead(lead, conversation)
    return {"ok": True, "lead_id": lead.id, "operators_notified": bool(notified.get("ok"))}


def lead_payment_type(lead):
    """Leadda saqlangan to'lov turi: "cash", "card" yoki bo'sh.

    Bo'sh qiymat "to'lov turi hali so'ralmagan" degani — AI shu maydonga qarab
    savolni takrorlamaydi yoki tashlab ketmaydi.
    """
    if not lead:
        return ""
    from . import payment_services

    return payment_services.payment_state(lead).get("type") or ""


def ai_response_schema():
    return {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "detected_language": {"type": "string", "enum": ["uz", "ru"]},
            "customer_name": {"type": ["string", "null"]},
            "phone": {"type": ["string", "null"]},
            "lead_ready": {"type": "boolean"},
            "lead_request": {"type": ["string", "null"]},
            "arrangement_type": {"type": ["string", "null"], "enum": ["bouquet", "basket", "catalog", None]},
            "estimated_price": {"type": ["number", "null"]},
            "handoff": {"type": "boolean"},
            "catalog_items": {
                "type": "array",
                "items": {"type": "object", "properties": {"catalog_name": {"type": "string"}, "quantity": {"type": "integer"}}, "required": ["catalog_name", "quantity"], "additionalProperties": False},
            },
        },
        "required": ["reply", "detected_language", "customer_name", "phone", "lead_ready", "lead_request", "arrangement_type", "estimated_price", "handoff", "catalog_items"],
        "additionalProperties": False,
    }


def ai_follow_up_schema():
    return {
        "type": "object",
        "properties": {
            "send_follow_up": {"type": "boolean"},
            "message": {"type": ["string", "null"]},
            "reason": {"type": "string"},
        },
        "required": ["send_follow_up", "message", "reason"],
        "additionalProperties": False,
    }


def ai_reply(conversation):
    customer = conversation.customer
    history_messages = list(conversation.messages.exclude(sender="system").order_by("created_at", "id"))
    fresh_session = bool(len(history_messages) > 1 and history_messages[-1].created_at - history_messages[-2].created_at >= timedelta(hours=24))
    latest_customer_text = next((message.text for message in reversed(history_messages) if message.sender == "customer"), "")
    latest_ai_index = max((index for index, message in enumerate(history_messages) if message.sender == "ai"), default=-1)
    pending_customer_messages = [message.text for message in history_messages[latest_ai_index + 1:] if message.sender == "customer"]
    # O'zbek kirill suhbatda model lotinda aniqroq yozadi. Kirill matnni lotinga o'girib beramiz,
    # javobni esa oxirida kirillga qaytaramiz. Rus tiliga tegilmaydi.
    customer_script = conversation_script(pending_customer_messages or [latest_customer_text])
    # Ruscha mijozning qisqa xabarlarida ("На сегодня", "Карта") til belgisi
    # bo'lmaydi va detect_text_script ularni o'zbekcha deb hisoblaydi. Real
    # suhbatda shu sabab rus mijozga butun buyurtma o'zbek kirilida yozilgan.
    # Butun suhbatda ruscha dalil bo'lib, o'zbekchasi umuman bo'lmasa javob
    # rus tilida ketadi. Qolgan holatlarda yuqoridagi qaror o'zgarmaydi.
    if conversation_reply_script(conversation) == "ru":
        customer_script = "ru"
    cyrillic_mode = customer_script == "uz_cyril"
    history = []
    for message in history_messages:
        content = message.text
        if message.metadata:
            content = json.dumps({"text": message.text, "metadata": message.metadata}, ensure_ascii=False, default=str)
        if cyrillic_mode:
            content = cyrillic_to_latin(content)
        history.append({"role": "user" if message.sender == "customer" else "assistant", "content": content})
    # "Sessiya" — bu joriy suhbat, butun tarix emas. Mijoz ertasi kuni qaytganda
    # kechagi javoblarimiz "bu suhbatda allaqachon salomlashdik" degani emas —
    # aks holda u salomsiz, "Ahmad, sizga qanday gul kerak?" bilan kutib olinadi.
    session_messages = history_messages[-1:] if fresh_session else history_messages
    ai_replies_count = sum(1 for message in session_messages if message.sender == "ai")
    has_ai_reply_in_session = ai_replies_count > 0
    last_customer_message = next((message.text for message in reversed(history_messages) if message.sender == "customer"), "")
    business_settings, _ = BusinessSettings.objects.get_or_create(pk=1)
    ai_settings, _ = AISettings.objects.get_or_create(pk=1)
    open_lead = conversation.leads.order_by("-created_at", "-id").first()
    working_hours = business_settings.working_hours or {}
    context = {
        "today": timezone.localdate().isoformat(),
        "now": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
        "customer": {
            "name": customer.name if valid_customer_name(customer.name) else "",
            "phone": customer.masked_phone,
            "has_phone": bool(customer.phone),
            "instagram_username": customer.instagram_username,
            "previous_orders_count": customer.leads.count(),
            "is_returning": bool(valid_customer_name(customer.name) and customer.phone),
            "last_delivery_address": (customer.leads.exclude(delivery_address="").order_by("-created_at", "-id").values_list("delivery_address", flat=True).first() or ""),
            **previous_visit_context(conversation, history_messages),
        },
        "conversation": {
            "source": "telegram" if customer.instagram_user_id.startswith("telegram:") else "instagram",
            "fresh_session": fresh_session,
            "has_ai_reply_in_session": has_ai_reply_in_session,
            "pending_customer_messages": pending_customer_messages,
            # Javob tili shu maydondan olinadi. Bitta so'zga qarab til tanlash
            # xato: "доставка", "адрес", "локация" ikkala tilda ham bir xil.
            "customer_script": customer_script,
            "customer_attachments": customer_attachment_rows(history_messages),
            "social_post": ai_post_context(conversation),
            "open_lead": {
                "id": open_lead.id,
                "request": open_lead.request_uz,
                "arrangement_type": open_lead.arrangement_type,
                "estimated_price": str(open_lead.estimated_price) if open_lead.estimated_price is not None else "",
                "fulfillment": open_lead.fulfillment,
                "delivery_address": open_lead.delivery_address,
                "desired_date": open_lead.desired_date.isoformat() if open_lead.desired_date else "",
                "desired_time": open_lead.desired_time,
            } if open_lead else None,
            "already_known": {
                "name": bool(valid_customer_name(customer.name)),
                "phone": bool(customer.phone),
                "fulfillment": open_lead.fulfillment if open_lead else "",
                # Bo'sh bo'lsa to'lov turi hali so'ralmagan — 00J shu maydonga qaraydi.
                "payment_type": lead_payment_type(open_lead),
                "delivery_address": bool(open_lead.delivery_address) if open_lead else False,
                "desired_date": bool(open_lead.desired_date) if open_lead else False,
                "desired_time": bool(open_lead.desired_time) if open_lead else False,
            },
            # Bu suhbatda nima allaqachon yuborilgan va nimaga javob berilgan.
            # Model shu maydonlarga qarab qaysi funksiyani chaqirishni hal qiladi.
            **conversation_state_for_ai(conversation, business_settings, history_messages),
        },
        "business": {
            # florist_fee ataylab berilmaydi: raqamni ko'rsa AI uni katalog narxiga
            # qo'shib "jami" chiqaradi. Ko'rmagan raqamini qo'sha olmaydi.
            "delivery_fee": str(business_settings.delivery_fee),
            "delivery_area_uz": business_settings.delivery_area_uz,
            "delivery_area_ru": business_settings.delivery_area_ru,
            "working_hours_uz": working_hours.get("uz", ""),
            "working_hours_uz_cyril": uz_latin_to_cyril(working_hours.get("uz", "")),
            "working_hours_ru": working_hours.get("ru", ""),
            "shop_address_uz": business_settings.shop_address_uz,
            "shop_address_uz_cyril": business_settings.shop_address_uz_cyril or uz_latin_to_cyril(business_settings.shop_address_uz),
            "shop_address_ru": business_settings.shop_address_ru,
            "shop_orientir_uz": business_settings.shop_orientir_uz,
            "shop_orientir_uz_cyril": business_settings.shop_orientir_uz_cyril or uz_latin_to_cyril(business_settings.shop_orientir_uz),
            "shop_orientir_ru": business_settings.shop_orientir_ru,
            "shop_location_link": business_settings.shop_location_link,
            "shop_phone": business_settings.shop_phone,
            "operator_phone": business_settings.operator_phone or business_settings.shop_phone,
            # AI javob berolmaydigan savol yoki aniqlanmagan gul shu akkauntga yo'naltiriladi.
            # operator_telegram ataylab berilmaydi: handle'ni ko'rsa model uni
            # javobga yozib qo'yadi va mijozni hech qayerga yozmasligi kerak
            # bo'lgan akkauntga yuboradi. Real chatlarda 54 marta shunday bo'ldi.
            # Ko'rmagan handle'ni yoza olmaydi.
            "operator_telegram_text": operator_telegram_text(business_settings.operator_telegram),
            "operator_hours_uz": business_settings.operator_hours,
            "operator_hours_ru": business_settings.operator_hours_ru,
        },
    }
    api_key = openai_api_key()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    client = OpenAI(api_key=api_key)
    model_input = [{"role": "user", "content": "REAL_CONTEXT_JSON:\n" + json.dumps(context, ensure_ascii=False)}]
    model_input += history
    effective_instructions = MEDIA_MATCHING_PRIORITY_INSTRUCTION.strip() + "\n\n" + (ai_settings.system_prompt or "")
    response_kwargs = {
        "model": ai_settings.openai_model or settings.OPENAI_MODEL,
        "instructions": effective_instructions,
        "input": model_input,
        "max_output_tokens": 8000,
        "reasoning": {"effort": ai_settings.reasoning_effort or "low"},
        "tools": ai_tool_definitions(),
        "parallel_tool_calls": False,
        "text": {"format": {"type": "json_schema", "name": "sales_reply", "strict": True, "schema": ai_response_schema()}},
    }
    response = client.responses.create(**response_kwargs)
    tool_results = []
    for _ in range(10):
        calls = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", "") == "function_call":
                calls.append(item)
        if not calls:
            break
        tool_outputs = []
        for call in calls:
            try:
                arguments = json.loads(call.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            output = execute_ai_tool(call.name, arguments, conversation, tool_results=tool_results)
            tool_results.append({"name": call.name, "arguments": arguments, "output": output})
            tool_outputs.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": json.dumps(output, ensure_ascii=False, default=str),
            })
        response = client.responses.create(
            model=ai_settings.openai_model or settings.OPENAI_MODEL,
            instructions=effective_instructions,
            previous_response_id=response.id,
            input=tool_outputs,
            max_output_tokens=8000,
            reasoning={"effort": ai_settings.reasoning_effort or "low"},
            tools=ai_tool_definitions(),
            parallel_tool_calls=False,
            text={"format": {"type": "json_schema", "name": "sales_reply", "strict": True, "schema": ai_response_schema()}},
        )
    try:
        result = json.loads(response.output_text)
    except json.JSONDecodeError:
        status_detail = getattr(response, "status", "") or ""
        incomplete = getattr(response, "incomplete_details", None)
        print(f"OPENAI_JSON_DECODE_FAILED conversation={conversation.id} status={status_detail} incomplete={incomplete} output={response.output_text!r}", flush=True)
        response_kwargs["max_output_tokens"] = 16000
        response_kwargs["reasoning"] = {"effort": "low"}
        response = client.responses.create(**response_kwargs)
        try:
            result = json.loads(response.output_text)
        except json.JSONDecodeError:
            print(f"OPENAI_JSON_DECODE_FAILED_RETRY conversation={conversation.id} status={getattr(response, 'status', '')} output={response.output_text!r}", flush=True)
            raise
    result.setdefault("catalog_items", [])
    result.setdefault("stock_items", [])
    if tool_results:
        result["tool_results"] = tool_results
        result = apply_media_match_safeguard(conversation, result, tool_results)
    # Bu qorovul tool ishlamagan navbatda ham kerak — aynan o'shanda AI hech
    # narsa yubormay turib "yubordim" deb yozib qo'ygan edi.
    # Reklamadan kelgan birinchi xabar: katalog har holda ko'rsatiladi.
    result = apply_ad_opening_safeguard(conversation, result, tool_results)
    result = apply_sent_photo_claim_safeguard(conversation, result, tool_results)
    # Operator va'dasi ham xuddi shunday: tool ishlamagan navbatda ham tekshiriladi.
    result = apply_operator_promise_safeguard(conversation, result, tool_results)
    if cyrillic_mode and result.get("reply"):
        result["reply_latin"] = result["reply"]
        result["reply"] = latin_to_cyrillic(result["reply"])
    created_leads = [row["output"].get("lead_id") for row in tool_results if row.get("name") == "client_lead_create" and row.get("output", {}).get("ok")]
    if created_leads:
        result["lead_created_id"] = created_leads[-1]
        result["lead_ready"] = False
    return result


def ai_follow_up_decision(conversation, expected_ai_message):
    customer = conversation.customer
    history_messages = list(conversation.messages.exclude(sender="system").order_by("created_at", "id"))
    latest_customer_message = next((message.text for message in reversed(history_messages) if message.sender == "customer"), "")
    history = []
    for message in history_messages:
        content = message.text
        if message.metadata:
            content = json.dumps({"text": message.text, "metadata": message.metadata}, ensure_ascii=False, default=str)
        history.append({"role": "user" if message.sender == "customer" else "assistant", "content": content})
    context = {
        "customer": {
            "name": customer.name if valid_customer_name(customer.name) else "",
            "phone": customer.masked_phone,
            "has_phone": bool(customer.phone),
        },
        "conversation": {
            "id": conversation.id,
            "source": "telegram" if customer.instagram_user_id.startswith("telegram:") else "instagram",
            "latest_customer_message": latest_customer_message,
            "expected_ai_message_id": expected_ai_message.id,
            "expected_ai_message": expected_ai_message.text,
            "expected_ai_metadata": expected_ai_message.metadata or {},
            "leads_count": conversation.leads.count(),
        },
    }
    api_key = openai_api_key()
    if not api_key:
        return {"send_follow_up": False, "message": None, "reason": "openai_api_key_missing"}
    ai_settings, _ = AISettings.objects.get_or_create(pk=1)
    instructions = (
        "Sen EUROFLOWERS PREMIUM uchun follow up qarorini chiqarasan.\n"
        "Chat oxirgi AI javobidan keyin 30 minut jim turgan bo‘lsa, faqat kerakli holatda bitta tabiiy follow up yoz.\n"
        "Shablon yozma, conversationni to‘liq tahlil qil.\n"
        "Agar lead yaratilgan bo‘lsa, ism va raqam olingan bo‘lsa, mijoz rad etsa, boshqa joydan olaman desa, yoqmadi desa, hop yaxshi rahmat yoki shunga o‘xshash yopuvchi gap yozgan bo‘lsa send_follow_up false qil.\n"
        "Agar AI katalog, rasm, narx yoki custom buket hisobini ko‘rsatganidan keyin mijoz jim qolgan bo‘lsa, budjetiga mos variantni operatorlar ko‘rsatishi mumkinligini tabiiy aytib ism va raqamini so‘ra.\n"
        "Agar mijoz faqat salom yoki bitta noaniq xabar yozib jim qolgan bo‘lsa, qanday gul kerakligini, vitrinadan ko‘rishini yoki o‘zi yig‘dirmoqchiligini qisqa so‘ra.\n"
        "Mijoz qaysi tilda va qaysi yozuvda yozgan bo‘lsa, message ham aynan o‘sha tilda va yozuvda bo‘lsin.\n"
        "Savollarni ko‘paytirma, bitta qisqa premium ohangdagi follow up yoz.\n"
        "Javob faqat JSON schema bo‘yicha bo‘lsin."
    )
    client = OpenAI(api_key=api_key)
    reply_script = conversation_reply_script(conversation)
    response = client.responses.create(
        model=ai_settings.openai_model or settings.OPENAI_MODEL,
        instructions=instructions,
        input=[
            {"role": "user", "content": "REAL_CONTEXT_JSON:\n" + json.dumps(context, ensure_ascii=False, default=str)},
            *history,
        ],
        max_output_tokens=3000,
        reasoning={"effort": ai_settings.reasoning_effort or "low"},
        text={"format": {"type": "json_schema", "name": "follow_up_decision", "strict": True, "schema": ai_follow_up_schema()}},
    )
    try:
        data = json.loads(response.output_text)
    except json.JSONDecodeError:
        print(f"OPENAI_FOLLOW_UP_JSON_DECODE_FAILED conversation={conversation.id} output={response.output_text!r}", flush=True)
        return {"send_follow_up": False, "message": None, "reason": "json_decode_failed"}
    if reply_script in ["uz_cyril", "uz_latin"] and data.get("send_follow_up"):
        customer.language = "uz"
        customer.save(update_fields=["language", "updated_at"])
    elif reply_script == "ru" and data.get("send_follow_up"):
        customer.language = "ru"
        customer.save(update_fields=["language", "updated_at"])
    return data


def ingest_customer_message(conversation, message_text, instagram_message_id="", metadata=None):
    if instagram_message_id and Message.objects.filter(instagram_message_id=instagram_message_id, conversation=conversation).exists():
        return None
    message = Message.objects.create(conversation=conversation, sender="customer", text=message_text, instagram_message_id=instagram_message_id, metadata=metadata or {})
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=["last_message_at", "updated_at"])
    return message


def ai_reply_wait_seconds_remaining(conversation_id, expected_message_id, wait_seconds=None):
    conversation = Conversation.objects.filter(id=conversation_id).first()
    if not conversation:
        return None
    latest = conversation.messages.filter(sender="customer").order_by("-created_at", "-id").first()
    if not latest or latest.id != expected_message_id:
        return None
    elapsed = (timezone.now() - latest.created_at).total_seconds()
    return max(0, (wait_seconds or AI_REPLY_WAIT_SECONDS) - elapsed)


def compact_match_text(value):
    return re.sub(r"[^a-zа-я0-9]+", " ", (value or "").lower()).strip()


def _catalog_text_aliases(text):
    aliases = {text}
    replacements = {
        "пиони": "pion",
        "пион": "pion",
        "пионл": "pion",
        "атиргул": "atirgul",
        "атиргулл": "atirgul",
        "роза": "atirgul",
        "розы": "atirgul",
        "пушти": "pushti",
        "кизил": "qizil",
        "қизил": "qizil",
        "гортензия": "gortenziya",
        "гортензи": "gortenziya",
    }
    expanded = text
    for source, target in replacements.items():
        expanded = expanded.replace(source, target)
    aliases.add(expanded)
    return aliases


def _catalog_item_for_ai(*values):
    texts = []
    for value in values:
        if value:
            texts.extend(_catalog_text_aliases(compact_match_text(value)))
    available_items = list(available_ai_catalog_queryset())
    for text in texts:
        exact = [item for item in available_items if compact_match_text(item.name) == text]
        if len(exact) == 1:
            return exact[0]
    substring_matches = []
    for text in texts:
        for item in available_items:
            name = compact_match_text(item.name)
            if name and name in text:
                substring_matches.append((len(name), item))
    if substring_matches:
        return sorted(substring_matches, key=lambda row: row[0], reverse=True)[0][1]
    scored = []
    ignored = {"buketi", "buket", "guldasta", "kompozitsiya", "kompazitsia", "kompozitsiya"}
    for text in texts:
        text_tokens = set(text.split())
        for item in available_items:
            name = compact_match_text(item.name)
            tokens = [token for token in name.split() if token not in ignored]
            if not tokens:
                continue
            matched = sum(1 for token in tokens if token in text_tokens)
            if matched >= 2:
                scored.append((matched, matched / max(len(tokens), 1), len(name), item))
            if len(tokens) == 1 and len(tokens[0]) >= 4 and tokens[0] in text_tokens:
                matches = [row for row in available_items if tokens[0] in compact_match_text(row.name).split()]
                if len(matches) == 1:
                    return item
    if scored:
        scored.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
        best = scored[0]
        tied = [row for row in scored if row[:3] == best[:3]]
        if len(tied) == 1:
            return best[3]
    return None


def money_uz(value):
    try:
        amount = int(Decimal(str(value)))
    except Exception:
        return str(value or "")
    return f"{amount:,}".replace(",", " ")


def money_text(value, script="latin"):
    """Narx qatori javob tiliga mos valyuta so'zi bilan."""
    return f"{money_uz(value)} " + ("сум" if script == "ru" else "so'm")


# Katalog nomlari faqat o'zbekcha saqlanadi va erkin yozilgan: "Oq Atir Guldan
# Kompazitsia", "Buket Kotta Shoxli Bambastic Gulidan Yasalgan". Ruscha suhbatda
# modelga tayyor ruscha nom beriladi — o'zbekcha nomni ko'rmasa ko'chirolmaydi.
# Nom uch bo'lakka ajratiladi: mahsulot turi (savat, buket, kompazitsia),
# sifatlar (katta, shoxli, rang) va nav nomi. Ruschada tur oldinga chiqadi,
# qolgani "iz" dan keyin qaratqichda yoziladi. Nav nomi lotinda qoladi.
CATALOG_KIND_RU = {
    "savat": ("Корзина", "f", 0), "savatli": ("Корзина", "f", 0),
    "savatcha": ("Корзинка", "f", 0), "buket": ("Букет", "m", 0),
    "karobka": ("Коробка", "f", 0), "quti": ("Коробка", "f", 0), "box": ("Коробка", "f", 0),
    "kompazitsia": ("композиция", "f", 1), "kompazitsiya": ("композиция", "f", 1),
    "kompozitsia": ("композиция", "f", 1), "kompozitsiya": ("композиция", "f", 1),
    "kompazitsa": ("композиция", "f", 1), "kompozitsiyasi": ("композиция", "f", 1),
    "kompazitsiyasi": ("композиция", "f", 1),
}
# Har juftlik: erkak va ayol rodidagi shakl. Rod mahsulot turidan olinadi —
# "Букет" erkak, "Корзина" va "композиция" ayol.
CATALOG_ADJ_RU = {
    "katta": ("большой", "большая"), "kotta": ("большой", "большая"),
    "kichik": ("маленький", "маленькая"), "shoxli": ("ветвистый", "ветвистая"),
}
# Har juftlik: bosh kelishik va "iz" dan keyingi ko'plik qaratqich shakli.
CATALOG_COLOR_RU = {
    "oq": ("белая", "белых"), "qizil": ("красная", "красных"),
    "pushti": ("розовая", "розовых"), "sariq": ("жёлтая", "жёлтых"),
    "tilla": ("золотая", "золотых"), "yashil": ("зелёная", "зелёных"),
    "binafsha": ("фиолетовая", "фиолетовых"), "qora": ("чёрная", "чёрных"),
    "kok": ("синяя", "синих"), "ko'k": ("синяя", "синих"),
}
CATALOG_FLOWER_RU = {
    "atir": ("Роза", "роз"), "atirgul": ("Роза", "роз"),
    "atirguldan": ("Роза", "роз"), "atirgulidan": ("Роза", "роз"),
    "lola": ("Тюльпан", "тюльпанов"), "pion": ("Пион", "пионов"),
    "xrizantema": ("Хризантема", "хризантем"),
}
# Ruschada tarjimasi yo'q, ma'no ham qo'shmaydigan o'zbekcha qo'shimchalar.
CATALOG_SKIP_RU = {
    "gul", "gullar", "gulli", "gulidan", "guldan", "gulimizdan", "gullaridan",
    "guli", "gullarimiz", "gullarimizdan", "yasalgan", "ta", "tali", "tasi", "dona",
}


def catalog_name_ru(name):
    """O'zbekcha katalog nomining ruscha ko'rinishi.

    Kirill yozilgan nom ("Оқ Жумила") avval lotinga o'giriladi, aks holda ruscha
    javobga o'zbek kirili tushib qoladi.
    """
    value = cyrillic_to_latin(name or "").strip()
    if not value:
        return ""
    kinds, adjectives = [], []
    count, colors, flowers, rest = "", [], [], []
    # "va" tushib qolmasligi uchun qo'shni so'z qaysi ro'yxatga tushsa, "и" ham
    # o'sha yerga qo'yiladi. Ro'yxatlar keyin qayta tartiblanadi, shuning uchun
    # ikki xil ro'yxat orasidagi bog'lovchi tashlab yuboriladi — "Композиция iz
    # красных роз Jumila" bog'lovchisiz ham to'g'ri jumla.
    pending_and, last_bucket = False, None

    def put(bucket, word):
        nonlocal pending_and, last_bucket
        if pending_and and last_bucket is bucket:
            bucket.append("и")
        bucket.append(word)
        pending_and, last_bucket = False, bucket

    for word in value.split():
        key = word.lower().strip(".,")
        if key == "va":
            pending_and = True
            continue
        if key in CATALOG_KIND_RU:
            kinds.append(CATALOG_KIND_RU[key])
        elif key in CATALOG_ADJ_RU:
            adjectives.append(CATALOG_ADJ_RU[key])
        elif key in CATALOG_COLOR_RU:
            put(colors, CATALOG_COLOR_RU[key])
        elif key in CATALOG_FLOWER_RU:
            put(flowers, CATALOG_FLOWER_RU[key])
        elif key.isdigit():
            count = f"{word} шт"
        elif key in CATALOG_SKIP_RU:
            continue
        else:
            # "Hermossodan" — nav nomiga o'zbekcha "-dan" qo'shimchasi qo'shilgan.
            put(rest, re.sub(r"(?i)dan$", "", word) if len(key) > 6 and key.endswith("dan") else word)
    kinds = sorted(set(kinds), key=lambda kind: kind[2])
    gender = 0 if kinds and kinds[0][1] == "m" else 1
    head = " ".join(adjective[gender] for adjective in adjectives)
    kind = "-".join(item[0] for item in kinds)
    forms = lambda words, index: [word if word == "и" else word[index] for word in words]
    if kind:
        tail = " ".join(word for word in [count] + forms(colors, 1) + forms(flowers, 1) + rest if word)
        kind = f"{head} {kind}" if head else kind
        result = f"{kind} из {tail}" if tail else kind
    else:
        result = " ".join(word for word in
                          [head] + forms(colors, 0) + forms(flowers, 0) + [count] + rest if word)
    return result[:1].upper() + result[1:] if result else ""


def script_evidence(text):
    """Matnda til haqida haqiqiy dalil bormi.

    detect_text_script hech qanday belgi topmasa "uz_cyril" qaytaradi — o'zbek
    mijozlar ko'p bo'lgani uchun shunday qilingan. Lekin "На сегодня" yoki
    "А какая длина" da ham belgi yo'q va ular o'zbekcha emas. Shuning uchun
    dalilsiz xabar ovoz bermaydi, faqat dalillilari sanaladi.
    """
    value = text or ""
    # Bitta so'zga qarab qaror qilinmaydi — ikki tomonning dalillari sanaladi.
    # Real suhbat 2243: "Здравствуйте, мне нужен букет ... за 199.000 сум" da
    # to'rtta ruscha belgi va bitta o'zbekcha belgi bor edi, lekin o'zbekchasi
    # birinchi tekshirilgani uchun darrov g'olib bo'lib qolardi.
    if UZ_CYRIL_LETTERS.search(value):
        return "uz_cyril"
    uz_score = len(UZ_CYRIL_MARKERS.findall(value)) + len(UZ_CYRIL_SUFFIXES.findall(value))
    ru_score = len(RU_MARKERS.findall(value))
    if uz_score > ru_score:
        return "uz_cyril"
    if ru_score > 0:
        return "ru"
    return "uz_cyril" if uz_score else ""


def conversation_reply_script(conversation):
    """Suhbatga qaysi tilda javob berilyapti.

    Albom sarlavhalarini kod o'zi yuboradi, model orqali o'tmaydi — mijoz ruscha
    yozsa sarlavha ham ruscha bo'lishi kerak. Bitta xabar yetarli emas: ruscha
    suhbatda ham belgisiz qisqa xabarlar bo'ladi, shuning uchun oxirgi bir necha
    xabar birga qaraladi. Bittasi ham o'zbekcha bo'lsa suhbat o'zbekcha qoladi.
    """
    texts = list(conversation.messages.filter(sender="customer")
                 .order_by("-created_at", "-id").values_list("text", flat=True)[:8])
    scripts = [script_evidence(text) for text in texts]
    # Ovozlar sanaladi. Ilgari bitta o'zbekcha dalil butun suhbatni o'zbekcha
    # qilib qo'yardi va real suhbat 2243 da sof ruscha suhbat o'zbek kirilida
    # javob oldi. Teng bo'lsa o'zbekcha qoladi — do'konning asosiy tili shu.
    uz_votes = scripts.count("uz_cyril")
    ru_votes = scripts.count("ru")
    if uz_votes or ru_votes:
        return "uz_cyril" if uz_votes >= ru_votes else "ru"
    return conversation_script(texts[:1])


def create_ai_reply_for_conversation(conversation):
    if not ai_allowed_for_conversation(conversation):
        return None
    if conversation.status == "closed":
        return None
    if conversation.ai_paused_until and conversation.ai_paused_until > timezone.now():
        return None
    if conversation.ai_paused_until:
        conversation.ai_paused_until = None
        conversation.ai_pause_reason = ""
        conversation.save(update_fields=["ai_paused_until", "ai_pause_reason", "updated_at"])
    if conversation.status != "ai":
        conversation.status = "ai"
        conversation.save(update_fields=["status", "updated_at"])
    result = ai_reply(conversation)
    customer = conversation.customer
    changed = []
    if valid_customer_name(result.get("customer_name")) and not valid_customer_name(customer.name):
        customer.name = result["customer_name"][:160]
        changed.append("name")
    phone = normalize_phone(result.get("phone"))
    if phone:
        customer.phone = phone
        changed.append("phone")
    if result.get("detected_language") in ["uz", "ru"]:
        customer.language = result["detected_language"]
        changed.append("language")
    if changed:
        customer.save(update_fields=list(set(changed)) + ["updated_at"])
    if result.get("lead_created_id"):
        result["lead_ready"] = False
    reply = Message.objects.create(conversation=conversation, sender="ai", text=result.get("reply", ""), metadata=result)
    if result.get("handoff"):
        Notification.objects.create(notification_type="handoff", title_uz=f"Operator aloqasi kerak: {customer}", title_ru=f"Нужна связь оператора: {customer}", body_uz=result.get("lead_request") or result.get("reply", ""), body_ru=result.get("lead_request") or result.get("reply", ""), reference_type="conversation", reference_id=conversation.id)
    return reply


def process_customer_message(conversation, message_text, instagram_message_id=""):
    message = ingest_customer_message(conversation, message_text, instagram_message_id)
    if not message:
        return None
    return create_ai_reply_for_conversation(conversation)


def should_start_ai_reply(conversation_id, expected_message_id):
    conversation = Conversation.objects.filter(id=conversation_id).first()
    if not conversation:
        return False
    if not ai_allowed_for_conversation(conversation):
        return False
    if conversation.status == "closed":
        return False
    if conversation.ai_paused_until and conversation.ai_paused_until > timezone.now():
        return False
    latest = conversation.messages.filter(sender="customer").order_by("-created_at", "-id").first()
    if not latest or latest.id != expected_message_id:
        return False
    if conversation.ai_replied_to_message_id == latest.id:
        return False
    if unlinked_shared_media_silence(conversation):
        # Tizimda yo'q reel: javob ham, typing indikatori ham, operator xabari
        # ham chiqmaydi. Butun katalogni yuborib operatorni chaqirish mijozga
        # ham, operatorga ham foyda bermadi — 119 ta havolada shunday bo'ldi.
        print(f"UNLINKED_MEDIA_IGNORED conversation={conversation.id} message={latest.id}", flush=True)
        return False
    return True


def process_pending_customer_reply(conversation_id, expected_message_id):
    stale_started_at = timezone.now() - timedelta(seconds=120)
    with transaction.atomic():
        conversation = Conversation.objects.select_for_update().filter(id=conversation_id).first()
        if not conversation:
            return None
        latest = conversation.messages.filter(sender="customer").order_by("-created_at", "-id").first()
        if not latest or latest.id != expected_message_id:
            return None
        if conversation.ai_replied_to_message_id == latest.id:
            return None
        if unlinked_shared_media_silence(conversation):
            return None
        if conversation.ai_reply_started_for_message_id == latest.id and conversation.ai_reply_started_at and conversation.ai_reply_started_at > stale_started_at:
            return None
        conversation.ai_reply_started_for_message = latest
        conversation.ai_reply_started_at = timezone.now()
        conversation.save(update_fields=["ai_reply_started_for_message", "ai_reply_started_at", "updated_at"])
    try:
        conversation = Conversation.objects.select_related("customer", "social_post").get(id=conversation_id)
        reply = create_ai_reply_for_conversation(conversation)
    except Exception:
        Conversation.objects.filter(id=conversation_id, ai_reply_started_for_message_id=expected_message_id).update(ai_reply_started_for_message=None, ai_reply_started_at=None)
        raise
    if reply:
        Conversation.objects.filter(id=conversation_id, ai_reply_started_for_message_id=expected_message_id).update(ai_replied_to_message_id=expected_message_id, ai_reply_started_for_message=None, ai_reply_started_at=None)
    else:
        Conversation.objects.filter(id=conversation_id, ai_reply_started_for_message_id=expected_message_id).update(ai_reply_started_for_message=None, ai_reply_started_at=None)
    return reply


def process_stalled_conversation_follow_up(conversation_id, expected_ai_message_id):
    if not ai_globally_active():
        return None
    conversation = Conversation.objects.select_related("customer").filter(id=conversation_id).first()
    if not conversation or conversation.status != "ai":
        return None
    if conversation.ai_paused_until and conversation.ai_paused_until > timezone.now():
        return None
    expected_ai_message = conversation.messages.filter(id=expected_ai_message_id, sender="ai").first()
    if not expected_ai_message:
        return None
    latest_message = conversation.messages.order_by("-created_at", "-id").first()
    if not latest_message or latest_message.id != expected_ai_message.id:
        return None
    if conversation.leads.exists() or expected_ai_message.metadata.get("lead_created_id"):
        return None
    elapsed = (timezone.now() - expected_ai_message.created_at).total_seconds()
    if elapsed < AI_FOLLOW_UP_DELAY_SECONDS:
        return None
    decision = ai_follow_up_decision(conversation, expected_ai_message)
    message_text = (decision.get("message") or "").strip()
    if not decision.get("send_follow_up") or not message_text:
        Message.objects.create(conversation=conversation, sender="system", text="", metadata={"follow_up_cancelled": True, "expected_ai_message_id": expected_ai_message.id, "reason": decision.get("reason") or ""})
        return None
    follow_up = Message.objects.create(conversation=conversation, sender="ai", text=message_text, metadata={"follow_up": True, "expected_ai_message_id": expected_ai_message.id, "reason": decision.get("reason") or ""})
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=["last_message_at", "updated_at"])
    return follow_up
