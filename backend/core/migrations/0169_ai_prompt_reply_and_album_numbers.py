from django.db import migrations


MARKER = "Mijoz MATNLI xabarga reply qilsa"

OLD_BLOCK = """Izohda bir necha nom bo'lsa mijoz butun albomga javob qilgan — o'shanda qaysi
biri ekanini so'ra.
"""

NEW_BLOCK = """Izohda bir necha nom bo'lsa mijoz butun albomga javob qilgan — o'shanda qaysi
biri ekanini so'ra.

Mijoz MATNLI xabarga reply qilsa ham xuddi shunday. Suhbat matnida shu
qatorlardan biri turadi:
"Tizim izohi: mijoz aynan shu javobimizga javob qildi — «...»"
"Tizim izohi: mijoz aynan shu operator xabariga javob qildi — «...»"
"Tizim izohi: mijoz o'zining oldingi xabariga javob qildi — «...»"

Qo'shtirnoq ichidagi gap — mijozning yangi xabari NIMAGA tegishli ekani.
Yangi xabarni o'sha gapning davomi deb o'qi. Bu qator turgan joyda "qaysi
gulni nazarda tutyapsiz", "tushunmadim", "qaytadan yozing" deb SO'RAMA.

Real suhbatlar: "Kami borm8" — "Jumilia Kompozitsiyasi — 800 000 so'm" ga
javob edi, ya'ni aynan shu gulning arzonrog'ini so'radi. "Bula hammasi 200
mingku" — katalog albomi haqidagi javobga. "Bu manzilgamas" — biz yozgan
manzilga. "Dollar deyilganku" — bizning dollar savolimizga. Bu izohsiz
ularning hammasi havoda qolgan edi.

Mijoz o'zining xabariga reply qilgan bo'lsa (ko'pincha o'zi yuborgan post yoki
rasmga), u shu mediani QAYTA ko'rsatyapti — o'sha media haqida gapir.

════════════════════════════════════
00R. ALBOM RAQAMLARI — HAMMA QOIDADAN USTUN
════════════════════════════════════
Raqamlar faqat OXIRGI yuborilgan albomga tegishli. Suhbatda ikkinchi albom
ketgan bo'lsa birinchisining raqamlari BEKOR — mijozning ekranida oxirgisi
turadi.

REAL_CONTEXT_JSON dagi manbalar, aynan shu tartibda:
1. already_sent.album_choice — mijoz raqam bilan tanlagan mahsulot. Bu tayyor
   javob: position, catalog_id, name, price. Shundan boshqa mahsulot YOZMA.
2. already_sent.last_album — oxirgi albomdagi mahsulotlar o'z raqamlari bilan.
3. already_sent.older_album_numbers_void true bo'lsa, o'zingning oldingi
   javobingdagi raqamlangan ro'yxat eskirgan — uni TAKRORLAMA.

O'z javobingdagi tartibni xotiradan qaytarish eng katta xato. Real suhbat
3051: reklama rasmiga bitta albom ketdi (1 Alfalob, 2 Jumila), o'n sakkiz
soniyadan keyin reel bo'yicha to'qqiz rasmli ikkinchi albom ketdi (1 Jumila,
2 Alfalob). AI javobida birinchi albomning tartibini yozdi, mijoz esa
ekranidagi ikkinchi albomdan "2" deb tanladi — leadga Alfalob o'rniga Jumila
tushdi va operatorga boshqa gulning rasmi ketdi.

client_lead_create ga catalog_id yozganda ham shu tartib: mijoz raqam yozgan
bo'lsa album_choice.catalog_id dan olinadi, nomdan yoki xotiradan emas.
"""


def forwards(apps, schema_editor):
    AISettings = apps.get_model("core", "AISettings")
    row = AISettings.objects.filter(pk=1).first()
    if not row or not row.system_prompt:
        return
    prompt = row.system_prompt
    if MARKER in prompt:
        return
    if prompt.count(OLD_BLOCK) != 1:
        raise RuntimeError("00K bo'limidagi bog'lash nuqtasi topilmadi: %s" % prompt.count(OLD_BLOCK))
    prompt = prompt.replace(OLD_BLOCK, NEW_BLOCK)
    row.system_prompt = prompt
    row.save(update_fields=["system_prompt", "updated_at"])
    if MARKER not in row.system_prompt:
        raise RuntimeError("reply qoidasi promptga yozilmadi")
    if "00R. ALBOM RAQAMLARI" not in row.system_prompt:
        raise RuntimeError("albom raqamlari bo'limi promptga yozilmadi")


def backwards(apps, schema_editor):
    AISettings = apps.get_model("core", "AISettings")
    row = AISettings.objects.filter(pk=1).first()
    if not row or not row.system_prompt:
        return
    prompt = row.system_prompt
    if MARKER not in prompt:
        return
    if prompt.count(NEW_BLOCK) != 1:
        return
    row.system_prompt = prompt.replace(NEW_BLOCK, OLD_BLOCK)
    row.save(update_fields=["system_prompt", "updated_at"])


class Migration(migrations.Migration):

    dependencies = [("core", "0168_ai_prompt_one_album_per_reply")]

    operations = [migrations.RunPython(forwards, backwards)]
