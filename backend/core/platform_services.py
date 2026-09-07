import json
import requests
from urllib.parse import parse_qs, urlparse
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import IntegrationSettings, Lead, Notification, SocialPost


def normalize_instagram_permalink(value):
    return (value or "").split("?")[0].rstrip("/")


def media_id_from_url(value):
    if not value:
        return ""
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    return (query.get("asset_id") or [""])[0]

def instagram_credentials():
    integration, _ = IntegrationSettings.objects.get_or_create(pk=1)
    return integration, integration.instagram_access_token or settings.INSTAGRAM_ACCESS_TOKEN


def instagram_account_token_pairs():
    integration, _ = IntegrationSettings.objects.get_or_create(pk=1)
    pairs = {}
    primary_account_id = integration.instagram_account_id or settings.INSTAGRAM_ACCOUNT_ID or integration.instagram_business_id
    primary_token = integration.instagram_access_token or settings.INSTAGRAM_ACCESS_TOKEN
    if primary_account_id and primary_token:
        pairs[str(primary_account_id)] = primary_token
    for row in settings.INSTAGRAM_ACCOUNT_ACCESS_TOKENS:
        if ":" not in row:
            continue
        account_id, token = row.split(":", 1)
        account_id = account_id.strip()
        token = token.strip()
        if account_id and token:
            pairs[account_id] = token
    return pairs


def instagram_credentials_for_account(account_id=None):
    integration, _ = IntegrationSettings.objects.get_or_create(pk=1)
    primary_account_id = integration.instagram_account_id or settings.INSTAGRAM_ACCOUNT_ID or integration.instagram_business_id
    if account_id:
        account_id = str(account_id)
        token = instagram_account_token_pairs().get(account_id, "")
        return account_id, token
    return primary_account_id, integration.instagram_access_token or settings.INSTAGRAM_ACCESS_TOKEN


def openai_api_key():
    integration, _ = IntegrationSettings.objects.get_or_create(pk=1)
    return (integration.extra or {}).get("openai_api_key") or settings.OPENAI_API_KEY


def instagram_user_id(access_token):
    integration, _ = IntegrationSettings.objects.get_or_create(pk=1)
    if integration.instagram_account_id:
        return integration.instagram_account_id
    if outbound_blocked():
        return ""
    response = requests.get(f"https://graph.instagram.com/{settings.INSTAGRAM_API_VERSION}/me", params={"access_token": access_token, "fields": "id,username"}, timeout=20)
    response.raise_for_status()
    data = response.json()
    account_id = data.get("id", "")
    if account_id:
        integration.instagram_account_id = account_id
        integration.save(update_fields=["instagram_account_id", "updated_at"])
    return account_id


def instagram_lookup_accounts(account_id=None):
    """Story va postni qaysi akkauntlardan qidirish kerak.

    Tizimga bitta emas, bir nechta Instagram akkaunt ulanadi. Mijozning xabari
    qaysi akkauntga kelganini bilsak faqat o'shanikini so'raymiz; bilmasak
    (masalan operator postga link qo'yayotganda) hammasidan qidiramiz. Faqat
    asosiy akkauntdan qidirish boshqa akkauntning storysini "yo'q" qilib
    ko'rsatadi va o'sha story hech qachon postga bog'lanmay qoladi.
    """
    pairs = instagram_account_token_pairs()
    if account_id and str(account_id) in pairs:
        return [(str(account_id), pairs[str(account_id)])]
    if pairs:
        return list(pairs.items())
    _, access_token = instagram_credentials()
    if not access_token:
        return []
    resolved = instagram_user_id(access_token)
    return [(resolved, access_token)] if resolved else []


def instagram_active_stories(account_id=None):
    rows = []
    for account, access_token in instagram_lookup_accounts(account_id):
        try:
            if outbound_blocked():
                return rows
            response = requests.get(
                f"https://graph.instagram.com/{settings.INSTAGRAM_API_VERSION}/{account}/stories",
                params={"access_token": access_token, "fields": "id,media_type,media_url,permalink,timestamp"},
                timeout=20,
            )
            response.raise_for_status()
            rows.extend(response.json().get("data", []))
        except Exception as error:
            # Bitta akkauntning tokeni eskirgani qolganlarini qidirishga to'sqinlik qilmaydi.
            print(f"INSTAGRAM_ACTIVE_STORIES_FAILED account={account} error={error}", flush=True)
    return rows


