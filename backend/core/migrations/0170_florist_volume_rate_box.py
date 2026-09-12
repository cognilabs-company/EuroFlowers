from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0169_ai_prompt_reply_and_album_numbers"),
    ]

    operations = [
        migrations.AlterField(
            model_name="floristvolumerate",
            name="arrangement_type",
            field=models.CharField(choices=[("bouquet", "Buket"), ("basket", "Savat"), ("box", "Quti")], max_length=20),
        ),
    ]
