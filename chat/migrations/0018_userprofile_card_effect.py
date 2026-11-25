from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0017_follow"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="card_effect",
            field=models.CharField(blank=True, default="none", max_length=50),
        ),
    ]