def instagram_recent_media(account_id=None):
    rows = []
    for account, access_token in instagram_lookup_accounts(account_id):
        url = f"https://graph.instagram.com/{settings.INSTAGRAM_API_VERSION}/{account}/media"
        params = {"access_token": access_token, "fields": "id,caption,media_type,media_url,permalink,timestamp,thumbnail_url", "limit": 100}
        try:
            for _ in range(5):
                if outbound_blocked():
                    return rows
                response = requests.get(url, params=params, timeout=20)
                response.raise_for_status()
                data = response.json()
                rows.extend(data.get("data", []))
                url = data.get("paging", {}).get("next")
                params = {}
                if not url:
                    break
        except Exception as error:
            print(f"INSTAGRAM_RECENT_MEDIA_FAILED account={account} error={error}", flush=True)
    return rows


def instagram_media_row(media_id, account_id=None):
    """Media raqami bo'yicha postni Instagram dan so'raydi.

    Graph API faqat o'z akkauntimizning mediasini beradi, shuning uchun javob
    kelishining o'zi "bu post bizning profilimizdan" degani. Ro'yxat
    (`/media` chekkasi) esa hamma postni bermaydi: real o'lchov — profilda
    151 post bor, ro'yxat 178 qator qaytardi, lekin mijozlar eng ko'p
    ulashadigan `DIlfKrugybL` (136 suhbat) va `DXL_kLeALd0` (23 suhbat) o'sha
    ro'yxatda yo'q. Ikkalasi ham shu funksiyada 200 bilan topiladi.

    (row, "ours"|"foreign"|"unknown") qaytaradi. "unknown" — tarmoq yoki token
    xatosi: bunda post begona deb hisoblanmaydi.
    """
    media_id = str(media_id or "").strip()
    if not media_id:
        return None, "unknown"
    accounts = instagram_lookup_accounts(account_id)
    if not accounts:
        return None, "unknown"
    verdict = "unknown"
    for account, access_token in accounts:
        if outbound_blocked():
            return None, "unknown"
        url = f"https://graph.instagram.com/{settings.INSTAGRAM_API_VERSION}/{media_id}"
        try:
            response = requests.get(url, params={"access_token": access_token, "fields": "id,caption,media_type,media_url,permalink,timestamp,thumbnail_url"}, timeout=20)
        except Exception as error:
            print(f"INSTAGRAM_MEDIA_ROW_FAILED media={media_id} account={account} error={error}", flush=True)
            continue
        if response.status_code == 200:
            try:
                return response.json(), "ours"
            except ValueError:
                continue
        # 400 "does not exist, cannot be loaded due to missing permissions" —
        # boshqa profilning posti. 5xx esa bizning xabarimiz emas.
        if response.status_code in (400, 404):
            verdict = "foreign"
            continue
        print(f"INSTAGRAM_MEDIA_ROW_STATUS media={media_id} account={account} status={response.status_code}", flush=True)
    return None, verdict


def find_active_story_by_permalink(permalink, account_id=None):
    normalized = normalize_instagram_permalink(permalink)
    if not normalized:
        return None
    for story in instagram_active_stories(account_id):
        if normalize_instagram_permalink(story.get("permalink")) == normalized:
            return story
    return None


def find_active_story_by_media_url(media_url, account_id=None):
    normalized = normalize_instagram_permalink(media_url)
    asset_id = media_id_from_url(media_url)
    if not normalized and not asset_id:
        return None
    for story in instagram_active_stories(account_id):
        story_media_url = story.get("media_url", "")
        if asset_id and str(story.get("id", "")) == asset_id:
            return story
        if asset_id and media_id_from_url(story_media_url) == asset_id:
            return story
        if normalized and normalize_instagram_permalink(story_media_url) == normalized:
            return story
    return None


def find_media_by_permalink(permalink, account_id=None):
    normalized = normalize_instagram_permalink(permalink)
    if not normalized:
        return None
    for media in instagram_recent_media(account_id):
        if normalize_instagram_permalink(media.get("permalink")) == normalized:
            return media
    return None


def find_media_by_id(media_id, account_id=None):
    if not media_id:
        return None
    for media in instagram_recent_media(account_id):
        if str(media.get("id", "")) == str(media_id):
            return media
    return None

def instagram_send(recipient_id, text, account_id=None):
    account_id, access_token = instagram_credentials_for_account(account_id)
    if not access_token or not account_id:
        return {"mocked": True}
    url = f"https://graph.instagram.com/{settings.INSTAGRAM_API_VERSION}/{account_id}/messages"
    if outbound_blocked():
        return BlockedResponse().json()
    response = requests.post(url, params={"access_token": access_token}, json={"recipient": {"id": recipient_id}, "message": {"text": text}}, timeout=20)
    response.raise_for_status()
    return response.json()


def instagram_send_image(recipient_id, image_url, account_id=None):
    account_id, access_token = instagram_credentials_for_account(account_id)
    if not access_token or not account_id:
        return {"mocked": True}
    url = f"https://graph.instagram.com/{settings.INSTAGRAM_API_VERSION}/{account_id}/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {"url": image_url, "is_reusable": True},
            }
        },
    }
    if outbound_blocked():
        return BlockedResponse().json()
    response = requests.post(url, params={"access_token": access_token}, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def instagram_send_carousel(recipient_id, elements, account_id=None):
    """Bir nechta rasmni bitta xabarda karusel qilib yuboradi.

    elements: [{"title": ..., "subtitle": ..., "image_url": ...}]
    Instagram generic template bitta xabarda ko'pi bilan 10 ta element ko'taradi.
    """
    account_id, access_token = instagram_credentials_for_account(account_id)
    if not access_token or not account_id:
        return {"mocked": True}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": [
                        {
                            "title": (row.get("title") or "")[:80],
                            "subtitle": (row.get("subtitle") or "")[:80],
                            "image_url": row.get("image_url") or "",
                        }
                        for row in elements
                    ],
                },
            }
        },
    }
    url = f"https://graph.instagram.com/{settings.INSTAGRAM_API_VERSION}/{account_id}/messages"
    if outbound_blocked():
        return BlockedResponse().json()
    response = requests.post(url, params={"access_token": access_token}, json=payload, timeout=40)
    if response.status_code >= 400:
        # Instagram sababni faqat javob tanasida yozadi, status kodda emas.
        print(f"INSTAGRAM_CAROUSEL_REJECTED account={account_id} status={response.status_code} body={response.text[:600]}", flush=True)
    response.raise_for_status()
    return response.json()


def instagram_sender_action(recipient_id, action, account_id=None):
    account_id, access_token = instagram_credentials_for_account(account_id)
    if not access_token or not account_id:
        return {"mocked": True}
    url = f"https://graph.instagram.com/{settings.INSTAGRAM_API_VERSION}/{account_id}/messages"
    if outbound_blocked():
        return BlockedResponse().json()
    response = requests.post(url, params={"access_token": access_token}, json={"recipient": {"id": recipient_id}, "sender_action": action}, timeout=20)
    response.raise_for_status()
    return response.json()


# Haqiqiy funksiyalar import paytida eslab qolinadi. Test ularni almashtirgan
# bo'lsa so'rov tarmoqqa emas, o'sha testning o'rnini bosuvchisiga ketadi.
REAL_HTTP_POST = requests.post
REAL_HTTP_GET = requests.get


def outbound_blocked():
    """Test paytida haqiqiy tashqi so'rov chiqmasin.

    Sotuv va operator guruhlariga test sotuvlari haqiqiy xabar bo'lib borib
    turgan edi. Endi test yugurtirilganda tarmoqqa umuman chiqilmaydi.

    Test o'zi requests.post yoki requests.get ni almashtirgan bo'lsa
    to'silmaydi — u so'rov baribir tarmoqqa chiqmaydi, va to'sib qo'ysak
    o'sha testlar tekshirayotgan narsasini yo'qotadi.
    """
    if not getattr(settings, "TESTING", False):
        return False
    return requests.post is REAL_HTTP_POST and requests.get is REAL_HTTP_GET


class BlockedResponse:
    """Test paytida tarmoq o'rniga qaytadigan javob."""

    @staticmethod
    def json():
        return {"ok": True, "mocked": True, "result": {"message_id": 1}}


def telegram_bot_token():
    integration, _ = IntegrationSettings.objects.get_or_create(pk=1)
    return integration.telegram_bot_token


def telegram_file_url(file_id):
    token = telegram_bot_token()
    if not token or not file_id:
        return ""
    if outbound_blocked():
        return ""
    response = requests.post(f"https://api.telegram.org/bot{token}/getFile", json={"file_id": file_id}, timeout=20)
    response.raise_for_status()
    file_path = response.json().get("result", {}).get("file_path", "")
    if not file_path:
        return ""
    return f"https://api.telegram.org/file/bot{token}/{file_path}"


def telegram_api(method, payload):
    token = telegram_bot_token()
    if not token:
        return {"mocked": True}
    if outbound_blocked():
        return BlockedResponse().json()
    response = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=20)
    response.raise_for_status()
    return response.json()


def telegram_api_with_token(token, method, payload):
    if not token:
        return {"skipped": True, "reason": "token yo‘q"}
    if outbound_blocked():
        return BlockedResponse().json()
    response = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def telegram_chat_id_variants(chat_id):
    value = str(chat_id or "").strip()
    if not value:
        return []
    rows = [value]
    if value.startswith("-100") and len(value) > 4:
        rows.append("-" + value[4:])
    elif value.startswith("-") and value[1:].isdigit():
        rows.append("-100" + value[1:])
    return list(dict.fromkeys(rows))


def telegram_api_with_token_and_chat_fallback(token, method, payload, files=None):
    if not token:
        return {"skipped": True, "reason": "token yo‘q"}
    variants = telegram_chat_id_variants(payload.get("chat_id"))
    if not variants:
        return {"skipped": True, "reason": "chat_id yo‘q"}
    last_error = None
    for chat_id in variants:
        current = dict(payload, chat_id=chat_id)
        try:
            url = f"https://api.telegram.org/bot{token}/{method}"
            if files:
                if outbound_blocked():
                    return BlockedResponse().json()
                response = requests.post(url, data=current, files=files, timeout=30)
            else:
                if outbound_blocked():
                    return BlockedResponse().json()
                response = requests.post(url, json=current, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            last_error = exc
    raise last_error


def telegram_send(chat_id, text):
    return telegram_api("sendMessage", {"chat_id": chat_id, "text": text})


def telegram_send_with(token, chat_id, text, reply_markup=None, message_thread_id=""):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    return telegram_api_with_token(token, "sendMessage", payload)


def telegram_send_message_with(token, chat_id, text, parse_mode="Markdown", reply_markup=None, message_thread_id=""):
    payload = {"chat_id": str(chat_id), "text": text[:4096]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    return telegram_api_with_token_and_chat_fallback(token, "sendMessage", payload)


def telegram_send_image(chat_id, image_url, caption=""):
    payload = {"chat_id": chat_id, "photo": image_url}
    if caption:
        payload["caption"] = caption[:1024]
    return telegram_api("sendPhoto", payload)


def telegram_send_media_group(chat_id, media):
    """Bir nechta rasmni bitta xabarda albom qilib yuboradi.

    media: [{"image_url": ..., "caption": ...}]
    Telegram media group bitta xabarda ko'pi bilan 10 ta rasm ko'taradi.
    """
    payload = {
        "chat_id": chat_id,
        "media": [
            {"type": "photo", "media": row.get("image_url") or "", "caption": (row.get("caption") or "")[:1024]}
            for row in media
        ],
    }
    return telegram_api("sendMediaGroup", payload)


def telegram_send_media_group_with(token, chat_id, media, message_thread_id=""):
    if not token:
        return {"skipped": True, "reason": "token yo‘q"}
    variants = telegram_chat_id_variants(chat_id)
    if not variants:
        return {"skipped": True, "reason": "chat_id yo‘q"}
    media_payload = []
    files = {}
    for index, row in enumerate(media):
        key = f"photo{index}"
        payload_row = {
            "type": row.get("type") or "photo",
            "media": row.get("url") or row.get("image_url") or "",
        }
        if row.get("bytes") is not None:
            payload_row["media"] = f"attach://{key}"
            files[key] = (row.get("filename") or f"photo{index}.jpg", row.get("bytes"))
        caption = (row.get("caption") or "")[:1024]
        if caption:
            payload_row["caption"] = caption
            payload_row["parse_mode"] = row.get("parse_mode") or "Markdown"
        media_payload.append(payload_row)
    last_error = None
    for target in variants:
        url = f"https://api.telegram.org/bot{token}/sendMediaGroup"
        try:
            if files:
                if outbound_blocked():
                    return BlockedResponse().json()
                data = {"chat_id": target, "media": json.dumps(media_payload)}
                if message_thread_id:
                    data["message_thread_id"] = message_thread_id
                response = requests.post(url, data=data, files=files, timeout=30)
            else:
                if outbound_blocked():
                    return BlockedResponse().json()
                payload = {"chat_id": target, "media": media_payload}
                if message_thread_id:
                    payload["message_thread_id"] = message_thread_id
                response = requests.post(url, json=payload, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            last_error = exc
    raise last_error


def telegram_send_rich_message_with(token, chat_id, rich_message, reply_markup=None, message_thread_id=""):
    payload = {"chat_id": chat_id, "rich_message": rich_message}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    return telegram_api_with_token(token, "sendRichMessage", payload)


def telegram_send_photo_with(token, chat_id, photo, caption="", parse_mode="Markdown"):
    """Alohida bot orqali rasm yuboradi.

    Sotuv xabari uchun boshqa bot va guruh ishlatiladi, shuning uchun token
    tashqaridan beriladi. Rasm URL bo'lsa link yuboriladi, bayt bo'lsa fayl.
    """
    if not token or not chat_id:
        return {"skipped": True, "reason": "token yoki chat_id yo‘q"}
    data = {"chat_id": str(chat_id), "caption": caption[:1024], "parse_mode": parse_mode}
    if isinstance(photo, (bytes, bytearray)):
        return telegram_api_with_token_and_chat_fallback(token, "sendPhoto", data, files={"photo": ("sale.jpg", photo)})
    else:
        data["photo"] = photo
        return telegram_api_with_token_and_chat_fallback(token, "sendPhoto", data)


def telegram_sender_action(chat_id, action="typing"):
    return telegram_api("sendChatAction", {"chat_id": chat_id, "action": action})


def send_lead_recall(lead_id):
    with transaction.atomic():
        lead = Lead.objects.select_for_update().select_related("customer").filter(id=lead_id).first()
        if not lead or lead.status == "lost" or lead.recall_sent_at or not lead.recall_at or lead.recall_at > timezone.now():
            return None
        title = f"Recall: Lead #{lead.id}"
        body = f"{lead.customer} buyurtmasi 1 soat ichida yuborilishi kerak. Telefon: {lead.customer.phone or lead.customer.masked_phone}. So‘rov: {lead.request_uz or lead.request_ru}"
        notification = Notification.objects.create(
            notification_type="lead",
            title_uz=title,
            title_ru=title,
            body_uz=body,
            body_ru=body,
            reference_type="lead",
            reference_id=lead.id,
        )
        lead.recall_sent_at = timezone.now()
        lead.save(update_fields=["recall_sent_at", "updated_at"])
    integration, _ = IntegrationSettings.objects.get_or_create(pk=1)
    group_chat_id = integration.telegram_group_chat_id or settings.TELEGRAM_GROUP_CHAT_ID
    if group_chat_id:
        try:
            telegram_send(group_chat_id, f"{title}\n{body}")
        except Exception as exc:
            print(f"LEAD_RECALL_TELEGRAM_FAILED lead={lead_id} error={exc}", flush=True)
    # Eslatma guruhiga qisqa karta: kim, qaysi gul, qachon kerak.
    from .recall_services import send_recall_card

    try:
        send_recall_card(lead)
    except Exception as exc:
        print(f"LEAD_RECALL_CARD_FAILED lead={lead_id} error={exc}", flush=True)
    return notification


def send_due_lead_recalls():
    due_ids = list(Lead.objects.filter(recall_at__lte=timezone.now(), recall_sent_at__isnull=True).exclude(status="lost").values_list("id", flat=True)[:100])
    sent = 0
    for lead_id in due_ids:
        if send_lead_recall(lead_id):
            sent += 1
    return sent
